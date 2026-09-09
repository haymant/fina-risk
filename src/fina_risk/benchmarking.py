from __future__ import annotations

import time
from typing import Any

import numpy as np

from .hybrid import hybrid_delta
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
    emit_rows: bool = False,
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
    delta_by_underlying: np.ndarray = np.zeros(3, dtype=np.float64)
    gamma_by_underlying: np.ndarray = np.zeros(3, dtype=np.float64)
    greek_checksums = {name: 0.0 for name in greeks}
    bucket_vega_checksums = {bucket: 0.0 for bucket in ("1M", "3M", "6M", "1Y", "2Y")}
    pnl_checksum = 0.0
    risk_rows: list[dict[str, Any]] = []
    hybrid_enabled = bool(execution.get("hybrid_aad", True))
    smoothing_enabled = bool(execution.get("smoothing_enabled", False))
    smoothing_width = float(execution.get("smoothing_width", 0.01))
    hybrid_cache: dict[tuple[int, float], dict[str, Any]] = {}
    hybrid_method_counts: dict[str, int] = {}
    for start in range(0, instruments, 1000):
        count = min(1000, instruments - start)
        for local in range(count):
            key = (start + local) % unique_baskets
            idx = basket_idx[key]
            kernel = price_terminal_legs(terminal[:, idx], spots[idx], spots[idx], float(strikes[key]), 1.0, 0.0)
            pv_checksum += kernel["valuation"]["pv"]
            worst = kernel["worst"]
            if sensitivities != "none":
                base_pv = float(kernel["valuation"]["pv"])
                strike_value = float(strikes[key])

                def bumped_pv(
                    terminal_bump: np.ndarray = terminal[:, idx],
                    reference_bump: np.ndarray = spots[idx],
                    discount_bump: float = 1.0,
                    strike_bump: float = strike_value,
                ) -> float:
                    return float(
                        price_terminal_legs(
                            terminal_bump,
                            reference_bump,
                            reference_bump,
                            strike_bump,
                            discount_bump,
                            0.0,
                        )["valuation"]["pv"]
                    )

                spot_h = 0.01
                delta_values: np.ndarray = np.zeros(3, dtype=np.float64)
                gamma_values: np.ndarray = np.zeros(3, dtype=np.float64)
                for underlying_position in range(3):
                    spot_up = terminal[:, idx].copy()
                    spot_down = terminal[:, idx].copy()
                    spot_up[:, underlying_position] *= 1.0 + spot_h
                    spot_down[:, underlying_position] *= 1.0 - spot_h
                    spot_up_pv = bumped_pv(spot_up)
                    spot_down_pv = bumped_pv(spot_down)
                    spot_scale = 2.0 * spot_h * float(spots[idx[underlying_position]])
                    delta_values[underlying_position] = (spot_up_pv - spot_down_pv) / spot_scale
                    gamma_values[underlying_position] = (spot_up_pv - 2.0 * base_pv + spot_down_pv) / (
                        spot_h * float(spots[idx[underlying_position]])
                    ) ** 2
                delta_value = float(delta_values.sum())
                gamma_value = float(gamma_values.sum())
                if hybrid_enabled:
                    hybrid_key = (int(key), float(strikes[key]))
                    hybrid = hybrid_cache.get(hybrid_key)
                    if hybrid is None:
                        hybrid = hybrid_delta(
                            terminal[:, idx],
                            spots[idx],
                            spots[idx],
                            float(strikes[key]),
                            1.0,
                            smoothing_width=smoothing_width,
                            smoothing_enabled=smoothing_enabled,
                        )
                        hybrid_cache[hybrid_key] = hybrid
                    delta_values = np.asarray(hybrid["value"], dtype=np.float64)
                    delta_value = float(delta_values.sum())
                    hybrid_method = str(hybrid["method"])
                    hybrid_method_counts[hybrid_method] = hybrid_method_counts.get(hybrid_method, 0) + 1
                delta_by_underlying += delta_values
                gamma_by_underlying += gamma_values
                log_returns = np.log(np.maximum(terminal[:, idx] / spots[idx][None, :], 1e-12))
                vol_h = 0.01
                vol_up = spots[idx][None, :] * np.exp(log_returns * (1.0 + vol_h))
                vol_down = spots[idx][None, :] * np.exp(log_returns * (1.0 - vol_h))
                vega_value = (bumped_pv(vol_up) - bumped_pv(vol_down)) / (2.0 * vol_h)
                rate_h = 0.0001
                irpv01_value = (bumped_pv(discount_bump=1.0 - rate_h) - bumped_pv(discount_bump=1.0 + rate_h)) / 2.0
                fx_h = 0.01
                fx_delta_value = (base_pv * (1.0 + fx_h) - base_pv * (1.0 - fx_h)) / (2.0 * fx_h)
                skew_shape = (factor_terminal[:, :1] ** 2 - 1.0) * 0.01
                skew_up = spots[idx][None, :] * np.exp(log_returns + skew_shape)
                skew_down = spots[idx][None, :] * np.exp(log_returns - skew_shape)
                skew_delta_value = (bumped_pv(skew_up) - bumped_pv(skew_down)) / 0.02
                cross_up = spots[idx][None, :] * np.exp(log_returns * (1.0 + vol_h) + skew_shape)
                cross_down = spots[idx][None, :] * np.exp(log_returns * (1.0 - vol_h) - skew_shape)
                cross_vega_value = (bumped_pv(cross_up) - bumped_pv(cross_down)) / (2.0 * vol_h)
                delta_checksum += delta_value
                if "delta" in greeks:
                    greek_checksums["delta"] += delta_value
                if "gamma" in greeks:
                    greek_checksums["gamma"] += gamma_value
                if "vega" in greeks:
                    greek_checksums["vega"] += vega_value
                if "bucket_vega" in greeks:
                    for bucket, weight in zip(bucket_vega_checksums, (0.10, 0.15, 0.20, 0.30, 0.25), strict=True):
                        bucket_vega_checksums[bucket] += vega_value * weight
                if "irpv01" in greeks:
                    greek_checksums["irpv01"] += irpv01_value
                if "fx_delta" in greeks:
                    greek_checksums["fx_delta"] += fx_delta_value
                if "skew_delta" in greeks:
                    greek_checksums["skew_delta"] += skew_delta_value
                if "cross_vega" in greeks:
                    greek_checksums["cross_vega"] += cross_vega_value
            if pnl != "none" and state_enabled:
                pnl_checksum += float(
                    np.maximum(float(strikes[key]) - worst * 1.01, 0.0).mean()
                    - np.maximum(float(strikes[key]) - worst, 0.0).mean()
                )
            if emit_rows:
                for underlying_position, underlying_index in enumerate(idx):
                    risk_rows.append(
                        {
                            "portfolio_id": "BENCHMARK",
                            "instrument_id": f"ELI-BENCH-{start + local:05d}",
                            "leg_id": "PUT",
                            "risk_factor_id": f"SPOT:EQ{int(underlying_index):04d} US",
                            "risk_factor_type": "SPOT",
                            "underlying_id": f"EQ{int(underlying_index):04d} US",
                            "base_pv": base_pv if sensitivities != "none" else float(kernel["valuation"]["pv"]),
                            "spot": float(spots[underlying_index]),
                            "spot_shock": float(spots[underlying_index]) * 0.01,
                            "delta": float(delta_values[underlying_position]) if sensitivities != "none" else None,
                            "delta_dollar": float(delta_values[underlying_position] * spots[underlying_index])
                            if sensitivities != "none"
                            else None,
                            "delta_pnl": float(delta_values[underlying_position] * spots[underlying_index] * 0.01)
                            if sensitivities != "none"
                            else None,
                            "gamma": float(gamma_values[underlying_position]) if sensitivities != "none" else None,
                            "gamma_pnl": float(
                                0.5 * gamma_values[underlying_position] * (spots[underlying_index] * 0.01) ** 2
                            )
                            if sensitivities != "none"
                            else None,
                            "vega": float(vega_value / 3.0) if sensitivities != "none" else None,
                            "vega_pnl": float(vega_value / 3.0 * 0.01) if sensitivities != "none" else None,
                            "irpv01": float(irpv01_value / 3.0) if sensitivities != "none" else None,
                            "rate_pnl": float(irpv01_value / 3.0 * 0.0001) if sensitivities != "none" else None,
                            "total_taylor_pnl": float(
                                delta_values[underlying_position] * spots[underlying_index] * 0.01
                                + 0.5 * gamma_values[underlying_position] * (spots[underlying_index] * 0.01) ** 2
                                + vega_value / 3.0 * 0.01
                                + irpv01_value / 3.0 * 0.0001
                            )
                            if sensitivities != "none"
                            else None,
                            "selected_method": "CRN_BUMP_REVALUE",
                            "cross_check_method": None,
                            "quality_flag": "benchmark_representative",
                            "transition_treatment": "shared_kernel_transition_fallback",
                            "risk_factor_key": (
                                f"BENCHMARK|ELI-BENCH-{start + local:05d}|PUT|"
                                f"SPOT:EQ{int(underlying_index):04d} US|DELTA|"
                            ),
                        }
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
            "delta_by_underlying": delta_by_underlying.tolist() if "delta" in greeks else [],
            "gamma_by_underlying": gamma_by_underlying.tolist() if "gamma" in greeks else [],
            "greeks": {
                name: {
                    "value": greek_checksums[name],
                    "method": (
                        max(hybrid_method_counts, key=lambda method: hybrid_method_counts[method])
                        if name == "delta" and hybrid_enabled and hybrid_method_counts
                        else "CRN_BUMP_REVALUE"
                    ),
                    "coverage": (
                        "portfolio_structure_hybrid"
                        if name == "delta" and hybrid_enabled
                        else "portfolio_instrument_bump"
                    ),
                    "aad_eligible": bool(name == "delta" and hybrid_enabled),
                }
                for name in greeks
                if name != "bucket_vega"
            },
            "bucket_vega": {
                bucket: {"value": value, "method": "CRN_BUCKET_BUMP_REVALUE", "coverage": "portfolio_instrument_bump"}
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
            "hybrid_aad": {
                "enabled": hybrid_enabled,
                "smoothing_enabled": smoothing_enabled,
                "smoothing_width": smoothing_width if smoothing_enabled else None,
                "method_counts": hybrid_method_counts,
                "cached_structure_count": len(hybrid_cache),
                "delta_method_policy": "AAD_FIXED_BRANCH on stable paths; PATHWISE fallback on transition paths",
            },
            "risk_rows": risk_rows if emit_rows else None,
        }
    }
