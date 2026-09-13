"""fina-risk scheduler adapter + MCP tool."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from fina_risk import mcp, scheduler_adapter


def test_run_risk_task_batch_small() -> None:
    out = scheduler_adapter.run_risk_task(
        {"mode": "batch", "instruments": 6, "underlyings": 4, "paths": 64, "factors": 4, "seed": 1, "persist": False}
    )
    assert out["status"] == "ok"
    assert out["mode"] == "batch"
    assert out["instruments"] == 6
    assert "benchmark" in out


def test_run_risk_benchmark_mode() -> None:
    out = scheduler_adapter.run_risk_task(
        {"mode": "benchmark", "instruments": 8, "underlyings": 4, "paths": 256, "factors": 4, "seed": 1}
    )
    assert out["status"] == "ok"
    assert out["mode"] == "benchmark"
    b = out["benchmark"]
    assert b["requested_instruments"] == 8
    assert b["paths"] == 256
    assert b["elapsed_seconds"] > 0
    assert b["instruments_per_second"] > 0
    assert "risk_rows" not in b


def test_run_risk_single_requires_legacy_request() -> None:
    with pytest.raises(ValueError, match="Chunk.Jobs"):
        scheduler_adapter.run_risk_task({"mode": "single", "pricing_request": {"legs": []}})


def test_run_risk_task_mcp_tool() -> None:
    async def call() -> Any:
        return await mcp.call_tool(
            "run_risk_task",
            {
                "mode": "batch",
                "instruments": 4,
                "underlyings": 4,
                "paths": 32,
                "factors": 4,
                "seed": 1,
                "persist": False,
            },
        )

    result = asyncio.run(call())
    assert "batch" in str(result)


def test_run_etl_augment_and_compile_modes() -> None:
    augmented = scheduler_adapter.run_risk_task({"mode": "augment", "count": 3, "seed": 1})
    assert augmented["mode"] == "augment"
    assert augmented["count"] == 3 and len(augmented["instruments"]) == 3

    compiled = scheduler_adapter.run_risk_task({"mode": "compile", "count": 2, "seed": 1, "validate": True})
    assert compiled["count"] == 2
    assert compiled["requests"][0]["parameters"]["bump_size"] == 0.01


def test_run_etl_task_mcp_tool() -> None:
    async def call() -> Any:
        return await mcp.call_tool("run_etl_task", {"mode": "augment", "count": 2, "seed": 5})

    assert "augment" in str(asyncio.run(call()))
