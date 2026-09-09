from __future__ import annotations

from typing import Any

import numpy as np

from .aad import aad_put_sensitivity

HYBRID_METHODS = {"AAD_FIXED_BRANCH", "AAD_SMOOTHED", "PATHWISE", "CRN_FD", "CRN_BUCKET_FD", "LRM"}


def select_method(
    greek: str,
    *,
    transition_fraction: float,
    smoothing_enabled: bool = False,
    xad_available: bool = True,
) -> str:
    """Select the fastest defensible method for one risk component."""
    if greek == "delta" and xad_available:
        if transition_fraction == 0.0:
            return "AAD_FIXED_BRANCH"
        if smoothing_enabled:
            return "AAD_SMOOTHED"
        return "AAD_FIXED_BRANCH+PATHWISE"
    if greek in {"vega", "irpv01", "fx_delta", "skew_delta"} and smoothing_enabled and xad_available:
        return "AAD_SMOOTHED"
    if greek in {"delta", "vega", "irpv01", "fx_delta", "skew_delta"}:
        return "PATHWISE" if transition_fraction == 0.0 else "CRN_FD"
    if greek == "gamma":
        return "PATHWISE" if transition_fraction == 0.0 else "CRN_FD"
    if greek == "bucket_vega":
        return "AAD_SMOOTHED" if smoothing_enabled and xad_available else "CRN_BUCKET_FD"
    if greek == "cross_vega":
        return "AAD_SMOOTHED" if smoothing_enabled and xad_available else "CRN_FD"
    return "CRN_FD"


def _transition_mask(terminal: np.ndarray, references: np.ndarray, strike: float, width: float) -> np.ndarray:
    performance = terminal / references[None, :]
    worst = performance.min(axis=1)
    # The band includes the exercise kink and the knock-in-style barrier band.
    return (np.abs(worst - strike) <= width) | (np.abs(worst - 0.70) <= width)


def hybrid_delta(
    terminal: np.ndarray,
    quoted_spots: np.ndarray,
    reference_spots: np.ndarray,
    strike: float,
    discount_factor: float,
    *,
    smoothing_width: float = 0.0,
    smoothing_enabled: bool = False,
) -> dict[str, Any]:
    """Compute delta with XAD on stable paths and pathwise fallback at transitions.

    The result is a weighted combination of actual fixed-branch XAD and the
    pathwise derivative on the transition subset. It never labels a hard
    transition as pure AAD.
    """
    terminal = np.asarray(terminal, dtype=float)
    quoted_spots = np.asarray(quoted_spots, dtype=float)
    reference_spots = np.asarray(reference_spots, dtype=float)
    paths = terminal.shape[0]
    width = float(smoothing_width if smoothing_enabled else 0.0)
    transition = _transition_mask(terminal, reference_spots, strike, max(width, 1e-8))
    stable = ~transition
    performance = terminal / reference_spots[None, :]
    worst = performance.min(axis=1)
    worst_index = performance.argmin(axis=1)
    active = worst < strike
    pathwise = np.zeros((paths, terminal.shape[1]), dtype=float)
    fallback_rows = np.where(active & transition)[0]
    pathwise[fallback_rows, worst_index[fallback_rows]] = -discount_factor / reference_spots[worst_index[fallback_rows]]
    pathwise_value = pathwise.mean(axis=0)
    aad_value = np.zeros(terminal.shape[1], dtype=float)
    aad_available = False
    aad_reason = None
    if stable.any():
        multipliers = terminal[stable] / quoted_spots[None, :]
        aad_result = aad_put_sensitivity(
            quoted_spots,
            reference_spots,
            multipliers,
            strike,
            discount_factor,
        )
        aad_available = bool(aad_result.get("available", False))
        if aad_available:
            aad_value = np.asarray(aad_result["deltas"], dtype=float) * (stable.sum() / max(paths, 1))
        else:
            aad_reason = aad_result.get("reason")
    if smoothing_enabled and width > 0.0:
        # A smooth local approximation is explicitly separated from hard AAD.
        distance = (strike - worst) / width
        smooth_weight = 1.0 / (1.0 + np.exp(-np.clip(distance, -40.0, 40.0)))
        smooth = np.zeros_like(pathwise)
        smooth[np.arange(paths), worst_index] = -discount_factor * smooth_weight / reference_spots[worst_index]
        value = smooth.mean(axis=0)
        method = "AAD_SMOOTHED" if aad_available else "PATHWISE_SMOOTHED"
    else:
        value = aad_value + pathwise_value
        method = (
            "AAD_FIXED_BRANCH+PATHWISE"
            if aad_available and transition.any()
            else ("AAD_FIXED_BRANCH" if aad_available else "PATHWISE")
        )
    return {
        "value": value.tolist(),
        "method": method,
        "aad_available": aad_available,
        "stable_path_fraction": float(stable.mean()),
        "transition_path_fraction": float(transition.mean()),
        "pathwise_fallback": bool(transition.any()),
        "fallback_reason": aad_reason or ("exercise/barrier transition band" if transition.any() else None),
        "smoothing_enabled": smoothing_enabled,
        "smoothing_width": width if smoothing_enabled else None,
    }
