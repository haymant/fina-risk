"""Adapter that runs fina-risk compute as a ``fina-core-scheduler`` handler.

The scheduler is transport-neutral: wire it with a callable that forwards to
:func:`run_risk_task` (or the fina-risk MCP tool of the same name). The handler
names used in FinaProcess YAML are ``fina-risk.risk_batch`` and
``fina-risk.pricing_and_sensitivity``.
"""

from __future__ import annotations

from typing import Any

from .benchmarking import run_benchmark
from .olap import write_risk_store


def run_risk_task(request: dict[str, Any]) -> dict[str, Any]:
    """Dispatch a scheduler task: ETL (augment/compile), sizing (benchmark) or pricing (batch/single)."""
    request = request or {}
    mode = str(request.get("mode", "batch")).lower()
    if mode == "augment":
        return run_augment(request)
    if mode == "compile":
        return run_compile(request)
    if mode == "benchmark":
        return run_risk_benchmark(request)
    if mode == "single" or (request.get("pricing_request") and "instruments" not in request):
        return run_risk_single(request)
    return run_risk_batch(request)


def run_augment(request: dict[str, Any]) -> dict[str, Any]:
    """ETL stage: augment the term sheet into permuted variants (``fina-etl.augment_termsheet``)."""
    from .etl import augment_termsheet

    variants = augment_termsheet(
        request.get("termsheet") or None, count=int(request.get("count", 10)), seed=int(request.get("seed", 20260909))
    )
    return {"status": "ok", "mode": "augment", "count": len(variants), "instruments": variants}


def run_compile(request: dict[str, Any]) -> dict[str, Any]:
    """ETL stage: compile variants into pricing-request objects (``fina-etl.compile_pricing_requests``)."""
    from .etl import augment_termsheet, compile_pricing_request, validate_pricing_request

    variants = augment_termsheet(
        request.get("termsheet") or None, count=int(request.get("count", 10)), seed=int(request.get("seed", 20260909))
    )
    requests = [compile_pricing_request(v) for v in variants]
    if request.get("validate", True):
        for compiled in requests:
            validate_pricing_request(compiled)
    return {"status": "ok", "mode": "compile", "count": len(requests), "requests": requests}


def run_risk_batch(request: dict[str, Any]) -> dict[str, Any]:
    """Price a batch via the shared-path benchmark and persist the risk store."""
    instruments = int(request.get("instruments", 100))
    result = run_benchmark(
        instruments=instruments,
        underlyings=int(request.get("underlyings", max(4, instruments // 4))),
        paths=int(request.get("paths", 1000)),
        factors=int(request.get("factors", 12)),
        seed=int(request.get("seed", 20260909)),
        execution=request.get("execution"),
        greeks=request.get("greeks"),
        emit_rows=True,
    )
    benchmark = dict(result.get("benchmark", {}))
    rows = benchmark.pop("risk_rows", None) or []
    summary: dict[str, Any] = {
        "status": "ok",
        "mode": "batch",
        "instruments": instruments,
        "paths": int(request.get("paths", 1000)),
        "benchmark": benchmark,
    }
    if request.get("persist", True) and rows:
        summary["store"] = write_risk_store(
            [{"wide": rows, "long": []}],
            metadata={"source": "scheduler", "seed": int(request.get("seed", 20260909))},
        )
    return summary


def run_risk_benchmark(request: dict[str, Any]) -> dict[str, Any]:
    """Sizing benchmark of the shared-path risk engine (no store write, no risk rows).

    Same kernel and per-instrument risk scope as ``run_risk_batch``
    (``emit_rows=False``), so ``elapsed_seconds`` / ``instruments_per_second``
    reflect the production risk cost per instrument without the persistence
    overhead. Use to sweep instrument counts at a fixed path count.
    """
    instruments = max(1, int(request.get("instruments", 2000)))
    result = run_benchmark(
        instruments=instruments,
        underlyings=int(request.get("underlyings", max(4, instruments // 4))),
        paths=int(request.get("paths", 1000)),
        factors=int(request.get("factors", 12)),
        sensitivities=str(request.get("sensitivities", "delta")),
        pnl=str(request.get("pnl", "taylor1")),
        seed=int(request.get("seed", 20260909)),
        execution=request.get("execution"),
        greeks=request.get("greeks"),
        emit_rows=False,
    )
    benchmark = dict(result.get("benchmark", {}))
    benchmark.pop("risk_rows", None)
    return {"status": "ok", "mode": "benchmark", "benchmark": benchmark}


def run_risk_single(request: dict[str, Any]) -> dict[str, Any]:
    """Price one legacy term-sheet job (``Chunk.Jobs``) and return PV + sensitivities."""
    from .pricing import bump_result, common_from_job, load_legacy_request

    termsheet = request.get("pricing_request") or request.get("termsheet") or request
    if not isinstance(termsheet, dict) or "Chunk" not in termsheet:
        raise ValueError(
            "fina-risk.pricing_and_sensitivity expects a legacy request with Chunk.Jobs; "
            "use mode=batch for instrument batches"
        )
    jobs = load_legacy_request(termsheet)
    result = bump_result(common_from_job(jobs[0]), paths=request.get("paths"), seed=int(request.get("seed", 1729)))
    return {
        "status": "ok",
        "mode": "single",
        "base": result.get("base"),
        "sensitivities": result.get("sensitivities"),
        "risk_representation": result.get("risk_representation"),
    }


__all__ = [
    "run_augment",
    "run_compile",
    "run_risk_batch",
    "run_risk_benchmark",
    "run_risk_single",
    "run_risk_task",
]
