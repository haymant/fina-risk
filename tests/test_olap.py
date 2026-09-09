from pathlib import Path

from fina_risk.olap import link_view_state, query_ssrm, storage_status, write_risk_store


def _views() -> list[dict]:
    return [
        {
            "wide": [
                {
                    "portfolio_id": "P1",
                    "instrument_id": "I1",
                    "leg_id": "PUT",
                    "risk_factor_id": "SPOT:ADBE",
                    "delta": 2.0,
                    "delta_pnl": 5.0,
                    "base_pv": 10.0,
                    "risk_factor_key": "P1|I1|PUT|SPOT:ADBE|DELTA|",
                },
                {
                    "portfolio_id": "P2",
                    "instrument_id": "I2",
                    "leg_id": "PUT",
                    "risk_factor_id": "SPOT:AMZN",
                    "delta": -1.0,
                    "delta_pnl": -3.0,
                    "base_pv": 20.0,
                    "risk_factor_key": "P2|I2|PUT|SPOT:AMZN|DELTA|",
                },
            ],
            "long": [],
        }
    ]


def test_write_and_ssrm_query(tmp_path: Path) -> None:
    result = write_risk_store(_views(), root=tmp_path)
    assert result["datasets"]["risk_wide"] == 2
    response = query_ssrm(
        {"startRow": 0, "endRow": 1, "sortModel": [{"colId": "delta", "sort": "desc"}]},
        root=tmp_path,
    )
    assert response["success"] is True
    assert len(response["rows"]) == 1
    assert response["lastRow"] == -1
    assert response["rows"][0]["risk_factor_id"] == "SPOT:ADBE"


def test_ssrm_filter_group_and_pagination(tmp_path: Path) -> None:
    write_risk_store(_views(), root=tmp_path)
    response = query_ssrm(
        {
            "startRow": 0,
            "endRow": 10,
            "rowGroupCols": [{"id": "portfolio_id", "field": "portfolio_id"}],
            "valueCols": [{"field": "delta_pnl", "aggFunc": "sum"}],
            "filterModel": {"delta": {"filterType": "number", "type": "lessThan", "filter": 0}},
        },
        root=tmp_path,
    )
    assert response["rows"] == [{"portfolio_id": "P2", "delta_pnl": -3.0}]
    assert response["lastRow"] == 1


def test_linked_view_driver_and_status(tmp_path: Path) -> None:
    write_risk_store(_views(), root=tmp_path)
    state = link_view_state({"views": [{"id": "risk", "dataset": "risk_wide"}], "shared": {"portfolio_id": "P1"}})
    assert state["schema_version"] == "olap-view-link.v1"
    assert storage_status(root=tmp_path)["backend"] == "duckdb_arrow_parquet"
