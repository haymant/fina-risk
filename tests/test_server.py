import asyncio
import os
from typing import Any

from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

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
        "olap",
        "pipeline",
        "scheduler",
    }
    assert len(ALL_TOOLS) > 40


def test_key_placeholder_tools_registered() -> None:
    names = {name for name, _ in ALL_TOOLS}
    required_tools = (
        "compile_trade",
        "generate_risk_cube",
        "build_path_cube",
        "run_adjoint",
        "forecast_pnl",
        "olap_query",
        "write_risk_store",
        "ingest_instruments",
        "ingest_market_data",
        "trigger_pnl_forecast",
    )
    for required in required_tools:
        assert required in names


def test_compile_tool_returns_explicit_legacy_legs() -> None:
    async def _call() -> Any:
        return await mcp.call_tool("compile_trade", {"context": {"notional": 50000, "currency": "USD"}})

    result = asyncio.run(_call())
    assert "PUT" in str(result)
    assert "FUNDING" in str(result)
    assert "COUPON" in str(result)


def test_default_allowlist_includes_deployed_vercel_hosts() -> None:
    saved = os.environ.get("ALLOWED_HOSTS")
    os.environ.pop("ALLOWED_HOSTS", None)
    try:
        from fina_risk.server import _resolve_allowed_hosts

        hosts = _resolve_allowed_hosts()
    finally:
        if saved:
            os.environ["ALLOWED_HOSTS"] = saved
        else:
            os.environ.pop("ALLOWED_HOSTS", None)
    assert "fina-risk-zmrl.vercel.app" in hosts
    assert "fina-risk-zmrl.vercel.app:*" in hosts


def _fresh_app() -> Any:
    from fina_risk.server import _resolve_allowed_hosts

    server = FastMCP(
        "fina-risk-test",
        stateless_http=True,
        transport_security=TransportSecuritySettings(allowed_hosts=_resolve_allowed_hosts()),
    )
    return server.streamable_http_app()


async def _mcp_initialize(host: str) -> int:
    headers = {"Accept": "application/json, text/event-stream"}
    async with LifespanManager(_fresh_app()) as manager:
        transport = ASGITransport(app=manager.app)
        async with AsyncClient(transport=transport, base_url=f"http://{host}") as client:
            response = await client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "0"},
                    },
                    "method": "initialize",
                },
            )
            return response.status_code


def test_vercel_deployment_host_passes_host_security() -> None:
    status = asyncio.run(_mcp_initialize("fina-risk-zmrl.vercel.app"))
    assert status in (200, 406), f"/mcp rejected deployment host with {status}"


def test_unknown_host_still_rejected() -> None:
    status = asyncio.run(_mcp_initialize("attacker.example.com"))
    assert status == 421


def test_shared_store_tools_registered_and_configurable() -> None:
    """fina-risk exposes the same store_config/store_configure/store_resolve tools."""

    async def _call(name: str, args: dict[str, Any]) -> Any:
        return await mcp.call_tool(name, args)

    try:
        configured = asyncio.run(
            _call("store_configure", {"store": "local", "parquet_root": "/tmp/fina-risk-e2e", "hive_partitioning": "1"})
        )
        assert "store" in str(configured)

        resolved = asyncio.run(_call("store_resolve", {"table_name": "risk_wide"}))
        assert "risk_wide" in str(resolved)
        assert "/tmp/fina-risk-e2e" in str(resolved)

        config = asyncio.run(_call("store_config", {}))
        assert "partition_glob" in str(config)
    finally:
        asyncio.run(_call("store_configure", {"clear": True}))
