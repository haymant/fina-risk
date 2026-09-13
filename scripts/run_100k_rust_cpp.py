#!/usr/bin/env python3
"""100k legacy ELIFCN_KI -> fina native -> C++ PV/sensi/Taylor pipeline.

Stages (glued in Python because the Rust and C++ cores ship as thin Python
extensions):

  1. generate   100k term-sheet-style JSON records (``Chunk``-free job/dEAL shape,
                i.e. the termsheet1.md.json economics: PUT/FUNDING/COUPON with
                global KO, EKI knock-in, memory-carry GKOLocked, N1/N2 accrual).
  2. ETL        convert them to the fina-native columnar schema with the Rust
                sonic-rs engine (``sonicetl.run_pipelines``), streaming, no DOM.
  3. price      feed the fina-native corpus to the native C++ kernel
                (``fina_risk_cpp.run_cpp_parity``) with AAD disabled, a 3k-path
                Cholesky-correlated terminal cube, and a 1% CRN bump ->
                PV / delta / gamma / Taylor-2 P&L.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow.parquet as pq

import fina_risk_cpp

SEED = 20260909
SYMBOLS = ("ADBE UW", "AMZN UW", "MSFT UW", "NVDA UW")
SPOTS = {"ADBE UW": 267.885, "AMZN UW": 258.355, "MSFT UW": 512.30, "NVDA UW": 118.75}
NOTIONAL = (100_000.0, 250_000.0, 500_000.0, 1_000_000.0)

# Rust sonic-rs ETL: legacy dealData columns -> fina-native columns, plus a
# per-underlying unwind. No RNG (see BENCHMARK.md); it extracts/transforms.
ETL_YAML = """\
pipelines:
  - name: fina-native
    sources:
      - {name: inst, uri: file://instruments.json, format: json}
      - {name: mkt,  uri: file://market.json, format: json}
    datasets:
      - name: spot
        type: raw
        source: mkt
        to: {uri: file://store, format: parquet}
        fields:
          - {name: symbol, expression: "$._id"}
          - {name: spot, expression: "cast($.spot as double)"}
      - name: instrument_master
        type: master
        source: inst
        to: {uri: file://store, format: parquet}
        fields:
          - {name: instrument_id, expression: "$.commonData.dealData.instrumentName"}
          - {name: notional,      expression: "cast($.commonData.dealData.notional as double)"}
          - {name: strike,        expression: "cast($.commonData.dealData.strike as double)"}
          - {name: ki_barrier,    expression: "cast($.commonData.dealData.knockInStar.KIBarrier as double)"}
          - {name: call_barrier,  expression: "cast($.commonData.dealData.RGACCLKO.gblBarPrice as double)"}
          - {name: global_ko,     expression: "cast($.commonData.dealData.KIKOSelect.globalKO as boolean)"}
          - {name: local_ko,      expression: "cast($.commonData.dealData.KIKOSelect.localKO as boolean)"}
          - {name: knockin_type,  expression: "$.commonData.dealData.KIKOSelect.knockInType"}
          - {name: itm_payment,   expression: "$.commonData.dealData.KIKOSelect.ITMPayment"}
          - {name: memory_locked, expression: "cast($.commonData.dealData.KIKOSelect.GKOLocked[0] as boolean)"}
          - {name: n1,            expression: "cast($.commonData.dealData.RGACCLKO.N1[0] as integer)"}
          - {name: n2,            expression: "cast($.commonData.dealData.RGACCLKO.N2[0] as integer)"}
          - {name: coupon_rate,   expression: "cast($.commonData.dealData.RGACCLKO.accruRate[1] as double)"}
          - {name: expiry,        expression: "cast($.commonData.dealData.expiryDate as integer)"}
          - {name: currency,      expression: "$.commonData.dealData.paymentCurrency"}
      - name: underlying_unwound
        type: unwound
        source: inst
        to: {uri: file://store, format: parquet}
        unwind_rules:
          - name: u
            condition: "$.commonData.dealData.KIKOSelect.underlying[0]"
            unwind_path: "$.commonData.dealData.KIKOSelect.underlying"
            output_alias: u
        fields:
          - {name: instrument_id, expression: "$.commonData.dealData.instrumentName"}
          - {name: symbol,        expression: "$.u"}
"""


def generate_legacy(count: int, out: Path) -> float:
    """Stream ``count`` term-sheet-style records as a JSON array (fast)."""
    rng = np.random.default_rng(SEED)
    strike = np.round(0.78 * rng.uniform(0.85, 1.05, count), 6)
    barrier = np.round(strike * rng.uniform(0.80, 0.95, count), 6)
    call = np.round(rng.uniform(1.02, 1.20, count), 6)
    notional = rng.choice(NOTIONAL, count)
    coupon = np.round(rng.uniform(0.006, 0.018, count), 8)
    shift = rng.integers(-10, 11, count)
    global_ko = rng.random(count) < 0.66
    local_ko = rng.random(count) < 0.25
    memo0 = rng.random(count) < 0.40
    n1 = rng.integers(0, 8, count)
    n2 = n1 + rng.integers(0, 8, count)
    picks = rng.integers(0, len(SYMBOLS), (count, 2))  # 2-name baskets

    started = time.perf_counter()
    with (out / "instruments.json").open("w") as fh:
        fh.write("[")
        for i in range(count):
            a, b = SYMBOLS[picks[i, 0]], SYMBOLS[picks[i, 1]]
            if a == b:
                b = SYMBOLS[(picks[i, 1] + 1) % len(SYMBOLS)]
            expiry = 46419 + int(shift[i])
            rec = {
                "finaRefJobID": i,
                "commonData": {
                    "marketData": {"evaluationDate": 46272},
                    "dealData": {
                        "instrumentName": f"ELIFCN_KI-{i:06d}",
                        "notional": float(notional[i]),
                        "strike": float(strike[i]),
                        "paymentCurrency": "USD",
                        "legName": "PUT",
                        "expiryDate": expiry,
                        "maturityDate": expiry + 2,
                        "instrument": {"type": "ELIFCN"},
                        "KIKOSelect": {
                            "knockIn": True,
                            "knockInType": "EKI",
                            "ITMPayment": "Delivery",
                            "globalKO": bool(global_ko[i]),
                            "localKO": bool(local_ko[i]),
                            "underlying": [a, b],
                            "referencePrice": [SPOTS[a], SPOTS[b]],
                            "GKOLocked": [bool(memo0[i]), False],
                            "GKODate": [46174 if memo0[i] else 0, 0],
                        },
                        "knockInStar": {
                            "KIBarrier": float(barrier[i]),
                            "strikeKI2": float(strike[i]),
                            "maturBarrier": float(strike[i]),
                        },
                        "RGACCLKO": {
                            "accIndicator": "WPS",
                            "gblBarPrice": float(call[i]),
                            "accruRate": [0.0, float(coupon[i])],
                            "endDate": [46174, expiry],
                            "paymentDate": [46176, expiry + 2],
                            "N1": [int(n1[i]), int(n1[i])],
                            "N2": [int(n2[i]), int(n2[i])],
                        },
                    },
                },
            }
            fh.write(json.dumps(rec, separators=(",", ":")))
            fh.write("," if i + 1 < count else "]")
    (out / "market.json").write_text(json.dumps([{"_id": s, "spot": SPOTS[s]} for s in SYMBOLS]))
    return time.perf_counter() - started


def cholesky_terminal(
    ids: list[str], paths: int, corr_value: float, time_to_expiry: float, rate: float
) -> np.ndarray:
    n = len(ids)
    corr = np.full((n, n), corr_value)
    np.fill_diagonal(corr, 1.0)
    chol = np.linalg.cholesky(corr)
    rng = np.random.default_rng(SEED)
    z = rng.standard_normal((paths, n)) @ chol.T
    spot = np.asarray([SPOTS[i] for i in ids])
    vol = np.asarray([0.4495, 0.3562, 0.40, 0.55])  # per-symbol terminal vol
    drift = (rate - 0.5 * vol**2) * time_to_expiry
    return (spot[None, :] * np.exp(drift[None, :] + vol * math.sqrt(time_to_expiry) * z)).astype(np.float32)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--instruments", type=int, default=100_000)
    ap.add_argument("--paths", type=int, default=3_000)
    ap.add_argument("--out", type=Path, default=Path("/tmp/fina-risk-rust-cpp"))
    ap.add_argument("--bump", type=float, default=0.01)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "store").mkdir(parents=True, exist_ok=True)

    print(f"[gen] streaming {args.instruments} legacy ELIFCN_KI records ...", flush=True)
    gen_s = generate_legacy(args.instruments, args.out)
    print(f"[gen] {gen_s:.1f}s -> {args.out/'instruments.json'} "
          f"({(args.out/'instruments.json').stat().st_size/1e6:.1f} MB)", flush=True)

    # Stage 2: Rust sonic-rs ETL (legacy -> fina native columnar).
    import os

    import sonicetl

    cfg = args.out / "etl.yml"
    cfg.write_text(ETL_YAML)
    cwd = Path.cwd()
    os.chdir(args.out)
    try:
        t = time.perf_counter()
        result = sonicetl.run_pipelines(str(cfg))
        etl_s = time.perf_counter() - t
    finally:
        os.chdir(cwd)
    print(f"[etl] Rust sonic-rs run_pipelines: {etl_s:.2f}s rows={result.rows} "
          f"timing={result.timing}", flush=True)

    # Stage 3: rebuild the nested fina-native corpus from the ETL parquet output.
    master = pq.read_table(args.out / "store" / "instrument_master.parquet").to_pylist()
    und = pq.read_table(args.out / "store" / "underlying_unwound.parquet").to_pylist()
    by_inst: dict[str, list[str]] = {}
    for row in und:
        by_inst.setdefault(row["instrument_id"], []).append(row["symbol"])
    instruments = [
        {
            "instrumentId": m["instrument_id"],
            "underlyings": by_inst.get(m["instrument_id"], []),
            "legs": [{"leg_id": 1, "leg_type": "intrinsic_option", "leg_name": "PUT",
                      "multiplier": -1, "payoff": {"basket": "worst_of", "strike": float(m["strike"])}}],
        }
        for m in master
    ]
    def _b(v: Any) -> bool:
        return v is True or (isinstance(v, str) and v.lower() == "true")

    feats = {
        "global_ko": sum(_b(m["global_ko"]) for m in master),
        "local_ko": sum(_b(m["local_ko"]) for m in master),
        "memory_locked": sum(_b(m["memory_locked"]) for m in master),
        "N1_gt_0": sum(int(m["n1"]) > 0 for m in master),
        "N2_gt_N1": sum(int(m["n2"]) > int(m["n1"]) for m in master),
        "EKI": sum(m["knockin_type"] == "EKI" for m in master),
        "physical_delivery": sum(m["itm_payment"] == "Delivery" for m in master),
        "mean_coupon_rate": float(np.mean([m["coupon_rate"] for m in master])),
        "mean_ki_barrier": float(np.mean([m["ki_barrier"] for m in master])),
    }

    ids = list(SYMBOLS)
    terminal = cholesky_terminal(ids, args.paths, 0.4593248180905, (46419 - 46272) / 365.0, 0.042341)
    market_json = {"underlyings": [{"id": i, "spot": SPOTS[i]} for i in ids]}
    (args.out / "cpp_instruments.json").write_text(json.dumps({"instruments": instruments}))
    (args.out / "cpp_market.json").write_text(json.dumps(market_json))

    print(f"[cpp] native run_cpp_parity (AAD disabled, paths={args.paths}, "
          f"Cholesky corr=0.459325) ...", flush=True)
    t = time.perf_counter()
    cpp = json.loads(
        fina_risk_cpp.run_cpp_parity(
            json.dumps({"instruments": instruments}), json.dumps(market_json), terminal, SEED, args.bump
        )
    )
    cpp_s = time.perf_counter() - t

    summary = {
        "instruments": args.instruments,
        "paths": args.paths,
        "aad_engine": "disabled",
        "correlation": {"method": "cholesky", "rho": 0.4593248180905, "n": len(ids)},
        "gen_seconds": gen_s,
        "rust_etl_seconds": etl_s,
        "rust_etl_rows": result.rows,
        "cpp_seconds": cpp_s,
        "cpp": cpp,
        "features": feats,
    }
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2))
    print(f"[cpp] {cpp_s:.3f}s")
    for k in ("instruments", "underlyings", "paths", "pv_checksum", "delta_dollar_checksum",
              "gamma_checksum", "taylor_forecast_checksum", "taylor_actual_checksum",
              "taylor_unexplained_checksum"):
        print(f"  {k:<28} = {cpp.get(k)}")
    print(f"\n[features] {json.dumps(feats)}")
    print(f"[out] {args.out}/summary.json  (rust etl {etl_s:.2f}s, cpp {cpp_s:.2f}s)")


if __name__ == "__main__":
    main()
