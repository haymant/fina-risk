"""The term-sheet lane dispatcher picks the lane, never a human.

``cpp/include/fina_risk/lane_dispatch.hpp`` classifies a term sheet by its root
keys and by the inputs that were actually supplied (cube dimensionality, dates),
and ``run_termsheet`` (``cpp/src/fina_risk_cpp.cpp``) executes the chosen lane
or refuses with a dispatch envelope. Before the dispatcher, every caller picked
the lane by hand -- ``server.py`` unwrapped ``Chunk.Jobs`` into a single common
and called ``price_fixture``, ``cpp_parity.py`` handed the terminal cube to
``run_cpp_parity``, ``daily_termsheet.py`` and ``fcn_native.py`` handed the
daily cube to one of the daily lanes -- and a lane picked by hand and a lane
picked from the term sheet drift apart silently.

The classifier's rules are the contract:

==============================  ==========================  ==========================
root keys                       lane                        inputs required
==============================  ==========================  ==========================
instrument_key/market_data/     price_fcn_rakiplus          daily (P,O,U) cube, dates
  legs/parameters
Chunk.Jobs[]                    run_daily_termsheet         daily (P,O,U) cube, dates
instruments + market            run_daily_termsheet_batch   daily (P,O,U) cube, dates
  rate/evaluation_date
instruments + market            run_cpp_parity              terminal (P,U) float32 cube
  underlyings only
dealData/marketData             price_fixture               none (simulates internally)
anything else                   refused                     -
==============================  ==========================  ==========================

That table is hand-derived in BOTH places independently: the C++ block test
``block/lane_dispatch`` asserts it in C++ and prints machine-readable
``CASE``/``REFUSED`` lines, and this module re-states it from the spec and diffs
the binary's stdout against it. A rule that drifts breaks the diff even if the
C++ assertions were edited to match the bug.

The binary links nlohmann_json only (never ``fina_risk_core``), so it never sees
the library's ``-ffast-math`` policy; the classifier is a pure string decision.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# Hand-derived from the lane inventory (see the module docstring). Keyed by case
# name so the pytest diff can point at the exact rule that drifted. The refusal
# strings here are the contract: block/lane_dispatch pins the same wording.
EXPECTED: dict[str, tuple[str, list[str]]] = {
    # --- valid picks (zero refusals) ---------------------------------------
    "canonical_rfq": ("price_fcn_rakiplus", []),
    "chunk_jobs": ("run_daily_termsheet", []),
    "parity_corpus": ("run_cpp_parity", []),
    "batch_corpus": ("run_daily_termsheet_batch", []),
    "fixture_common": ("price_fixture", []),
    # --- wrong/missing inputs (refused, would-be lane still reported) -------
    "canonical_no_cube": (
        "price_fcn_rakiplus",
        [
            "canonical FCN request requires the daily (P, O, U) cube",
            "canonical FCN request requires observation dates",
        ],
    ),
    "canonical_terminal_cube": (
        "price_fcn_rakiplus",
        [
            "terminal cube cannot feed the canonical FCN lane",
            "canonical FCN request requires the daily (P, O, U) cube",
            "canonical FCN request requires observation dates",
        ],
    ),
    "chunk_no_cube": (
        "run_daily_termsheet",
        [
            "daily accrual demanded but no (P, O, U) cube supplied",
            "daily accrual demanded but no observation dates supplied",
        ],
    ),
    "chunk_no_dates": (
        "run_daily_termsheet",
        ["daily accrual demanded but no observation dates supplied"],
    ),
    "parity_no_cube": (
        "run_cpp_parity",
        ["parity lane requires the terminal (P, U) float32 cube"],
    ),
    "parity_daily_cube": (
        "run_cpp_parity",
        [
            "parity lane requires the terminal (P, U) float32 cube",
            "daily cube cannot feed the terminal parity lane",
        ],
    ),
    "batch_no_cube": (
        "run_daily_termsheet_batch",
        [
            "batch lane requires the daily (P, O, U) cube",
            "batch lane requires observation dates",
        ],
    ),
    "batch_no_dates": (
        "run_daily_termsheet_batch",
        ["batch lane requires observation dates"],
    ),
    "fixture_with_cube": (
        "price_fixture",
        ["price_fixture simulates internally; a supplied cube or dates is a mismatch"],
    ),
    "unknown_root": ("none", ["unrecognized term-sheet shape"]),
    "instruments_no_market": (
        "none",
        ["instruments corpus requires market underlyings (parity) or rate/evaluation_date (batch)"],
    ),
}


def _binary() -> Path | None:
    """Locate the block test binary, or None if the C++ tree is not built."""
    candidates = [
        REPO / "build" / "fina-risk-block-lane-dispatch",
        REPO / "cpp" / "build" / "fina-risk-block-lane-dispatch",
    ]
    return next((path for path in candidates if path.is_file()), None)


def _dump() -> tuple[dict[str, tuple[str, list[str]]], int]:
    """Run the block binary and parse its CASE/REFUSED lines.

    Returns (observed table, refusal_total) where the table maps case name to
    (lane, refusals) exactly like EXPECTED. The binary's own C++ assertions
    write to stderr; a non-zero exit means one of them failed, which this module
    surfaces rather than papering over.
    """
    binary = _binary()
    if binary is None:
        pytest.skip("block/lane_dispatch is not built; run the C++ build to diff the lane table")
    proc = subprocess.run([str(binary)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"block/lane_dispatch failed its own checks:\n{proc.stderr}"
    observed: dict[str, tuple[str, list[str]]] = {}
    refusal_total = 0
    for line in proc.stdout.splitlines():
        if line.startswith("CASE\t"):
            _, name, lane, _count = line.split("\t")
            observed[name] = (lane, [])
        elif line.startswith("REFUSED\t"):
            _, name, reason = line.split("\t")
            lane, refusals = observed.setdefault(name, ("", []))
            observed[name] = (lane, refusals + [reason])
            refusal_total += 1
    assert observed, "block/lane_dispatch printed no CASE lines"
    return observed, refusal_total


def test_lane_table_matches_the_hand_derived_spec() -> None:
    """Every CASE/REFUSED line must agree with the hand-derived table."""
    observed, refusal_total = _dump()
    assert set(observed) == set(EXPECTED), (
        f"case names drifted: only-C++={sorted(set(observed) - set(EXPECTED))}, "
        f"only-pytest={sorted(set(EXPECTED) - set(observed))}"
    )
    for name, (lane, refusals) in observed.items():
        expected_lane, expected_refusals = EXPECTED[name]
        assert lane == expected_lane, (
            f"{name}: dispatcher chose lane={lane!r}, hand-derived spec says {expected_lane!r}"
        )
        assert refusals == expected_refusals, (
            f"{name}: refusals drifted: observed={refusals!r}, hand-derived={expected_refusals!r}"
        )
    assert refusal_total == sum(len(refusals) for _, refusals in EXPECTED.values())


def test_every_rule_row_has_a_case() -> None:
    """The spec table's five root shapes must each appear as a valid pick."""
    lanes = {lane for lane, _ in EXPECTED.values()}
    assert {
        "price_fcn_rakiplus",
        "run_daily_termsheet",
        "run_daily_termsheet_batch",
        "run_cpp_parity",
        "price_fixture",
    } <= lanes, "a spec row has no green case; add one to lane_dispatch_test.cpp"
