"""Canonical RiskCube metadata lifecycle and typed dataset query boundary."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from .olap import query_ssrm

REPORT_KINDS = {"risk", "pnl", "taylor", "forecast"}
CONFIG_GROUPS = {
    "sensitivity": {"risk"},
    "sensitivity-pnl": {"risk", "pnl"},
    "sensitivity-pnl-taylor": {"risk", "pnl", "taylor"},
    "full": REPORT_KINDS,
}
GENERATED_KEYS = {"value", "pv", "pnl", "delta", "gamma", "vega", "rho", "theta", "taylor", "forecast"}


def _root() -> Path:
    root = Path(os.getenv("FINA_RISKCUBE_METADATA_ROOT", "/tmp/fina-riskcube-metadata"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def _path(kind: str) -> Path:
    return _root() / f"{kind}.json"


def _load(kind: str) -> list[dict[str, Any]]:
    path = _path(kind)
    if not path.exists():
        return []
    raw = json.loads(path.read_text())
    return raw if isinstance(raw, list) else []


def _save(kind: str, rows: list[dict[str, Any]]) -> None:
    tmp = _path(kind).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rows, indent=2, sort_keys=True))
    tmp.replace(_path(kind))


def _assert_metadata(value: Any, path: str = "$") -> None:
    if isinstance(value, list):
        for i, child in enumerate(value):
            _assert_metadata(child, f"{path}[{i}]")
    elif isinstance(value, dict):
        for key, child in value.items():
            if key.lower() in GENERATED_KEYS or key.lower().endswith(("_value", "_amount")):
                raise ValueError(f"generated value field {path}.{key} must be stored in Parquet, not JSON")
            _assert_metadata(child, f"{path}.{key}")


def create_slice(definition: dict[str, Any]) -> dict[str, Any]:
    key = str(definition.get("slice_key") or "").strip()
    if not key:
        raise ValueError("slice_key is required")
    rows = _load("slices")
    if any(str(row.get("slice_key")) == key for row in rows):
        raise ValueError(f"slice already exists: {key}")
    record = {**definition, "slice_key": key, "status": "draft", "created_at": int(time.time())}
    _assert_metadata(record)
    rows.append(record)
    _save("slices", rows)
    return record


def list_slices() -> list[dict[str, Any]]:
    return _load("slices")


def create_scenario(definition: dict[str, Any]) -> dict[str, Any]:
    key = str(definition.get("scenario_key") or "").strip()
    if not key:
        raise ValueError("scenario_key is required")
    rows = _load("scenarios")
    if any(str(row.get("scenario_key")) == key for row in rows):
        raise ValueError(f"scenario already exists: {key}")
    record = {**definition, "scenario_key": key, "status": "draft", "created_at": int(time.time())}
    _assert_metadata(record)
    rows.append(record)
    _save("scenarios", rows)
    return record


def list_scenarios() -> list[dict[str, Any]]:
    return _load("scenarios")


def create_report(request: dict[str, Any]) -> dict[str, Any]:
    slice_key = str(request.get("slice_key") or "").strip()
    if not slice_key:
        raise ValueError("slice_key is required")
    kinds = set(request.get("report_kinds") or [])
    unknown = kinds - REPORT_KINDS
    if unknown:
        raise ValueError(f"unsupported report kinds: {sorted(unknown)}")
    group = str(request.get("configuration_group") or "sensitivity-pnl-taylor")
    if group not in CONFIG_GROUPS or not kinds.issubset(CONFIG_GROUPS[group]):
        raise ValueError(f"configuration group {group} does not include requested report kinds")
    rows = _load("reports")
    used = [int(row.get("version_id", 0)) for row in rows]
    version_id = max(used, default=0) + 1
    epoch = int(time.time())
    report_name = str(request.get("report_name") or slice_key)
    record = {
        "report_key": f"{report_name}-{epoch}",
        "version_id": version_id,
        "slice_key": slice_key,
        "evaluation_date": request.get("evaluation_date"),
        "market_data_datetime": request.get("market_data_datetime"),
        "configuration_group": group,
        "report_kinds": sorted(kinds),
        "status": "requested",
        "created_at": epoch,
        "provenance": request.get("provenance", {}),
    }
    _assert_metadata(record)
    rows.append(record)
    _save("reports", rows)
    return record


def list_reports() -> list[dict[str, Any]]:
    return _load("reports")


def get_report(report_key: str) -> dict[str, Any]:
    for report in _load("reports"):
        if report.get("report_key") == report_key:
            return report
    raise KeyError(f"report not found: {report_key}")


def query_report(report_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    report = get_report(report_key)
    if report.get("status") != "ready":
        raise ValueError("report is not ready")
    return query_ssrm({**payload, "report_key": report_key})
