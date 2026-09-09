from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, cast

import pyarrow as pa
import pyarrow.parquet as pq

from .benchmarking import run_benchmark
from .olap import write_risk_store
from .pricing import bump_result, common_from_job, load_legacy_request

PIPELINE_STATE: dict[str, dict[str, Any]] = {}


def _records(payload: Any, key: str) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        value = payload.get(key, payload.get("records", payload))
    else:
        value = payload
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list of records")
    return [x for x in value if isinstance(x, dict)]


def _append_partition(root: Path, dataset: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    if not records:
        return {"dataset": dataset, "rows": 0, "files": []}
    directory = root / dataset
    directory.mkdir(parents=True, exist_ok=True)
    partition = directory / f"part-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}.parquet"
    pq.write_table(pa.Table.from_pylist(records), partition, compression="zstd")
    manifest_path = root / "ingestion-manifest.json"
    manifest = (
        json.loads(manifest_path.read_text())
        if manifest_path.exists()
        else {"schema_version": "ingestion-manifest.v1", "datasets": {}}
    )
    datasets = cast(dict[str, dict[str, Any]], manifest["datasets"])
    entry = datasets.setdefault(dataset, {"rows": 0, "files": []})
    entry["rows"] += len(records)
    entry["files"].append(str(partition))
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return {"dataset": dataset, "rows": len(records), "files": [str(partition)], "append_only": True}


def ingest_instruments(payload: dict[str, Any], *, root: Path | str) -> dict[str, Any]:
    root = Path(root)
    records = _records(payload.get("instruments", payload), "instruments")
    normalized: list[dict[str, Any]] = []
    for row in records:
        normalized.append(
            {
                "instrument_id": row.get("instrumentId", row.get("instrument_id")),
                "product_type": row.get("productType", row.get("product_type")),
                "currency": row.get("currency"),
                "notional": row.get("notional"),
                "underlyings": json.dumps(row.get("underlyings", [])),
                "features": json.dumps(row.get("features", {}), sort_keys=True),
                "legs": json.dumps(row.get("legs", []), sort_keys=True),
                "legacy_semantics": json.dumps(row.get("legacySemantics", {}), sort_keys=True),
            }
        )
    result = _append_partition(root, "instruments", normalized)
    result["schema_version"] = "instrument-atomic.v1"
    return result


def ingest_market_data(payload: dict[str, Any], *, root: Path | str) -> dict[str, Any]:
    root = Path(root)
    records = _records(payload.get("underlyings", payload), "underlyings")
    normalized: list[dict[str, Any]] = []
    for row in records:
        normalized.append(
            {
                "underlying_id": row.get("id", row.get("underlying_id")),
                "currency": row.get("currency"),
                "sector": row.get("sector"),
                "spot": row.get("spot"),
                "reference_spot": row.get("referenceSpot", row.get("reference_spot")),
                "bid": row.get("bid"),
                "ask": row.get("ask"),
                "dividend": json.dumps(row.get("dividend", []), sort_keys=True),
                "vol_surface": json.dumps(row.get("volSurface", row.get("vol_surface", {})), sort_keys=True),
            }
        )
    result = _append_partition(root, "market", normalized)
    result["schema_version"] = "market-atomic.v1"
    result["evaluation_date"] = payload.get("evaluationDate", payload.get("evaluation_date"))
    return result


def risk_metadata() -> dict[str, Any]:
    return {
        "schema_version": "risk-metadata.v1",
        "risk_factor_key": "portfolio_id|instrument_id|leg_id|risk_factor_id|greek|bucket_id",
        "wide_components": [
            "base_pv",
            "spot",
            "spot_shock",
            "delta",
            "gamma",
            "vega",
            "irpv01",
            "delta_pnl",
            "gamma_pnl",
            "vega_pnl",
            "rate_pnl",
            "total_taylor_pnl",
        ],
        "methods": {
            "AAD": "smooth fixed branch",
            "PATHWISE": "pathwise payoff derivative",
            "CRN_FD": "discontinuous or cross-check",
            "LRM": "likelihood-ratio estimator",
        },
        "datasets": {
            "risk_wide": "one atomic row per risk factor with Taylor components",
            "risk_long": "one row per method observation",
        },
        "units": {
            "delta": "PV per spot unit",
            "delta_dollar": "hedge value per underlying",
            "vega": "PV per volatility point",
            "irpv01": "PV per one basis point",
        },
    }


def dashboard_metadata() -> dict[str, Any]:
    return {
        "schema_version": "dashboard-metadata.v1",
        "views": [
            {
                "id": "portfolio_summary",
                "dataset": "risk_wide",
                "drill": ["portfolio_id", "instrument_id", "leg_id", "risk_factor_id"],
                "measures": ["base_pv", "total_taylor_pnl", "delta_pnl", "gamma_pnl", "vega_pnl", "rate_pnl"],
            },
            {
                "id": "factor_exposure",
                "dataset": "risk_wide",
                "drill": ["risk_factor_type", "underlying_id", "risk_factor_id"],
                "measures": ["delta", "delta_dollar", "total_taylor_pnl"],
            },
            {
                "id": "method_audit",
                "dataset": "risk_long",
                "drill": ["risk_factor_key", "method", "method_status"],
                "measures": ["value", "dollar_value"],
            },
        ],
        "global_driver": {
            "shared_filters": ["portfolio_id", "instrument_id", "leg_id", "underlying_id", "risk_factor_id"],
            "state": ["selection", "drill_path", "sort_model", "filter_model"],
        },
    }


def plan_pnl_forecast(payload: dict[str, Any]) -> dict[str, Any]:
    instruments = int(payload.get("instruments", 0))
    paths = int(payload.get("paths", 30000))
    sensitivities = payload.get("sensitivities", ["delta"])
    if isinstance(sensitivities, str):
        sensitivities = [x.strip() for x in sensitivities.split(",") if x.strip()]
    expensive = [x for x in sensitivities if x in {"bucket_vega", "cross_vega", "skew_delta", "gamma"}]
    return {
        "schema_version": "execution-plan.v1",
        "target": {"instruments": instruments, "paths": paths, "sensitivities": sensitivities},
        "reuse": ["market snapshot", "simulation paths", "payoff structure", "AAD tape"],
        "execution": {
            "structure_reuse": True,
            "common_random_numbers": True,
            "append_partitions": True,
            "aad_first": True,
            "fallback_fd_for_discontinuities": True,
        },
        "cost_drivers": {
            "expensive_sensitivities": expensive,
            "estimated_factor_rows": instruments * max(1, len(sensitivities)),
            "estimated_path_work": instruments * paths,
        },
        "phases": [
            "ingest",
            "normalize",
            "shared_path_and_tape",
            "pv_and_sensitivities",
            "append_parquet",
            "SSRM_analysis",
        ],
    }


def trigger_pnl_forecast(payload: dict[str, Any]) -> dict[str, Any]:
    request = payload.get("request")
    if request:
        jobs = load_legacy_request(request)
        results = [
            bump_result(common_from_job(job), paths=payload.get("paths"), seed=int(payload.get("seed", 1729)))
            for job in jobs
        ]
        store = write_risk_store(
            [x["risk_representation"] for x in results],
            root=payload.get("root", "/tmp/fina-risk-olap"),
            metadata={"pipeline": "pnl_forecast"},
        )
        return {
            "status": "completed",
            "trade_count": len(results),
            "store": store,
            "pv": sum(x["base"]["valuation"]["pv"] for x in results),
        }
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


def pipeline_state(payload: dict[str, Any]) -> dict[str, Any]:
    pipeline_id = str(payload.get("pipeline_id", "default"))
    PIPELINE_STATE[pipeline_id] = {
        **PIPELINE_STATE.get(pipeline_id, {}),
        **payload,
        "state_backend": payload.get("state_backend", "memory"),
        "mcp_transport": payload.get("mcp_transport", "stdio"),
    }
    return {
        "status": "ok",
        "pipeline_id": pipeline_id,
        "state": PIPELINE_STATE[pipeline_id],
        "redis_ready": bool(payload.get("state_backend") == "redis"),
    }
