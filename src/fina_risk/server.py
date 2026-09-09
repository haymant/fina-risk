"""fina-risk MCP orchestration skeleton.

Placeholder tools for the 13 fina-risk tool groups defined in
``skills/fina-risk/SKILL.md``. Every tool returns ``{"status": "to be done"}``
until the corresponding backend is implemented. The server mirrors the
fina-pricer deployment practice: MCP stdio local, Streamable HTTP locally via
uvicorn, and the same ASGI app behind ``api/index.py`` on Vercel.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

# Tool groups -> (tool name, help text). Mirrors skills/fina-risk/SKILL.md.
TOOLS: dict[str, list[tuple[str, str]]] = {
    "market": [
        ("load_market", "Load a market snapshot into the curve/vol/correlation stores."),
        ("store_market", "Store a market snapshot under a market version."),
        ("diff_market", "Diff two market snapshots by their risk factor ids."),
        ("freeze_market", "Freeze a market version as immutable for risk runs."),
        ("validate_market", "Validate a market snapshot against the market schemas."),
    ],
    "universe": [
        ("discover_universes", "Discover trade / risk / simulation universes from the catalog."),
        ("merge_universes", "Merge multiple universes into one simulation universe."),
        ("split_universe", "Split a universe by slice, currency, or underlyings."),
        ("estimate_cost", "Estimate path-cube cost (paths x steps x factors x precision)."),
    ],
    "compiler": [
        ("compile_trade", "Compile a trade JSON into a canonical CompiledTrade with legs and payoff graph."),
        ("compile_portfolio", "Compile a portfolio into a set of CompiledTrades plus shared underlyings."),
    ],
    "quantlib": [
        ("build_quantlib_market", "Build QuantLib market handles from a market snapshot."),
        ("build_processes", "Build QuantLib stochastic processes from model definitions."),
        ("build_schedules", "Build QuantLib schedules from observation/generation dates."),
        ("build_payoffs", "Build QuantLib payoff objects for the compiled payoff graph."),
    ],
    "simulation": [
        ("build_time_grid", "Build the simulation time grid from observation schedules."),
        ("build_path_cube", "Build a shared path cube over the simulation universe."),
        ("reuse_path_cube", "Reuse an existing path cube for path reuse."),
        ("inspect_path_cube", "Inspect path cube metadata, precision, and storage backend."),
    ],
    "gpu": [
        ("gpu_status", "Report GPU availability and memory metrics."),
        ("gpu_allocate", "Allocate a GPU buffer for a path cube."),
        ("gpu_release", "Release a GPU buffer."),
        ("tune_batch_size", "Tune batch sizing for GPU kernels."),
        ("kernel_profile", "Profile a simulation/payoff kernel."),
    ],
    "state": [
        ("build_state_cube", "Build the state cube (memory/KO/KI/coupon/autocall) over a path cube."),
        ("update_state_cube", "Update state cube for realized fixings and lifecycle events."),
        ("inspect_state_cube", "Inspect state cube dimensions and per-path states."),
    ],
    "payoff": [
        ("compile_payoff_graph", "Compile a payoff graph from the compiled trade."),
        ("inspect_payoff_graph", "Inspect payoff graph nodes and edges."),
        ("evaluate_payoff_graph", "Evaluate the payoff graph over a state cube."),
    ],
    "aad": [
        ("build_aad_graph", "Build the AAD valuation graph and tape for a trade."),
        ("check_aad_eligibility", "Check smooth-payoff AAD eligibility per risk factor."),
        ("run_adjoint", "Run reverse-mode adjoint and emit per-RFK gradients."),
        ("inspect_gradient", "Inspect stored gradients with method and fallback metadata."),
    ],
    "risk": [
        ("generate_risk_cube", "Generate the risk cube with AAD-first method selection."),
        ("aggregate_risk", "Aggregate risk cells across trades, legs, and features."),
        ("generate_greeks", "Generate the full Greek vector per risk factor."),
        ("compare_methods", "Compare AAD / pathwise / likelihood / FD results."),
    ],
    "pnl": [
        ("forecast_pnl", "Forecast P&L from risk cube and scenario definitions."),
        ("explain_pnl", "Explain realized P&L with a taylor decomposition."),
        ("taylor_decomposition", "Decompose P&L change into delta/gamma/theta contributions."),
    ],
    "portfolio": [
        ("aggregate_portfolio", "Aggregate portfolio PV and net sensitivities."),
        ("net_sensitivities", "Net sensitivities across trades by risk factor."),
        ("portfolio_scenarios", "Run portfolio-level scenario shocks."),
    ],
    "scheduler": [
        ("submit_job", "Submit a risk-cube or batch-pricing job."),
        ("cancel_job", "Cancel a running job."),
        ("rebalance_job", "Rebalance a job across workers."),
        ("inspect_job", "Inspect job status from the warm Redis cache."),
    ],
}

ALL_TOOLS = [(name, help_text) for group in TOOLS.values() for name, help_text in group]


def _load_local_env() -> None:
    """Load a gitignored .env.local file for local development (Vercel env is authoritative)."""
    env = Path(__file__).resolve().parent.parent.parent / ".env.local"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_local_env()


def _resolve_allowed_hosts() -> list[str]:
    # Default follows the fina-pricer fix for the MCP 421 "Invalid Host header":
    # the mcp matcher only supports exact host matches plus "host:*" port wildcards
    # (no subdomain wildcards), so every deployed Vercel hostname must be listed.
    # Set ALLOWED_HOSTS in the deployment environment to override this default.
    default = (
        "localhost,127.0.0.1,[::1],localhost:*,127.0.0.1:*,[::1]:*,"
        "fina-risk.vercel.app,fina-risk.vercel.app:*,"
        "fina-risk-zmrl.vercel.app,fina-risk-zmrl.vercel.app:*"
    )
    return [host.strip() for host in os.getenv("ALLOWED_HOSTS", default).split(",") if host.strip()]


mcp = FastMCP(
    "fina-risk",
    stateless_http=True,
    transport_security=TransportSecuritySettings(allowed_hosts=_resolve_allowed_hosts()),
)

_TO_DO = "to be done"


def _register_tools() -> None:
    for name, description in ALL_TOOLS:

        def _todo(context: dict[str, Any] | None = None, *, tool_name: str = name) -> dict[str, Any]:
            return {"status": _TO_DO, "tool": tool_name}

        _todo.__name__ = name
        _todo.__doc__ = description
        mcp.tool(name=name, description=description)(_todo)


_register_tools()


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_request: Any) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "fina-risk", "tools": len(ALL_TOOLS)})


app = mcp.streamable_http_app()


@mcp.prompt()
def fina_risk_guidance() -> str:
    return (
        "fina-risk orchestrates pricing, risk-cube generation, shared path simulation, and portfolio risk through "
        "MCP tools. Every tool is currently a placeholder and returns {\"status\": \"to be done\"}; orchestration and "
        "the wire schemas are defined in skills/fina-risk/SKILL.md, schema/ (hot/warm/cold JSON Schemas), and "
        "refs/termsheet1.md(.json) for validation against the legacy engine. The executable reference pricing "
        "engine is the fina-pricer riskcube MCP server; the trade repository boundary is modules/fina-trade."
    )


@mcp.prompt()
def fina_risk_execution() -> str:
    return (
        "To make fina-risk executable end-to-end, validate the bundled term-sheet fixture (refs/termsheet1.md.json) "
        "against fina-pricer pricing_and_sensitivity, then replace the to-be-done placeholders group by group in "
        "AAD-first order (market, universe, compiler, quantlib, simulation, gpu, state, payoff, aad, risk, pnl, "
        "portfolio, scheduler). Deploy the Streamable HTTP app at /mcp with /healthz readiness; warm metadata in "
        "Redis, cold artifacts as Parquet on GCS per the fina-pricer setup (S3_API_KEY/S3_API_SECRET/S3_BUCKET_NAME, "
        "ALLOWED_HOSTS)."
    )


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()