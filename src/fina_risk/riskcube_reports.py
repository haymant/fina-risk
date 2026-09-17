from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from .pricing import bump_result, common_from_job, load_legacy_request

_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe(value: Any) -> str:
    text = _SAFE.sub("-", str(value or "unknown")).strip("-")
    return text or "unknown"


def _root() -> Path:
    return Path(os.getenv("TAC_LAKE_DIR") or os.getenv("FINA_OLAP_DATASET_ROOT") or "/tmp/tac-lake") / "reports"


def _read_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return fallback


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, default=str))


def _instrument_id(request: dict[str, Any], index: int) -> str:
    key = request.get("InstrumentKey")
    if isinstance(key, dict):
        value = key.get("instrument_id") or key.get("name") or key.get("isin")
        if value:
            return str(value)
    for key_name in ("instrument_id", "instrumentId", "instrument_key"):
        value = request.get(key_name)
        if value:
            return str(value)
    return f"instrument-{index + 1}"


def _field(request: dict[str, Any], instrument_id: str, field: str) -> Any:
    if field == "instrument_id":
        return instrument_id
    if field == "product_type":
        key = request.get("InstrumentKey")
        if isinstance(key, dict):
            return key.get("product_type") or request.get("product_type")
        return request.get("product_type")
    key = request.get("InstrumentKey")
    if isinstance(key, dict) and field in key:
        return key[field]
    return request.get(field)


def _calculation_values(request: dict[str, Any]) -> dict[str, float]:
    """Best-effort extraction from the reference pricer; missing lanes remain explicit nulls."""
    try:
        jobs = load_legacy_request(request)
        if not jobs:
            return {}
        result = bump_result(common_from_job(jobs[0]))
        values: dict[str, float] = {}
        valuation = result.get("base", {}).get("valuation", {})
        for key in ("pv", "pv_amount", "price_pct_of_notional"):
            value = valuation.get(key)
            if isinstance(value, (int, float)):
                values[key] = float(value)
        for item in result.get("sensitivities", []):
            if not isinstance(item, dict):
                continue
            measure = str(item.get("measure") or item.get("greek") or "").lower()
            value = item.get("value")
            if measure and isinstance(value, (int, float)):
                values[measure] = values.get(measure, 0.0) + float(value)
        return values
    except Exception:
        return {}


def trigger_report(request: dict[str, Any]) -> dict[str, Any]:
    slice_key = str(request.get("slice_key") or request.get("sliceName") or request.get("slice") or "default")
    report_name = str(request.get("report_name") or f"{slice_key}-report")
    version = int(request.get("report_version") or request.get("reportVersion") or time.time())
    report_kinds = [str(x) for x in request.get("report_kinds", ["risk", "pnl", "taylor"])]
    groups = request.get("groups") or []
    if not isinstance(groups, list) or not groups:
        groups = [{"name": "default", "group_by": ["instrument_id"], "measures": ["delta", "vega", "total_taylor_pnl"]}]
    requests = request.get("requests") or []
    if not isinstance(requests, list):
        raise ValueError("requests must be a list")

    rows: list[dict[str, Any]] = []
    for index, raw in enumerate(requests):
        if not isinstance(raw, dict):
            continue
        instrument_id = _instrument_id(raw, index)
        values = _calculation_values(raw)
        for group in groups:
            if not isinstance(group, dict):
                continue
            group_name = str(group.get("name") or "default")
            group_by = [str(x) for x in group.get("group_by", ["instrument_id"])]
            measures = [str(x) for x in group.get("measures", [])]
            base = {
                "slice": slice_key,
                "version": version,
                "report_name": report_name,
                "instrument_id": instrument_id,
                "configuration_group": group_name,
                "report_kinds": ",".join(report_kinds),
            }
            for field in group_by:
                base[field] = _field(raw, instrument_id, field)
            for measure in measures:
                base[measure] = values.get(measure)
            rows.append(base)

    root = _root()
    partition = root / f"slice={_safe(slice_key)}" / f"version={version}"
    partition.mkdir(parents=True, exist_ok=True)
    file_path = partition / "riskcube_report.parquet"
    if rows:
        pq.write_table(pa.Table.from_pylist(rows), file_path, compression="zstd")
    else:
        pq.write_table(pa.table({"slice": [slice_key], "version": [version], "status": ["empty"]}), file_path)

    report = {
        "report_id": f"R-{uuid.uuid4().hex}",
        "report_name": report_name,
        "slice_key": slice_key,
        "version": version,
        "report_kinds": report_kinds,
        "groups": groups,
        "rows": len(rows),
        "path": str(file_path),
        "partition": f"slice={_safe(slice_key)}/version={version}",
        "status": "completed",
        "storage_root": str(root),
        "evaluation_date": request.get("evaluation_date"),
        "market_data_datetime": request.get("market_data_datetime"),
        "provenance": request.get("provenance") or {},
    }
    _write_json(partition / "report.json", report)
    index_path = root / "index.json"
    index = _read_json(index_path, [])
    if not isinstance(index, list):
        index = []
    index = [
        item
        for item in index
        if not (isinstance(item, dict) and item.get("slice_key") == slice_key and item.get("version") == version)
    ]
    index.append(report)
    _write_json(index_path, index)
    return report


def list_reports() -> list[dict[str, Any]]:
    index = _read_json(_root() / "index.json", [])
    return index if isinstance(index, list) else []


def get_report(
    report_id: str | None = None, slice_key: str | None = None, version: Any = None
) -> dict[str, Any] | None:
    for report in reversed(list_reports()):
        if report_id and str(report.get("report_id")) == str(report_id):
            return report
        if slice_key and str(report.get("slice_key")) == str(slice_key) and (
            version is None or str(report.get("version")) == str(version)
        ):
            return report
    return None


def query_report(query: dict[str, Any]) -> dict[str, Any]:
    report = get_report(
        query.get("report_id"),
        query.get("slice_key") or query.get("sliceName"),
        query.get("version") or query.get("reportVersion"),
    )
    if not report:
        raise KeyError("report not found")
    table = pq.read_table(report["path"])
    rows = table.to_pylist()
    return {"report": report, "columns": table.column_names, "rows": rows}


def slice_list() -> list[dict[str, Any]]:
    return _read_json(_root().parent / "slices.json", [])


def slice_create(definition: dict[str, Any]) -> dict[str, Any]:
    key = str(definition.get("slice_key") or definition.get("sliceName") or "").strip()
    if not key:
        raise ValueError("slice_key is required")
    path = _root().parent / "slices.json"
    items = slice_list()
    record = {**definition, "slice_key": key, "slice_id": definition.get("slice_id") or key, "updated_at": time.time()}
    items = [item for item in items if str(item.get("slice_key")) != key]
    items.append(record)
    _write_json(path, items)
    return record


def scenario_list() -> list[dict[str, Any]]:
    return _read_json(_root().parent / "scenarios.json", [])


def scenario_create(definition: dict[str, Any]) -> dict[str, Any]:
    key = str(definition.get("scenario_key") or definition.get("scenarioKey") or "").strip()
    if not key:
        raise ValueError("scenario_key is required")
    path = _root().parent / "scenarios.json"
    items = scenario_list()
    record = {
        **definition,
        "scenario_key": key,
        "scenario_id": definition.get("scenario_id") or key,
        "updated_at": time.time(),
    }
    items = [item for item in items if str(item.get("scenario_key")) != key]
    items.append(record)
    _write_json(path, items)
    return record
