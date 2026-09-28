"""The NYSE trading calendar exists twice. Prove the two copies agree.

``cpp/include/fina_risk/nyse_calendar.hpp`` (used by ``fina_risk_cpp.cpp``) and
``daily_termsheet.us_market_holidays`` / ``nyse_serials`` are two hand-written
holiday lists.  Until now nothing compared them, so a divergence would have
changed every fixing count -- and therefore every accrual fraction -- on an
affected day, silently, in whichever lane you happened to be running.

``block/nyse_calendar`` in CTest asserts the C++ copy against invariants and
prints the schedule.  This module runs that binary and diffs its output against
the Python copy day for day.

The comparison is the point.  Neither side is treated as the reference, because
we do not actually know which is right -- only that they must not differ.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from fina_risk.daily_termsheet import nyse_serials

REPO_ROOT = Path(__file__).resolve().parents[1]
FIRST_YEAR = 2015
LAST_YEAR = 2040
EXCEL_EPOCH_OFFSET = 25569  # days from the Excel epoch to the Unix epoch


def _serial_to_date(serial: int):
    from datetime import date, timedelta

    return date(1970, 1, 1) + timedelta(days=serial - EXCEL_EPOCH_OFFSET)


def _cpp_schedule() -> list[int]:
    """Run the C++ block test and read the schedule it prints."""
    candidates = [
        REPO_ROOT / "build" / "fina-risk-block-nyse-calendar",
        REPO_ROOT / "cpp" / "build" / "fina-risk-block-nyse-calendar",
    ]
    binary = next((path for path in candidates if path.is_file()), None)
    if binary is None:
        pytest.skip(
            "fina-risk-block-nyse-calendar not built; "
            "run: cmake -S cpp -B build && cmake --build build"
        )
    done = subprocess.run(
        [str(binary), str(FIRST_YEAR), str(LAST_YEAR)],
        capture_output=True,
        text=True,
        check=False,
    )
    # The binary's own invariant checks write to stderr; a non-zero exit means
    # the C++ calendar violates one of them, which this module should surface
    # rather than paper over.
    assert done.returncode == 0, f"C++ calendar failed its own checks:\n{done.stderr}"
    return [int(line) for line in done.stdout.split() if line.strip()]


def test_cpp_calendar_passes_its_own_invariants() -> None:
    """Run the binary for its assertions alone, discarding the schedule dump."""
    _cpp_schedule()


def test_cpp_and_python_calendars_agree_day_for_day() -> None:
    cpp = _cpp_schedule()
    assert cpp, "C++ calendar produced no trading days"

    first = _serial_to_date(cpp[0])
    last = _serial_to_date(cpp[-1])
    python_days = [int(value) for value in nyse_serials(int(cpp[0]), int(cpp[-1]))]

    only_cpp = sorted(set(cpp) - set(python_days))
    only_python = sorted(set(python_days) - set(cpp))

    def describe(serials: list[int]) -> str:
        return ", ".join(f"{_serial_to_date(s)} ({_serial_to_date(s):%a})" for s in serials[:12])

    assert not only_cpp and not only_python, (
        "the C++ and Python NYSE calendars disagree. "
        "C++ trades but Python does not: "
        f"{describe(only_cpp) or 'none'}. "
        "Python trades but C++ does not: "
        f"{describe(only_python) or 'none'}. "
        f"Window {first}..{last}."
    )
    assert len(cpp) == len(python_days)


def test_python_calendar_excludes_the_known_holidays() -> None:
    """Guard the Python side against the same drift the diff would catch.

    Kept separate from the cross-language diff so that a failure points at one
    copy rather than at "the two of them".
    """
    from datetime import date

    from fina_risk.daily_termsheet import _nth_weekday, _observed

    days = set(int(v) for v in nyse_serials(45000, 50000))
    for year in (2026, 2027):
        for holiday in (
            _observed(date(year, 1, 1)),
            _nth_weekday(year, 1, 3, 0),
            _nth_weekday(year, 2, 3, 0),
            _nth_weekday(year, 5, -1, 0),  # Memorial Day: last MONDAY
            _observed(date(year, 6, 19)),
            _observed(date(year, 7, 4)),
            _nth_weekday(year, 9, 1, 0),
            _nth_weekday(year, 11, 4, 3),
            _observed(date(year, 12, 25)),
        ):
            serial = (holiday - date(1899, 12, 30)).days
            assert serial not in days, f"Python calendar trades on {holiday} ({holiday:%a})"
