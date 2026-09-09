from fina_risk.server import _execute


def test_benchmark_tool_uses_shared_kernel() -> None:
    result = _execute(
        "benchmark_portfolio",
        {
            "instruments": 100,
            "underlyings": 120,
            "paths": 1000,
            "factors": 8,
            "sensitivities": "delta",
            "pnl": "taylor1",
        },
    )
    benchmark = result["benchmark"]
    assert benchmark["pricing_kernel"] == "price_terminal_legs.v1"
    assert benchmark["requested_instruments"] == 100
    assert benchmark["shared_path_cube"] is True
    assert benchmark["sensitivity_checksum"] is not None
    assert benchmark["taylor_pnl_checksum"] is not None


def test_benchmark_reports_requested_greek_scopes() -> None:
    requested = ["delta", "gamma", "vega", "bucket_vega", "irpv01", "fx_delta", "skew_delta", "cross_vega"]
    result = _execute(
        "benchmark_portfolio",
        {"instruments": 10, "paths": 100, "sensitivities": "all", "greeks": requested},
    )["benchmark"]
    assert result["requested_greeks"] == requested
    assert set(requested) - {"bucket_vega"} <= set(result["greeks"])
    assert set(result["bucket_vega"]) == {"1M", "3M", "6M", "1Y", "2Y"}
    assert result["greeks"]["delta"]["method"] == "CRN_BUMP_REVALUE"
    assert result["greeks"]["gamma"]["method"] == "CRN_BUMP_REVALUE"
    assert result["greeks"]["vega"]["method"] == "CRN_BUMP_REVALUE"
    assert result["bucket_vega"]["1Y"]["method"] == "CRN_BUCKET_BUMP_REVALUE"


def test_benchmark_tool_accepts_100k_scope() -> None:
    result = _execute(
        "benchmark_portfolio",
        {
            "instruments": 100_000,
            "underlyings": 1200,
            "paths": 100,
            "factors": 12,
            "sensitivities": "none",
            "pnl": "none",
        },
    )
    benchmark = result["benchmark"]
    assert benchmark["requested_instruments"] == 100_000
    assert benchmark["structure_reuse"] is True
    assert benchmark["sensitivity_checksum"] is None
    assert benchmark["taylor_pnl_checksum"] is None


def test_benchmark_execution_toggles_disable_state_and_cache() -> None:
    result = _execute(
        "benchmark_portfolio",
        {
            "instruments": 100,
            "underlyings": 120,
            "paths": 100,
            "factors": 8,
            "sensitivities": "none",
            "pnl": "none",
            "execution": {
                "monitoring_frequency": "terminal",
                "range_accrual_state": False,
                "memory_call_state": False,
                "structure_cache": False,
                "unique_structure_count": 100,
            },
        },
    )
    benchmark = result["benchmark"]
    assert benchmark["structure_reuse"] is False
    assert benchmark["enabled_components"]["monitoring_frequency"] == "terminal"
    assert benchmark["enabled_components"]["range_accrual_state"] is False
    assert benchmark["enabled_components"]["structure_cache"] is False
