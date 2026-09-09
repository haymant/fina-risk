import json
from pathlib import Path

from fina_risk.server import _execute

FIXTURE = Path(__file__).parents[1] / "skills/fina-risk/refs/termsheet1.md.json"


def test_fixture_emits_wide_and_long_risk_views() -> None:
    result = _execute(
        "pricing_and_sensitivity", {"request": json.loads(FIXTURE.read_text()), "paths": 4000, "seed": 1729}
    )
    view = result["risk_representation"]
    assert view["schema_version"] == "risk-view.v1"
    assert {row["risk_factor_id"] for row in view["wide"]} >= {"EQ:ADBE UW:SPOT", "EQ:AMZN UW:SPOT"}
    assert len(view["long"]) >= 4
    adbe = next(row for row in view["wide"] if row["risk_factor_id"] == "EQ:ADBE UW:SPOT")
    assert adbe["delta_dollar"] < 0
    assert adbe["selected_method"] == "AAD_WITH_PATHWISE_TRANSITION_FALLBACK"
    assert adbe["cross_check_method"] == "CRN_FD"
    assert "spot_shock" in adbe
    assert "delta_pnl" in adbe


def test_aggregate_risk_returns_factor_view() -> None:
    request = json.loads(FIXTURE.read_text())
    result = _execute("aggregate_portfolio", {"request": request, "paths": 1000, "seed": 1729})
    assert result["trade_count"] == 3
    assert result["risk_representation"]["summary"]["row_count"] == 2
    assert len(result["wide"]) == 2
    assert len(result["long"]) >= 4
