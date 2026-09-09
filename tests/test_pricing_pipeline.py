import json
from pathlib import Path

import numpy as np

from fina_risk.pricing import common_from_job, market_from_legacy, price_terminal_legs
from fina_risk.server import _execute

FIXTURE = Path(__file__).parents[1] / "skills/fina-risk/refs/termsheet1.md.json"


def test_fixture_pricing_put_and_leg_sign() -> None:
    result = _execute(
        "pricing_and_sensitivity", {"request": json.loads(FIXTURE.read_text()), "paths": 30000, "seed": 1729}
    )
    base = result["base"]
    assert abs(base["put_option_price"] - 0.02113) < 0.00015
    assert base["put_leg_pv"] < 0
    assert abs(sum(x["pv"] for x in base["legs"]) - base["valuation"]["pv"]) < 1e-12
    assert base["explainability"]["ki_monitoring"] == "EKI"
    assert base["explainability"]["physical_delivery"] is True
    assert base["explainability"]["memory_ko"] == [True, False]


def test_legacy_relative_bumps_use_quoted_spots_and_crn() -> None:
    result = _execute(
        "pricing_and_sensitivity", {"request": json.loads(FIXTURE.read_text()), "paths": 4000, "seed": 1729}
    )
    assert result["legacy_parity"]["common_random_numbers"] is True
    assert result["legacy_parity"]["spot_bump_convention"] == "1% relative"
    assert len(result["scenarios"]) == 5
    assert all(x["bump_mode"] == "relative" for x in result["sensitivities"])
    assert {x["legacy_data_index"] for x in result["sensitivities"]} == {0, 1}
    assert all("dollar_delta" in x for x in result["sensitivities"])
    equity_dollar_delta = sum(
        x["dollar_delta"] for x in result["sensitivities"] if x["risk_factor_id"].startswith("EQ:")
    )
    assert equity_dollar_delta < -8000.0


def test_compiler_exposes_all_three_legacy_legs() -> None:
    result = _execute("compile_trade", {"request": json.loads(FIXTURE.read_text())})
    assert [(x["leg_name"], x["multiplier"]) for x in result["legs"]] == [("PUT", -1), ("FUNDING", 1), ("COUPON", 1)]
    assert result["features"] == {"ki_monitoring": "EKI", "physical_delivery": True, "memory_ko": [True, False]}


def test_coupon_leg_uses_unpaid_counts_and_payment_lag() -> None:
    request = json.loads(FIXTURE.read_text())
    result = _execute("pricing_and_sensitivity", {"request": request, "paths": 30000, "seed": 1729})
    coupon = next(x["pv"] for x in result["base"]["legs"] if x["leg_name"] == "COUPON")
    detail = result["base"]["explainability"]["coupon"]
    assert 0.48 < coupon < 0.54
    assert len(detail["unpaid_periods"]) == 6
    assert sum(x["unpaid_fixings"] for x in detail["unpaid_periods"]) == 111
    assert [x["payment_lag_days"] for x in detail["unpaid_periods"]] == [2, 4, 2, 2, 2, 2]
    assert detail["quote_scale"] == 10.0


def test_fixture_reports_aad_boundary_and_taylor_pnl() -> None:
    result = _execute(
        "pricing_and_sensitivity", {"request": json.loads(FIXTURE.read_text()), "paths": 4000, "seed": 1729}
    )
    assert result["aad"]["available"] is True
    assert result["aad"]["engine"] == "QuantLib-Risks/XAD"
    assert abs(result["aad"]["value"] - result["base"]["put_option_price"]) < 1e-12
    assert result["taylor_decomposition"]["method"] == "AAD_PLUS_FD_RESIDUAL"
    assert all(x["method"] == "AAD_WITH_PATHWISE_TRANSITION_FALLBACK" for x in result["sensitivities"])
    assert result["base"]["explainability"]["risk_methods"]["put"]["aad_eligible"] is True
    assert result["base"]["explainability"]["risk_methods"]["funding"]["selected_method"] == "AAD"


def test_fixture_adapter_matches_shared_terminal_leg_kernel() -> None:
    request = json.loads(FIXTURE.read_text())
    common = common_from_job(request["Chunk"]["Jobs"][0])
    result = _execute("pricing_and_sensitivity", {"request": request, "paths": 4000, "seed": 1729})
    market = market_from_legacy(common, paths=4000, seed=1729)
    terminal = result["base"]["artifacts"]["path_cube"]["terminal"]
    coupon = next(x["pv"] for x in result["base"]["legs"] if x["leg_name"] == "COUPON")
    kernel = price_terminal_legs(
        np.asarray(terminal),
        market.quoted_spots,
        market.reference_spots,
        result["base"]["explainability"]["moneyness"],
        result["base"]["discount_factor"],
        coupon,
    )
    assert kernel["pricing_kernel"] == "price_terminal_legs.v1"
    assert kernel["valuation"]["pv"] == result["base"]["valuation"]["pv"]
