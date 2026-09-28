"""The Dupire local-vol surface, C++ against Python.

`cpp/include/fina_risk/locvol.hpp` is a port of `src/fina_risk/locvol.py`, and
before the port the C++ terminal lane had no local vol at all -- it froze one
scalar per name before the path loop. Two implementations of a model is a
silent way to make the two lanes disagree, so this test diffs them.

The block test `block/locvol` prints sigma(t, S) for every real eqVol surface on
the term sheet, at 9 tenor points and 6 spot levels plus the full 96-point dense
strike axis. This file recomputes each of those with the Python surface and
compares. A formula that is subtly but consistently different -- a factor on one
derivative, a half-step, a transpose -- moves every point by much more than the
tolerance, so this is the test that actually pins the port. The block test's own
assertions only catch gross errors.

Two details the comparison depends on:

* The evaluation date and the two spots are pinned here, not read from the
  payload, so a change in how the lane resolves them shows up as a diff rather
  than as two moving targets agreeing.
* Both sides are compared with a relative tolerance. The two implementations
  order their floating-point operations differently (Thomas elimination against
  `np.linalg.solve`, among others), so an exact bit-for-bit comparison would
  report a failure for arithmetic that is correct to rounding.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
from pathlib import Path

import numpy as np
import pytest

from fina_risk.locvol import GRID_X, LV_CLAMP_HI, LV_CLAMP_LO, LocalVolSurface

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "skills" / "fina-risk" / "refs" / "termsheet1.md.json"

# Pinned here, not read from the payload. 46272 is marketData.evaluationDate
# (2026-09-07); the spots are the term sheet's initial fixings.
EVAL_SERIAL = 46272
RATE = 0.037405  # the 1M USD Std Curve pillar the lane reads
SPOTS = {"ADBE UW": 239.823, "AMZN UW": 260.00}

# Matches kTValues / kSValues in cpp/tests/locvol_test.cpp.
T_VALUES = (0.0, 0.05, 0.25, 0.402740, 0.75, 1.5, 3.0, 4.9, 5.5)
S_VALUES = (100.0, 150.0, 239.823, 260.0, 400.0, 800.0)


def _cpp_binary() -> Path | None:
    """Locate the block test binary, or None if the C++ tree is not built."""
    candidates = [
        REPO / "build" / "fina-risk-block-locvol",
        REPO / "cpp" / "build" / "fina-risk-block-locvol",
    ]
    return next((path for path in candidates if path.is_file()), None)


def _surfaces() -> dict[str, dict]:
    """The distinct eqVol grids on the term sheet, first occurrence of each id."""
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    out: dict[str, dict] = {}
    for job in raw["Chunk"]["Jobs"]:
        for surface in job["commonData"]["marketData"]["eqVol"]:
            out.setdefault(surface["_id"], surface)
    return out


def _dumped() -> list[tuple[str, float, float, float]]:
    binary = _cpp_binary()
    if binary is None:
        pytest.skip("block/locvol is not built; run the C++ build to compare the two surfaces")
    proc = subprocess.run(
        [str(binary), str(FIXTURE)], capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 0, f"block/locvol failed:\n{proc.stderr}"
    rows = []
    for line in proc.stdout.splitlines():
        if not line.startswith("SIGMA\t") and not line.startswith("AXIS\t"):
            continue
        _, name, t, level, sigma = line.split("\t")
        rows.append((name, float(t), float(level), float(sigma)))
    assert rows, "block/locvol printed no surface samples"
    return rows


@pytest.fixture(scope="module")
def dumped() -> list[tuple[str, float, float, float]]:
    return _dumped()


def test_every_underlying_is_covered(dumped) -> None:
    """Both names on the term sheet must appear, or the diff is partial."""
    covered = {name for name, *_ in dumped}
    assert covered == set(SPOTS), f"expected {sorted(SPOTS)}, got {sorted(covered)}"


@pytest.mark.parametrize("name", sorted(SPOTS))
def test_sigma_matches_python(dumped, name: str) -> None:
    """Every printed sigma(t, S) must equal the Python surface's value.

    Non-finite values are rejected explicitly. `abs(a - b) <= rtol * abs(b)` is
    false when either side is NaN, so a NaN would silently satisfy the negated
    assertion and the test would pass on a broken surface.
    """
    surface = _surfaces()[name]
    lv = LocalVolSurface(surface, EVAL_SERIAL, SPOTS[name], RATE)
    rows = [(t, s, sig) for n, t, s, sig in dumped if n == name]
    assert len(rows) == len(T_VALUES) * len(S_VALUES) + GRID_X

    for t, level, cpp in rows:
        expected = float(lv.sigma(t, np.array([level]))[0])
        assert math.isfinite(cpp), f"C++ sigma(t={t}, S={level}) is not finite"
        assert math.isfinite(expected), f"Python sigma(t={t}, S={level}) is not finite"
        assert cpp == pytest.approx(expected, rel=1e-9, abs=1e-12), (
            f"{name}: sigma(t={t}, S={level}) C++ {cpp!r} vs Python {expected!r}"
        )


def _cpp_constant(name: str) -> float:
    """Read an `inline constexpr double name = <number>;` out of the C++ header.

    Asserting the C++ clamp against the Python constant only holds if both are
    spelled the same way, and a text comparison of formatted floats is a
    reliable way to fail on `0.10` versus `0.1`. Parsing the literal out is not
    prettier; it is the only version of this check that does not break when
    someone reformats a constant.
    """
    import re

    header = (REPO / "cpp" / "include" / "fina_risk" / "locvol.hpp").read_text(encoding="utf-8")
    match = re.search(rf"inline constexpr double {name} = ([0-9.eE+-]+);", header)
    assert match is not None, f"{name} is not declared as an inline constexpr double in locvol.hpp"
    return float(match.group(1))


def test_clamp_bounds_agree() -> None:
    """The two ports must clamp to the same bounds.

    A local vol of 300% or 10% is a signal about the input surface, not a
    modelling choice, and the two lanes must not disagree about when to floor it.
    `LV_CLAMP_LO` also decides what a zero-vol grid prices, which is why the
    zero-vol global-KO lane test has to opt out of local vol: under Dupire a
    flat zero surface is a 10% surface.
    """
    assert _cpp_constant("kClampLo") == LV_CLAMP_LO
    assert _cpp_constant("kClampHi") == LV_CLAMP_HI

    header = (REPO / "cpp" / "include" / "fina_risk" / "locvol.hpp").read_text(encoding="utf-8")
    assert f"inline constexpr int kGridX = {GRID_X};" in header


def test_flat_implied_surface_collapses_to_itself() -> None:
    """A flat implied surface must return that implied vol, on both sides.

    This is the property that makes the old scalar lane a special case of the
    local-vol lane rather than a different model, so it is worth pinning on both
    implementations independently of the diff above.
    """
    strikes = [80.0, 90.0, 100.0, 110.0, 120.0]
    surface = {
        "_id": "flat",
        "strike": strikes,
        "maturity": [46272 + 100, 46272 + 190, 46272 + 280],
        "vol": [[20.0] * 5, [20.0] * 5, [20.0] * 5],
    }
    lv = LocalVolSurface(surface, EVAL_SERIAL, 100.0, RATE)
    for t in (0.0, 0.1, 0.3, 0.5):
        for level in (85.0, 100.0, 118.0):
            assert float(lv.sigma(t, np.array([level]))[0]) == pytest.approx(0.20, abs=1e-12)
