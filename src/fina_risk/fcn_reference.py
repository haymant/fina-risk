"""Readable scalar oracle for canonical FCN/RakiPlus conformance fixtures.

The oracle is intentionally not a production backend. It preserves explicit
lifecycle ordering for comparison with the native C++ lane.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np


def _at_or_before(dates: np.ndarray, target: int) -> int:
    return max(0, min(int(np.searchsorted(dates, target, side="right") - 1), dates.size - 1))


def _after(dates: np.ndarray, target: int) -> int:
    return min(int(np.searchsorted(dates, target, side="right")), dates.size)


def _compare(value: float, barrier: float, operator: str) -> bool:
    return {">": value > barrier, ">=": value >= barrier, "<": value < barrier, "<=": value <= barrier}[operator]


def price_fcn_reference(request: dict[str, Any], paths: np.ndarray, dates: np.ndarray) -> dict[str, Any]:
    """Run a scalar interpretation of the implemented canonical FCN semantics."""
    terms = request["fcn_terms"]
    market = request["market_data"]
    put_leg = next(item for item in request["legs"] if item["leg_type"] == "intrinsic_option")
    funding_leg = next(item for item in request["legs"] if item["leg_type"] == "funding")
    payoff = put_leg["payoff"]
    barrier = terms["barriers"]
    references = np.asarray([item["reference_spot"] for item in market["underlyings"]], dtype=float)
    rate = float(market.get("curves", [{}])[0].get("pillars", [{}])[0].get("rate", 0.0))
    evaluation = int(market["evaluation_date"])
    final_index = _at_or_before(dates, int(terms["final_fixing_date"]))
    periods = terms["coupon_periods"]
    coupon_sums = np.zeros(len(periods), dtype=float)
    coupon_pvs = np.zeros(len(periods), dtype=float)
    funding_pv = 0.0
    put_pv = 0.0
    ki_count = ko_count = local_count = global_count = 0

    for path_index in range(paths.shape[0]):
        locks_local = np.zeros(paths.shape[2], dtype=bool)
        locks_global = np.zeros(paths.shape[2], dtype=bool)
        selected = "none"
        ko_index: int | None = None
        ki = False
        terminal = 1.0
        for observation in range(final_index + 1):
            performance = paths[path_index, observation, :] / references
            if not np.all(np.isfinite(performance)) or np.any(performance <= 0):
                raise ValueError("missing or invalid fixing in reference oracle")
            worst = float(performance.min())
            if observation == final_index:
                terminal = worst
                ki = _compare(worst, float(payoff["knock_in"]["barrier"]), payoff["knock_in"].get("operator", "<="))
            if barrier.get("memory_ko", False):
                if barrier.get("local_enabled", False):
                    locks_local |= np.asarray([_compare(value, float(barrier["local_barrier"]), barrier.get("local_operator", ">=")) for value in performance])
                if barrier.get("global_enabled", False):
                    locks_global |= np.asarray([_compare(value, float(barrier["global_barrier"]), barrier.get("global_operator", ">=")) for value in performance])
                local_hit = bool(locks_local.all()) if barrier.get("local_enabled", False) else False
                global_hit = bool(locks_global.all()) if barrier.get("global_enabled", False) else False
            else:
                local_hit = bool(barrier.get("local_enabled", False) and _compare(worst, float(barrier["local_barrier"]), barrier.get("local_operator", ">=")))
                global_hit = bool(barrier.get("global_enabled", False) and _compare(worst, float(barrier["global_barrier"]), barrier.get("global_operator", ">=")))
            if local_hit or global_hit:
                if local_hit and global_hit:
                    selected = f"{barrier.get('same_day_ko_precedence', 'global')}_ko"
                else:
                    selected = "local_ko" if local_hit else "global_ko"
                ko_index = observation
                break

        ki_count += int(ki)
        ko_count += int(ko_index is not None)
        local_count += int(selected == "local_ko")
        global_count += int(selected == "global_ko")
        memory = 0.0
        for period_index, period in enumerate(periods):
            begin, end = _after(dates, int(period["start_date"])), _at_or_before(dates, int(period["end_date"]))
            if begin > end or begin >= dates.size or (ko_index is not None and ko_index < begin):
                break
            capped_end = min(end, ko_index) if ko_index is not None else end
            values = (paths[path_index, begin : capped_end + 1, :] / references).min(axis=1)
            lower = values >= float(period.get("lower_bound", 0.0)) if terms.get("range_lower_inclusive", True) else values > float(period.get("lower_bound", 0.0))
            upper = values <= float(period.get("upper_bound", 1.0e12)) if terms.get("range_upper_inclusive", True) else values < float(period.get("upper_bound", 1.0e12))
            qualifying = float((lower & upper).sum())
            total = max(float(period.get("total_fixings", 0)), 1.0)
            unpaid = max(qualifying - float(period.get("already_paid_fixings", 0)), 0.0)
            last_performance = float(values[-1])
            coupon_barrier_ok = float(period.get("coupon_barrier", 0.0)) <= 0.0 or last_performance >= float(period["coupon_barrier"])
            coupon_rate = float(period.get("fixed_coupon", 0.0))
            if coupon_barrier_ok:
                coupon_rate += float(period.get("range_rate", 0.0)) * min((unpaid + (memory if terms.get("coupon_memory", False) else 0.0)) / total, 1.0)
            is_ko_period = ko_index is not None and ko_index <= end
            if is_ko_period:
                coupon_rate += float(period.get("local_ko_coupon", 0.0) if selected == "local_ko" else period.get("global_ko_coupon", 0.0))
            settlement = int(dates[ko_index]) if is_ko_period else int(period["payment_date"])
            cash = float(terms["notional"]) * coupon_rate
            coupon_sums[period_index] += cash
            coupon_pvs[period_index] += cash * math.exp(-rate * max(settlement - evaluation, 0) / 365.0)
            memory = max(total - unpaid, 0.0) if terms.get("coupon_memory", False) and coupon_barrier_ok else 0.0
            if is_ko_period:
                break

        funding_date = int(dates[ko_index]) if ko_index is not None else int(terms["maturity_date"])
        funding_pv += float(terms["notional"]) * float(funding_leg["payoff"].get("return_ratio", 1.0)) * math.exp(-rate * max(funding_date - evaluation, 0) / 365.0) / max(float(terms["notional"]), 1.0)
        if ko_index is None and (not payoff.get("knock_in") or ki):
            option = max(float(payoff["strike"]) - terminal, 0.0)
            put_pv += option * math.exp(-rate * max(int(terms["maturity_date"]) - evaluation, 0) / 365.0)

    count = float(paths.shape[0])
    coupon_pv = float(coupon_pvs.sum() / count * float(terms.get("coupon_quote_scale", 1.0)) / max(float(terms["notional"]), 1.0))
    funding = funding_pv / count
    put = put_pv / count
    selected_branch = "no_ki_maturity" if ko_count == 0 and ki_count == 0 else "mixed_ki_maturity" if ko_count == 0 else "local_ko" if local_count == ko_count else "global_ko" if global_count == ko_count else "mixed_ko"
    return {
        "pv": funding + coupon_pv - put,
        "legs": {"FUNDING": funding, "COUPON": coupon_pv, "PUT / Terminal Optionality": -put},
        "ki_probability": ki_count / count,
        "ko_probability": ko_count / count,
        "selected_branch": selected_branch,
    }


__all__ = ["price_fcn_reference"]
