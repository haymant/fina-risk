from __future__ import annotations

import json
import time
from pathlib import Path

from fina_risk.benchmarking import run_benchmark
from fina_risk.olap import query_ssrm, write_risk_store


def main() -> None:
    execution = {
        "unique_structure_count": 1000,
        "structure_cache": True,
        "shared_market_data": True,
        "shared_path_cube": True,
        "correlation_factorization": True,
        "curve_bootstrap": True,
        "vol_surface_interpolation": True,
        "dividend_projection": True,
        "fx_conversion": True,
        "ek_monitoring": True,
        "range_accrual_state": True,
        "memory_call_state": True,
        "payment_date_discounting": True,
        "physical_delivery": True,
        "price_put_leg": True,
        "price_funding_leg": True,
        "price_coupon_leg": True,
        "aad_tape_scope": "per_structure",
        "taylor_pnl": "second_order",
        "portfolio_aggregation": True,
        "serialize_outputs": True,
    }
    root = Path("/tmp/fina-risk-100k-olap")
    root.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    result = run_benchmark(
        instruments=100_000,
        underlyings=1_200,
        paths=128,
        factors=12,
        sensitivities="spot",
        pnl="taylor2",
        seed=20260909,
        execution=execution,
        greeks=["delta", "gamma", "vega", "bucket_vega", "irpv01", "fx_delta", "skew_delta", "cross_vega"],
        emit_rows=True,
    )
    benchmark = result["benchmark"]
    rows = benchmark.pop("risk_rows") or []
    calc_seconds = time.perf_counter() - started
    store_started = time.perf_counter()
    store = write_risk_store([{"wide": rows, "long": []}], root=root, metadata={"benchmark": benchmark, "paths": 128})
    store_seconds = time.perf_counter() - store_started
    portfolio_query = query_ssrm(
        {
            "dataset": "risk_wide",
            "startRow": 0,
            "endRow": 20,
            "rowGroupCols": [{"field": "portfolio_id"}],
            "valueCols": [
                {"field": "base_pv", "aggFunc": "sum"},
                {"field": "total_taylor_pnl", "aggFunc": "sum"},
                {"field": "delta_pnl", "aggFunc": "sum"},
                {"field": "vega_pnl", "aggFunc": "sum"},
            ],
        },
        root=root,
    )
    factor_query = query_ssrm(
        {
            "dataset": "risk_wide",
            "startRow": 0,
            "endRow": 10,
            "rowGroupCols": [{"field": "underlying_id"}],
            "valueCols": [{"field": "total_taylor_pnl", "aggFunc": "sum"}],
            "sortModel": [{"colId": "total_taylor_pnl", "sort": "desc"}],
        },
        root=root,
    )
    report = {
        "benchmark": benchmark,
        "calculation_seconds": calc_seconds,
        "storage_seconds": store_seconds,
        "total_seconds": time.perf_counter() - started,
        "persisted_rows": len(rows),
        "store": store,
        "olap": {
            "portfolio_rows": portfolio_query["rows"],
            "portfolio_last_row": portfolio_query["lastRow"],
            "factor_rows": factor_query["rows"],
            "factor_last_row": factor_query["lastRow"],
        },
    }
    Path("/tmp/fina-risk-100k-benchmark.json").write_text(json.dumps(report, indent=2, default=str))
    print(
        json.dumps(
            {
                "calculation_seconds": calc_seconds,
                "storage_seconds": store_seconds,
                "total_seconds": report["total_seconds"],
                "persisted_rows": len(rows),
                "portfolio": portfolio_query["rows"],
                "factor_sample": factor_query["rows"][:3],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
