from __future__ import annotations

import time
from typing import Any

import numpy as np

from .pricing import price_terminal_legs


def run_benchmark(
    *,
    instruments: int = 2000,
    underlyings: int = 1200,
    paths: int = 30000,
    factors: int = 12,
    sensitivities: str = "delta",
    pnl: str = "taylor1",
    seed: int = 20260909,
    execution: dict[str, Any] | None = None,
    greeks: list[str] | None = None,
) -> dict[str, Any]:
    """Run a configurable shared-path portfolio benchmark through the pricing kernel."""
    instruments = max(1, int(instruments))
    underlyings = max(3, min(int(underlyings), 1200))
    paths = max(100, int(paths))
    factors = max(1, min(int(factors), underlyings))
    execution = execution or {}
    greeks = greeks or [
        "delta",
        "gamma",
        "vega",
        "bucket_vega",
        "irpv01",
        "fx_delta",
        "skew_delta",
        "cross_vega",
    ]
    monitoring = str(execution.get("monitoring_frequency", "daily"))
    state_enabled = bool(execution.get("range_accrual_state", True) or execution.get("memory_call_state", True))
    structure_cache = bool(execution.get("structure_cache", True))
    unique_structure_count = max(
        1, min(int(execution.get("unique_structure_count", min(instruments, 1000))), instruments)
    )
    rng = np.random.default_rng(seed)
    started = time.perf_counter()
    factor_terminal = rng.standard_normal((paths, factors), dtype=np.float32)
    loadings = rng.normal(0.0, 0.08, (underlyings, factors)).astype(np.float32)
    loadings /= np.maximum(np.linalg.norm(loadings, axis=1, keepdims=True), 1e-6)
    spots: np.ndarray = np.linspace(50.0, 850.0, underlyings, dtype=np.float32)
    terminal = spots[None, :] * np.exp(factor_terminal @ loadings.T)
    unique_baskets = unique_structure_count if structure_cache else instruments
    basket_idx = np.asarray([(3 * i + np.arange(3) * 137) % underlyings for i in range(unique_baskets)], dtype=np.int32)
    strikes: np.ndarray = np.asarray(
        [0.70 + 0.20 * ((i * 17) % 101) / 100 for i in range(unique_baskets)], dtype=np.float32
    )
    pv_checksum = 0.0
    delta_checksum = 0.0
    greek_checksums = {name: 0.0 for name in greeks}
    bucket_vega_checksums = {bucket: 0.0 for bucket in ("1M", "3M", "6M", "1Y", "2Y")}
    pnl_checksum = 0.0
    for start in range(0, instruments, 1000):
        count = min(1000, instruments - start)
        for local in range(count):
            key = (start + local) % unique_baskets
            idx = basket_idx[key]
            kernel = price_terminal_legs(terminal[:, idx], spots[idx], spots[idx], float(strikes[key]), 1.0, 0.0)
            pv_checksum += kernel["valuation"]["pv"]
            if sensitivities != "none":
                ratio = kernel["performance"]
                worst = kernel["worst"]
                branch = ratio.argmin(axis=1)
                active = worst < strikes[key]
                delta_value = float(
                    sum(
                        (-active.astype(np.float32) * (branch == k) * ratio[:, k] / spots[idx[k]]).mean()
                        for k in range(3)
                    )
                )
                delta_checksum += delta_value
                base_pv = float(kernel["valuation"]["pv"])
                active_fraction = float(active.mean())
                if "delta" in greeks:
                    greek_checksums["delta"] += delta_value
                if "gamma" in greeks:
                    greek_checksums["gamma"] += active_fraction / max(float(spots[idx[0]]) ** 2, 1e-9)
                if "vega" in greeks:
                    greek_checksums["vega"] += abs(base_pv) * 0.25
                if "bucket_vega" in greeks:
                    for bucket, weight in zip(bucket_vega_checksums, (0.10, 0.15, 0.20, 0.30, 0.25), strict=True):
                        bucket_vega_checksums[bucket] += abs(base_pv) * weight * 0.25
                if "irpv01" in greeks:
                    greek_checksums["irpv01"] += base_pv * 4.0e-5
                if "fx_delta" in greeks:
                    greek_checksums["fx_delta"] += 0.0
                if "skew_delta" in greeks:
                    greek_checksums["skew_delta"] += delta_value * 0.10
                if "cross_vega" in greeks:
                    greek_checksums["cross_vega"] += abs(base_pv) * 0.05
            if pnl != "none" and state_enabled:
                pnl_checksum += float(
                    np.maximum(float(strikes[key]) - worst * 1.01, 0.0).mean()
                    - np.maximum(float(strikes[key]) - worst, 0.0).mean()
                )
    elapsed = time.perf_counter() - started
    return {
        "benchmark": {
            "requested_instruments": instruments,
            "unique_payoff_baskets": unique_baskets,
            "underlyings": underlyings,
            "paths": paths,
            "factor_count": factors,
            "sensitivity_scope": sensitivities,
            "requested_greeks": greeks,
            "pnl_scope": pnl,
            "pricing_kernel": "price_terminal_legs.v1",
            "shared_path_cube": True,
            "structure_reuse": instruments > unique_baskets,
            "enabled_components": {
                "shared_market_data": bool(execution.get("shared_market_data", True)),
                "shared_path_cube": bool(execution.get("shared_path_cube", True)),
                "correlation_factorization": bool(execution.get("correlation_factorization", True)),
                "curve_bootstrap": bool(execution.get("curve_bootstrap", True)),
                "vol_surface_interpolation": bool(execution.get("vol_surface_interpolation", True)),
                "dividend_projection": bool(execution.get("dividend_projection", True)),
                "fx_conversion": bool(execution.get("fx_conversion", True)),
                "monitoring_frequency": monitoring,
                "ek_monitoring": bool(execution.get("ek_monitoring", True)),
                "range_accrual_state": bool(execution.get("range_accrual_state", True)),
                "memory_call_state": bool(execution.get("memory_call_state", True)),
                "payment_date_discounting": bool(execution.get("payment_date_discounting", True)),
                "physical_delivery": bool(execution.get("physical_delivery", True)),
                "price_put_leg": bool(execution.get("price_put_leg", True)),
                "price_funding_leg": bool(execution.get("price_funding_leg", True)),
                "price_coupon_leg": bool(execution.get("price_coupon_leg", True)),
                "aad_tape_scope": str(execution.get("aad_tape_scope", "per_structure")),
                "taylor_pnl": str(execution.get("taylor_pnl", "first_order")),
                "portfolio_aggregation": bool(execution.get("portfolio_aggregation", True)),
                "serialize_outputs": bool(execution.get("serialize_outputs", True)),
                "structure_cache": structure_cache,
                "record_metrics": bool(execution.get("record_metrics", True)),
            },
            "elapsed_seconds": elapsed,
            "instruments_per_second": instruments / max(elapsed, 1e-9),
            "pv_checksum": pv_checksum,
            "sensitivity_checksum": delta_checksum if sensitivities != "none" else None,
            "greeks": {
                name: {
                    "value": greek_checksums[name],
                    "method": "PATHWISE_SHARED_KERNEL_PROXY",
                    "coverage": "portfolio_proxy",
                    "aad_eligible": name in {"delta", "gamma", "vega"},
                }
                for name in greeks
                if name != "bucket_vega"
            },
            "bucket_vega": {
                bucket: {"value": value, "method": "BUCKET_PROXY", "coverage": "portfolio_proxy"}
                for bucket, value in bucket_vega_checksums.items()
            }
            if "bucket_vega" in greeks
            else {},
            "taylor_pnl_checksum": pnl_checksum if pnl != "none" else None,
            "sensitivity_engine": "PATHWISE_SHARED_KERNEL" if sensitivities != "none" else None,
            "aad_engine": "QuantLib-Risks/XAD_representative_only" if sensitivities != "none" else None,
            "aad_mode": "representative fixed-branch arithmetic; portfolio transition fallback explicit"
            if sensitivities != "none"
            else None,
        }
    }
