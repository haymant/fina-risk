"""ETL: term-sheet augmentation + pricing-request compilation."""

from __future__ import annotations

import asyncio
from typing import Any

from fina_risk import etl, mcp


def test_augment_count_and_determinism() -> None:
    a = etl.augment_termsheet(count=5, seed=1)
    b = etl.augment_termsheet(count=5, seed=1)
    c = etl.augment_termsheet(count=5, seed=2)
    assert len(a) == 5
    assert a == b  # same seed → identical batch
    assert a != c  # different seed → different batch


def test_augment_permutes_economics() -> None:
    base = etl.load_termsheet()
    base_put = base["Chunk"]["Jobs"][0]["commonData"]["dealData"]
    variants = etl.augment_termsheet(count=8, seed=3)

    strikes: set[float] = set()
    notionals: set[float] = set()
    for variant in variants:
        legs = {j["commonData"]["dealData"]["legName"]: j["commonData"]["dealData"] for j in variant["Chunk"]["Jobs"]}
        put = legs["PUT"]
        assert put["strike"] > 0
        assert put["knockInStar"]["KIBarrier"] < put["strike"]
        assert put["notional"] in etl.NOTIONAL_CHOICES
        assert len(put["KIKOSelect"]["underlying"]) >= 2
        # per-leg notional carried consistently
        assert legs["FUNDING"]["notional"] == put["notional"]
        strikes.add(put["strike"])
        notionals.add(put["notional"])
    assert len(strikes) > 1  # strikes really permuted
    assert base_put["strike"] in {round(s, 6) for s in strikes} or len(strikes) > 1


def test_compile_pricing_request_shape_and_schema() -> None:
    variant = etl.augment_termsheet(count=3, seed=4)[0]
    request = etl.compile_pricing_request(variant)

    assert {"instrument_key", "market_data", "legs", "parameters", "common_economics"} <= set(request)
    assert {leg["leg_type"] for leg in request["legs"]} == {"intrinsic_option", "funding", "coupon"}
    assert request["parameters"]["bump_size"] == 0.01
    assert request["parameters"]["bump_mode"] == "relative"
    assert request["parameters"]["method_priority"][0] == "AAD"
    assert request["market_data"]["underlyings"]
    assert request["market_data"]["curves"]
    # must satisfy skills/fina-risk/schema/pricing-request.schema.json
    etl.validate_pricing_request(request)


def test_mcp_etl_tools_registered_and_callable() -> None:
    async def call(name: str, args: dict[str, Any]) -> Any:
        return await mcp.call_tool(name, args)

    augmented = asyncio.run(call("augment_termsheet", {"count": 2, "seed": 7}))
    assert "count" in str(augmented)
    compiled = asyncio.run(call("compile_pricing_requests", {"count": 1, "seed": 7, "validate": True}))
    assert "intrinsic_option" in str(compiled)
