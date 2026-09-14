"""Correlation lookup from the TradeAC lake correlation store.

The store is a ~1.3M-row table keyed by ``(leg_a, leg_b)`` over ~1.6k stock/FX
legs (``<lake>/correlations.parquet``), written by the tac-engine
``corr_benchmark`` tool: columns ``t, leg_a, leg_b, rho0, rho, alpha,
window_days, n_obs, source``.

Performance design (the "lookup before sending" option): the orchestration /
ETL layer resolves the **small** universe correlation matrix once and packs that
resolved matrix with the market/instrument payload. The pricing engine therefore
consumes ``U x U`` floats and never parses the raw 1.3M-row table. The lookup
itself is a single columnar DuckDB scan filtered to the traded legs, not a
full-table Python load.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from typing import Any

import numpy as np

DEFAULT_CORRELATIONS_PATH = os.getenv("TAC_CORRELATIONS_PATH", "/home/data/lake/correlations.parquet")


def _connect() -> Any:  # pragma: no cover - thin duckdb wrapper
    import duckdb

    return duckdb.connect()


def table_available(path: str = DEFAULT_CORRELATIONS_PATH) -> bool:
    try:
        import duckdb  # noqa: F401
    except ImportError:  # pragma: no cover - duckdb is a hard dep in the venv
        return False
    return os.path.exists(path)


def table_stats(path: str = DEFAULT_CORRELATIONS_PATH) -> dict[str, Any]:
    """Row/leg counts and provenance for the correlation store."""
    con = _connect()
    rows, legs_a, legs_b, as_of, window, alpha, source = con.execute(
        "select count(*), count(distinct leg_a), count(distinct leg_b), "
        "max(t), max(window_days), max(alpha), max(source) from read_parquet(?)",
        [path],
    ).fetchone()
    return {
        "path": path,
        "rows": int(rows),
        "legs": int(max(legs_a, legs_b)),
        "as_of": str(as_of),
        "window_days": int(window) if window is not None else None,
        "alpha": float(alpha) if alpha is not None else None,
        "source": source,
    }


def lookup_pairwise(legs: Iterable[str], path: str = DEFAULT_CORRELATIONS_PATH) -> dict[frozenset[str], float]:
    """Return ``{frozenset({leg_a, leg_b}): rho}`` for stored pairs whose two
    legs are both in ``legs`` (one filtered columnar scan)."""
    unique = list(dict.fromkeys(legs))
    if len(unique) < 2:
        return {}
    con = _connect()
    marks = ",".join("?" for _ in unique)
    query = (
        f"select leg_a, leg_b, rho from read_parquet(?) "
        f"where leg_a in ({marks}) and leg_b in ({marks})"
    )
    rows = con.execute(query, [path, *unique, *unique]).fetchall()
    lookup: dict[frozenset[str], float] = {}
    for leg_a, leg_b, rho in rows:
        lookup[frozenset((leg_a, leg_b))] = float(rho)
    return lookup


def _nearest_psd(matrix: np.ndarray) -> np.ndarray:
    """Repair a symmetric matrix to the nearest PSD correlation matrix so the
    Cholesky factor always exists (the alpha-dampened table is near-corr)."""
    symmetric = (matrix + matrix.T) / 2.0
    values, vectors = np.linalg.eigh(symmetric)
    values = np.clip(values, 1e-10, None)
    repaired = vectors @ np.diag(values) @ vectors.T
    scale = np.sqrt(np.outer(np.diag(repaired), np.diag(repaired)))
    repaired = repaired / scale
    np.fill_diagonal(repaired, 1.0)
    return repaired


def resolve_correlation_matrix(
    legs: Iterable[str],
    path: str = DEFAULT_CORRELATIONS_PATH,
    *,
    fallback_rho: float | None = None,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Build the ``U x U`` correlation matrix for ``legs`` from the lake store.

    Returns ``(matrix, meta)`` where ``meta`` records the provenance (table path,
    row count, as-of, found/missing pairs) plus the resolved matrix itself so it
    can be packed into the pricing payload.
    """
    unique = list(legs)
    n = len(unique)
    matrix = np.eye(n)
    meta: dict[str, Any] = {"legs": unique, "requested_pairs": n * (n - 1) // 2}
    usable = table_available(path)
    lookup: dict[frozenset[str], float] = {}
    if usable:
        try:
            lookup = lookup_pairwise(unique, path)
            meta["table"] = table_stats(path)
        except Exception as error:  # pragma: no cover - defensive
            usable = False
            meta["error"] = str(error)
    if fallback_rho is None:
        fallback_rho = float(np.mean(list(lookup.values()))) if lookup else 0.0
    misses = 0
    for i in range(n):
        for j in range(i + 1, n):
            value = lookup.get(frozenset((unique[i], unique[j])))
            if value is None:
                value = fallback_rho
                misses += 1
            matrix[i, j] = matrix[j, i] = value
    matrix = _nearest_psd(matrix)
    meta.update(
        {
            "source": "lake_corr_table" if usable else "fallback",
            "found_pairs": len(lookup),
            "missing_pairs": misses,
            "fallback_rho": float(fallback_rho),
            "matrix": matrix.tolist(),
        }
    )
    return matrix, meta
