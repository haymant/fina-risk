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
    # Lake-store correlation (ADBE-AMZN 0.4041) replaces the market-data value.
    assert abs(base["put_option_price"] - 0.02143) < 0.00015
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


def test_locvol_flat_surface_collapses_to_scalar() -> None:
    # A flat implied surface has dw/dT = sigma^2 and vanishing smile derivatives,
    # so the Dupire local vol must equal the constant implied vol pointwise.
    from fina_risk.locvol import LocalVolSurface

    flat = [[30.0, 30.0, 30.0, 30.0, 30.0] for _ in range(3)]
    surface = {"_id": "FLAT", "strike": [80.0, 90.0, 100.0, 110.0, 120.0],
               "maturity": [30.0, 182.0, 365.0], "vol": flat}
    lv = LocalVolSurface(surface, 0, 100.0, 0.0)
    assert np.allclose(lv.sigma(0.5, np.array([70.0, 100.0, 130.0])), 0.30, atol=1e-9)


def test_build_locvol_map_reads_fixture_surfaces_with_skew() -> None:
    from fina_risk.locvol import build_locvol_map

    md = json.loads(FIXTURE.read_text())["Chunk"]["Jobs"][0]["commonData"]["marketData"]
    lv = build_locvol_map(md, ["ADBE UW", "AMZN UW"], int(md["evaluationDate"]), 0.037405)
    assert set(lv) == {"ADBE UW", "AMZN UW"}
    for name, surface in lv.items():
        spot = next(float(e["spot"]) for e in md["equity"] if e["_id"] == name)
        downside, atm = surface.sigma(0.5, np.array([spot * 0.7, spot]))
        assert downside > atm  # equity skew: higher vol on the downside wing


def test_locvol_lane_reprices_fixture_differently_from_scalar() -> None:
    request = json.loads(FIXTURE.read_text())
    scalar = _execute("pricing_and_sensitivity", {"request": request, "paths": 4000, "seed": 1729})["base"]
    locvol = _execute(
        "pricing_and_sensitivity", {"request": request, "paths": 4000, "seed": 1729, "locvol": True}
    )["base"]
    assert scalar["explainability"]["model"] == "correlated_gbm_terminal_reference_cpu"
    assert locvol["explainability"]["model"] == "dupire_locvol_terminal_reference_cpu"
    # Dupire sigma at the conservative wing is above the flat ATM scalar, so the
    # short worst-of put is worth more under local vol.
    assert locvol["put_option_price"] > scalar["put_option_price"]
