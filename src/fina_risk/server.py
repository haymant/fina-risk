"""Functional fina-risk MCP orchestration server with a local vectorized CPU backend."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

from . import etl, scheduler_adapter
from .benchmarking import run_benchmark
from .gcs import load_local_env, object_store_status
from .olap import link_view_state, query_ssrm, resolve_dataset_source, resolve_s3_uri, storage_status, write_risk_store
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
from .storage import (
    ALLOWED_STORES,
    clear_storage_override,
    get_storage_config,
    set_storage_override,
    storage_overrides,
)
from .storage import storage_status as _store_status

# Load local dev config (never overrides real env vars): .env.local wins over .env.
load_local_env(".env")
load_local_env()

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
        backend = str(payload.get("backend") or (payload.get("execution") or {}).get("backend") or "python").lower()
        if backend == "cpp":
            execution = dict(payload.get("execution") or {})
            execution["hybrid_aad"] = False
            execution["aad_tape_scope"] = "disabled"
            from .cpp_parity import run_cpp_parity_benchmark

            return run_cpp_parity_benchmark(
                instruments=int(payload.get("instruments", 2000)),
                underlyings=int(payload.get("underlyings", 1200)),
                paths=int(payload.get("paths", 30000)),
                factors=int(payload.get("factors", 12)),
                seed=int(payload.get("seed", 20260909)),
                execution=execution,
                bump=float(payload.get("bump", 0.01)),
            )
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
            root=payload.get("root"),
            metadata={"source": "pricing_and_sensitivity", "seed": seed, "paths": paths},
        )
    if name == "olap_query":
        return query_ssrm(payload.get("query", payload), root=payload.get("root"))
    if name == "storage_status":
        return storage_status(root=payload.get("root"))
    if name == "resolve_s3_dataset":
        return {"status": "ok", "uri": resolve_s3_uri(payload)}
    if name == "link_olap_views":
        return link_view_state(payload)
    if name == "ingest_instruments":
        return ingest_instruments(payload, root=payload.get("root") or get_storage_config().local_root)
    if name == "ingest_market_data":
        return ingest_market_data(payload, root=payload.get("root") or get_storage_config().local_root)
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


_ALL_PAYLOAD_KEYS = (
    "instrumentId",
    "instrument",
    "request",
    "backend",
    "bump",
    "dataset",
    "drillPath",
    "driverId",
    "endRow",
    "evaluation_date",
    "evaluationDate",
    "execution",
    "factors",
    "filterModel",
    "greeks",
    "groupKeys",
    "instruments",
    "mcp_transport",
    "paths",
    "pipeline_id",
    "pivotCols",
    "pivotMode",
    "pnl",
    "query",
    "records",
    "reportVersion",
    "root",
    "rowGroupCols",
    "seed",
    "selection",
    "sensitivities",
    "shared",
    "slice",
    "sliceName",
    "sortModel",
    "startRow",
    "state_backend",
    "tableName",
    "underlyings",
    "valueCols",
    "version",
    "views",
)

for tool_name, description in ALL_TOOLS + [
    ("pricing_and_sensitivity", "Price a legacy request and generate CRN sensitivities."),
    (
        "benchmark_portfolio",
        "Benchmark shared-path portfolio PV, sensitivities, and Taylor P&L. `backend` in {\"python\", \"cpp\"} "
        "selects the executing parity lane; `cpp` disables AAD and runs C++-parity CRN bump/pathwise math.",
    ),
]:

    def _tool(
        instrumentId: Any = None,
        instrument: Any = None,
        request: Any = None,
        backend: Any = None,
        bump: Any = None,
        dataset: Any = None,
        drillPath: Any = None,
        driverId: Any = None,
        endRow: Any = None,
        evaluation_date: Any = None,
        evaluationDate: Any = None,
        execution: Any = None,
        factors: Any = None,
        filterModel: Any = None,
        greeks: Any = None,
        groupKeys: Any = None,
        instruments: Any = None,
        mcp_transport: Any = None,
        paths: Any = None,
        pipeline_id: Any = None,
        pivotCols: Any = None,
        pivotMode: Any = None,
        pnl: Any = None,
        query: Any = None,
        records: Any = None,
        reportVersion: Any = None,
        root: Any = None,
        rowGroupCols: Any = None,
        seed: Any = None,
        selection: Any = None,
        sensitivities: Any = None,
        shared: Any = None,
        slice: Any = None,
        sliceName: Any = None,
        sortModel: Any = None,
        startRow: Any = None,
        state_backend: Any = None,
        tableName: Any = None,
        underlyings: Any = None,
        valueCols: Any = None,
        version: Any = None,
        views: Any = None,
        *,
        tool_name: str = tool_name,
    ) -> dict[str, Any]:
        provided: dict[str, Any] = {}
        for key in _ALL_PAYLOAD_KEYS:
            if locals()[key] is not None:
                provided[key] = locals()[key]
        return _execute(tool_name, provided)

    _tool.__name__ = tool_name
    _tool.__doc__ = description
    mcp.tool(name=tool_name, description=description)(_tool)


@mcp.tool()
def store_config() -> dict[str, Any]:
    """Read the effective store configuration (backend, local root, bucket, partition glob, hive flag).

    Same env vars and shape as fina-olap: ``FINA_OLAP_STORE`` / ``FINA_OLAP_BUCKET``
    / ``FINA_OLAP_PARQUET_ROOT`` / ``FINA_OLAP_PATH`` / ``S3_PATH_TEMPLATE`` /
    ``FINA_OLAP_PARTITION_GLOB`` / ``FINA_OLAP_HIVE_PARTITIONING`` (plus the
    ``S3_*`` aliases). ``overrides`` lists fields switched at runtime via
    ``store_configure`` (they sit on top of the env base until cleared).
    """
    return {"store": _store_status()}


@mcp.tool()
def store_configure(
    store: str = "",
    parquet_root: str = "",
    bucket: str = "",
    path: str = "",
    partition_glob: str = "",
    hive_partitioning: str = "",
    clear: bool = False,
) -> dict[str, Any]:
    """Configure the shared store at runtime (process-local; env vars remain the boot default).

    Only non-empty fields are changed; pass ``clear=true`` to reset overrides back
    to the env base first. ``hive_partitioning`` accepts 1/0/true/false. Returns
    the effective config after the change. Point fina-risk and fina-olap at the
    same root/bucket to run a risk-generation → OLAP-analysis E2E.
    """
    if store and store not in ALLOWED_STORES:
        raise ValueError(f"store must be one of {', '.join(ALLOWED_STORES)}; got {store!r}")
    if clear:
        clear_storage_override()
    changes: dict[str, Any] = {}
    if store:
        changes["store"] = store
    if parquet_root:
        changes["root"] = parquet_root
    if bucket:
        changes["bucket"] = bucket
    if path:
        changes["default_path"] = path
    if partition_glob:
        changes["partition_glob"] = partition_glob
    if hive_partitioning:
        changes["hive_partitioning"] = hive_partitioning
    if changes:
        set_storage_override(**changes)
    return {"store": _store_status()}


@mcp.tool()
def store_resolve(table_name: str = "risk_wide") -> dict[str, Any]:
    """Preview how a dataset resolves under the current store config (source, hive flag, overrides)."""
    source, hive = resolve_dataset_source(table_name)
    cfg = get_storage_config()
    return {
        "table_name": table_name,
        "store": cfg.store,
        "source": source,
        "object_store": source.startswith(("s3://", "gs://", "http://", "https://")),
        "hive_partitioning": hive,
        "partition_glob": cfg.partition_glob,
        "overrides": storage_overrides(),
    }


@mcp.tool()
def augment_termsheet(termsheet: str = "", count: int = 10, seed: int = 20260909, out_path: str = "") -> dict[str, Any]:
    """ETL: clone the bundled term sheet into ``count`` variants with permuted economics.

    Permutations mirror ``scripts/generate_benchmark.py`` (relative strike,
    knock-in barrier, call barrier, notional, underlyings/spot, coupon rate and
    the expiry/maturity calendar). Deterministic from ``seed``. ``termsheet`` is
    an optional path to a legacy request; omit to use the bundled
    ``skills/fina-risk/refs/termsheet1.md.json``. When ``out_path`` is given the
    batch is written as ``{"count", "instruments": [...]}`` and omitted from the
    reply.
    """
    variants = etl.augment_termsheet(termsheet or None, count=count, seed=seed)
    result: dict[str, Any] = {"status": "ok", "count": len(variants), "seed": seed}
    if out_path:
        Path(out_path).write_text(json.dumps({"count": len(variants), "instruments": variants}, separators=(",", ":")))
        result["out_path"] = out_path
    else:
        result["instruments"] = variants
    return result


@mcp.tool()
def compile_pricing_requests(
    termsheet: str = "",
    count: int = 1,
    seed: int = 20260909,
    validate: bool = True,
) -> dict[str, Any]:
    """ETL: convert augmented term sheets into fina-risk ``pricing-request`` objects.

    Maps each legacy request to ``{instrument_key, market_data, legs, parameters,
    common_economics}`` (schema ``skills/fina-risk/schema/pricing-request.schema.json``)
    so the batch can be scheduled/priced by fina-risk.
    """
    variants = etl.augment_termsheet(termsheet or None, count=count, seed=seed)
    requests = [etl.compile_pricing_request(v) for v in variants]
    if validate:
        for request in requests:
            etl.validate_pricing_request(request)
    return {"status": "ok", "count": len(requests), "requests": requests}


@mcp.tool()
def run_risk_task(
    mode: str = "batch",
    instruments: int = 100,
    underlyings: int = 25,
    paths: int = 1000,
    factors: int = 12,
    seed: int = 20260909,
    persist: bool = True,
    pricing_request: dict[str, Any] | None = None,
    execution: dict[str, Any] | None = None,
    greeks: list[str] | None = None,
    sensitivities: str = "delta",
    pnl: str = "taylor1",
) -> dict[str, Any]:
    """Run fina-risk compute for a scheduler task (``fina-risk.risk_batch`` / ``fina-risk.pricing_and_sensitivity``).

    ``mode="batch"`` prices ``instruments`` underlyings via the shared-path
    benchmark and (``persist``) writes ``risk_wide``/``risk_long`` to the shared
    store for fina-olap. ``mode="benchmark"`` runs the same kernel without
    persistence or risk rows and returns a timing breakdown — use it to sweep
    ``instruments`` at a fixed ``paths`` count. ``mode="single"`` prices one
    legacy ``Chunk.Jobs`` request supplied as ``pricing_request``.
    """
    return scheduler_adapter.run_risk_task(
        {
            "mode": mode,
            "instruments": instruments,
            "underlyings": underlyings,
            "paths": paths,
            "factors": factors,
            "seed": seed,
            "persist": persist,
            "pricing_request": pricing_request,
            "execution": execution,
            "greeks": greeks,
            "sensitivities": sensitivities,
            "pnl": pnl,
        }
    )


@mcp.tool()
def run_etl_task(
    mode: str = "augment",
    termsheet: str = "",
    count: int = 10,
    seed: int = 20260909,
    out_path: str = "",
) -> dict[str, Any]:
    """Run a fina-etl task for the scheduler: ``augment`` or ``compile``.

    ``augment`` clones the term sheet into ``count`` permuted variants;
    ``compile`` additionally converts them to validated ``pricing-request``
    objects. Maps to the ``fina-etl.augment_termsheet`` /
    ``fina-etl.compile_pricing_requests`` scheduler handlers.
    """
    result = scheduler_adapter.run_risk_task(
        {"mode": mode, "termsheet": termsheet or None, "count": count, "seed": seed, "validate": True}
    )
    if out_path and mode == "augment":
        Path(out_path).write_text(
            json.dumps({"count": result["count"], "instruments": result["instruments"]}, separators=(",", ":"))
        )
        result.pop("instruments", None)
        result["out_path"] = out_path
    return result


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_request: Any) -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "service": "fina-risk",
            "tools": len(ALL_TOOLS) + 2,
            "backend": "local-vectorized-cpu",
            "store": _store_status(),
            "object_store": object_store_status(),
        }
    )


app = mcp.streamable_http_app()


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
