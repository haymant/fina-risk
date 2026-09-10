from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

import duckdb
import pyarrow.csv as csv
import pyarrow.parquet as parquet


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("instruments")
    parser.add_argument("market")
    parser.add_argument("output")
    parser.add_argument("--paths", type=int, default=1000)
    parser.add_argument("--binary", default="cpp/build/fina-risk-cpp-e2e")
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    native = subprocess.run(
        [args.binary, args.instruments, args.market, str(output), str(args.paths)],
        check=True,
        capture_output=True,
        text=True,
    )
    report = json.loads(native.stdout)
    conversion_started = time.perf_counter()
    for dataset in ("risk_wide", "risk_long"):
        table = csv.read_csv(output / f"{dataset}.csv")
        parquet.write_table(table, output / f"{dataset}.parquet", compression="zstd")
    con = duckdb.connect()
    query_started = time.perf_counter()
    summary = con.execute(
        f"WITH positions AS (SELECT instrument_id, MAX(base_pv) AS base_pv "
        f"FROM read_parquet('{output / 'risk_wide.parquet'}') GROUP BY instrument_id) "
        f"SELECT (SELECT COUNT(*) FROM read_parquet('{output / 'risk_wide.parquet'}')) AS rows, "
        f"SUM(base_pv) AS pv_sum, (SELECT SUM(delta_dollar) FROM read_parquet('{output / 'risk_wide.parquet'}')) AS dollar_delta_sum "
        f"FROM positions"
    ).fetchone()
    audit = con.execute(
        f"SELECT method, COUNT(*) AS rows FROM read_parquet('{output / 'risk_long.parquet'}') GROUP BY method ORDER BY method"
    ).fetchall()
    query_seconds = time.perf_counter() - query_started
    report["storage"].update(
        {
            "wide": str(output / "risk_wide.parquet"),
            "long": str(output / "risk_long.parquet"),
            "olap_query": "DuckDB read_parquet SSRM-compatible adapter",
            "parquet_conversion_seconds": query_started - conversion_started,
            "olap_query_seconds": query_seconds,
            "olap_summary": {"rows": summary[0], "pv_sum": summary[1], "dollar_delta_sum": summary[2]},
            "method_audit": [{"method": row[0], "rows": row[1]} for row in audit],
        }
    )
    report["timings"]["python_arrow_duckdb_seconds"] = time.perf_counter() - conversion_started
    report["timings"]["end_to_end_seconds"] = time.perf_counter() - started
    (output / "e2e-report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
