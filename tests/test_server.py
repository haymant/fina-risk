import asyncio

from fina_risk import ALL_TOOLS, TOOLS, mcp


def test_tool_specs_cover_skill_groups() -> None:
    assert set(TOOLS) == {
        "market",
        "universe",
        "compiler",
        "quantlib",
        "simulation",
        "gpu",
        "state",
        "payoff",
        "aad",
        "risk",
        "pnl",
        "portfolio",
        "scheduler",
    }
    assert len(ALL_TOOLS) > 40


def test_key_placeholder_tools_registered() -> None:
    names = {name for name, _ in ALL_TOOLS}
    for required in ("compile_trade", "generate_risk_cube", "build_path_cube", "run_adjoint", "forecast_pnl"):
        assert required in names


def test_placeholder_tool_returns_to_be_done() -> None:
    async def _call() -> dict:
        return await mcp.call_tool("compile_trade", {"context": {"notional": 50000, "currency": "USD"}})

    result = asyncio.run(_call())
    assert "to be done" in str(result).lower()