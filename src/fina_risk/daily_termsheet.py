from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import numpy as np

from .pricing import excel_date, year_fraction


@dataclass(frozen=True)
class DailyTermsSheetResult:
    pv: float
    put_price: float
    coupon_pv: float
    ki_probability: float
    ko_probability: float
    observation_dates: list[int]
    daily_observations: int
    coupon_fixings: list[float]
    memory_carry: list[float]
    legs: list[dict[str, Any]]


def weekday_serials(start: int, end: int) -> np.ndarray:
    """Placeholder schedule: every Mon-Fri, exchange holidays NOT removed.

    Kept for regression comparisons. New pricing work should use
    :func:`nyse_serials`, which is the calendar the term sheet references.
    """
    current = excel_date(start)
    finish = excel_date(end)
    values: list[int] = []
    while current <= finish:
        if current.weekday() < 5:
            values.append((current - excel_date(0)).days)
        current += timedelta(days=1)
    return np.asarray(values, dtype=np.int64)


def _nth_weekday(year: int, month: int, n: int, target: int) -> date:
    first = date(year, month, 1)
    delta = (target - first.weekday()) % 7
    if n > 0:
        return first + timedelta(days=delta + (n - 1) * 7)
    # n < 0: last occurrence in the month
    next_month = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    last = next_month - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - target) % 7)


def _easter_sunday(year: int) -> date:
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month = (h + ell - 7 * m + 114) // 31
    day = ((h + ell - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _observed(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def us_market_holidays(year: int) -> set[date]:
    """NYSE full-day closures. Mirrors ``is_us_market_holiday`` in the C++ lane."""
    holidays = {
        _observed(date(year, 1, 1)),
        _nth_weekday(year, 1, 3, 0),   # MLK Day
        _nth_weekday(year, 2, 3, 0),   # Washington's Birthday
        _easter_sunday(year) - timedelta(days=2),  # Good Friday
        _nth_weekday(year, 5, -1, 0),  # Memorial Day
        _observed(date(year, 7, 4)),   # Independence Day
        _nth_weekday(year, 9, 1, 0),   # Labor Day
        _nth_weekday(year, 11, 4, 3),  # Thanksgiving
        _observed(date(year, 12, 25)), # Christmas
    }
    if year >= 2022:
        holidays.add(_observed(date(year, 6, 19)))  # Juneteenth
    return holidays


def nyse_serials(start: int, end: int) -> np.ndarray:
    """NYSE trading days (Mon-Fri minus exchange holidays) as Excel serials."""
    first, last = excel_date(start), excel_date(end)
    holidays: set[date] = set()
    for year in range(first.year, last.year + 1):
        holidays |= us_market_holidays(year)
    current = first
    values: list[int] = []
    while current <= last:
        if current.weekday() < 5 and current not in holidays:
            values.append((current - excel_date(0)).days)
        current += timedelta(days=1)
    return np.asarray(values, dtype=np.int64)


def price_daily_termsheet(
    common: dict[str, Any], daily_spots: np.ndarray, *, paths: int | None = None
) -> DailyTermsSheetResult:
    deal = common["dealData"]
    market = common["marketData"]
    observations = np.asarray(daily_spots, dtype=float)
    if observations.ndim != 3 or observations.shape[2] < 2:
        raise ValueError("daily_spots must have shape (paths, observations, underlyings)")
    evaluation = int(market["evaluationDate"])
    expiry = int(deal.get("expiryDate", deal.get("maturityDate")))
    dates = nyse_serials(evaluation, expiry)
    if observations.shape[1] != dates.size:
        raise ValueError(f"daily path observations {observations.shape[1]} != schedule {dates.size}")
    refs = np.asarray([float(x["spot"]) for x in deal["instrument"]["underlyings"][:2]])
    performance = observations[:, :, :2] / refs[None, None, :]
    worst = performance.min(axis=2)
    ki_barrier = float(deal.get("knockInStar", {}).get("KIBarrier", 0.70))
    # EKI: European knock-in observed on the final fixing date only, mirroring
    # the native daily kernel (`knockInType == "EKI"`). Continuous monitoring
    # would instead be `np.any(worst <= ki_barrier, axis=1)`.
    ki_hit = worst[:, -1] <= ki_barrier
    global_barrier = float(deal.get("RGACCLKO", {}).get("gblBarPrice", 1.10))
    global_ko = np.all(performance >= global_barrier, axis=2)
    ko_hit = np.any(global_ko, axis=1)
    ko_step = np.where(ko_hit, np.argmax(global_ko, axis=1), observations.shape[1])
    terminal_worst = worst[:, -1]
    strike = float(deal.get("knockInStar", {}).get("strikeKI2", deal.get("strike", 0.78)))
    put_payoff = np.where(ki_hit & ~ko_hit, np.maximum(strike - terminal_worst, 0.0), 0.0)
    disc_rate = float(
        market.get("discCurves", [{}])[0].get("curve", [{"rate": 0.0}])[0].get("rate", 0.0)
    )
    put_df = math.exp(-disc_rate * year_fraction(evaluation, expiry))
    put = float(put_df * put_payoff.mean())

    rg = deal.get("RGACCLKO", {})
    ends = [int(x) for x in rg.get("endDate", [])]
    payments = [int(x) for x in rg.get("paymentDate", [])]
    rates = [float(x) for x in rg.get("accruRate", [])]
    n1 = [int(x) for x in rg.get("N1", [])]
    n2 = [int(x) for x in rg.get("N2", [])]
    lows = [float(x) for x in rg.get("lowRange", [])]
    ups = [float(x) for x in rg.get("upRange", [])]
    notional = float(deal.get("notional", 1.0))
    coupon_paths = np.zeros(observations.shape[0], dtype=float)
    fixings_report: list[float] = []
    memory_report: list[float] = []
    memory = np.zeros(observations.shape[0], dtype=float)
    previous_end = evaluation
    for i, (end, payment, rate, paid, total) in enumerate(zip(ends, payments, rates, n1, n2, strict=False)):
        end_index = int(np.searchsorted(dates, end, side="right") - 1)
        start_index = int(np.searchsorted(dates, previous_end, side="right"))
        if end_index < start_index or rate == 0.0:
            previous_end = end
            continue
        low = lows[i] if i < len(lows) else 0.0
        up = ups[i] if i < len(ups) else 1.0e9
        in_range = (worst[:, start_index : end_index + 1] >= low) & (worst[:, start_index : end_index + 1] <= up)
        observed_fixings = in_range.sum(axis=1).astype(float)
        future_fixings = np.maximum(observed_fixings - paid, 0.0)
        effective_total = max(total, 1)
        # Memory carries missed coupon fixing amounts into a later period; global KO
        # terminates future coupon accrual before the KO observation.
        alive_through = ko_step > end_index
        accrued_fraction = np.minimum((future_fixings + memory) / effective_total, 1.0)
        amount = np.where(alive_through, notional * rate * accrued_fraction, 0.0)
        payment_disc_rate = float(
            market.get("discCurves", [{}])[0].get("curve", [{"rate": 0.0}])[0].get("rate", 0.0)
        )
        payment_df = math.exp(-payment_disc_rate * year_fraction(evaluation, payment))
        coupon_paths += amount * payment_df
        missed = np.maximum(effective_total - future_fixings, 0.0)
        memory = np.where(alive_through & (future_fixings < effective_total), missed, 0.0)
        fixings_report.append(float(observed_fixings.mean()))
        memory_report.append(float(memory.mean()))
        previous_end = end
    coupon_raw = float(coupon_paths.mean())
    quote_scale = float(deal.get("legacyCouponQuoteScale", 10.0))
    coupon = coupon_raw / max(notional, 1.0) * quote_scale
    funding = put_df
    return DailyTermsSheetResult(
        pv=funding - put + coupon,
        put_price=put,
        coupon_pv=coupon,
        ki_probability=float(ki_hit.mean()),
        ko_probability=float(ko_hit.mean()),
        observation_dates=dates.tolist(),
        daily_observations=int(dates.size),
        coupon_fixings=fixings_report,
        memory_carry=memory_report,
        legs=[
            {"leg_name": "PUT", "multiplier": -1, "pv": -put},
            {"leg_name": "FUNDING", "multiplier": 1, "pv": funding},
            {"leg_name": "COUPON", "multiplier": 1, "pv": coupon},
        ],
    )
