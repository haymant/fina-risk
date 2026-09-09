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
) -> dict[str, Any]:
    """Run a configurable shared-path portfolio benchmark through the pricing kernel."""
    instruments = max(1, int(instruments))
    underlyings = max(3, min(int(underlyings), 1200))
    paths = max(100, int(paths))
    factors = max(1, min(int(factors), underlyings))
    rng = np.random.default_rng(seed)
    started = time.perf_counter()
    factor_terminal = rng.standard_normal((paths, factors), dtype=np.float32)
    loadings = rng.normal(0.0, 0.08, (underlyings, factors)).astype(np.float32)
    loadings /= np.maximum(np.linalg.norm(loadings, axis=1, keepdims=True), 1e-6)
    spots: np.ndarray = np.linspace(50.0, 850.0, underlyings, dtype=np.float32)
    terminal = spots[None, :] * np.exp(factor_terminal @ loadings.T)
    unique_baskets = min(instruments, max(1000, underlyings // 3))
    basket_idx = np.asarray([(3 * i + np.arange(3) * 137) % underlyings for i in range(unique_baskets)], dtype=np.int32)
    strikes: np.ndarray = np.asarray(
        [0.70 + 0.20 * ((i * 17) % 101) / 100 for i in range(unique_baskets)], dtype=np.float32
    )
    pv_checksum = 0.0
    delta_checksum = 0.0
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
                delta_checksum += float(
                    sum(
                        (-active.astype(np.float32) * (branch == k) * ratio[:, k] / spots[idx[k]]).mean()
                        for k in range(3)
                    )
                )
            if pnl != "none":
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
            "pnl_scope": pnl,
            "pricing_kernel": "price_terminal_legs.v1",
            "shared_path_cube": True,
            "structure_reuse": instruments > unique_baskets,
            "elapsed_seconds": elapsed,
            "instruments_per_second": instruments / max(elapsed, 1e-9),
            "pv_checksum": pv_checksum,
            "sensitivity_checksum": delta_checksum if sensitivities != "none" else None,
            "taylor_pnl_checksum": pnl_checksum if pnl != "none" else None,
            "sensitivity_engine": "PATHWISE_SHARED_KERNEL" if sensitivities != "none" else None,
            "aad_engine": "QuantLib-Risks/XAD_representative_only" if sensitivities != "none" else None,
            "aad_mode": "representative fixed-branch arithmetic; portfolio transition fallback explicit"
            if sensitivities != "none"
            else None,
        }
    }
