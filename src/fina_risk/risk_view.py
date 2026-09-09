from __future__ import annotations

from typing import Any


def _identity(result: dict[str, Any], *, portfolio_id: str, instrument_id: str, leg_id: str) -> dict[str, str]:
    return {
        "portfolio_id": portfolio_id,
        "instrument_id": instrument_id,
        "leg_id": leg_id,
    }


def _factor_name(risk_factor_id: str) -> str:
    parts = risk_factor_id.split(":")
    return parts[1] if len(parts) > 1 else risk_factor_id


def build_risk_views(
    result: dict[str, Any],
    *,
    portfolio_id: str = "PORTFOLIO",
    instrument_id: str = "INSTRUMENT",
    leg_id: str = "PUT",
    notional: float = 1.0,
) -> dict[str, Any]:
    """Build audit-friendly long observations and a user-facing wide Taylor view.

    The long rows preserve every method observation. The wide rows select the preferred
    method and put market shocks beside Greek values and P&L contributions.
    """
    base = result.get("base", {})
    sensitivities = result.get("sensitivities", [])
    fd_sensitivities = result.get("fd_sensitivities", [])
    fd_by_factor = {x.get("risk_factor_id"): x for x in fd_sensitivities}
    taylor_components = {x.get("factor_id"): x for x in result.get("taylor_decomposition", {}).get("components", [])}
    conventions = base.get("conventions", {})
    quoted = conventions.get("quoted_spots", [])
    long_rows: list[dict[str, Any]] = []
    wide_by_factor: dict[str, dict[str, Any]] = {}

    for selected in sensitivities:
        factor = str(selected["risk_factor_id"])
        fd = fd_by_factor.get(factor, {})
        idx = selected.get("legacy_data_index")
        spot = float(quoted[idx]) if isinstance(idx, int) and idx < len(quoted) else None
        raw_value = float(selected.get("value", 0.0))
        dollar_value = float(selected.get("dollar_delta", raw_value * (spot or 1.0) * notional))
        shock = float(0.01 * spot) if spot is not None else None
        component = taylor_components.get(factor, {})
        selected_key = f"{portfolio_id}|{instrument_id}|{leg_id}|{factor}|DELTA|"
        common = {
            **_identity(result, portfolio_id=portfolio_id, instrument_id=instrument_id, leg_id=leg_id),
            "risk_factor_id": factor,
            "risk_factor_type": factor.split(":", 1)[0],
            "underlying_id": _factor_name(factor),
            "greek": "DELTA",
            "measure_variant": "RAW",
            "value": raw_value,
            "unit": "per_spot_unit",
            "dollar_value": dollar_value,
            "shock_size": 0.01,
            "shock_mode": "relative",
            "risk_factor_key": selected_key,
            "transition_treatment": "frozen_branch_with_transition_fallback",
            "quality_flag": "conditional",
        }
        long_rows.append(
            {
                **common,
                "method": selected.get("method", "AAD"),
                "method_status": "selected",
                "aad_scope": "fixed_branch" if "AAD" in str(selected.get("method")) else None,
                "bump_size": None,
                "smoothing_width": None,
            }
        )
        if fd:
            long_rows.append(
                {
                    **common,
                    "value": float(fd.get("value", 0.0)),
                    "dollar_value": float(fd.get("dollar_delta", 0.0)),
                    "method": "CRN_FD",
                    "method_status": "cross_check",
                    "aad_scope": None,
                    "bump_size": float(fd.get("bump_size", 0.01)),
                    "smoothing_width": None,
                    "quality_flag": "bump_estimate",
                }
            )
        contribution = float(component.get("contribution", raw_value * shock)) if shock is not None else 0.0
        row = wide_by_factor.setdefault(
            factor,
            {
                **_identity(result, portfolio_id=portfolio_id, instrument_id=instrument_id, leg_id=leg_id),
                "risk_factor_id": factor,
                "risk_factor_type": factor.split(":", 1)[0],
                "underlying_id": _factor_name(factor),
                "base_pv": float(base.get("valuation", {}).get("pv", 0.0)),
                "spot": spot,
                "spot_shock": shock,
                "delta": raw_value,
                "delta_dollar": dollar_value,
                "delta_pnl": contribution,
                "gamma": None,
                "gamma_pnl": 0.0,
                "vega": None,
                "vega_pnl": 0.0,
                "irpv01": None,
                "rate_pnl": 0.0,
                "total_taylor_pnl": contribution,
                "selected_method": selected.get("method", "AAD"),
                "cross_check_method": "CRN_FD" if fd else None,
                "transition_treatment": "frozen_branch_with_transition_fallback",
                "quality_flag": "conditional",
                "risk_factor_key": selected_key,
            },
        )
        row["method_agreement"] = (
            abs(raw_value - float(fd.get("value", raw_value))) / max(abs(float(fd.get("value", raw_value))), 1e-12)
            if fd
            else None
        )

    for row in wide_by_factor.values():
        row["unexplained_pnl"] = None
    taylor = result.get("taylor_decomposition", {})
    wide_rows = list(wide_by_factor.values())
    selected_total = sum(float(x["total_taylor_pnl"]) for x in wide_rows)
    return {
        "schema_version": "risk-view.v1",
        "presentation": {
            "wide": "one row per risk factor with Greek values, shocks, and Taylor P&L attribution",
            "long": "one row per method observation for audit, validation, and aggregation",
        },
        "wide": wide_rows,
        "long": long_rows,
        "summary": {
            "base_pv": float(base.get("valuation", {}).get("pv", 0.0)),
            "selected_taylor_pnl": selected_total,
            "reported_taylor_pnl": taylor.get("forecast_pnl"),
            "unexplained_pnl": taylor.get("unexplained_pnl"),
            "selected_method_policy": "AAD when eligible; CRN_FD/pathwise fallback otherwise",
            "aggregation_key": "portfolio_id|instrument_id|leg_id|risk_factor_id|greek|bucket_id",
        },
    }


def aggregate_risk_views(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate wide/long views without mixing units or calculation methods."""
    wide: dict[str, dict[str, Any]] = {}
    long_rows: list[dict[str, Any]] = []
    for result in results:
        view = result.get("risk_representation", result)
        long_rows.extend(view.get("long", []))
        for row in view.get("wide", []):
            key = row["risk_factor_key"]
            if key not in wide:
                wide[key] = dict(row)
                continue
            for field in (
                "base_pv",
                "delta",
                "delta_dollar",
                "delta_pnl",
                "gamma_pnl",
                "vega_pnl",
                "rate_pnl",
                "total_taylor_pnl",
            ):
                if isinstance(row.get(field), (int, float)):
                    wide[key][field] = float(wide[key].get(field, 0.0) or 0.0) + float(row[field])
    return {
        "schema_version": "risk-view.v1",
        "wide": list(wide.values()),
        "long": long_rows,
        "summary": {"row_count": len(wide), "long_observation_count": len(long_rows)},
    }
