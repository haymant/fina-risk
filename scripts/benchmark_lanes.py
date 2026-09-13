#!/usr/bin/env python3
"""Benchmark the fina-risk pricing lanes at 100k instruments, 3k paths, AAD off.

Lanes:
  terminal_parity   fina_risk_cpp.run_cpp_parity      terminal cube, static features
  daily_eki_batch   fina_risk_cpp.run_daily_termsheet_batch
                                                      daily cube, ALL features
                                                      (N1/N2 in-range fixings,
                                                      memory carry, global-KO
                                                      termination, EKI put)

Both price the same 100k worst-of ELIFCN_KI instruments on a 3k-path
Cholesky-correlated GBM cube (rho=0.459325). Writes benchmark/lanes.json.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path
from typing import Any

import numpy as np

import fina_risk_cpp
from fina_risk.daily_termsheet import weekday_serials

SEED = 20260909
SYMBOLS = ("ADBE UW", "AMZN UW", "MSFT UW", "NVDA UW")
SPOTS = {"ADBE UW": 267.885, "AMZN UW": 258.355, "MSFT UW": 512.30, "NVDA UW": 118.75}
VOLS = {"ADBE UW": 0.4495, "AMZN UW": 0.3562, "MSFT UW": 0.40, "NVDA UW": 0.55}
RHO = 0.4593248180905
RATE = 0.042341
EVAL = 46272
NOTIONAL = (100_000.0, 250_000.0, 500_000.0, 1_000_000.0)


def generate(count: int) -> dict[str, Any]:
    rng = np.random.default_rng(SEED)
    strike = np.round(0.78 * rng.uniform(0.85, 1.05, count), 6)
    ki = np.round(strike * rng.uniform(0.80, 0.95, count), 6)
    call = np.round(rng.uniform(1.02, 1.20, count), 6)
    notional = rng.choice(NOTIONAL, count)
    coupon = np.round(rng.uniform(0.006, 0.018, count), 8)
    expiry = 46419 + rng.integers(-10, 11, count)
    n1 = rng.integers(0, 8, count)
    n2 = n1 + rng.integers(1, 9, count)
    picks = rng.integers(0, len(SYMBOLS), (count, 2))
    terminal, daily, compact = [], [], []
    ids = list(SYMBOLS)
    for i in range(count):
        a, b = SYMBOLS[picks[i, 0]], SYMBOLS[picks[i, 1]]
        if a == b:
            b = SYMBOLS[(picks[i, 1] + 1) % len(SYMBOLS)]
        terminal.append(
            {
                "instrumentId": f"ELI-{i:06d}",
                "underlyings": [a, b],
                "legs": [{"leg_id": 1, "leg_type": "intrinsic_option", "leg_name": "PUT",
                          "multiplier": -1, "payoff": {"basket": "worst_of", "strike": float(strike[i])}}],
            }
        )
        exp = int(expiry[i])
        compact.append(
            {
                "refs": [SPOTS[a], SPOTS[b]],
                "strike": float(strike[i]),
                "ki": float(ki[i]),
                "call": float(call[i]),
                "expiry": exp,
                "notional": float(notional[i]),
                "quote_scale": 10.0,
                "periods": [
                    {"end": EVAL + int((exp - EVAL) * (j + 1) / 5), "pay": EVAL + int((exp - EVAL) * (j + 1) / 5) + 2,
                     "rate": float(coupon[i]), "n1": int(n1[i]), "n2": int(n2[i]), "low": 0.0, "up": 999.99}
                    for j in range(5)
                ],
            }
        )
    return {
        "terminal": {"instruments": terminal},
        "daily": {"instruments": compact},
        "market": {"underlyings": [{"id": i, "spot": SPOTS[i]} for i in ids]},
        "daily_market": {"rate": RATE, "evaluation_date": EVAL, "underlyings": [{"id": i, "spot": SPOTS[i]} for i in ids]},
        "ids": ids,
    }


def cholesky_paths(ids: list[str], paths: int, observations: int | None) -> np.ndarray:
    n = len(ids)
    corr = np.full((n, n), RHO)
    np.fill_diagonal(corr, 1.0)
    chol = np.linalg.cholesky(corr)
    rng = np.random.default_rng(SEED)
    spot = np.asarray([SPOTS[i] for i in ids])
    vol = np.asarray([VOLS[i] for i in ids])
    if observations is None:
        z = rng.standard_normal((paths, n)) @ chol.T
        t = (46419 - EVAL) / 365.0
        return (spot[None, :] * np.exp((RATE - 0.5 * vol**2) * t + vol * math.sqrt(t) * z)).astype(np.float64)
    dt = 1.0 / 252.0
    z = rng.standard_normal((paths, observations, n)) @ chol.T
    drift = (RATE - 0.5 * vol**2)[None, None, :] * dt
    return (spot[None, None, :] * np.exp(np.cumsum(drift + vol[None, None, :] * math.sqrt(dt) * z, axis=1))).astype(np.float64)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--instruments", type=int, default=100_000)
    ap.add_argument("--paths", type=int, default=3_000)
    ap.add_argument("--out", type=Path, default=Path("benchmark/lanes.json"))
    args = ap.parse_args()

    t = time.perf_counter()
    corpus = generate(args.instruments)
    gen_s = time.perf_counter() - t
    ids = corpus["ids"]
    dates = weekday_serials(EVAL, 46421).astype(np.int32)
    obs = int(dates.size)
    terminal_cube = cholesky_paths(ids, args.paths, None)
    daily_cube = cholesky_paths(ids, args.paths, obs)

    print(f"[gen] {args.instruments} instruments in {gen_s:.2f}s  (obs={obs}, paths={args.paths})", flush=True)

    # Lane 1: terminal parity.
    t = time.perf_counter()
    lane1 = json.loads(
        fina_risk_cpp.run_cpp_parity(
            json.dumps(corpus["terminal"]), json.dumps(corpus["market"]), terminal_cube, SEED, 0.01
        )
    )
    lane1_s = time.perf_counter() - t

    # Lane 2: daily EKI batch (all features on).
    t = time.perf_counter()
    lane2 = json.loads(
        fina_risk_cpp.run_daily_termsheet_batch(
            json.dumps(corpus["daily"]), json.dumps(corpus["daily_market"]), daily_cube, dates
        )
    )
    lane2_s = time.perf_counter() - t

    result = {
        "instruments": args.instruments,
        "paths": args.paths,
        "observations": obs,
        "correlation": {"method": "cholesky", "rho": RHO},
        "aad_engine": "disabled",
        "gen_seconds": gen_s,
        "lanes": {
            "terminal_parity": {**lane1, "wall_seconds": lane1_s, "instruments_per_second": args.instruments / lane1_s},
            "daily_eki_batch": {**lane2, "wall_seconds": lane2_s},
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2))

    print("\n=== lane benchmark (100k × 3k paths, AAD off) ===")
    print(f"{'lane':<18}{'wall s':>9}{'inst/s':>12}{'mean PV':>14}   features")
    print(f"{'terminal_parity':<18}{lane1_s:>9.3f}{args.instruments/lane1_s:>12.0f}{lane1['pv_checksum']/args.instruments:>14.6f}   static (terminal)")
    print(f"{'daily_eki_batch':<18}{lane2_s:>9.3f}{lane2['instruments_per_second']:>12.0f}{lane2['mean_pv']:>14.6f}   N1/N2 + memory + KO + EKI")
    print(f"\nterminal: pv={lane1['pv_checksum']:.3f} delta$={lane1['delta_dollar_checksum']:.3f} gamma={lane1['gamma_checksum']:.4f} "
          f"taylor_forecast={lane1['taylor_forecast_checksum']:.4f} unexplained={lane1['taylor_unexplained_checksum']:.4f}")
    print(f"daily:    put={lane2['put_checksum']:.4f} coupon={lane2['coupon_checksum']:.4f} pv={lane2['pv_checksum']:.4f} "
          f"ki={lane2['ki_probability_mean']:.4f} ko={lane2['ko_probability_mean']:.4f}")
    print(f"[out] {args.out}")


if __name__ == "__main__":
    main()
