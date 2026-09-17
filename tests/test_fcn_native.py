from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import numpy as np
from jsonschema import Draft202012Validator

from fina_risk.fcn_native import price_fcn_request
from fina_risk.fcn_reference import price_fcn_reference
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
