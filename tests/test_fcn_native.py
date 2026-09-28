from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from mcp.server.fastmcp.exceptions import ToolError
from jsonschema import Draft202012Validator

from fina_risk.fcn_native import price_fcn_request
from fina_risk.fcn_reference import price_fcn_reference
from fina_risk.pricing import common_from_job, market_from_legacy
from fina_risk.server import mcp


def _request(*, local: bool = False, global_ko: bool = False, memory: bool = True) -> dict[str, Any]:
    return {
        "instrument_key": "FCN-UNIT-1",
        "request_id": "request-unit-1",
        "process_id": "process-unit-1",
        "market_data": {
            "evaluation_date": 1,
            "underlyings": [
                {"id": "U1", "quoted_spot": 100.0, "reference_spot": 100.0, "currency": "USD"},
                {"id": "U2", "quoted_spot": 100.0, "reference_spot": 100.0, "currency": "USD"},
            ],
            "curves": [{"id": "USD", "day_count": "Actual/365", "pillars": [{"date": 3, "rate": 0.0}]}],
            "vol_surfaces": [],
            "correlations": [],
        },
        "parameters": {"bump_size": 0.01, "bump_mode": "relative", "method_priority": ["FD"], "seed": 42, "paths": 4},
        "common_economics": {"notional": 100.0, "payment_currency": "USD"},
        "legs": [
            {"leg_id": "put", "leg_type": "intrinsic_option", "multiplier": -1.0, "notional": 100.0,
             "payoff": {"strike": 0.78, "knock_in": {"barrier": 0.70, "operator": "<="}, "settlement": "physical_delivery"}},
            {"leg_id": "funding", "leg_type": "funding", "multiplier": 1.0, "notional": 100.0,
             "payoff": {"notional_return": True, "return_ratio": 1.0}},
            {"leg_id": "coupon", "leg_type": "coupon", "multiplier": 1.0, "notional": 100.0, "payoff": {}},
        ],
        "fcn_terms": {
            "currency": "USD",
            "notional": 100.0,
            "coupon_quote_scale": 1.0,
            "final_fixing_date": 3,
            "maturity_date": 3,
            "performance_indicator": "worst_of",
            "coupon_memory": memory,
            "physical_delivery": True,
            "range_lower_inclusive": True,
            "range_upper_inclusive": True,
            "barriers": {
                "local_enabled": local,
                "global_enabled": global_ko,
                "memory_ko": False,
                "local_barrier": 1.10,
                "global_barrier": 1.10,
                "local_operator": ">=",
                "global_operator": ">=",
                "same_day_ko_precedence": "global",
            },
            "coupon_periods": [
                {"id": "p1", "start_date": 1, "end_date": 2, "payment_date": 2, "range_rate": 0.10,
                 "fixed_coupon": 0.01, "lower_bound": 0.80, "upper_bound": 1.00, "already_paid_fixings": 0, "total_fixings": 1},
                {"id": "p2", "start_date": 2, "end_date": 3, "payment_date": 3, "range_rate": 0.10,
                 "fixed_coupon": 0.01, "lower_bound": 0.80, "upper_bound": 1.00, "already_paid_fixings": 0, "total_fixings": 1},
            ],
        },
    }


def _cube() -> np.ndarray:
    # Path 0: ordinary in-range coupon. Path 1: out-of-range then a memory release.
    # Path 2: final KI, no KO, physical delivery. Path 3: same-day local/global KO.
    return np.asarray(
        [
            [[100.0, 100.0], [100.0, 100.0], [100.0, 100.0]],
            [[100.0, 100.0], [70.0, 70.0], [100.0, 100.0]],
            [[100.0, 100.0], [80.0, 80.0], [65.0, 65.0]],
            [[100.0, 100.0], [120.0, 120.0], [120.0, 120.0]],
        ],
        dtype=np.float64,
    )


def _leg(result: dict[str, Any], name: str) -> dict[str, Any]:
    return next(item for item in result["legs"] if item["name"] == name)


def test_native_fcn_coupon_memory_boundaries_and_physical_delivery() -> None:
    result = price_fcn_request(_request(), path_cube=_cube(), dates=np.asarray([1, 2, 3], dtype=np.int32))
    schema_path = Path(__file__).resolve().parents[2] / "fina-skills/schema/fcn-native-pricing-result.schema.json"
    Draft202012Validator(json.loads(schema_path.read_text())).validate(result)
    assert result["status"] == "ok"
    assert result["engine_marker"] == "cpp_fcn_rakiplus_v1"
    assert result["evidence_status"] == "implemented_and_evidenced"
    assert [item["name"] for item in result["legs"]] == ["FUNDING", "COUPON", "PUT / Terminal Optionality"]
    assert _leg(result, "FUNDING")["pv"] > 0.0
    assert _leg(result, "COUPON")["pv"] > 0.0
    assert _leg(result, "PUT / Terminal Optionality")["pv"] < 0.0
    assert len(result["delta"]) == 2
    assert len(result["gamma"]) == 2
    assert len(result["vega"]) == 2
    assert any(value != 0.0 for value in result["delta"])
    assert any(item["physical_delivery"] for item in result["cashflows"] if item["leg"] == "PUT / Terminal Optionality")
    assert result["ki_probability"] == 0.25


def test_same_day_global_ko_precedence_and_local_ko() -> None:
    result = price_fcn_request(_request(local=True, global_ko=True), path_cube=_cube(), dates=np.asarray([1, 2, 3], dtype=np.int32))
    assert result["status"] == "ok"
    assert result["ko_probability"] == 0.25
    assert result["selected_branch"] == "global_ko"
    assert any(item["transition"] == "global_ko" for item in result["state_transitions"])

    local = price_fcn_request(_request(local=True), path_cube=_cube(), dates=np.asarray([1, 2, 3], dtype=np.int32))
    assert local["selected_branch"] == "local_ko"


def test_explicit_per_underlying_memory_ko_locks() -> None:
    request = _request(global_ko=True)
    request["fcn_terms"]["memory_ko"] = True
    request["fcn_terms"]["memory_ko_mode"] = "per_underlying_ever"
    request["fcn_terms"]["barriers"]["memory_ko"] = True
    # Each asset locks on a different date. This branch is accepted only because
    # the request records the per-underlying-ever convention explicitly.
    cube = np.asarray([[[120.0, 100.0], [100.0, 120.0], [100.0, 100.0]]], dtype=np.float64)
    result = price_fcn_request(request, path_cube=cube, dates=np.asarray([1, 2, 3], dtype=np.int32))
    assert result["status"] == "ok"
    assert result["ko_probability"] == 1.0
    assert result["selected_branch"] == "global_ko"


def test_native_lane_matches_readable_reference_oracle() -> None:
    request = _request(local=True, global_ko=True)
    dates = np.asarray([1, 2, 3], dtype=np.int32)
    native = price_fcn_request(request, path_cube=_cube(), dates=dates)
    oracle = price_fcn_reference(request, _cube(), dates)
    assert native["pv"] == oracle["pv"]
    assert native["ki_probability"] == oracle["ki_probability"]
    assert native["ko_probability"] == oracle["ko_probability"]
    assert native["selected_branch"] == oracle["selected_branch"]
    for name, expected in oracle["legs"].items():
        assert _leg(native, name)["pv"] == expected


def test_missing_fixing_and_ambiguous_memory_ko_are_explicit() -> None:
    invalid = _cube()
    invalid[0, 1, 0] = np.nan
    result = price_fcn_request(_request(), path_cube=invalid, dates=np.asarray([1, 2, 3], dtype=np.int32))
    schema_path = Path(__file__).resolve().parents[2] / "fina-skills/schema/fcn-native-pricing-result.schema.json"
    Draft202012Validator(json.loads(schema_path.read_text())).validate(result)
    assert result["status"] == "unresolved"
    assert "missing or invalid fixing" in result["unsupported_reason"]

    ambiguous = _request()
    ambiguous["fcn_terms"]["memory_ko"] = True
    try:
        price_fcn_request(ambiguous, path_cube=_cube(), dates=np.asarray([1, 2, 3], dtype=np.int32))
    except ValueError as exc:
        assert "memory_ko_mode" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("ambiguous memory KO was accepted")


def test_stdio_mcp_quote_price_uses_native_engine() -> None:
    async def invoke() -> Any:
        return await mcp.call_tool("quote.price", {"pricing_request": _request(), "process_id": "mcp-process-1"})

    value = asyncio.run(invoke())
    text = str(value)
    assert "cpp_fcn_rakiplus_v1" in text
    assert "python_mirror" not in text


def test_quote_price_rejects_legacy_noncanonical_payload() -> None:
    async def invoke() -> Any:
        return await mcp.call_tool("quote.price", {"pricing_request": {"chunk": {}}, "process_id": "contract-test"})

    with pytest.raises(ToolError, match="requires canonical fina-risk pricing-request fields"):
        asyncio.run(invoke())


def test_missing_discount_curve_is_explicit() -> None:
    """A note with no discount curve must not be priced, not priced at 0%.

    ``curve_rate_at`` falls back to ``terms.rate`` when there are no pillars, and
    ``terms.rate`` is left at 0.0 when the curve is absent. Every coupon, funding
    and put cashflow was therefore discounted at zero and the note came out at
    its undiscounted value -- a plausible-looking PV for a note nobody could
    sell. The refusal belongs with the missing-fixing one: same class of gap,
    we do not have the data, so we must not emit a number.

    Two layers catch this, and both are exercised: the request schema requires
    ``market_data.curves`` to be present, and the C++ engine refuses a curve that
    is present but carries no usable pillar.
    """
    cube, dates = _cube(), np.asarray([1, 2, 3], dtype=np.int32)

    # Baseline: the same request WITH a curve prices.
    priced = price_fcn_request(_request(), path_cube=cube, dates=dates)
    assert priced["status"] == "ok", priced.get("unsupported_reason")

    # Layer 1: omitting curves entirely is a schema violation, not a silent zero.
    without_curves = _request()
    without_curves["market_data"].pop("curves", None)
    with pytest.raises(Exception, match="(?i)curves"):
        price_fcn_request(without_curves, path_cube=cube, dates=dates)

    # Layer 2: a curve that is present but unusable reaches the engine, which
    # must refuse rather than discount at zero.
    for label, curves in (
        ("empty curves list", []),
        ("no pillars", [{"id": "USD", "day_count": "Actual/365", "pillars": []}]),
        ("pillars carry no date", [{"id": "USD", "day_count": "Actual/365", "pillars": [{"rate": 0.05}]}]),
        ("pillars carry no rate", [{"id": "USD", "day_count": "Actual/365", "pillars": [{"date": 3}]}]),
    ):
        request = _request()
        request["market_data"]["curves"] = curves
        result = price_fcn_request(request, path_cube=cube, dates=dates)
        assert result["status"] == "unresolved", f"{label}: got {result['status']} {result.get('unsupported_reason')}"
        assert "discount curve" in result["unsupported_reason"], f"{label}: {result['unsupported_reason']}"
        # Crucially, no number: a zero-PV "answer" is what this is refusing to be.
        assert result["pv"] == 0.0
        assert result["legs"] == []

    # A curve whose rates are legitimately zero is NOT the same as no curve, and
    # must still price. The unit fixture uses a 0% curve precisely to check this.
    zero_rate = _request()
    zero_rate["market_data"]["curves"] = [
        {"id": "USD", "day_count": "Actual/365", "pillars": [{"date": 3, "rate": 0.0}]}
    ]
    assert price_fcn_request(zero_rate, path_cube=cube, dates=dates)["status"] == "ok"

    # The Python cube builder refuses the same case, so the two lanes cannot
    # disagree about whether a curve-less request is priceable.
    from fina_risk.fcn_native import build_daily_path_cube

    with pytest.raises(ValueError, match="no discount curve"):
        build_daily_path_cube(_request() | {"market_data": {**_request()["market_data"],
                                                           "curves": [{"id": "USD", "day_count": "Actual/365", "pillars": []}]}})


def test_basket_wider_than_two_names_is_refused_not_truncated() -> None:
    """Three names must be an error, not a two-name basket priced as if it existed.

    ``market_from_legacy`` sliced ``equities[:2]``, and the path cube was built
    from two correlated Brownian columns, so a three-name deal was accepted and
    priced with the third underlying silently absent. Refusing is the honest
    answer: widening the lane to N names is a product change, not a fix.
    """
    fixture = Path(__file__).parents[1] / "skills/fina-risk/refs/termsheet1.md.json"
    common = common_from_job(json.loads(fixture.read_text())["Chunk"]["Jobs"][0])
    assert len(common["dealData"]["instrument"]["underlyings"]) == 2, "fixture should be a two-name basket"

    third = {"_id": "TEST3", "spot": 50.0, "reference": 50.0}
    widened_market = {**common, "marketData": {**common["marketData"], "equity": [*common["marketData"]["equity"], third]}}
    with pytest.raises(ValueError, match="two-name basket only"):
        market_from_legacy(widened_market, paths=8, seed=1729)

    widened_deal = {
        **common,
        "dealData": {
            **common["dealData"],
            "instrument": {**common["dealData"]["instrument"], "underlyings": [*common["dealData"]["instrument"]["underlyings"], {"spot": 50.0}]},
        },
    }
    with pytest.raises(ValueError, match="two-name basket only"):
        market_from_legacy(widened_deal, paths=8, seed=1729)
