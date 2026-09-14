#!/usr/bin/env python3
"""Benchmark the fina-risk pricing lanes at 100k instruments, 3k paths, AAD off.

Lanes:
  terminal_parity   fina_risk_cpp.run_cpp_parity      terminal cube, static features
  daily_eki_batch   fina_risk_cpp.run_daily_termsheet_batch
                                                      daily cube, ALL features
                                                      (N1/N2 in-range fixings,
                                                      memory carry, global-KO
                                                      termination, EKI put)

Correlation is no longer hard coded: the universe correlation matrix is looked
up from the ~1.3M-row lake store (`<lake>/correlations.parquet`) by the ETL
layer, resolved once, and packed with the market payload; the engine consumes
the resolved matrix (and the cube built from it), never the raw table.

Both price the same 100k worst-of ELIFCN_KI instruments on a 3k-path
Cholesky-correlated GBM cube, generated with conservative
surface-wing vols and the real NYSE calendar (103 fixings to 2027-02-03).
The corpus also varies the schedule: 2-9 coupon periods per trade, daily /
weekly / monthly observation strides with a random phase, explicit per-period
start dates, and a 0-5 day earlier EKI observation date. All variants index
into the one shared cube, so a single path set is reused throughout.
Writes benchmark/lanes.json.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np

import fina_risk_cpp
from fina_risk.correlation import resolve_correlation_matrix
from fina_risk.daily_termsheet import nyse_serials

SEED = 20260909
SYMBOLS = ("ADBE UW", "AMZN UW", "MSFT UW", "NVDA UW")
# Lake correlation-table legs for each instrument symbol ("ADBE UW" -> "ADBE").
LEGS = {name: name.split()[0] for name in SYMBOLS}
# Only used if the lake correlation store is unavailable (missing pairs too).
FALLBACK_RHO = 0.4593248180905
SPOTS = {"ADBE UW": 267.885, "AMZN UW": 258.355, "MSFT UW": 512.30, "NVDA UW": 118.75}
# Conservative full-surface reads (downside-wing at the exercise/knock-in
# moneyness) instead of flat ATM: ADBE/AMZN come from the fixture eqVol surface
# (interpolated at the 70-78% strikes); MSFT/NVDA have no surface here and are
# carried at a conservative uplift over their prior ATM placeholders.
VOLS = {"ADBE UW": 0.4748, "AMZN UW": 0.4006, "MSFT UW": 0.45, "NVDA UW": 0.60}
CALENDAR = "NYSE"
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
    # Schedule variants: 2-9 coupon periods per trade, and daily / weekly /
    # monthly observation sampling with a random phase, plus a 0-5 day earlier
    # EKI observation date (final-fixing vs settlement lag stress).
    n_periods = rng.integers(2, 10, count)
    stride = rng.choice([1, 5, 21], count)
    obs_offset = np.array([int(rng.integers(0, int(s))) for s in stride])
    ki_obs_shift = rng.integers(0, 6, count)
    picks = rng.integers(0, len(SYMBOLS), (count, 2))
    terminal, compact = [], []
    ids = list(SYMBOLS)
    period_counts: Counter[int] = Counter()
    stride_counts: Counter[int] = Counter()
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
        npd = int(n_periods[i])
        st = int(stride[i])
        off = int(obs_offset[i])
        ends = [EVAL + int((exp - EVAL) * (j + 1) / npd) for j in range(npd)]
        periods = []
        prev = EVAL
        for end in ends:
            trading_days = max(1, round((end - prev) / 365.0 * 252.0))
            scheduled = max(1, (trading_days + st - 1) // st)
            periods.append(
                {
                    "start": prev + 1,
                    "end": end,
                    "pay": end + 2,
                    "rate": float(coupon[i]),
                    "n1": min(int(n1[i]), scheduled),
                    "n2": scheduled,
                    "low": 0.0,
                    "up": 999.99,
                    "stride": st,
                    "offset": off,
                }
            )
            prev = end
        period_counts[npd] += 1
        stride_counts[st] += 1
        compact.append(
            {
                "refs": [SPOTS[a], SPOTS[b]],
                "strike": float(strike[i]),
                "ki": float(ki[i]),
                "call": float(call[i]),
                "expiry": exp,
                "ki_obs": exp - int(ki_obs_shift[i]),
                "notional": float(notional[i]),
                "quote_scale": 10.0,
                "periods": periods,
            }
        )
    return {
        "terminal": {"instruments": terminal},
        "daily": {"instruments": compact},
        "market": {"underlyings": [{"id": i, "spot": SPOTS[i]} for i in ids]},
        "daily_market": {"rate": RATE, "evaluation_date": EVAL, "underlyings": [{"id": i, "spot": SPOTS[i]} for i in ids]},
        "ids": ids,
        "variants": {
            "coupon_periods": {int(k): int(v) for k, v in sorted(period_counts.items())},
            "obs_stride": {int(k): int(v) for k, v in sorted(stride_counts.items())},
        },
    }


def cholesky_paths(
    ids: list[str], paths: int, observations: int | None, cholesky: np.ndarray
) -> np.ndarray:
    n = len(ids)
    rng = np.random.default_rng(SEED)
    spot = np.asarray([SPOTS[i] for i in ids])
    vol = np.asarray([VOLS[i] for i in ids])
    if observations is None:
        z = rng.standard_normal((paths, n)) @ cholesky.T
        t = (46419 - EVAL) / 365.0
        return (spot[None, :] * np.exp((RATE - 0.5 * vol**2) * t + vol * math.sqrt(t) * z)).astype(np.float64)
    dt = 1.0 / 252.0
    z = rng.standard_normal((paths, observations, n)) @ cholesky.T
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
    dates = nyse_serials(EVAL, 46421).astype(np.int32)
    obs = int(dates.size)

    # Correlation lookup: resolve the (small) universe matrix from the ~1.3M-row
    # lake store once, up front, and hand the *resolved* matrix to the engine —
    # never the raw table. One filtered columnar scan, not a full-table load.
    lookup_t = time.perf_counter()
    corr, corr_meta = resolve_correlation_matrix([LEGS[i] for i in ids], fallback_rho=FALLBACK_RHO)
    corr_meta["lookup_seconds"] = time.perf_counter() - lookup_t
    cholesky = np.linalg.cholesky(corr)
    corpus["daily_market"]["correlationMatrix"] = corr.tolist()
    corpus["daily_market"]["correlationLegs"] = [LEGS[i] for i in ids]

    terminal_cube = cholesky_paths(ids, args.paths, None, cholesky)
    daily_cube = cholesky_paths(ids, args.paths, obs, cholesky)

    print(f"[gen] {args.instruments} instruments in {gen_s:.2f}s  (obs={obs}, paths={args.paths})", flush=True)
    table = corr_meta.get("table", {})
    print(
        f"[corr] source={corr_meta['source']} table_rows={table.get('rows')} "
        f"found={corr_meta['found_pairs']}/{corr_meta['requested_pairs']} "
        f"missing={corr_meta['missing_pairs']} lookup={corr_meta['lookup_seconds']*1000:.1f}ms "
        f"matrix={np.round(corr, 4).tolist()}",
        flush=True,
    )

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
        "calendar": CALENDAR,
        "vols": VOLS,
        "vol_model": "conservative_surface_wing",
        "correlation": {**corr_meta, "method": "cholesky"},
        "aad_engine": "disabled",
        "schedule_variants": corpus.get("variants", {}),
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
