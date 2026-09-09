from pathlib import Path

from fina_risk.data import load_json_source


def test_zip_member_json_loader() -> None:
    archive = Path(__file__).parents[1] / "benchmark/fina-risk-benchmark.zip"
    if not archive.exists():
        return
    instruments = load_json_source(f"{archive}/instruments.json")
    market = load_json_source(f"{archive}/market.json")
    assert instruments["count"] == 2000
    assert len(market["underlyings"]) == 1200
