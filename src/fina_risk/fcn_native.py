"""Canonical FCN request adapter for the real typed C++ lifecycle engine.

This module is deliberately small: schema validation, deterministic path-cube
construction, request correlation, and FastMCP transport remain outside the
native path × observation loop.  It does not provide a Python pricing fallback.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import subprocess
from pathlib import Path
from typing import Any

import numpy as np

from .daily_termsheet import nyse_serials
from .etl import validate_pricing_request

_REPO = Path(__file__).resolve().parents[2]


def _stable_id(prefix: str, value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return f"{prefix}-{hashlib.sha256(encoded).hexdigest()[:16]}"


def _revision() -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(_REPO), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _require_native() -> Any:
    try:
        return importlib.import_module("fina_risk_cpp")
    except ImportError as exc:  # No mirror: production evidence requires C++.
        raise RuntimeError("native C++ fina_risk_cpp module is required for quote.price") from exc


def _surface_point(surface: dict[str, Any], strike: float, maturity: int, evaluation: int) -> float:
    """Linear-in-total-variance canonical-surface read, matching the native contract."""
    strikes = np.asarray(surface.get("strikes", surface.get("strike", [])), dtype=float)
    maturities = np.asarray(surface.get("maturities", surface.get("maturity", [])), dtype=float)
    values = np.asarray(surface.get("volatility", surface.get("vol", [])), dtype=float)
    if strikes.size == 0 or values.size == 0:
        return 0.35
    if values.ndim == 1:
        value = float(np.interp(strike, strikes, values))
        return value / 100.0 if value > 3.0 else value
    row_values = np.asarray([np.interp(strike, strikes, row) for row in values], dtype=float)
    if np.nanmax(np.abs(row_values)) > 3.0:
        row_values /= 100.0
    if maturities.size != row_values.size or maturities.size < 2:
        return float(row_values[0])
    times = np.maximum((maturities - evaluation) / 365.0, 1.0e-6)
    target = max((maturity - evaluation) / 365.0, 1.0e-6)
    # Flat extrapolation in vol beyond the bracketing pillars: dividing a flat
    # total variance by a near-zero target would blow up the front-dated vol.
    if target <= times[0]:
        return float(row_values[0])
    if target >= times[-1]:
        return float(row_values[-1])
    variance = np.interp(target, times, row_values**2 * times)
    return float(np.sqrt(max(variance, 1.0e-12) / target))


def _curve_rate_at(pillars: list[dict[str, Any]], target_date: int) -> float:
    """Linear-in-rate zero-curve interpolation (ACT/365) to an Excel serial date.

    Pillars are ``{"date": serial, "rate": r}``; the read is flat beyond the first
    and last pillar. An empty curve returns 0.0. Mirrors the native C++
    ``curve_rate_at`` so both engines discount the same way.
    """
    points: list[tuple[int, float]] = []
    for pillar in pillars or []:
        date = pillar.get("date")
        rate = pillar.get("rate")
        if date is None or rate is None:
            continue
        points.append((int(date), float(rate)))
    if not points:
        return 0.0
    points.sort()
    if target_date <= points[0][0]:
        return points[0][1]
    if target_date >= points[-1][0]:
        return points[-1][1]
    for i in range(1, len(points)):
        if target_date <= points[i][0]:
            d0, r0 = points[i - 1]
            d1, r1 = points[i]
            w = 0.0 if d1 == d0 else (target_date - d0) / (d1 - d0)
            return r0 + w * (r1 - r0)
    return points[-1][1]


def _correlation(market: dict[str, Any], count: int) -> np.ndarray:
    matrix = np.eye(count, dtype=float)
    if count == 2:
        items = market.get("correlations", [])
        if items:
            value = items[0].get("value", [0.0])
            rho = float(value[0] if isinstance(value, list) and value else value)
            matrix[0, 1] = matrix[1, 0] = max(-0.999, min(0.999, rho))
    return matrix


def build_daily_path_cube(request: dict[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    """Build a deterministic GBM cube from an already validated canonical request."""
    market = request["market_data"]
    terms = request["fcn_terms"]
    evaluation = int(market["evaluation_date"])
    maturity = max(int(terms["maturity_date"]), int(terms["final_fixing_date"]))
    dates = nyse_serials(evaluation, maturity)
    if dates.size == 0:
        raise ValueError("no NYSE observations between evaluation and maturity")
    underlyings = market["underlyings"]
    spots = np.asarray([float(item["quoted_spot"]) for item in underlyings], dtype=float)
    if np.any(spots <= 0.0):
        raise ValueError("quoted_spot must be positive")
    surfaces = {str(item.get("underlying")): item for item in market.get("vol_surfaces", [])}
    put = next((item for item in request["legs"] if item.get("leg_type") == "intrinsic_option"), {})
    payoff = put.get("payoff", {})
    strike_ratio = float(payoff.get("strike", 0.78))
    ki_ratio = float(payoff.get("knock_in", {}).get("barrier", 0.70))
    pillars = market.get("curves", [{}])[0].get("pillars", [])
    parameters = request["parameters"]
    paths = int(parameters.get("paths", 30000))
    if paths < 1:
        raise ValueError("parameters.paths must be >= 1")
    generator = np.random.default_rng(int(parameters["seed"]))
    independent = generator.standard_normal((paths, dates.size, spots.size))
    shocks = independent @ np.linalg.cholesky(_correlation(market, spots.size)).T
    dt = 1.0 / 252.0
    # Term structure of rates: per-observation zero from the curve (flat when the
    # payload carries a single pillar), matching the C++ discount curve.
    step_rates = np.asarray([_curve_rate_at(pillars, int(date)) for date in dates], dtype=float)
    # Per-observation vol: the worst of the exercise (0.78x) and knock-in (0.70x)
    # moneyness read at each fixing date, so the smile term structure flows into
    # the paths. A single-maturity grid collapses to today's flat sigma.
    step_vols = np.empty((dates.size, spots.size), dtype=float)
    for index, item in enumerate(underlyings):
        surface = surfaces.get(str(item["id"]), {})
        reference = float(item["reference_spot"])
        for step, date in enumerate(dates):
            step_vols[step, index] = max(
                _surface_point(surface, strike_ratio * reference, int(date), evaluation),
                _surface_point(surface, ki_ratio * reference, int(date), evaluation),
            )
    increments = (step_rates[:, None] - 0.5 * step_vols**2) * dt + step_vols * np.sqrt(dt) * shocks
    cube = np.exp(np.log(spots)[None, None, :] + np.cumsum(increments, axis=1))
    return np.ascontiguousarray(cube, dtype=np.float64), np.asarray(dates, dtype=np.int32)


def validate_fcn_request(request: dict[str, Any]) -> None:
    """Validate generic input once and reject ambiguous semantics explicitly."""
    validate_pricing_request(request)
    terms = request.get("fcn_terms")
    if not isinstance(terms, dict):
        raise ValueError("canonical FCN pricing requests require fcn_terms")
    required = {"final_fixing_date", "maturity_date", "coupon_periods", "barriers", "physical_delivery"}
    missing = sorted(field for field in required if field not in terms)
    if missing:
        raise ValueError(f"fcn_terms missing required fields: {', '.join(missing)}")
    if terms.get("performance_indicator", "worst_of") != "worst_of":
        raise ValueError("only worst_of performance_indicator is implemented")
    if terms.get("memory_ko") and terms.get("memory_ko_mode") != "per_underlying_ever":
        raise ValueError("memory_ko is ambiguous without memory_ko_mode=per_underlying_ever")


def _native_price(native: Any, request: dict[str, Any], path_cube: np.ndarray, dates: np.ndarray) -> dict[str, Any]:
    return json.loads(native.price_fcn_rakiplus(json.dumps(request), path_cube, dates))


def _bumped_request(request: dict[str, Any], index: int, *, spot_factor: float | None = None, vol_shift: float = 0.0) -> dict[str, Any]:
    bumped = json.loads(json.dumps(request))
    if spot_factor is not None:
        underlying = bumped["market_data"]["underlyings"][index]
        underlying["quoted_spot"] = float(underlying["quoted_spot"]) * spot_factor
        underlying["reference_spot"] = float(underlying["reference_spot"]) * spot_factor
    if vol_shift:
        target = str(bumped["market_data"]["underlyings"][index]["id"])
        for surface in bumped["market_data"].get("vol_surfaces", []):
            if str(surface.get("underlying")) != target:
                continue
            values = surface.get("volatility", surface.get("vol"))
            if isinstance(values, list):
                surface["volatility"] = [
                    [float(value) + vol_shift for value in row] if isinstance(row, list) else float(row) + vol_shift
                    for row in values
                ]
            break
    return bumped


def price_fcn_request(
    request: dict[str, Any], *, path_cube: np.ndarray | None = None, dates: np.ndarray | None = None
) -> dict[str, Any]:
    """Execute the canonical request on the native C++ FCN/RakiPlus lane."""
    validate_fcn_request(request)
    if path_cube is None or dates is None:
        path_cube, dates = build_daily_path_cube(request)
    if path_cube.ndim != 3:
        raise ValueError("path_cube must be (paths, observations, underlyings)")
    request = {**request, "source_revision": request.get("source_revision", _revision())}
    request.setdefault("request_id", _stable_id("request", request))
    native = _require_native()
    dates = np.asarray(dates, dtype=np.int32)
    result = _native_price(native, request, path_cube, dates)
    if result.get("engine_marker") != "cpp_fcn_rakiplus_v1":
        raise RuntimeError("native FCN engine marker missing; refusing non-native quote result")
    if result.get("status") != "ok":
        return result
    bump = float(request["parameters"].get("bump_size", 0.01))
    base_pv = float(result.get("pv", 0.0))
    deltas: list[float] = []
    gammas: list[float] = []
    vegas: list[float] = []
    for index, _underlying in enumerate(request["market_data"]["underlyings"]):
        up = _native_price(native, _bumped_request(request, index, spot_factor=1.0 + bump), path_cube, dates)
        down = _native_price(native, _bumped_request(request, index, spot_factor=1.0 - bump), path_cube, dates)
        up_pv = float(up.get("pv", base_pv))
        down_pv = float(down.get("pv", base_pv))
        spot = float(request["market_data"]["underlyings"][index]["quoted_spot"])
        dollar_bump = bump * spot
        deltas.append((up_pv - down_pv) / (2.0 * dollar_bump))
        gammas.append((up_pv - 2.0 * base_pv + down_pv) / (dollar_bump * dollar_bump))
        vol_up = _native_price(native, _bumped_request(request, index, vol_shift=0.01), path_cube, dates)
        vol_down = _native_price(native, _bumped_request(request, index, vol_shift=-0.01), path_cube, dates)
        vegas.append((float(vol_up.get("pv", base_pv)) - float(vol_down.get("pv", base_pv))) / 0.02)
    result["relative_delta"] = deltas
    result["relative_gamma"] = gammas
    result["relative_vega"] = vegas
    result["native"] = True
    result["path_cube"] = {
        "paths": int(path_cube.shape[0]),
        "observations": int(path_cube.shape[1]),
        "underlyings": int(path_cube.shape[2]),
        "calendar": "NYSE",
        "seed": int(request["parameters"]["seed"]),
    }
    return result


__all__ = ["build_daily_path_cube", "price_fcn_request", "validate_fcn_request"]
