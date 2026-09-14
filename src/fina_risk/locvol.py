"""Local volatility (Dupire) support derived from the legacy eqVol grid.

The market implied-vol surface is a sparse ``[tenor x strike]`` grid in
percentage points.  This module turns it into a callable local volatility
sigma_LV(S, t):

1. Per tenor, fit a natural cubic spline to the implied smile in log moneyness
   ``x = ln(K / F(T))`` with ``F(T) = S0 * exp(rate * T)``.
2. Evaluate the spline and its first/second x-derivatives on a dense
   absolute-strike axis and assemble the total-variance Dupire expression
   ``sigma_LV^2 = dT w / (1 - (x/w) dx w + 1/4*(-1/4 - 1/w)*(dx w)^2 + 1/2 dxx w)``
   with ``w = sigma_imp^2 * T``.
3. Interpolate sigma_LV bilinearly (linear in tenor, log-linear in strike) at
   simulation time.

A flat implied surface collapses sigma_LV back to the constant implied vol, so
the scalar-vol lanes are the special case of the local-vol lane.  The C++ daily
kernel inherits local vol automatically because it prices the shared cube
(``generate_daily_paths.py``); the C++ terminal ``price_fixture`` remains
scalar-vol.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

GRID_X = 96
LV_CLAMP_LO = 0.10  # 10% floor on local vol defensively (smile spline noise)
LV_CLAMP_HI = 3.00


def _nat_second_derivatives(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Second derivatives of the natural cubic spline through (x, y)."""
    n = x.size
    if n < 3:
        return np.zeros(n)
    h = np.diff(x)
    if np.any(h <= 0) or np.any(np.isnan(y)):
        return np.zeros(n)
    diag = np.zeros(n)
    diag[0] = diag[-1] = 1.0
    sub = np.zeros(n)
    for i in range(1, n - 1):
        diag[i] = 2.0 * (h[i - 1] + h[i])
        sub[i] = h[i]
    m = np.diag(diag) + np.diag(sub[1:], 1) + np.diag(sub[1:], -1)
    rhs = np.zeros(n)
    for i in range(1, n - 1):
        rhs[i] = 6.0 * ((y[i + 1] - y[i]) / h[i] - (y[i] - y[i - 1]) / h[i - 1])
    return np.linalg.solve(m, rhs)


def _spline_eval(
    xp: np.ndarray, x: np.ndarray, y: np.ndarray, y2: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Value, first and second derivative of the natural spline at xp."""
    n = x.size
    seg = np.searchsorted(x, xp) - 1
    seg = np.clip(seg, 0, n - 2)
    h = x[seg + 1] - x[seg]
    t = np.clip((xp - x[seg]) / h, 0.0, 1.0)
    f = (1.0 - t) * y[seg] + t * y[seg + 1] + (h * h / 6.0) * (
        (t**3 - t) * y2[seg] + ((1.0 - t) ** 3 - (1.0 - t)) * y2[seg + 1]
    )
    f1 = (
        y[seg + 1]
        - y[seg]
        + (h * h / 6.0) * ((3.0 * t**2 - 1.0) * y2[seg] - (3.0 * (1.0 - t) ** 2 - 1.0) * y2[seg + 1])
    ) / h
    f2 = (1.0 - t) * y2[seg + 1] + t * y2[seg]
    return f, f1, f2


class LocalVolSurface:
    """Dupire local volatility for one underlying, interpolable at (t, S)."""

    def __init__(self, surface: dict[str, Any], eval_serial: int, spot: float, rate: float):
        strikes = np.asarray(surface.get("strike", []), dtype=float)
        tenors_raw = np.asarray(surface.get("maturity", []), dtype=float)
        raw = np.asarray(surface.get("vol", []), dtype=float) / 100.0
        if strikes.size == 0 or raw.size == 0 or raw.ndim < 2:
            raise ValueError(f"eqVol surface for {surface.get('_id')} has no 2D vol grid")
        if raw.shape[0] == tenors_raw.size:
            values = raw
        else:
            values = raw.T
        order = np.argsort(tenors_raw)
        self.t_axis = np.asarray((tenors_raw[order] - eval_serial) / 365.0, dtype=float)
        values = values[order, :]
        self.S_axis = np.exp(
            np.linspace(math.log(max(float(strikes.min()) * 0.55, 1e-6)), math.log(float(strikes.max()) * 1.05), GRID_X)
        )
        self.log_s = np.log(self.S_axis)
        n_t = self.t_axis.size
        n_s = GRID_X
        forwards = spot * np.exp(rate * self.t_axis)
        w_arr = np.empty((n_t, n_s))
        wx_arr = np.empty((n_t, n_s))
        wxx_arr = np.empty((n_t, n_s))
        x_arr = np.empty((n_t, n_s))
        for k in range(n_t):
            x_j = np.log(strikes / forwards[k])
            y = values[k]
            y2 = _nat_second_derivatives(x_j, y)
            x_d = np.clip(np.log(self.S_axis / forwards[k]), x_j.min(), x_j.max())
            sigma, sigma1, sigma2 = _spline_eval(x_d, x_j, y, y2)
            tk = self.t_axis[k]
            w_ = sigma**2 * tk
            w_arr[k] = w_
            wx_arr[k] = 2.0 * sigma * tk * sigma1
            wxx_arr[k] = 2.0 * tk * (sigma1**2 + sigma * sigma2)
            x_arr[k] = x_d
        dwdT = np.empty((n_t, n_s))
        for k in range(n_t):
            if n_t == 1:
                dwdT[k] = w_arr[k] / max(self.t_axis[k], 1e-9)
            elif k == 0:
                dwdT[k] = (w_arr[1] - w_arr[0]) / max(self.t_axis[1] - self.t_axis[0], 1e-9)
            elif k == n_t - 1:
                dwdT[k] = (w_arr[-1] - w_arr[-2]) / max(self.t_axis[-1] - self.t_axis[-2], 1e-9)
            else:
                dwdT[k] = (w_arr[k + 1] - w_arr[k - 1]) / max(self.t_axis[k + 1] - self.t_axis[k - 1], 1e-9)
        denominator = (
            1.0
            - (x_arr / np.maximum(w_arr, 1e-12)) * wx_arr
            + 0.25 * (-0.25 - 1.0 / np.maximum(w_arr, 1e-12)) * wx_arr**2
            + 0.5 * wxx_arr
        )
        lv2 = np.divide(
            dwdT, denominator, out=np.full_like(denominator, LV_CLAMP_LO**2), where=np.isfinite(denominator)
        )
        self.V = np.sqrt(np.clip(lv2, LV_CLAMP_LO**2, LV_CLAMP_HI**2))

    def _row(self, t: float) -> np.ndarray:
        """Local-vol row over the dense strike axis, linearly interpolated in t
        (clamped to the fitted tenor range)."""
        if self.t_axis.size == 1:
            return self.V[0]
        tc = min(max(float(t), float(self.t_axis[0])), float(self.t_axis[-1]))
        i = int(np.clip(np.searchsorted(self.t_axis, tc) - 1, 0, self.t_axis.size - 2))
        span = float(self.t_axis[i + 1] - self.t_axis[i])
        w = (tc - float(self.t_axis[i])) / span if span > 0.0 else 0.0
        return (1.0 - w) * self.V[i] + w * self.V[i + 1]

    def sigma_grid(self, t_vals: np.ndarray) -> np.ndarray:
        """Local vol rows at the given years to expiry, one per requested t.

        Returns shape ``(m, GRID_X)`` over the dense strike axis.
        """
        out: np.ndarray = np.empty((t_vals.size, GRID_X), dtype=float)
        for i, tv in enumerate(t_vals):
            out[i] = self._row(float(tv))
        return out

    def sigma(self, t: float, spots: np.ndarray) -> np.ndarray:
        """Local vol at year ``t`` for an array of spot levels (vectorized)."""
        if spots.size == 0:
            return np.zeros(0)
        row = self._row(t)
        return np.interp(np.log(spots), self.log_s, row, left=row[0], right=row[-1])

    def __call__(self, t: float, spots: np.ndarray) -> np.ndarray:
        return self.sigma(t, spots)


def build_locvol_map(
    market_data: dict[str, Any], names: list[str] | tuple[str, ...], eval_serial: int, rate: float
) -> dict[str, LocalVolSurface]:
    """Build one :class:`LocalVolSurface` per underlying that has an eqVol grid."""
    out: dict[str, LocalVolSurface] = {}
    for name in names:
        surface = next((s for s in market_data.get("eqVol", []) if s.get("_id") == name), None)
        if surface is None:
            continue
        spot = next((float(e["spot"]) for e in market_data.get("equity", []) if e.get("_id") == name), None)
        if spot is None:
            continue
        out[name] = LocalVolSurface(surface, int(eval_serial), spot, float(rate))
    return out