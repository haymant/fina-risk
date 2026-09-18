from __future__ import annotations

import pytest

from fina_risk import riskcube
from fina_risk.riskcube_reports import _field, _instrument_id


def test_slice_and_report_lifecycle_uses_incremental_version_ids(tmp_path, monkeypatch):
    monkeypatch.setenv("FINA_RISKCUBE_METADATA_ROOT", str(tmp_path))
    riskcube.create_slice({"slice_key": "all-fcn", "name": "All FCN", "instrument_filter": {"product_type": "FCN"}})
    first = riskcube.create_report(
        {
            "slice_key": "all-fcn",
            "report_name": "all-fcn-sensi",
            "evaluation_date": "2026-09-17",
            "market_data_datetime": "2026-09-17T16:00:00Z",
            "configuration_group": "sensitivity-pnl-taylor",
            "report_kinds": ["risk", "pnl", "taylor"],
        }
    )
    second = riskcube.create_report(
        {
            "slice_key": "all-fcn",
            "report_name": "all-fcn-sensi",
            "configuration_group": "sensitivity",
            "report_kinds": ["risk"],
        }
    )
    assert second["version_id"] == first["version_id"] + 1
    assert first["report_key"].startswith("all-fcn-sensi-")


def test_generated_values_are_not_metadata_json(tmp_path, monkeypatch):
    monkeypatch.setenv("FINA_RISKCUBE_METADATA_ROOT", str(tmp_path))
    with pytest.raises(ValueError, match="Parquet"):
        riskcube.create_slice({"slice_key": "bad", "delta": 1.0})


def test_slice_condition_value_is_metadata_input(tmp_path, monkeypatch):
    monkeypatch.setenv("FINA_RISKCUBE_METADATA_ROOT", str(tmp_path))
    record = riskcube.create_slice(
        {
            "slice_key": "fcn-only",
            "conditions": [{"field": "product_type", "op": "eq", "value": "FCN"}],
        }
    )
    assert record["conditions"][0]["value"] == "FCN"


def test_scenario_manipulation_value_is_metadata_input(tmp_path, monkeypatch):
    monkeypatch.setenv("FINA_RISKCUBE_METADATA_ROOT", str(tmp_path))
    record = riskcube.create_scenario(
        {
            "scenario_key": "spot-shift",
            "market_data_manipulations": [
                {
                    "type": "spot",
                    "underlying_name": "AAPL",
                    "shift_type": "relative",
                    "value": 0.05,
                }
            ],
        }
    )
    assert record["market_data_manipulations"][0]["value"] == 0.05


def test_configuration_group_rejects_unselected_report_kind(tmp_path, monkeypatch):
    monkeypatch.setenv("FINA_RISKCUBE_METADATA_ROOT", str(tmp_path))
    with pytest.raises(ValueError, match="does not include"):
        riskcube.create_report({"slice_key": "s", "configuration_group": "sensitivity", "report_kinds": ["forecast"]})


def test_query_requires_ready_report(tmp_path, monkeypatch):
    monkeypatch.setenv("FINA_RISKCUBE_METADATA_ROOT", str(tmp_path))
    report = riskcube.create_report({"slice_key": "s", "configuration_group": "sensitivity", "report_kinds": ["risk"]})
    with pytest.raises(ValueError, match="not ready"):
        riskcube.query_report(report["report_key"], {"startRow": 0, "endRow": 10})


def test_canonical_slice_payload_preserves_report_dimensions():
    request = {
        "instrument_key": "FCN-AAPL-001",
        "fcn_terms": {"currency": "USD", "notional": 100000.0},
        "parameters": {},
    }

    assert _instrument_id(request, 0) == "FCN-AAPL-001"
    assert _field(request, "FCN-AAPL-001", "name") == "FCN-AAPL-001"
    assert _field(request, "FCN-AAPL-001", "product_type") == "FCN"
    assert _field(request, "FCN-AAPL-001", "currency") == "USD"
    assert _field(request, "FCN-AAPL-001", "notional") == 100000.0
