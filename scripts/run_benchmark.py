from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

from fina_risk.aad import aad_put_sensitivity
from fina_risk.data import load_json_source
from fina_risk.pricing import price_terminal_legs

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
            strike = inst["legs"][0]["payoff"]["strike"]
            coupon = inst["legs"][2]["payoff"]["rate"] * 5.0 / 10.0
            kernel = price_terminal_legs(terminal_market[:, idx], spots[idx], spots[idx], strike, 1.0, coupon)
            prices[j] = kernel["valuation"]["pv"]
    pricing_seconds = time.perf_counter() - t1
    risk_results = []
    for inst in instruments["instruments"]:
        idx = [id_to_idx[x] for x in inst["underlyings"]]
        base_kernel = price_terminal_legs(
            terminal_market[:, idx], spots[idx], spots[idx], inst["legs"][0]["payoff"]["strike"], 1.0, 0.0
        )
        ratio = base_kernel["performance"]
        worst_index = ratio.argmin(axis=1)
        worst = base_kernel["worst"]
        strike = inst["legs"][0]["payoff"]["strike"]
        itm = worst < strike
        deltas = []
        for k, underlying_idx in enumerate(idx):
            active = itm & (worst_index == k)
            deltas.append(float((-active.astype(np.float32) * ratio[:, k] / spots[underlying_idx]).mean()))
        shocks = 0.01 * spots[idx]
        forecast = float(sum(deltas[k] * shocks[k] for k in range(len(idx))))
        base_put = base_kernel["put_option_price"]
        shocked_put = np.maximum(strike - (worst * 1.01), 0.0).mean()
        actual = float(shocked_put - base_put)
        risk_results.append(
            {
                "instrumentId": inst["instrumentId"],
                "riskFactors": [
                    {
                        "underlying": inst["underlyings"][k],
                        "measure": "delta",
                        "value": deltas[k],
                        "method": "PATHWISE",
                        "aadEligible": False,
                        "fallbackReason": "worst-of payoff kink",
                    }
                    for k in range(len(idx))
                ],
                "aad": {
                    "available": False,
                    "eligibleCells": ["funding"],
                    "fallbackCells": ["worst_of_put", "memory_coupon"],
                },
                "taylorPnl": {
                    "order": 1,
                    "forecastPnl": forecast,
                    "actualPnl": actual,
                    "unexplainedPnl": actual - forecast,
                    "method": "PATHWISE_PLUS_RESIDUAL",
                },
            }
        )
    representative = instruments["instruments"][0]
    rep_idx = [id_to_idx[x] for x in representative["underlyings"]]
    aad_summary = aad_put_sensitivity(
        spots[rep_idx],
        spots[rep_idx],
        terminal_market[: min(paths, 4000), rep_idx] / spots[rep_idx][None, :],
        representative["legs"][0]["payoff"]["strike"],
        1.0,
    )
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
            "risk_method": "PATHWISE_WITH_HONEST_AAD_FALLBACK",
            "pricing_kernel": "price_terminal_legs.v1",
            "aad_available": bool(aad_summary.get("available", False)),
            "aad_summary": aad_summary,
            "taylor_order": 1,
            "taylor_unexplained_pnl_sum": float(sum(x["taylorPnl"]["unexplainedPnl"] for x in risk_results)),
        },
        "instrument_results": risk_results,
    }


if __name__ == "__main__":
    root = ROOT / "benchmark"
    result = run(
        sys.argv[1] if len(sys.argv) > 1 else str(root / "instruments.json"),
        sys.argv[2] if len(sys.argv) > 2 else str(root / "market.json"),
    )
    print(json.dumps(result, indent=2))
