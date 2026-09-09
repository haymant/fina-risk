"""Local vectorized reference pricer for the fina-risk draft contract.

The implementation deliberately keeps market, universe, simulation, state, payoff and
risk artifacts separate.  It is a CPU reference backend using NumPy; the same DTOs can
be passed to a QuantLib/XAD or GPU backend without changing payoff semantics.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import numpy as np

from .aad import aad_put_sensitivity
from .risk_view import build_risk_views

EXCEL_EPOCH = date(1899, 12, 30)


def excel_date(value: int | float | str) -> date:
    if isinstance(value, str) and "-" in value:
        return date.fromisoformat(value)
    return EXCEL_EPOCH + timedelta(days=int(value))


def year_fraction(start: int | float | str, end: int | float | str) -> float:
    return max((excel_date(end) - excel_date(start)).days, 0) / 365.0


def _stable_id(prefix: str, payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, default=str).encode()
    return f"{prefix}-{hashlib.sha256(raw).hexdigest()[:16]}"


@dataclass(frozen=True)
class Market:
    names: tuple[str, str]
    quoted_spots: np.ndarray
    reference_spots: np.ndarray
    vols: np.ndarray
    correlation: float
    rate: float
    evaluation_date: int
    paths: int
    seed: int


def _parse_vol(market_data: dict[str, Any], name: str) -> float:
    surfaces = [x for x in market_data.get("eqVol", []) if x.get("_id") == name]
    if not surfaces:
        return 0.45
    surface = surfaces[0]
    values = np.asarray(surface.get("vol", []), dtype=float)
    if values.size == 0:
        return 0.45
    # Legacy grids store percentage points, indexed by strike and maturity.  Use the
    # nearest quoted strike and shortest maturity that spans the fixture expiry.
    strikes = np.asarray(surface.get("strike", []), dtype=float)
    quoted = float(next((x["spot"] for x in market_data.get("equity", []) if x["_id"] == name), 0))
    col = int(np.argmin(abs(strikes - quoted))) if strikes.size else 0
    if values.ndim > 1:
        pillars = np.asarray(surface.get("maturity", []), dtype=float)
        target = float(market_data.get("evaluationDate", 0)) + 0.40 * 365.0
        row = int(np.argmin(abs(pillars - target))) if pillars.size else min(3, values.shape[0] - 1)
        row = min(row, values.shape[0] - 1)
        return float(values[row, min(col, values.shape[1] - 1)]) / 100.0
    return float(np.nanmean(values)) / 100.0


def market_from_legacy(
    common: dict[str, Any], *, paths: int | None = None, seed: int = 1729, spots: list[float] | None = None
) -> Market:
    md = common.get("marketData", common)
    equities = md.get("equity", [])
    names = tuple(str(x["_id"]) for x in equities[:2])
    quoted: np.ndarray = np.asarray([float(x["spot"]) for x in equities[:2]], dtype=float)
    reference: np.ndarray = np.asarray([float(x.get("reference", x["spot"])) for x in equities[:2]], dtype=float)
    # Deal economics contain the true initial fixing/reference spots.
    deal = common.get("dealData", {})
    underlyings = deal.get("instrument", {}).get("underlyings", [])
    if underlyings:
        reference = np.asarray([float(x["spot"]) for x in underlyings[:2]], dtype=float)
    if spots is not None:
        quoted = np.asarray(spots, dtype=float)
    corr = 0.0
    try:
        corr = float(md["corr"][0]["correlation"][0]["correlation"])
    except (KeyError, IndexError, TypeError):
        corr = 0.0
    curves = md.get("discCurves", [])
    rates = curves[0].get("curve", []) if curves else []
    rate = float(rates[0]["rate"]) if rates else 0.0
    return Market(
        (names[0], names[1]),
        quoted,
        reference,
        np.asarray([_parse_vol(md, n) for n in names]),
        corr,
        rate,
        int(md.get("evaluationDate", 0)),
        int(paths or md.get("MCPara", {}).get("numPaths", 30000)),
        seed,
    )


def _simulate(market: Market, expiry_date: int, *, steps: int = 194) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    t = year_fraction(market.evaluation_date, expiry_date)
    steps = max(2, min(steps, int(max(2, round(t * 252)))))
    dt = t / steps
    rng = np.random.default_rng(market.seed)
    z1 = rng.standard_normal((market.paths, steps), dtype=np.float64)
    z2 = rng.standard_normal((market.paths, steps), dtype=np.float64)
    z2 = market.correlation * z1 + math.sqrt(max(1.0 - market.correlation**2, 0.0)) * z2
    drift = (market.rate - 0.5 * market.vols**2) * dt
    scale = market.vols * math.sqrt(dt)
    log1 = np.log(market.quoted_spots[0]) + np.cumsum(drift[0] + scale[0] * z1, axis=1)
    log2 = np.log(market.quoted_spots[1]) + np.cumsum(drift[1] + scale[1] * z2, axis=1)
    return np.exp(log1[:, -1]), np.exp(log2[:, -1]), np.stack((log1, log2), axis=2)


def price_terminal_legs(
    terminal_spots: np.ndarray,
    quoted_spots: np.ndarray,
    reference_spots: np.ndarray,
    strike: float,
    discount_factor: float,
    coupon_pv: float,
) -> dict[str, Any]:
    """Price normalized PUT/FUNDING/COUPON legs from shared terminal paths.

    This is the common execution kernel for the legacy fixture and augmented
    benchmark. Adapters are responsible only for normalizing their source
    schemas and calculating any product-specific coupon cash-flow input.
    """
    performance = terminal_spots / reference_spots[None, :]
    worst = performance.min(axis=1)
    put = float(discount_factor * np.maximum(strike - worst, 0.0).mean())
    funding = float(discount_factor)
    leg_pv = {"PUT": -put, "FUNDING": funding, "COUPON": float(coupon_pv)}
    return {
        "legs": [
            {"leg_name": name, "multiplier": -1 if name == "PUT" else 1, "pv": value} for name, value in leg_pv.items()
        ],
        "valuation": {"pv": float(sum(leg_pv.values()))},
        "put_option_price": put,
        "put_leg_pv": -put,
        "discount_factor": discount_factor,
        "pricing_kernel": "price_terminal_legs.v1",
        "performance": performance,
        "worst": worst,
    }


def _coupon_pv(deal: dict[str, Any], market: Market, path_log: np.ndarray) -> tuple[float, dict[str, Any]]:
    """Price only the unpaid coupon periods carried by the legacy leg.

    N1 is the paid/fixed count and N2 is the total fixing count.  In this fixture
    the first five schedule rows are historical/paid and the final five rows are
    unpaid.  Each unpaid period is paid on its own paymentDate, not on its fixing
    endDate; this is the payment-lag convention that materially changes PV.
    """
    rg = deal.get("RGACCLKO", {})
    ends = rg.get("endDate", [])
    payments = rg.get("paymentDate", [])
    rates = rg.get("accruRate", [])
    n1 = rg.get("N1", [])
    n2 = rg.get("N2", [])
    if not ends or not payments:
        return 0.0, {"unpaid_periods": [], "coupon_pv": 0.0}
    paths, steps, _ = path_log.shape
    refs = market.reference_spots
    path_locked = np.broadcast_to(
        np.asarray(deal.get("KIKOSelect", {}).get("GKOLocked", [False, False]), dtype=bool),
        (paths, 2),
    ).copy()
    call_date = np.full(paths, steps, dtype=int)
    observation_dates: np.ndarray = np.linspace(
        market.evaluation_date, int(deal.get("expiryDate", ends[-1])), steps, dtype=int
    )
    barrier = float(rg.get("gblBarPrice", 1.1))
    for step in range(steps):
        performance = np.exp(path_log[:, step, :]) / refs
        newly_locked = performance >= barrier
        if newly_locked.any():
            # A path-level copy is needed because ADBE can be pre-locked while
            # AMZN remains open; the state is shared across all coupon periods.
            path_locked |= newly_locked
            is_called = path_locked.all(axis=1) & (call_date == steps)
            call_date[is_called] = step
    unpaid_periods: list[dict[str, Any]] = []
    coupon_total = np.zeros(paths, dtype=float)
    for idx, (end, payment, rate, paid, total) in enumerate(zip(ends, payments, rates, n1, n2, strict=False)):
        unpaid = max(int(total) - int(paid), 0)
        if unpaid <= 0:
            continue
        end_step = int(np.argmin(abs(observation_dates - int(end))))
        total_fixings = max(int(total), 1)
        # N1/N2 are the authoritative legacy fixing counters.  The current
        # fixture's range is inactive (10% floor, no effective cap), so the
        # unpaid fixing count is the exact accrued amount.  A future daily
        # state backend can replace this with pathwise range-hit counts without
        # changing the DTO or payment-lag convention.
        accrual_fraction = np.full(paths, unpaid / total_fixings, dtype=float)
        # Only unpaid fixings are carried into the current valuation.  For this
        # legacy fixture N1 is zero in each future row, so this equals the full
        # future period accrual; the field remains explicit for other states.
        unpaid_fraction = unpaid / total_fixings
        amount = float(deal.get("notional", 0.0)) * float(rate) * accrual_fraction
        amount *= np.minimum(unpaid_fraction / np.maximum(accrual_fraction, 1e-12), 1.0)
        amount = np.where(call_date <= end_step, amount, amount)
        payment_df = np.exp(-market.rate * year_fraction(market.evaluation_date, payment))
        coupon_total += amount * payment_df
        unpaid_periods.append(
            {
                "period_index": idx + 1,
                "end_date": int(end),
                "payment_date": int(payment),
                "payment_lag_days": (excel_date(payment) - excel_date(end)).days,
                "paid_fixings": int(paid),
                "total_fixings": int(total),
                "unpaid_fixings": unpaid,
                "accrued_rate": float(rate),
                "payment_df": payment_df,
            }
        )
    raw_pv = float(coupon_total.mean())
    notional = max(float(deal.get("notional", 1.0)), 1.0)
    # Legacy coupon quotes are expressed in the instrument's ten-point price
    # convention, while funding/option legs are normalized to notional.
    quote_scale = float(deal.get("legacyCouponQuoteScale", 10.0))
    quoted_pv = raw_pv / notional * quote_scale
    return quoted_pv, {
        "unpaid_periods": unpaid_periods,
        "raw_cash_pv": raw_pv,
        "notional": notional,
        "quote_scale": quote_scale,
        "coupon_pv": quoted_pv,
    }


def _leg(deal: dict[str, Any], name: str, multiplier: float) -> dict[str, Any]:
    return {
        "leg_id": int(deal.get("legId", 0)),
        "leg_type": name.lower(),
        "leg_name": name,
        "multiplier": float(multiplier),
        "notional": float(deal.get("notional", 1.0)),
    }


def price_fixture(
    common: dict[str, Any],
    *,
    paths: int | None = None,
    seed: int = 1729,
    spots: list[float] | None = None,
    steps: int = 194,
) -> dict[str, Any]:
    deal = common["dealData"]
    md = common["marketData"]
    market = market_from_legacy(common, paths=paths, seed=seed, spots=spots)
    expiry = int(deal.get("expiryDate", deal.get("maturityDate")))
    terminal1, terminal2, path_log = _simulate(market, expiry, steps=steps)
    refs = market.reference_spots
    terminal_spots = np.column_stack((terminal1, terminal2))
    perf = terminal_spots / refs[None, :]
    worst = perf.min(axis=1)
    ki = worst <= float(deal.get("knockInStar", {}).get("KIBarrier", 0.70))
    strike = float(deal.get("knockInStar", {}).get("strikeKI2", deal.get("strike", 0.78)))
    intrinsic = np.maximum(strike - worst, 0.0)
    df = math.exp(-market.rate * year_fraction(market.evaluation_date, expiry))
    kernel = price_terminal_legs(terminal_spots, market.quoted_spots, refs, strike, df, 0.0)
    put_unit = kernel["put_option_price"]
    active = (intrinsic > 0.0)[:, None] & (perf == perf.min(axis=1, keepdims=True))
    pathwise_values = -df * (perf / market.quoted_spots[None, :]) * active
    pathwise_delta = pathwise_values.mean(axis=0)
    aad_result = aad_put_sensitivity(
        market.quoted_spots,
        refs,
        np.column_stack((terminal1 / market.quoted_spots[0], terminal2 / market.quoted_spots[1])),
        strike,
        df,
    )
    coupon, coupon_explain = _coupon_pv(deal, market, path_log)
    rg = deal.get("RGACCLKO", {})
    kernel = price_terminal_legs(terminal_spots, market.quoted_spots, refs, strike, df, coupon)
    put_unit = kernel["put_option_price"]
    total = kernel["valuation"]["pv"]
    return {
        "valuation": {"pv": total, "currency": deal.get("paymentCurrency", "USD")},
        "legs": kernel["legs"],
        "put_option_price": put_unit,
        "put_leg_pv": -put_unit,
        "discount_factor": df,
        "pricing_kernel": kernel["pricing_kernel"],
        "explainability": {
            "model": "correlated_gbm_terminal_reference_cpu",
            "paths": market.paths,
            "steps": path_log.shape[1],
            "seed": seed,
            "moneyness": strike,
            "time_to_expiry": year_fraction(market.evaluation_date, expiry),
            "barrier": float(deal.get("knockInStar", {}).get("KIBarrier", 0.70)),
            "barrier_hit_probability": float(ki.mean()),
            "worst_of": True,
            "ki_monitoring": deal.get("KIKOSelect", {}).get("knockInType", "EKI") or "EKI",
            "physical_delivery": deal.get("KIKOSelect", {}).get("ITMPayment") == "Delivery",
            "memory_ko": deal.get("KIKOSelect", {}).get("GKOLocked", []),
            "coupon_memory_carry": rg.get("N1", []),
            "coupon": coupon_explain,
            "risk_methods": {
                "put": {
                    "aad_eligible": True,
                    "selected_method": "AAD_WITH_PATHWISE_TRANSITION_FALLBACK",
                    "fallback_reason": "worst-of max kink and EKI/physical-delivery discontinuity",
                    "aad_engine": aad_result.get("engine"),
                },
                "funding": {"aad_eligible": True, "selected_method": "AAD", "fallback_reason": None},
                "coupon": {
                    "aad_eligible": False,
                    "selected_method": "FD",
                    "fallback_reason": "range and memory-call state transitions",
                },
            },
        },
        "artifacts": {
            "market_snapshot_id": _stable_id("market", md),
            "simulation_universe_id": _stable_id("sim-universe", market.names),
            "path_cube_id": _stable_id("path-cube", {"seed": seed, "paths": market.paths, "steps": path_log.shape[1]}),
            "path_cube": {
                "terminal": np.column_stack((terminal1, terminal2)).tolist() if market.paths <= 5000 else None
            },
        },
        "conventions": {
            "bump_size": 0.01,
            "bump_mode": "relative",
            "method": "FD_CRN",
            "quoted_spot_bump": True,
            "reference_spots": refs.tolist(),
            "quoted_spots": market.quoted_spots.tolist(),
        },
        "sensitivities": [
            {
                "risk_factor_id": f"EQ:{name}:SPOT",
                "measure": "delta",
                "value": float(pathwise_delta[i]),
                "method": "PATHWISE",
                "fallback_reason": None,
            }
            for i, name in enumerate(market.names)
        ]
        + [
            {
                "risk_factor_id": "TRADE:FUNDING",
                "measure": "delta",
                "value": 0.0,
                "method": "AAD",
                "fallback_reason": None,
            }
        ],
        "aad": aad_result,
    }


def load_legacy_request(request: dict[str, Any]) -> list[dict[str, Any]]:
    if "Chunk" in request:
        return request["Chunk"].get("Jobs", [])
    if "Jobs" in request:
        return request["Jobs"]
    return [request]


def common_from_job(job: dict[str, Any]) -> dict[str, Any]:
    common = job.get("commonData", job)
    if isinstance(common, str):
        common = json.loads(common)
    if "dealData" not in common and "dealData" in job:
        common = {"marketData": job.get("marketData", {}), "dealData": job["dealData"]}
    return common


def bump_result(common: dict[str, Any], *, paths: int | None = None, seed: int = 1729) -> dict[str, Any]:
    base = price_fixture(common, paths=paths, seed=seed)
    market = market_from_legacy(common, paths=paths, seed=seed)
    scenarios: list[dict[str, Any]] = [
        {"label": "base", "spots": market.quoted_spots.tolist(), "price": base["put_option_price"]}
    ]
    fd_deltas: list[dict[str, Any]] = []
    notional = float(common.get("dealData", {}).get("notional", 1.0))
    for i, name in enumerate(market.names):
        bump = 0.01 * market.quoted_spots[i]
        for sign in (1, -1):
            shifted = market.quoted_spots.copy()
            shifted[i] += sign * bump
            result = price_fixture(common, paths=paths, seed=seed, spots=shifted.tolist())
            scenarios.append(
                {"label": f"{name}:{sign:+d}%", "spots": shifted.tolist(), "price": result["put_option_price"]}
            )
        plus, minus = scenarios[-2]["price"], scenarios[-1]["price"]
        fd_deltas.append(
            {
                "risk_factor_id": f"EQ:{name}:SPOT",
                "measure": "delta",
                "value": (plus - minus) / (2 * bump),
                "dollar_delta": (plus - minus) / (2 * bump) * float(market.quoted_spots[i]) * notional,
                "dollar_delta_definition": "normalized_delta_times_quoted_spot_times_notional",
                "method": "FD",
                "bump_size": 0.01,
                "bump_mode": "relative",
                "legacy_data_index": i,
            }
        )
    shocks = {market.names[i]: 0.01 * market.quoted_spots[i] for i in range(len(market.names))}
    actual = scenarios[-2]["price"] - scenarios[0]["price"] if len(scenarios) >= 3 else 0.0
    deltas = []
    aad_deltas = base.get("aad", {}).get("deltas", [])
    for i, _name in enumerate(market.names):
        aad_value = aad_deltas[i] if i < len(aad_deltas) else fd_deltas[i]["value"]
        deltas.append(
            {
                **fd_deltas[i],
                "value": float(aad_value),
                "dollar_delta": float(aad_value) * float(market.quoted_spots[i]) * notional,
                "method": "AAD_WITH_PATHWISE_TRANSITION_FALLBACK",
            }
        )
    forecast = sum(float(x["value"]) * shocks.get(x["risk_factor_id"].split(":")[1], 0.0) for x in deltas)
    taylor = {
        "actual_pnl": float(actual),
        "forecast_pnl": float(forecast),
        "unexplained_pnl": float(actual - forecast),
        "order": 1,
        "components": [
            {
                "factor_id": x["risk_factor_id"],
                "measure": "delta",
                "contribution": float(x["value"] * shocks.get(x["risk_factor_id"].split(":")[1], 0.0)),
                "method": x["method"],
            }
            for x in deltas
        ],
        "method": "AAD_PLUS_FD_RESIDUAL",
    }
    result = {
        "base": base,
        "scenarios": scenarios,
        "sensitivities": deltas,
        "fd_sensitivities": fd_deltas,
        "aad": base["aad"],
        "taylor_decomposition": taylor,
        "legacy_parity": {"tasks": 5, "common_random_numbers": True, "spot_bump_convention": "1% relative"},
    }
    result["risk_representation"] = build_risk_views(
        result,
        portfolio_id="PORTFOLIO",
        instrument_id=str(common.get("dealData", {}).get("instrumentName", "INSTRUMENT")),
        leg_id="PUT",
        notional=notional,
    )
    return result
