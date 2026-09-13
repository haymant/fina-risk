from __future__ import annotations

import glob as globmod
import itertools
import json
import os
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from .gcs import configure_duckdb_object_store, object_store_configured, object_store_status
from .storage import StorageConfig, get_storage_config
from .storage import storage_status as store_status

DEFAULT_ROOT = Path(os.getenv("FINA_RISK_OLAP_ROOT", "/tmp/fina-risk-olap"))
ALLOWED_AGGREGATES = {"sum", "avg", "min", "max", "count", "first", "last"}


def _is_object_store(cfg: StorageConfig) -> bool:
    if cfg.store in ("s3", "gcs"):
        return True
    if cfg.store == "auto":
        return bool(cfg.effective_bucket)
    return False


def _object_scheme(cfg: StorageConfig, bucket: str) -> str:
    """Choose a DuckDB-readable scheme for the configured bucket.

    GCS interoperability credentials use the S3-compatible endpoint, which
    DuckDB reads through the ``s3://`` scheme (the tested fina-olap path).
    """
    if bucket.startswith("gs://"):
        return "gs://"
    if bucket.startswith("s3://"):
        return "s3://"
    if os.getenv("S3_API_KEY"):
        return "s3://"
    return "gs://" if cfg.store == "gcs" else "s3://"


def _base_location(cfg: StorageConfig) -> tuple[str, bool]:
    """Return ``(base, is_object)`` — a local root or ``scheme://bucket/prefix``."""
    if _is_object_store(cfg):
        raw = (cfg.effective_bucket or "").rstrip("/")
        scheme = _object_scheme(cfg, raw)
        bucket = raw.removeprefix("s3://").removeprefix("gs://")
        prefix = cfg.default_path.strip("/")
        base = f"{scheme}{bucket}"
        if prefix:
            base = f"{base}/{prefix}"
        return base, True
    return cfg.local_root, False


def _effective_hive(cfg: StorageConfig, is_object: bool) -> bool:
    if cfg.hive_partitioning is not None:
        return cfg.hive_partitioning
    return is_object


def resolve_dataset_source(dataset: str, root: Path | str | None = None) -> tuple[str, bool]:
    """Resolve the DuckDB source glob + hive flag for a dataset under the store config."""
    cfg = get_storage_config()
    glob_pat = cfg.glob_for(dataset)
    if root is not None:
        return f"{Path(root).as_posix().rstrip('/')}/{glob_pat}", _effective_hive(cfg, False)
    base, is_object = _base_location(cfg)
    return f"{base.rstrip('/')}/{glob_pat}", _effective_hive(cfg, is_object)


def _write_target(dataset: str, root: Path | str | None = None) -> tuple[str, bool]:
    """Resolve where a dataset is written (and whether it is an object-store URI)."""
    cfg = get_storage_config()
    directory_layout = "/" in cfg.glob_for(dataset)
    if root is not None:
        base, is_object = Path(root).as_posix(), False
    else:
        base, is_object = _base_location(cfg)
    if directory_layout:
        return f"{base.rstrip('/')}/{dataset}/part-000.parquet", is_object
    return f"{base.rstrip('/')}/{dataset}.parquet", is_object


def _ident(value: str) -> str:
    if not value or not value.replace("_", "").replace("-", "").isalnum():
        raise ValueError(f"Invalid column identifier: {value!r}")
    return f'"{value}"'


def _source_path(dataset: str, root: Path) -> Path:
    if dataset not in {"risk_wide", "risk_long"}:
        raise ValueError("dataset must be risk_wide or risk_long")
    return root / f"{dataset}.parquet"


def _local_source_exists(source: str) -> bool:
    if globmod.has_magic(source):
        return bool(globmod.glob(source))
    return Path(source).exists()


def _filter_sql(field: str, item: dict[str, Any], params: list[Any]) -> str:
    quoted = _ident(field)
    if "operator" in item:
        op = str(item["operator"]).upper()
        if op not in {"AND", "OR"}:
            raise ValueError("filter operator must be AND or OR")
        return (
            f"({_filter_sql(field, item['condition1'], params)} {op} {_filter_sql(field, item['condition2'], params)})"
        )
    kind = item.get("filterType")
    typ = item.get("type")
    if kind == "text":
        value = str(item.get("filter", ""))
        patterns = {
            "equals": value,
            "notEqual": value,
            "contains": f"%{value}%",
            "notContains": f"%{value}%",
            "startsWith": f"{value}%",
            "endsWith": f"%{value}",
        }
        if typ not in patterns:
            raise ValueError(f"unsupported text filter: {typ}")
        params.append(patterns[typ])
        operator = "=" if typ in {"equals", "startsWith", "endsWith", "contains"} else "!="
        if typ in {"contains", "notContains", "startsWith", "endsWith"}:
            operator = "ILIKE" if typ != "notContains" else "NOT ILIKE"
        return f"{quoted} {operator} ?"
    if kind == "number":
        numeric_value: Any = item.get("filter")
        if typ == "inRange":
            params.extend([numeric_value, item.get("filterTo")])
            return f"{quoted} BETWEEN ? AND ?"
        ops = {
            "equals": "=",
            "notEqual": "!=",
            "greaterThan": ">",
            "greaterThanOrEqual": ">=",
            "lessThan": "<",
            "lessThanOrEqual": "<=",
        }
        if typ not in ops:
            raise ValueError(f"unsupported number filter: {typ}")
        params.append(numeric_value)
        return f"{quoted} {ops[typ]} ?"
    if kind == "set":
        values = item.get("values", [])
        if not values:
            return "FALSE"
        params.extend(values)
        return f"{quoted} IN ({', '.join('?' for _ in values)})"
    raise ValueError(f"unsupported filter type: {kind}")


def _agg(expr: str, spec: dict[str, Any]) -> str:
    fn = str(spec.get("aggFunc", "sum")).lower()
    if fn not in ALLOWED_AGGREGATES:
        raise ValueError(f"unsupported aggregation: {fn}")
    return f"{fn.upper()}({_ident(expr)}) AS {_ident(expr)}"


def _read_relation(con: duckdb.DuckDBPyConnection, dataset: str, root: Path | str | None) -> str:
    if dataset not in {"risk_wide", "risk_long"}:
        raise ValueError("dataset must be risk_wide or risk_long")
    source, hive = resolve_dataset_source(dataset, root)
    if source.startswith(("s3://", "gs://", "http://", "https://")):
        if object_store_configured():
            configure_duckdb_object_store(con)
    elif not _local_source_exists(source):
        raise FileNotFoundError(f"OLAP dataset not found: {source}")
    escaped_path = source.replace("'", "''")
    con.execute(
        f"CREATE OR REPLACE TEMP VIEW olap_source AS "
        f"SELECT * FROM read_parquet('{escaped_path}', hive_partitioning = {str(bool(hive)).upper()})"
    )
    return "olap_source"


def _write_table(table: pa.Table, target: str, is_object: bool) -> None:
    if is_object or target.startswith(("s3://", "gs://")):
        con = duckdb.connect()
        try:
            if object_store_configured():
                configure_duckdb_object_store(con)
            con.register("risk_out", table)
            escaped = target.replace("'", "''")
            con.execute(f"COPY (SELECT * FROM risk_out) TO '{escaped}' (FORMAT PARQUET, COMPRESSION ZSTD)")
        finally:
            con.close()
    else:
        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, path, compression="zstd")


def write_risk_store(
    views: list[dict[str, Any]],
    *,
    root: Path | str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist atomic wide factor components and long method observations as Parquet.

    Without an explicit ``root`` the shared store config decides the destination
    (local root, or GCS/S3 bucket + prefix), so fina-olap can read the same data.
    """
    cfg = get_storage_config()
    wide = [row for view in views for row in view.get("wide", [])]
    long_rows = [row for view in views for row in view.get("long", [])]
    if not wide and not long_rows:
        raise ValueError("views contains no risk rows")
    written: dict[str, str] = {}
    for name, rows in (("risk_wide", wide), ("risk_long", long_rows)):
        if rows:
            table = pa.Table.from_pylist(rows)
            target, is_object = _write_target(name, root)
            _write_table(table, target, is_object)
            written[name] = target
    # Local manifest lives beside a local root (skipped for object stores).
    base, is_object = _base_location(cfg) if root is None else (Path(root).as_posix(), False)
    manifest = {
        "schema_version": "risk-store.v1",
        "datasets": {"risk_wide": len(wide), "risk_long": len(long_rows)},
        "targets": written,
        "metadata": metadata or {},
    }
    if not is_object:
        manifest_path = Path(base) / "manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, indent=2, default=str))
    return {"status": "ok", "root": base, **manifest}


def _build_query(
    payload: dict[str, Any], relation: str, con: duckdb.DuckDBPyConnection
) -> tuple[str, list[Any], list[str]]:
    start = max(0, int(payload.get("startRow", 0)))
    end = max(start, int(payload.get("endRow", start + 100)))
    groups = payload.get("rowGroupCols", [])
    keys = payload.get("groupKeys", [])
    values = payload.get("valueCols", [])
    pivot_cols = payload.get("pivotCols", [])
    params: list[Any] = []
    where: list[str] = []
    for index, key in enumerate(keys):
        if index >= len(groups):
            raise ValueError("groupKeys exceeds rowGroupCols")
        where.append(f"{_ident(groups[index].get('field') or groups[index].get('id'))} = ?")
        params.append(key)
    for field, item in payload.get("filterModel", {}).items():
        where.append(_filter_sql(field, item, params))
    is_grouping = len(groups) > len(keys)
    select: list[str] = []
    group_by: list[str] = []
    pivot_fields: list[str] = []
    if is_grouping:
        current = groups[len(keys)]
        current_field = current.get("field") or current.get("id")
        select.append(_ident(current_field))
        group_by.append(_ident(current_field))
        if payload.get("pivotMode") and pivot_cols and values:
            pivot_values: list[list[Any]] = []
            for col in pivot_cols:
                field = col.get("field") or col.get("id")
                rows = con.execute(
                    f"SELECT DISTINCT {_ident(field)} FROM {relation} "
                    f"WHERE {_ident(field)} IS NOT NULL ORDER BY {_ident(field)}"
                ).fetchall()
                pivot_values.append([row[0] for row in rows])
            for combo in itertools.product(*pivot_values):
                condition_parts = []
                for col, value in zip(pivot_cols, combo, strict=True):
                    field = col.get("field") or col.get("id")
                    condition_parts.append(f"{_ident(field)} = ?")
                    params.append(value)
                condition = " AND ".join(condition_parts)
                prefix = "_".join(str(x) for x in combo)
                for value in values:
                    field = value.get("field") or value.get("id")
                    fn = str(value.get("aggFunc", "sum")).upper()
                    if fn not in {x.upper() for x in ALLOWED_AGGREGATES}:
                        raise ValueError(f"unsupported aggregation: {fn}")
                    alias = f"{prefix}_{field}"
                    select.append(f"{fn}(CASE WHEN {condition} THEN {_ident(field)} END) AS {_ident(alias)}")
                    pivot_fields.append(alias)
        else:
            for value in values:
                select.append(_agg(value.get("field") or value.get("id"), value))
    else:
        select.append("*")
    sql = f"SELECT {', '.join(select)} FROM {relation}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    if group_by:
        sql += " GROUP BY " + ", ".join(group_by)
    order: list[str] = []
    allowed: set[str] | None = {
        str(x.get("field") or x.get("id")) for x in groups
    } | {str(x.get("field") or x.get("id")) for x in values}
    if not is_grouping:
        allowed = None
    for sort in payload.get("sortModel", []):
        col = sort.get("colId")
        direction = str(sort.get("sort", "asc")).upper()
        if direction not in {"ASC", "DESC"}:
            raise ValueError("sort direction must be asc or desc")
        if allowed is None or col in allowed:
            order.append(f"{_ident(col)} {direction}")
    if order:
        sql += " ORDER BY " + ", ".join(order)
    sql += " LIMIT ? OFFSET ?"
    params.extend([end - start + 1, start])
    return sql, params, pivot_fields


def query_ssrm(payload: dict[str, Any], *, root: Path | str | None = None) -> dict[str, Any]:
    """Execute an AG Grid SSRM request against normalized Parquet with DuckDB.

    With no explicit ``root`` the source is resolved from the shared store config
    (local root or GCS/S3 bucket + prefix/partition glob).
    """
    dataset = payload.get("dataset", "risk_wide")
    con = duckdb.connect()
    try:
        relation = _read_relation(con, dataset, root)
        sql, params, pivot_fields = _build_query(payload, relation, con)
        result = con.execute(sql, params)
        columns = [x[0] for x in result.description]
        records = [dict(zip(columns, row, strict=True)) for row in result.fetchall()]
    finally:
        con.close()
    start = max(0, int(payload.get("startRow", 0)))
    requested = max(0, int(payload.get("endRow", start + 100)) - start)
    has_more = len(records) > requested
    if has_more:
        records = records[:requested]
    return {
        "success": True,
        "rows": records,
        "lastRow": -1 if has_more else start + len(records),
        "pivotResultFields": pivot_fields,
    }


def storage_status(*, root: Path | str | None = None) -> dict[str, Any]:
    """Storage diagnostics: shared store config + the local risk-store manifest."""
    cfg = get_storage_config()
    base, is_object = (Path(root).as_posix(), False) if root is not None else _base_location(cfg)
    manifest: dict[str, Any] = {}
    if not is_object:
        manifest_path = Path(base) / "manifest.json"
        manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    return {
        "status": "ok",
        "backend": "duckdb_arrow_parquet",
        "root": None if is_object else base,
        "manifest": manifest,
        "s3_compatible": bool(os.getenv("S3_ENDPOINT")),
        "store": store_status(),
        "object_store": object_store_status(),
    }


def resolve_s3_uri(payload: dict[str, Any]) -> str:
    version = payload.get("reportVersion") or payload.get("version")
    slice_name = payload.get("sliceName") or payload.get("slice")
    if not version or not slice_name:
        raise ValueError("reportVersion/version and sliceName/slice are required")
    cfg = get_storage_config()
    raw_bucket = (cfg.effective_bucket or "").rstrip("/")
    scheme = _object_scheme(cfg, raw_bucket)
    bucket = raw_bucket.removeprefix("s3://").removeprefix("gs://")
    prefix = (cfg.default_path or os.getenv("S3_PATH_PREFIX", "")).strip("/")
    table = payload.get("tableName", "risk_wide")
    parts = [f"{scheme}{bucket}", prefix, str(version), str(slice_name), f"{table}*.parquet"]
    return "/".join(x.strip("/") for x in parts if x)


def link_view_state(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a serializable global UI driver state for linked OLAP views."""
    views = payload.get("views", [])
    if not isinstance(views, list) or not views:
        raise ValueError("views must be a non-empty list")
    shared = payload.get("shared", {})
    return {
        "schema_version": "olap-view-link.v1",
        "driver_id": payload.get("driverId", "global-risk-driver"),
        "views": views,
        "shared": shared,
        "drill_path": payload.get("drillPath", []),
        "selection": payload.get("selection", {}),
    }
