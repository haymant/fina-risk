from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

from fina_risk.data import load_json_source

ROOT = Path(__file__).resolve().parents[1]


def run(instrument_source: str, market_source: str, paths: int = 30000, seed: int = 20260909) -> dict:
    instruments = load_json_source(instrument_source)
    market = load_json_source(market_source)
    underlyings = market["underlyings"]
    n = len(underlyings)
    factors = int(market["simulation"]["factorCount"])
    steps = int(market["simulation"]["steps"])
    rng = np.random.default_rng(seed)
    # Reusable simulation universe: one factor path cube shared by every trade.
    t0 = time.perf_counter()
    factor_terminal = rng.standard_normal((paths, factors), dtype=np.float32)
    factor_daily = rng.standard_normal((paths, steps, factors), dtype=np.float32)
    # Keep the path cube factorized, avoiding a fake benchmark that materializes
    # 1,200 independent path arrays per instrument.
    loadings = rng.normal(0.0, 0.08, (n, factors)).astype(np.float32)
    loadings /= np.maximum(np.linalg.norm(loadings, axis=1, keepdims=True), 1e-6)
    id_to_idx = {u["id"]: i for i, u in enumerate(underlyings)}
    spots = np.asarray([u["spot"] for u in underlyings], dtype=np.float32)
    vols = np.asarray([0.15 + 0.45 * ((i * 17) % 101) / 100 for i in range(n)], dtype=np.float32)
    # Shared terminal shocks are sufficient for the payoff benchmark; daily cube
    # allocation is retained to account for lifecycle/range-state memory pressure.
    terminal_market = spots[None, :] * np.exp(factor_terminal @ loadings.T * vols[None, :])
    path_build_seconds = time.perf_counter() - t0
    prices = np.empty(len(instruments["instruments"]), dtype=np.float64)
    t1 = time.perf_counter()
    for start in range(0, len(prices), 100):
        batch = instruments["instruments"][start : start + 100]
        for j, inst in enumerate(batch, start):
            idx = [id_to_idx[x] for x in inst["underlyings"]]
            ratio = terminal_market[:, idx] / spots[idx][None, :]
            worst = ratio.min(axis=1)
            strike = inst["legs"][0]["payoff"]["strike"]
            put = np.maximum(strike - worst, 0.0).mean()
            coupon = inst["legs"][2]["payoff"]["rate"] * 5.0 / 10.0
            prices[j] = float(1.0 - put + coupon)
    pricing_seconds = time.perf_counter() - t1
    return {
        "benchmark": {
            "instruments": len(prices),
            "underlyings": n,
            "legs_average": instruments["averageLegs"],
            "paths": paths,
            "steps": steps,
            "factor_count": factors,
            "seed": seed,
            "path_cube_model": "shared_factorized_float32",
            "market_reverse_index": "underlying_id_to_column",
            "instrument_batch_size": 100,
            "path_build_seconds": path_build_seconds,
            "pricing_seconds": pricing_seconds,
            "total_seconds": path_build_seconds + pricing_seconds,
            "instruments_per_second": len(prices) / max(path_build_seconds + pricing_seconds, 1e-9),
            "peak_path_cube_bytes": int(factor_terminal.nbytes + factor_daily.nbytes + terminal_market.nbytes),
            "price_checksum": float(prices.sum()),
            "price_mean": float(prices.mean()),
        }
    }


if __name__ == "__main__":
    root = ROOT / "benchmark"
    result = run(
        sys.argv[1] if len(sys.argv) > 1 else str(root / "instruments.json"),
        sys.argv[2] if len(sys.argv) > 2 else str(root / "market.json"),
    )
    print(json.dumps(result, indent=2))
