#!/usr/bin/env python3
"""Scale benchmark of the fina-risk shared-path engine via the scheduler adapter.

Runs ``fina-risk.benchmark_risk`` (``scheduler_adapter.run_risk_task(mode="benchmark")``)
across instrument counts at a fixed simulation-path count and reports a timing
table. Same kernel + per-instrument risk scope as the production ``risk_batch``
scheduler task, without store persistence.

Default points: 2k / 15k / 40k / 100k instruments at ``--paths 1000``.

    uv --directory fina-risk run python scripts/benchmark_scale.py --paths 1000 --out benchmark/scale_results.json
"""

from __future__ import annotations

import argparse
import csv
import json
import time
from pathlib import Path
from typing import Any

from fina_risk.scheduler_adapter import run_risk_task

DEFAULT_POINTS = [2000, 15000, 40000, 100000]


def run_scale(
    instrument_counts: list[int],
    *,
    paths: int,
    seed: int,
    underlyings: int | None = None,
    factors: int | None = None,
    structures: list[int] | None = None,
    enable_aad: bool = True,
    backend: str = "python",
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for position, instruments in enumerate(instrument_counts):
        started = time.perf_counter()
        structures_val = None
        if structures is not None:
            structures_val = structures[min(position, len(structures) - 1)]
        underlyings_val = int(underlyings or max(4, instruments // 4))
        if backend == "cpp":
            from fina_risk.cpp_parity import run_cpp_parity_benchmark

            cpp_execution = {"unique_structure_count": structures_val} if structures_val is not None else None
            result = run_cpp_parity_benchmark(
                instruments=instruments,
                underlyings=min(underlyings_val, 1200),
                paths=paths,
                factors=factors or 12,
                seed=seed,
                execution=cpp_execution,
            )
            b = result.get("benchmark", {})
        else:
            execution: dict[str, Any] | None = None
            if structures_val is not None or not enable_aad:
                execution = {}
                if structures_val is not None:
                    execution["unique_structure_count"] = structures_val
                if not enable_aad:
                    execution["hybrid_aad"] = False
            result = run_risk_task(
                {
                    "mode": "benchmark",
                    "instruments": instruments,
                    "paths": paths,
                    "seed": seed,
                    "underlyings": underlyings_val,
                    "factors": factors or 12,
                    "execution": execution,
                }
            )
            b = result.get("benchmark", {})
        wall = time.perf_counter() - started
        aad = True
        if b.get("aad_engine") == "disabled":
            aad = False
        elif "hybrid_aad" in b:
            aad = bool(b["hybrid_aad"].get("enabled", True))
        row = {
            "backend": backend,
            "engine": b.get("backend_engine"),
            "instruments": b.get("instruments") or b.get("requested_instruments"),
            "unique_payoff_baskets": b.get("unique_payoff_baskets"),
            "underlyings": b.get("underlyings"),
            "paths": b.get("paths"),
            "factors": b.get("factors") or b.get("factor_count"),
            "structure_reuse": b.get("structure_reuse"),
            "aad_enabled": aad,
            "elapsed_seconds": round(float(b.get("elapsed_seconds", 0.0)), 3),
            "instruments_per_second": round(float(b.get("instruments_per_second", 0.0)), 1),
            "pv_checksum": round(float(b.get("pv_checksum", 0.0)), 6),
            "sensitivity_checksum": round(
                float(b.get("sensitivity_checksum") or b.get("delta_checksum") or 0.0), 6
            ),
            "kernel_exclusive_seconds": b.get("kernel_exclusive_seconds"),
            "aad_engine": b.get("aad_engine"),
            "method": b.get("method"),
            "greek_scope": b.get("greek_scope"),
            "scheduling_overhead_seconds": round(wall - float(b.get("elapsed_seconds", 0.0)), 4),
        }
        rows.append(row)
        struct = row["unique_payoff_baskets"]
        print(
            f"{instruments:>8,} instruments  {struct:>8,} structs  {paths:>6,} paths  "
            f"backend={backend:<6} aad={'on ' if aad else 'off'} "
            f"{row['elapsed_seconds']:>8.2f}s  {row['instruments_per_second']:>10,.1f} instr/s",
            flush=True,
        )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--points", type=int, nargs="*", default=DEFAULT_POINTS)
    parser.add_argument("--paths", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=20260909)
    parser.add_argument("--underlyings", type=int, default=None)
    parser.add_argument("--factors", type=int, default=None)
    parser.add_argument(
        "--structures",
        type=int,
        nargs="*",
        default=None,
        help="Unique payoff structures per point (broadcast: one value applies to all points). "
        "Set equal to instruments to disable structure reuse.",
    )
    parser.add_argument(
        "--no-aad",
        action="store_true",
        help="Disable the hybrid-AAD / market-AAD lanes: everything runs CRN bump-revalue.",
    )
    parser.add_argument(
        "--backend",
        choices=["python", "cpp"],
        default="python",
        help="`python` uses the run_risk_task benchmark lane; `cpp` uses the C++ parity lane "
        "(finite-difference/pathwise, AAD is always disabled there).",
    )
    parser.add_argument("--out", default="benchmark/scale_results.json")
    args = parser.parse_args()

    points = [int(x) for x in args.points]
    rows = run_scale(
        points,
        paths=args.paths,
        seed=args.seed,
        underlyings=args.underlyings,
        factors=args.factors,
        structures=args.structures,
        enable_aad=not args.no_aad,
        backend=args.backend,
    )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    values = {
        "command": " ".join(["benchmark_scale.py", *[str(x) for x in args.points]]),
        "backend": "python",
        "engine": "numpy_price_terminal_legs_v1",
        "paths": args.paths,
        "seed": args.seed,
        "points": rows,
        "checkpoints": {str(x["instruments"]): x for x in rows},
    }
    out.write_text(json.dumps(values, indent=2))
    csv_path = out.with_suffix(".csv")
    with csv_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else ["instruments"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {out} and {csv_path}")


if __name__ == "__main__":
    main()