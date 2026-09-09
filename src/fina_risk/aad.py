from __future__ import annotations

from typing import Any

import numpy as np


def _xad_inputs() -> tuple[Any, Any] | None:
    try:
        from xad.adj_1st import Real, Tape
    except ImportError:
        return None
    return Real, Tape


def aad_fixed_branch_market_sensitivities(
    quoted_spots: np.ndarray,
    reference_spots: np.ndarray,
    terminal_multipliers: np.ndarray,
    strike: float,
    discount_factor: float,
    *,
    log_returns: np.ndarray | None = None,
    skew_basis: np.ndarray | None = None,
) -> dict[str, Any]:
    """Differentiate frozen-branch payoff arithmetic with respect to market inputs.

    This tape treats branch membership and pathwise random numbers as fixed. The
    volatility input scales log returns, the skew input shifts a supplied skew
    basis, the discount input scales the cash flow, and FX scales the converted
    value. These are valid local derivatives away from payoff/state transitions.
    """
    xad = _xad_inputs()
    if xad is None:
        return {"available": False, "reason": "QuantLib-Risks/XAD unavailable"}
    Real, Tape = xad
    quoted = np.asarray(quoted_spots, dtype=float)
    refs = np.asarray(reference_spots, dtype=float)
    multipliers = np.asarray(terminal_multipliers, dtype=float)
    if log_returns is None:
        log_returns = np.log(np.maximum(multipliers, 1e-12))
    if skew_basis is None:
        skew_basis = np.zeros_like(log_returns)
    else:
        skew_basis = np.broadcast_to(np.asarray(skew_basis, dtype=float), multipliers.shape)
    performance = quoted[None, :] * multipliers / refs[None, :]
    worst_index = performance.argmin(axis=1)
    active = performance.min(axis=1) < strike
    spot_inputs = [Real(float(x)) for x in quoted]
    vol_input = Real(1.0)
    discount_input = Real(float(discount_factor))
    fx_input = Real(1.0)
    skew_input = Real(0.0)
    with Tape() as tape:
        for value in (*spot_inputs, vol_input, discount_input, fx_input, skew_input):
            tape.registerInput(value)
        tape.newRecording()
        value = Real(0.0)
        for multiplier_row, row, skew_row, is_active, branch in zip(
            multipliers, log_returns, skew_basis, active, worst_index, strict=False
        ):
            if is_active:
                i = int(branch)
                # First-order tangent around vol=1 and skew=0. This keeps the
                # tape in XAD-compatible scalar arithmetic while preserving the
                # local log-return and skew derivatives.
                terminal = (
                    spot_inputs[i]
                    * float(multiplier_row[i])
                    * (1.0 + float(row[i]) * (vol_input - 1.0) + float(skew_row[i]) * skew_input)
                )
                value = value + (strike - terminal / float(refs[i]))
        value = value * discount_input * fx_input / max(len(active), 1)
        tape.registerOutput(value)
        value.derivative = 1.0
        tape.computeAdjoints()
        return {
            "available": True,
            "engine": "QuantLib-Risks/XAD",
            "mode": "adjoint_first_order",
            "value": float(value.value),
            "deltas": [float(x.derivative) for x in spot_inputs],
            "vega": float(vol_input.derivative),
            "discount_sensitivity": float(discount_input.derivative),
            "fx_delta": float(fx_input.derivative),
            "skew_delta": float(skew_input.derivative),
            "branch_policy": "fixed base-path worst-of branch",
            "fallback_reason": "worst-of and strike kinks require PATHWISE/FD at branch transitions",
        }


def aad_put_sensitivity(
    quoted_spots: np.ndarray,
    reference_spots: np.ndarray,
    terminal_multipliers: np.ndarray,
    strike: float,
    discount_factor: float,
) -> dict[str, Any]:
    """Differentiate a fixed-branch Monte Carlo PUT payoff with XAD."""
    result = aad_fixed_branch_market_sensitivities(
        quoted_spots,
        reference_spots,
        terminal_multipliers,
        strike,
        discount_factor,
    )
    if result.get("available"):
        result["deltas"] = result["deltas"]
    return result
