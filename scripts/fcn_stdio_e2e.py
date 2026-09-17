#!/usr/bin/env python3
"""Exercise quote.price through a spawned stdio MCP server and native C++ lane."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from fina_risk.etl import compile_fcn_native_request

ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = ROOT.parent / "fina-skills"
DEFAULT_FIXTURE = ROOT / "skills/fina-risk/refs/termsheet1.md.json"
TERMSHEET_PUT_REFERENCE = 0.02112
TERMSHEET_PUT_TOLERANCE = 0.002


def _content(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    for item in getattr(result, "content", []):
        value = getattr(item, "text", None)
        if isinstance(value, str):
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
    raise RuntimeError("stdio MCP result did not contain a JSON object")


async def run(request: dict[str, Any]) -> dict[str, Any]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = f"{ROOT / 'src'}:{ROOT / 'cpp' / 'build'}"
    server = StdioServerParameters(command=str(ROOT / ".venv/bin/fina-risk-mcp"), env=environment)
    async with stdio_client(server) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            names = {tool.name for tool in (await session.list_tools()).tools}
            if "quote.price" not in names:
                raise RuntimeError("quote.price was not advertised by the stdio MCP server")
            result = await session.call_tool("quote.price", {"pricing_request": request, "process_id": "stdio-fcn-e2e"})
            if result.isError:
                raise RuntimeError(f"stdio MCP quote.price failed: {result.content!r}")
            return _content(result)


def _git_revision(path: Path) -> str:
    return subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()


def _validate_result(result: dict[str, Any]) -> None:
    schema = json.loads((SKILLS_ROOT / "schema/fcn-native-pricing-result.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(result)


def _manifest(result: dict[str, Any], paths: int, seed: int) -> dict[str, Any]:
    put_leg = next(item for item in result["legs"] if item["name"] == "PUT / Terminal Optionality")
    put_price = abs(float(put_leg["pv"]))
    return {
        "schema_version": "fina/evidence-manifest/v1",
        "run_id": str(uuid.uuid4()),
        "product_family": "fcn",
        "registry": {"path": "model-registry/fcn.yaml", "version": 1},
        "process": {
            "id": result["process_id"] or "stdio-fcn-e2e",
            "name": "quote.price stdio native FCN conformance",
            "state": "FINISHED",
            "graph": ["quote.price", "native_cpp_fcn_rakiplus"],
        },
        "source_revisions": {"fina-risk": _git_revision(ROOT), "fina-skills": _git_revision(SKILLS_ROOT)},
        "backend": {
            "kind": "native_cpp",
            "repository": "fina-risk",
            "module": "fina_risk_cpp",
            "callable": "price_fcn_rakiplus",
            "engine": result["engine_marker"],
            "native": True,
            "source_revision": result["source_revision"],
            "parity_reference": "fina-risk/benchmark/fcn-native-parity.md",
        },
        "market": {"calendar": "NYSE", "observation_style": "daily", "paths": paths, "seed": seed},
        "checks": {
            "canonical_pricing_request_valid": True,
            "stdio_mcp_initialized": True,
            "logical_quote_price_advertised": True,
            "native_cpp_quote_executed": result.get("native") is True,
            "native_engine_marker_verified": result["engine_marker"] == "cpp_fcn_rakiplus_v1",
            "leg_level_result_returned": [item["name"] for item in result["legs"]] == ["FUNDING", "COUPON", "PUT / Terminal Optionality"],
            "termsheet_put_close_to_reference": abs(put_price - TERMSHEET_PUT_REFERENCE) <= TERMSHEET_PUT_TOLERANCE,
            "result_schema_valid": True,
        },
        "results": {
            "pricing_engine": result["engine_marker"],
            "trade_status": "NOT_APPLICABLE",
            "events": ["quote.price.completed"],
            "olap": {"source": "not_applicable_for_quote_conformance"},
            "pv": result["pv"],
            "put_price": put_price,
            "put_reference": TERMSHEET_PUT_REFERENCE,
            "put_tolerance": TERMSHEET_PUT_TOLERANCE,
            "selected_branch": result["selected_branch"],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--termsheet", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--paths", type=int, default=2048)
    parser.add_argument("--out", type=Path, default=ROOT / "benchmark/fcn-stdio-e2e.json")
    parser.add_argument("--manifest", type=Path, default=ROOT / "benchmark/fcn-stdio-e2e.manifest.json")
    args = parser.parse_args()
    request = compile_fcn_native_request(args.termsheet)
    request["parameters"]["paths"] = args.paths
    result = asyncio.run(run(request))
    if result.get("engine_marker") != "cpp_fcn_rakiplus_v1" or result.get("status") != "ok" or not result.get("native"):
        raise RuntimeError(f"real native stdio evidence was not produced: {result}")
    put_price = abs(float(next(item for item in result["legs"] if item["name"] == "PUT / Terminal Optionality")["pv"]))
    if abs(put_price - TERMSHEET_PUT_REFERENCE) > TERMSHEET_PUT_TOLERANCE:
        raise RuntimeError(f"termsheet PUT {put_price:.8f} is outside {TERMSHEET_PUT_TOLERANCE:.6f} of {TERMSHEET_PUT_REFERENCE:.5f}")
    _validate_result(result)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    manifest = _manifest(result, args.paths, int(request["parameters"]["seed"]))
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    subprocess.run(
        [sys.executable, str(SKILLS_ROOT / "scripts/validate_model_registry.py"), "--manifest", str(args.manifest)], check=True
    )
    print(json.dumps({"engine_marker": result["engine_marker"], "pv": result["pv"], "put_price": put_price, "put_reference": TERMSHEET_PUT_REFERENCE, "put_tolerance": TERMSHEET_PUT_TOLERANCE, "out": str(args.out), "manifest": str(args.manifest)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
