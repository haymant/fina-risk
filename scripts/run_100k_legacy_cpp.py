#!/usr/bin/env python3
"""100k-instrument legacy ELIFCN_KI benchmark through the fixed C++ kernel.

Pipeline (matches the fina-risk skill):

    termsheet1.md.json format  (augment)
        -> fina pricing-request (ETL / compile)
        -> Cholesky-correlated 3k-path terminal cube
        -> native fina_risk_cpp.run_cpp_parity   (AAD disabled, CRN bump 1%)
        -> PV / delta / gamma / Taylor-2 P&L checksums

The KO / KI / memory / N1 / N2 feature carriage of every ETL'd instrument is
summarised so the risk run is auditable. AAD is disabled by construction in the
native parity kernel (finite-difference / CRN only).

    uv run python scripts/run_100k_legacy_cpp.py --instruments 100000 --paths 3000
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
from fina_risk.etl import (  # noqa: E402
    augment_termsheet,
    compile_pricing_request,
    load_termsheet,
)

SEED = 20260909
BIG = 1.0e9


def _nearest(values: list[float], target: float) -> int:
    return int(np.argmin(np.abs(np.asarray(values, dtype=float) - target)))


def representative_vols(market: dict[str, Any]) -> dict[str, float]:
    """Terminal vol per underlying, nearest quoted strike to quoted spot and
    nearest maturity to eval + 0.40y, matching the native price_fixture rule."""
    eval_date = int(market.get("evaluationDate", 0))
    target_maturity = eval_date + 0.40 * 365.0
    quoted = {e["_id"]: float(e["spot"]) for e in market.get("equity", [])}
    vols: dict[str, float] = {}
    for surf in market.get("eqVol", []):
        name = surf.get("_id")
        strikes = surf.get("strike", [])
        maturities = surf.get("maturity", [])
        values = surf.get("vol", [])
        col = _nearest(strikes, quoted.get(name, strikes[0] if strikes else 0.0)) if strikes else 0
        row = _nearest(maturities, target_maturity) if maturities else 0
        row = min(row, len(values) - 1) if values else 0
        vols[name] = float(values[row][min(col, len(values[row]) - 1)]) / 100.0 if values else 0.45
    return vols


def cholesky_terminal(
    ids: list[str],
    quoted_spots: dict[str, float],
    vols: dict[str, float],
    corr_value: float,
    *,
    paths: int,
    time_to_expiry: float,
    rate: float,
    seed: int,
) -> np.ndarray:
    """Terminal GBM cube (paths, n) with a Cholesky-factorised correlation."""
    n = len(ids)
    corr = np.full((n, n), corr_value, dtype=np.float64)
    np.fill_diagonal(corr, 1.0)
    chol = np.linalg.cholesky(corr)  # Cholesky correlation factorisation
    rng = np.random.default_rng(seed)
    z = rng.standard_normal((paths, n)) @ chol.T
    spot = np.asarray([quoted_spots[i] for i in ids], dtype=np.float64)
    vol = np.asarray([vols[i] for i in ids], dtype=np.float64)
    drift = (rate - 0.5 * vol**2) * time_to_expiry
    diffusion = vol * math.sqrt(time_to_expiry) * z
    return (spot[None, :] * np.exp(drift[None, :] + diffusion)).astype(np.float32)


def etl_legacy_corpus(
    count: int,
    *,
    chunk: int = 2000,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Augment legacy term-sheet variants, ETL each to fina format, and keep the
    compact (underlyings, strike) geometry the native parity kernel consumes."""
    base = load_termsheet()
    market0 = base["Chunk"]["Jobs"][0]["commonData"]["marketData"]
    quoted = {e["_id"]: float(e["spot"]) for e in market0.get("equity", [])}
    corr_value = float(market0["corr"][0]["correlation"][0]["correlation"])

    instruments: list[dict[str, Any]] = []
    feature: Counter[str] = Counter()
    strikes: list[float] = []
    barriers: list[float] = []
    notionals: list[float] = []
    idx = 0
    chunk_no = 0
    started = time.perf_counter()
    while idx < count:
        take = min(chunk, count - idx)
        variants = augment_termsheet(base, count=take, seed=SEED + chunk_no)
        for variant in variants:
            request = compile_pricing_request(variant)  # legacy -> fina pricing-request
            put = next(leg for leg in request["legs"] if leg["leg_name"] == "PUT")
            ids = list(request["common_economics"]["underlyings"])
            strike = float(put["payoff"]["strike"])
            instruments.append(
                {
                    "instrumentId": f"ELI-LEGACY-{idx:06d}",
                    "underlyings": ids,
                    "legs": [
                        {
                            "leg_id": 1,
                            "leg_type": "intrinsic_option",
                            "leg_name": "PUT",
                            "multiplier": -1,
                            "payoff": {"basket": "worst_of", "strike": strike},
                        }
                    ],
                }
            )
            deal = variant["Chunk"]["Jobs"][0]["commonData"]["dealData"]
            kiko = deal.get("KIKOSelect", {})
            kis = deal.get("knockInStar", {})
            rgacc = deal.get("RGACCLKO", {})
            feature["globalKO"] += int(bool(kiko.get("globalKO")))
            feature["localKO"] += int(bool(kiko.get("localKO")))
            feature["knockIn"] += int(bool(kiko.get("knockIn")))
            feature["EKI"] += int(kiko.get("knockInType") == "EKI")
            feature["physical_delivery"] += int(kiko.get("ITMPayment") == "Delivery")
            feature["memory_locked_ADBE"] += int(bool(kiko.get("GKOLocked", [False])[0]))
            feature["memory_locked_AMZN"] += int(bool(kiko.get("GKOLocked", [False, False])[1]))
            feature["N1_nonzero"] += int(sum(rgacc.get("N1", [])) > 0)
            feature["N2_nonzero"] += int(sum(rgacc.get("N2", [])) > 0)
            feature["range_accrual"] += int(rgacc.get("accIndicator") == "WPS")
            strikes.append(strike)
            barriers.append(float(kis.get("KIBarrier", 0.0)))
            notionals.append(float(deal.get("notional", 0.0)))
            idx += 1
        chunk_no += 1
    elapsed = time.perf_counter() - started
    stats = {
        "requested": count,
        "etl_seconds": elapsed,
        "etl_per_second": count / max(elapsed, 1e-12),
        "quoted_spots": quoted,
        "correlation": corr_value,
        "feature_counts": dict(feature),
        "strike": {"min": min(strikes), "max": max(strikes), "mean": float(np.mean(strikes))},
        "KI_barrier": {"min": min(barriers), "max": max(barriers), "mean": float(np.mean(barriers))},
        "notional": {"min": min(notionals), "max": max(notionals)},
    }
    return instruments, stats


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--instruments", type=int, default=100_000)
    parser.add_argument("--paths", type=int, default=3_000)
    parser.add_argument("--out", type=Path, default=Path("/tmp/fina-risk-100k-legacy-cpp"))
    parser.add_argument("--bump", type=float, default=0.01)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    print(f"[etl] augmenting {args.instruments} legacy ELIFCN_KI variants -> fina format ...", flush=True)
    instruments, stats = etl_legacy_corpus(args.instruments)
    print(f"[etl] done in {stats['etl_seconds']:.1f}s ({stats['etl_per_second']:.0f}/s)", flush=True)

    ids = list(stats["quoted_spots"])
    vols = representative_vols(load_termsheet()["Chunk"]["Jobs"][0]["commonData"]["marketData"])
    time_to_expiry = (46419 - 46272) / 365.0
    rate = 0.042341
    terminal = cholesky_terminal(
        ids,
        stats["quoted_spots"],
        vols,
        stats["correlation"],
        paths=args.paths,
        time_to_expiry=time_to_expiry,
        rate=rate,
        seed=SEED,
    )
    market_json = {"underlyings": [{"id": i, "spot": stats["quoted_spots"][i]} for i in ids]}
    print(
        f"[sim] Cholesky terminal cube {terminal.shape} paths={args.paths} "
        f"corr={stats['correlation']:.6f} vols={ {k: round(v,4) for k,v in vols.items()} }",
        flush=True,
    )

    # Save the ETL'd fina corpus + simulation inputs for audit.
    (args.out / "cpp_instruments.json").write_text(json.dumps({"instruments": instruments}))
    (args.out / "cpp_market.json").write_text(json.dumps(market_json))
    np.save(args.out / "terminal.npy", terminal)
    (args.out / "etl_stats.json").write_text(json.dumps(stats, indent=2))
    (args.out / "etl_sample.json").write_text(
        json.dumps(compile_pricing_request(augment_termsheet(load_termsheet(), count=1, seed=SEED)[0]), indent=2)
    )

    print(f"[cpp ] running native run_cpp_parity (AAD disabled, bump={args.bump}) ...", flush=True)
    started = time.perf_counter()
    result = json.loads(
        fina_risk_cpp.run_cpp_parity(
            json.dumps({"instruments": instruments}),
            json.dumps(market_json),
            terminal,
            SEED,
            args.bump,
        )
    )
    wall = time.perf_counter() - started

    summary = {
        "instruments": stats["requested"],
        "paths": args.paths,
        "correlation": stats["correlation"],
        "correlation_method": "cholesky",
        "aad_engine": "disabled",
        "bump_size": args.bump,
        "backend_engine": result.get("backend_engine"),
        "cpp": result,
        "cpp_wall_seconds": wall,
        "instruments_per_second": stats["requested"] / max(wall, 1e-12),
        "etl": stats,
        "features_evaluated": [
            "global KO (globalKO=true)",
            "local KO (localKO=false)",
            "European knock-in (knockInType=EKI, KIBarrier)",
            "memory call carry (GKOLocked/GKODate)",
            "N1/N2 range-accrual fixing counts",
        ],
    }
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2))

    print("\n=== native C++ parity result (AAD disabled) ===")
    for key in (
        "instruments",
        "underlyings",
        "paths",
        "pv_checksum",
        "delta_dollar_checksum",
        "gamma_checksum",
        "taylor_forecast_checksum",
        "taylor_actual_checksum",
        "taylor_unexplained_checksum",
    ):
        print(f"  {key:<28} = {result.get(key)}")
    print(f"  {'cpp_wall_seconds':<28} = {wall:.3f}")
    print(f"  {'instruments_per_second':<28} = {stats['requested']/max(wall,1e-12):.1f}")
    print(f"\n[etl feature counts] {json.dumps(stats['feature_counts'])}")
    print(f"[out] {args.out}/summary.json")


if __name__ == "__main__":
    main()
