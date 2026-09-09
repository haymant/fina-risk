from __future__ import annotations

from typing import Any

import numpy as np


def aad_put_sensitivity(
    quoted_spots: np.ndarray,
    reference_spots: np.ndarray,
    terminal_multipliers: np.ndarray,
    strike: float,
    discount_factor: float,
) -> dict[str, Any]:
    """Differentiate a fixed-branch Monte Carlo PUT payoff with XAD.

    Branch membership is frozen from the base paths. This is a valid local AAD
    derivative away from the worst-of and strike kinks; the caller must retain
    the explicit fallback label for paths crossing those discontinuities.
    """
    try:
        from xad.adj_1st import Real, Tape
    except ImportError as exc:  # pragma: no cover - dependency is declared
        return {"available": False, "reason": f"QuantLib-Risks/XAD unavailable: {exc}"}

    terminal = quoted_spots[None, :] * terminal_multipliers
    perf = terminal / reference_spots[None, :]
    worst_index = perf.argmin(axis=1)
    worst = perf.min(axis=1)
    active = worst < strike
    inputs = [Real(float(x)) for x in quoted_spots]
    with Tape() as tape:
        for x in inputs:
            tape.registerInput(x)
        tape.newRecording()
        value = Real(0.0)
        for row, is_active, branch in zip(terminal_multipliers, active, worst_index, strict=False):
            if is_active:
                i = int(branch)
                value = value + (strike - inputs[i] * float(row[i]) / float(reference_spots[i]))
        value = value * float(discount_factor) / max(len(active), 1)
        tape.registerOutput(value)
        value.derivative = 1.0
        tape.computeAdjoints()
        return {
            "available": True,
            "engine": "QuantLib-Risks/XAD",
            "mode": "adjoint_first_order",
            "value": float(value.value),
            "deltas": [float(x.derivative) for x in inputs],
            "branch_policy": "fixed base-path worst-of branch",
            "fallback_reason": "worst-of and strike kinks require PATHWISE/FD at branch transitions",
        }
