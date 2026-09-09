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
