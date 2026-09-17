"""Canonical RiskCube metadata lifecycle and typed dataset query boundary."""
from __future__ import annotations

import json
import os
import sqlite3
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


def _postgres_dsn() -> str | None:
    return os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL")


def _postgres_connection() -> Any:
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(_postgres_dsn(), row_factory=dict_row)


def _ensure_postgres_schema(conn: Any) -> None:
    schema = Path(__file__).resolve().parents[2] / "schema" / "postgres.sql"
    conn.execute(schema.read_text(encoding="utf-8"))


def _use_postgres() -> bool:
    return bool(_postgres_dsn())


def _path(kind: str) -> Path:
    return _root() / f"{kind}.json"


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(_root() / "riskcube.db", timeout=30.0)
    db.row_factory = sqlite3.Row
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS riskcube_metadata (
            kind TEXT NOT NULL,
            record_key TEXT NOT NULL,
            payload TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            PRIMARY KEY (kind, record_key)
        )
        """
    )
    db.execute("CREATE INDEX IF NOT EXISTS riskcube_metadata_version ON riskcube_metadata(kind, json_extract(payload, '$.version_id'))")
    db.commit()
    return db


def _migrate_json(db: sqlite3.Connection, kind: str, key_field: str) -> None:
    """One-way migration for metadata written by the original JSON implementation."""
    if db.execute("SELECT 1 FROM riskcube_metadata WHERE kind = ? LIMIT 1", (kind,)).fetchone():
        return
    path = _path(kind)
    if not path.exists():
        return
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(raw, list):
        return
    for row in raw:
        if not isinstance(row, dict) or not row.get(key_field):
            continue
        db.execute(
            "INSERT OR IGNORE INTO riskcube_metadata(kind, record_key, payload, created_at) VALUES (?, ?, ?, ?)",
            (kind, str(row[key_field]), json.dumps(row, separators=(",", ":")), int(row.get("created_at", 0))),
        )
    db.commit()


def _load(kind: str) -> list[dict[str, Any]]:
    key_field = {"scenarios": "scenario_key", "slices": "slice_key", "reports": "report_key"}[kind]
    if _use_postgres():
        with _postgres_connection() as conn:
            _ensure_postgres_schema(conn)
            rows = conn.execute(
                "SELECT payload FROM riskcube_metadata WHERE kind = %s ORDER BY created_at, record_key", (kind,)
            ).fetchall()
            return [dict(row["payload"]) for row in rows]
    db = _db()
    try:
        _migrate_json(db, kind, key_field)
        rows = db.execute(
            "SELECT payload FROM riskcube_metadata WHERE kind = ? ORDER BY created_at, record_key", (kind,)
        ).fetchall()
        return [json.loads(row["payload"]) for row in rows]
    finally:
        db.close()


def _insert(kind: str, key: str, record: dict[str, Any]) -> None:
    if _use_postgres():
        import psycopg
        from psycopg.types.json import Jsonb

        with _postgres_connection() as conn:
            _ensure_postgres_schema(conn)
            try:
                conn.execute(
                    "INSERT INTO riskcube_metadata(kind, record_key, payload, version_id, created_at) "
                    "VALUES (%s, %s, %s, %s, to_timestamp(%s))",
                    (kind, key, Jsonb(record), record.get("version_id"), int(record.get("created_at", 0))),
                )
            except psycopg.errors.UniqueViolation as error:
                raise ValueError(f"{kind[:-1]} already exists: {key}") from error
        return
    db = _db()
    try:
        _migrate_json(db, kind, {"scenarios": "scenario_key", "slices": "slice_key", "reports": "report_key"}[kind])
        db.execute(
            "INSERT INTO riskcube_metadata(kind, record_key, payload, created_at) VALUES (?, ?, ?, ?)",
            (kind, key, json.dumps(record, separators=(",", ":")), int(record.get("created_at", 0))),
        )
        db.commit()
    finally:
        db.close()


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
    record = {**definition, "slice_key": key, "status": "draft", "created_at": int(time.time())}
    _assert_metadata(record)
    try:
        _insert("slices", key, record)
    except sqlite3.IntegrityError as error:
        raise ValueError(f"slice already exists: {key}") from error
    return record


def list_slices() -> list[dict[str, Any]]:
    return _load("slices")


def create_scenario(definition: dict[str, Any]) -> dict[str, Any]:
    key = str(definition.get("scenario_key") or "").strip()
    if not key:
        raise ValueError("scenario_key is required")
    record = {**definition, "scenario_key": key, "status": "draft", "created_at": int(time.time())}
    _assert_metadata(record)
    try:
        _insert("scenarios", key, record)
    except sqlite3.IntegrityError as error:
        raise ValueError(f"scenario already exists: {key}") from error
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
    if _use_postgres():
        from psycopg.types.json import Jsonb

        with _postgres_connection() as conn:
            _ensure_postgres_schema(conn)
            conn.execute("SELECT pg_advisory_xact_lock(hashtext('fina-riskcube-report-version'))")
            version_id = int(
                conn.execute(
                    "SELECT COALESCE(MAX(version_id), 0) + 1 AS next_version "
                    "FROM riskcube_metadata WHERE kind = 'reports'"
                ).fetchone()["next_version"]
            )
            epoch = int(time.time())
            report_name = str(request.get("report_name") or slice_key)
            record = {
                "report_key": f"{report_name}-{epoch}-v{version_id}",
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
            conn.execute(
                "INSERT INTO riskcube_metadata(kind, record_key, payload, version_id, created_at) "
                "VALUES (%s, %s, %s, %s, to_timestamp(%s))",
                ("reports", record["report_key"], Jsonb(record), version_id, epoch),
            )
            return record
    db = _db()
    try:
        _migrate_json(db, "reports", "report_key")
        db.execute("BEGIN IMMEDIATE")
        version_id = int(
            db.execute(
                "SELECT COALESCE(MAX(json_extract(payload, '$.version_id')), 0) + 1 "
                "FROM riskcube_metadata WHERE kind = 'reports'"
            ).fetchone()[0]
        )
        epoch = int(time.time())
        report_name = str(request.get("report_name") or slice_key)
        record = {
            "report_key": f"{report_name}-{epoch}-v{version_id}",
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
        db.execute(
            "INSERT INTO riskcube_metadata(kind, record_key, payload, created_at) VALUES (?, ?, ?, ?)",
            ("reports", record["report_key"], json.dumps(record, separators=(",", ":")), epoch),
        )
        db.commit()
        return record
    finally:
        db.close()


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
