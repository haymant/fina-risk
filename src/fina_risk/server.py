"""Functional fina-risk MCP orchestration server with a local vectorized CPU backend."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

from .benchmarking import run_benchmark
from .olap import link_view_state, query_ssrm, resolve_s3_uri, storage_status, write_risk_store
from .pipeline import (
    dashboard_metadata,
    ingest_instruments,
    ingest_market_data,
    pipeline_state,
    plan_pnl_forecast,
    risk_metadata,
    trigger_pnl_forecast,
)
from .pricing import bump_result, common_from_job, load_legacy_request, price_fixture
from .risk_view import aggregate_risk_views

TOOLS = {
    "market": [
        ("load_market", "Normalize a legacy market snapshot."),
        ("store_market", "Store a market snapshot."),
        ("diff_market", "Compare market snapshots."),
        ("freeze_market", "Freeze a market snapshot."),
        ("validate_market", "Validate market inputs."),
    ],
    "universe": [
        ("discover_universe", "Build a trade universe."),
        ("merge_universe", "Merge trade universes."),
        ("split_universe", "Split a universe into batches."),
        ("estimate_cost", "Estimate vectorized pricing cost."),
    ],
    "compiler": [
        ("compile_trade", "Compile a legacy trade into explicit legs and features."),
        ("compile_portfolio", "Compile a portfolio into a shared simulation universe."),
    ],
    "quantlib": [
        ("build_quantlib_market", "Build a QuantLib-compatible market."),
        ("build_processes", "Build correlated stochastic processes."),
        ("build_schedules", "Build fixing and payment schedules."),
        ("build_payoffs", "Build payoff definitions."),
    ],
    "simulation": [
        ("build_time_grid", "Build a calendar-aware time grid."),
        ("build_path_cube", "Build a shared common-random-number path cube."),
        ("reuse_path_cube", "Reuse a path cube."),
        ("inspect_path_cube", "Inspect path cube metadata."),
    ],
    "gpu": [
        ("gpu_status", "Inspect GPU availability."),
        ("gpu_allocate", "Allocate a simulation batch."),
        ("gpu_release", "Release a simulation batch."),
        ("tune_batch_size", "Tune vectorized batch size."),
        ("kernel_profile", "Profile a pricing kernel."),
    ],
    "state": [
        ("build_state_cube", "Build lifecycle and memory state."),
        ("update_state_cube", "Apply a lifecycle event."),
        ("inspect_state_cube", "Inspect state metadata."),
    ],
    "payoff": [
        ("compile_payoff_graph", "Compile explicit payoff graph nodes."),
        ("inspect_payoff_graph", "Inspect payoff graph."),
        ("evaluate_payoff_graph", "Evaluate payoff graph."),
    ],
    "aad": [
        ("build_aad_graph", "Build an AAD valuation graph."),
        ("check_aad_eligibility", "Check AAD eligibility."),
        ("run_adjoint", "Run adjoint or honest fallback."),
        ("inspect_gradient", "Inspect gradients."),
    ],
    "risk": [
        ("generate_risk_cube", "Generate a risk cube."),
        ("aggregate_risk", "Aggregate risk cells."),
        ("generate_greeks", "Generate Greeks."),
        ("compare_methods", "Compare risk methods."),
    ],
    "pnl": [
        ("forecast_pnl", "Forecast P&L."),
        ("explain_pnl", "Explain realized P&L."),
        ("taylor_decomposition", "Decompose P&L."),
    ],
    "portfolio": [
        ("aggregate_portfolio", "Aggregate portfolio PV and sensitivities."),
        ("net_sensitivities", "Net sensitivities by risk factor."),
        ("portfolio_scenarios", "Run portfolio scenarios."),
    ],
    "olap": [
        ("write_risk_store", "Persist normalized risk-factor components to Parquet."),
        ("olap_query", "Query normalized Parquet through DuckDB using AG Grid SSRM semantics."),
        ("storage_status", "Inspect the DuckDB/Arrow/Parquet storage layer."),
        ("resolve_s3_dataset", "Resolve a versioned GCS/S3-compatible Parquet dataset URI."),
        ("link_olap_views", "Create global UI-driver state for linked OLAP views."),
    ],
    "pipeline": [
        ("ingest_instruments", "Normalize and append instrument records."),
        ("ingest_market_data", "Normalize and append market records."),
        ("read_risk_metadata", "Read risk-factor keys and decomposition storage metadata."),
        ("read_dashboard_metadata", "Read dashboard dimensions, measures, and shared-driver metadata."),
        ("plan_pnl_forecast", "Plan shared-resource execution and cost for a P&L forecast."),
        ("trigger_pnl_forecast", "Run PV and sensitivities, then persist normalized risk results."),
        ("pipeline_state", "Create or update pipeline state for stdio or Redis-backed HTTP use."),
    ],
    "scheduler": [
        ("submit_job", "Submit a local pricing job."),
        ("cancel_job", "Cancel a local job."),
        ("rebalance_job", "Rebalance a job."),
        ("inspect_job", "Inspect job status."),
    ],
}
ALL_TOOLS = [(n, d) for group in TOOLS.values() for n, d in group]


def _resolve_allowed_hosts() -> list[str]:
    default = (
        "localhost,127.0.0.1,[::1],localhost:*,127.0.0.1:*,[::1]:*,"
        "fina-risk.vercel.app,fina-risk.vercel.app:*,"
        "fina-risk-zmrl.vercel.app,fina-risk-zmrl.vercel.app:*"
    )
    return [x.strip() for x in os.getenv("ALLOWED_HOSTS", default).split(",") if x.strip()]


mcp = FastMCP(
    "fina-risk",
    stateless_http=True,
    transport_security=TransportSecuritySettings(allowed_hosts=_resolve_allowed_hosts()),
)


def _fixture_request(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("request"):
        return payload["request"]
    path = Path(__file__).resolve().parents[2] / "skills/fina-risk/refs/termsheet1.md.json"
    return json.loads(path.read_text())


def _execute(name: str, payload: dict[str, Any] | None) -> dict[str, Any]:
    payload = payload or {}
    jobs = load_legacy_request(_fixture_request(payload))
    common = common_from_job(jobs[0]) if jobs else {}
    paths, seed = payload.get("paths"), int(payload.get("seed", 1729))
    if name == "pricing_and_sensitivity":
        result = bump_result(common, paths=paths, seed=seed)
        if len(jobs) >= 3:
            coupon = price_fixture(common_from_job(jobs[2]), paths=paths, seed=seed)
            coupon_pv = next(x["pv"] for x in coupon["legs"] if x["leg_name"] == "COUPON")
            result["base"]["legs"][-1]["pv"] = coupon_pv
            result["base"]["valuation"]["pv"] = sum(x["pv"] for x in result["base"]["legs"])
            result["base"]["explainability"]["coupon"] = coupon["explainability"]["coupon"]
        return result
    if name == "benchmark_portfolio":
        return run_benchmark(
            instruments=int(payload.get("instruments", 2000)),
            underlyings=int(payload.get("underlyings", 1200)),
            paths=int(payload.get("paths", 30000)),
            factors=int(payload.get("factors", 12)),
            sensitivities=str(payload.get("sensitivities", "delta")),
            pnl=str(payload.get("pnl", "taylor1")),
            seed=int(payload.get("seed", 20260909)),
            execution=payload.get("execution"),
            greeks=payload.get("greeks"),
        )
    if name in {"generate_risk_cube", "generate_greeks", "run_adjoint", "forecast_pnl"}:
        result = _execute("pricing_and_sensitivity", payload)
        cells = [
            {
                "risk_factor_id": x["risk_factor_id"],
                "measure": x["measure"],
                "value": x["value"],
                "currency": "USD",
                "method": x["method"],
                "fallback_reason": "discontinuous EKI/physical delivery",
            }
            for x in result["sensitivities"]
        ]
        return {
            "cube_id": "riskcube-local",
            "valuation": result["base"]["valuation"],
            "cells": cells,
            "legs": result["base"]["legs"],
            "explainability": result["base"]["explainability"],
            "conventions": result["base"]["conventions"],
            "risk_representation": result["risk_representation"],
        }
    if name == "compile_trade":
        deal = common.get("dealData", {})
        return {
            "trade_id": deal.get("instrumentName", "legacy-trade"),
            "instrument": deal.get("instrument", {}),
            "legs": [
                {"leg_id": 1, "leg_type": "intrinsic_option", "leg_name": "PUT", "multiplier": -1},
                {"leg_id": 3, "leg_type": "funding", "leg_name": "FUNDING", "multiplier": 1},
                {"leg_id": 2, "leg_type": "coupon", "leg_name": "COUPON", "multiplier": 1},
            ],
            "features": {
                "ki_monitoring": "EKI",
                "physical_delivery": True,
                "memory_ko": deal.get("KIKOSelect", {}).get("GKOLocked", []),
            },
        }
    if name == "build_path_cube":
        r = price_fixture(common, paths=paths, seed=seed)
        return {
            "cube_id": r["artifacts"]["path_cube_id"],
            "universe_id": r["artifacts"]["simulation_universe_id"],
            "path_count": paths or common["marketData"].get("MCPara", {}).get("numPaths", 30000),
            "time_steps": r["explainability"]["steps"],
            "factor_count": 2,
            "simulation_model": "correlated_gbm",
            "storage_backend": "ram",
            "common_random_numbers": True,
        }
    if name in {"load_market", "store_market", "validate_market", "build_quantlib_market"}:
        md = common.get("marketData", {})
        return {
            "status": "ok",
            "market_version": "legacy-normalized",
            "underlyings": [x.get("_id") for x in md.get("equity", [])],
            "currency": "USD",
            "day_count": "Actual/365",
            "aad_backend": "quantlib_risks_xad",
            "vectorization": "numpy-batched",
        }
    if name in {"gpu_status", "gpu_allocate", "gpu_release", "kernel_profile"}:
        return {
            "status": "fallback_cpu",
            "backend": "numpy",
            "gpu_available": False,
            "note": "GPU adapter is lower priority; DTOs are batch-ready.",
        }
    if name in {"aggregate_portfolio", "net_sensitivities", "aggregate_risk"}:
        results = [bump_result(common_from_job(job), paths=paths, seed=seed) for job in jobs]
        aggregated = aggregate_risk_views(results)
        return {
            "status": "ok",
            "pv": sum(float(x["base"]["valuation"]["pv"]) for x in results),
            "trade_count": len(results),
            "risk_representation": aggregated,
            "wide": aggregated["wide"],
            "long": aggregated["long"],
        }
    if name == "write_risk_store":
        results = [bump_result(common_from_job(job), paths=paths, seed=seed) for job in jobs]
        return write_risk_store(
            [x["risk_representation"] for x in results],
            root=payload.get("root", "/tmp/fina-risk-olap"),
            metadata={"source": "pricing_and_sensitivity", "seed": seed, "paths": paths},
        )
    if name == "olap_query":
        return query_ssrm(payload.get("query", payload), root=payload.get("root", "/tmp/fina-risk-olap"))
    if name == "storage_status":
        return storage_status(root=payload.get("root", "/tmp/fina-risk-olap"))
    if name == "resolve_s3_dataset":
        return {"status": "ok", "uri": resolve_s3_uri(payload)}
    if name == "link_olap_views":
        return link_view_state(payload)
    if name == "ingest_instruments":
        return ingest_instruments(payload, root=payload.get("root", "/tmp/fina-risk-olap"))
    if name == "ingest_market_data":
        return ingest_market_data(payload, root=payload.get("root", "/tmp/fina-risk-olap"))
    if name == "read_risk_metadata":
        return risk_metadata()
    if name == "read_dashboard_metadata":
        return dashboard_metadata()
    if name == "plan_pnl_forecast":
        return plan_pnl_forecast(payload)
    if name == "trigger_pnl_forecast":
        return trigger_pnl_forecast(payload)
    if name == "pipeline_state":
        return pipeline_state(payload)
    return {
        "status": "ok",
        "tool": name,
        "artifact_model": "shared-market-path-state-payoff-risk",
        "note": "orchestration DTO ready",
    }


for tool_name, description in ALL_TOOLS + [
    ("pricing_and_sensitivity", "Price a legacy request and generate CRN sensitivities."),
    ("benchmark_portfolio", "Benchmark shared-path portfolio PV, sensitivities, and Taylor P&L."),
]:

    def _tool(context: dict[str, Any] | None = None, *, tool_name: str = tool_name) -> dict[str, Any]:
        return _execute(tool_name, context)

    _tool.__name__ = tool_name
    _tool.__doc__ = description
    mcp.tool(name=tool_name, description=description)(_tool)


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_request: Any) -> JSONResponse:
    return JSONResponse(
        {"status": "ok", "service": "fina-risk", "tools": len(ALL_TOOLS) + 2, "backend": "local-vectorized-cpu"}
    )


app = mcp.streamable_http_app()


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
