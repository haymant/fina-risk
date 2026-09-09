from __future__ import annotations

import itertools
import json
import os
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

DEFAULT_ROOT = Path(os.getenv("FINA_RISK_OLAP_ROOT", "/tmp/fina-risk-olap"))
ALLOWED_AGGREGATES = {"sum", "avg", "min", "max", "count", "first", "last"}


def _ident(value: str) -> str:
    if not value or not value.replace("_", "").replace("-", "").isalnum():
        raise ValueError(f"Invalid column identifier: {value!r}")
    return f'"{value}"'


def _source_path(dataset: str, root: Path) -> Path:
    if dataset not in {"risk_wide", "risk_long"}:
        raise ValueError("dataset must be risk_wide or risk_long")
    return root / f"{dataset}.parquet"


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


def _read_relation(con: duckdb.DuckDBPyConnection, dataset: str, root: Path) -> str:
    path = _source_path(dataset, root)
    if not path.exists():
        raise FileNotFoundError(f"OLAP dataset not found: {path}")
    escaped_path = str(path).replace("'", "''")
    con.execute(f"CREATE OR REPLACE TEMP VIEW olap_source AS SELECT * FROM read_parquet('{escaped_path}')")
    return "olap_source"


def write_risk_store(
    views: list[dict[str, Any]], *, root: Path | str = DEFAULT_ROOT, metadata: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Persist atomic wide factor components and long method observations as Parquet."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    wide = [row for view in views for row in view.get("wide", [])]
    long_rows = [row for view in views for row in view.get("long", [])]
    if not wide and not long_rows:
        raise ValueError("views contains no risk rows")
    for name, rows in (("risk_wide", wide), ("risk_long", long_rows)):
        if rows:
            table = pa.Table.from_pylist(rows)
            pq.write_table(table, _source_path(name, root), compression="zstd")
    manifest = {
        "schema_version": "risk-store.v1",
        "datasets": {"risk_wide": len(wide), "risk_long": len(long_rows)},
        "metadata": metadata or {},
    }
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str))
    return {"status": "ok", "root": str(root), **manifest}


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


def query_ssrm(payload: dict[str, Any], *, root: Path | str = DEFAULT_ROOT) -> dict[str, Any]:
    """Execute an AG Grid SSRM request against normalized Parquet with DuckDB."""
    dataset = payload.get("dataset", "risk_wide")
    root = Path(root)
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


def storage_status(*, root: Path | str = DEFAULT_ROOT) -> dict[str, Any]:
    root = Path(root)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    return {
        "status": "ok",
        "backend": "duckdb_arrow_parquet",
        "root": str(root),
        "manifest": manifest,
        "s3_compatible": bool(os.getenv("S3_ENDPOINT")),
    }


def resolve_s3_uri(payload: dict[str, Any]) -> str:
    version = payload.get("reportVersion") or payload.get("version")
    slice_name = payload.get("sliceName") or payload.get("slice")
    if not version or not slice_name:
        raise ValueError("reportVersion/version and sliceName/slice are required")
    bucket = os.getenv("S3_BUCKET_NAME", "")
    prefix = os.getenv("S3_PATH_PREFIX", "").strip("/")
    table = payload.get("tableName", "risk_wide")
    parts = [f"s3://{bucket}", prefix, str(version), str(slice_name), f"{table}*.parquet"]
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
