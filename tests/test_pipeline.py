import json
from pathlib import Path

from fina_risk.olap import query_ssrm
from fina_risk.pipeline import (
    dashboard_metadata,
    ingest_instruments,
    ingest_market_data,
    plan_pnl_forecast,
    risk_metadata,
    trigger_pnl_forecast,
)

ROOT = Path(__file__).resolve().parents[1]


def test_sample_journey_fixture_and_benchmark_inputs(tmp_path: Path) -> None:
    instruments = json.loads((ROOT / "benchmark/instruments.json").read_text())
    market = json.loads((ROOT / "benchmark/market.json").read_text())
    ingested_i = ingest_instruments(instruments, root=tmp_path)
    ingested_m = ingest_market_data(market, root=tmp_path)
    assert ingested_i["rows"] == 2000
    assert ingested_m["rows"] == 1200
    assert risk_metadata()["risk_factor_key"].startswith("portfolio_id")
    assert dashboard_metadata()["views"]
    plan = plan_pnl_forecast({"instruments": 2000, "paths": 30000, "sensitivities": ["delta", "gamma"]})
    assert plan["execution"]["structure_reuse"] is True

    fixture = json.loads((ROOT / "skills/fina-risk/refs/termsheet1.md.json").read_text())
    result = trigger_pnl_forecast({"request": fixture, "paths": 100, "root": tmp_path})
    assert result["status"] == "completed"
    assert result["trade_count"] == 3
    assert result["store"]["datasets"]["risk_wide"] > 0

    query = query_ssrm(
        {"startRow": 0, "endRow": 10, "valueCols": [{"field": "total_taylor_pnl", "aggFunc": "sum"}]},
        root=tmp_path,
    )
    assert query["success"] is True
    assert query["rows"]
