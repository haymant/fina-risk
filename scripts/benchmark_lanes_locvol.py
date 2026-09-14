#!/usr/bin/env python3
"""Benchmark the Dupire local-vol cube against the flat scalar cube.

Same lane harness as ``benchmark_lanes.py`` (30k instruments x 3k paths, AAD
off): one shared correlated cube is priced by the C++ terminal and daily EKI
kernels.  The only difference here is *how the cube is generated*:

  scalar   flat conservative per-underlying vol, vectorized cumsum
  locvol   per-step Dupire sigma(t, S_t) sampled from the eqVol surface, on the
           same CRN normals as the scalar cube

Because both lanes consume the same cube artifact, LV adds cost only at cube
generation (surface calibration + per-step sampling); the lane wall times should
match.  The PV checksums differ by the local-vol correction.

Writes the result JSON and prints a table.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))

import benchmark_lanes as bl  # noqa: E402
import fina_risk_cpp  # noqa: E402

from fina_risk.correlation import resolve_correlation_matrix  # noqa: E402
from fina_risk.daily_termsheet import nyse_serials  # noqa: E402
from fina_risk.locvol import build_locvol_map  # noqa: E402


def make_eqvol(symbols: tuple[str, ...]) -> dict:
    """Synthesize an eqVol surface per symbol: serial maturities, absolute
    strikes around spot, and a downward equity skew with a mild term slope."""
    grid_m = np.array([90, 180, 365, 545, 730], dtype=float)
    fracs = np.array([0.60, 0.70, 0.78, 0.90, 1.00, 1.10, 1.20, 1.30], dtype=float)
    surfaces = []
    for name in symbols:
        spot = bl.SPOTS[name]
        base = bl.VOLS[name]
        strikes = np.round(fracs * spot, 4).tolist()
        maturities = (bl.EVAL + grid_m).tolist()
        vol = np.empty((grid_m.size, fracs.size))
        for j, t in enumerate(grid_m / 365.0):
            # skew: ~ -1.5 vol pts per 10% below spot; small positive term slope.
            smile = base * (1.0 + 0.15 * t) - 0.35 * (fracs - 1.0)
            vol[j] = np.round(np.maximum(smile, 0.05) * 100.0, 4)
        surfaces.append({"_id": name, "strike": strikes, "maturity": maturities, "vol": vol.tolist()})
    equity = [{"_id": name, "spot": bl.SPOTS[name]} for name in symbols]
    return {"eqVol": surfaces, "equity": equity}


def correlated_normals(paths: int, observations: int, cholesky: np.ndarray) -> np.ndarray:
    rng = np.random.default_rng(bl.SEED)
    return rng.standard_normal((paths, observations, len(bl.SYMBOLS))) @ cholesky.T


def scalar_cube(ids: list[str], z: np.ndarray) -> np.ndarray:
    spot = np.asarray([bl.SPOTS[i] for i in ids])
    vol = np.asarray([bl.VOLS[i] for i in ids])
    dt = 1.0 / 252.0
    drift = (bl.RATE - 0.5 * vol**2)[None, None, :] * dt
    return (spot[None, None, :] * np.exp(np.cumsum(drift + vol[None, None, :] * math.sqrt(dt) * z, axis=1))).astype(
        np.float64
    )


def locvol_cube(ids: list[str], z: np.ndarray, lvmap: dict, dates: np.ndarray) -> np.ndarray:
    paths, observations, n = z.shape
    spot = np.asarray([bl.SPOTS[i] for i in ids])
    dt = 1.0 / 252.0
    sqrt_dt = math.sqrt(dt)
    log_s = np.broadcast_to(np.log(spot)[None, :], (paths, n)).copy()
    cube = np.empty((paths, observations, n), dtype=np.float64)
    for s in range(observations):
        t_s = float(dates[s] - bl.EVAL) / 365.0
        sig = np.empty((paths, n))
        for i in range(n):
            lv = lvmap.get(ids[i])
            sig[:, i] = lv.sigma(t_s, np.exp(log_s[:, i])) if lv is not None else bl.VOLS[ids[i]]
        log_s += (bl.RATE - 0.5 * sig**2) * dt + sig * sqrt_dt * z[:, s, :]
        cube[:, s, :] = np.exp(log_s)
    return cube


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--instruments", type=int, default=30_000)
    ap.add_argument("--paths", type=int, default=3_000)
    ap.add_argument("--out", type=Path, default=Path("benchmark/lanes_locvol.json"))
    args = ap.parse_args()

    corpus = bl.generate(args.instruments)
    ids = corpus["ids"]
    dates = nyse_serials(bl.EVAL, 46421).astype(np.int32)
    obs = int(dates.size)

    corr, _ = resolve_correlation_matrix([bl.LEGS[i] for i in ids], fallback_rho=bl.FALLBACK_RHO)
    cholesky = np.linalg.cholesky(corr)
    corpus["daily_market"]["correlationMatrix"] = corr.tolist()
    corpus["daily_market"]["correlationLegs"] = [bl.LEGS[i] for i in ids]

    z = correlated_normals(args.paths, obs, cholesky)

    # --- surface calibration (once per underlying) ---
    t0 = time.perf_counter()
    lvmap = build_locvol_map(make_eqvol(bl.SYMBOLS), list(bl.SYMBOLS), bl.EVAL, bl.RATE)
    calib_s = time.perf_counter() - t0

    # --- cube generation ---
    t0 = time.perf_counter()
    scalar_daily = scalar_cube(ids, z)
    scalar_gen_s = time.perf_counter() - t0
    scalar_terminal = scalar_daily[:, -1, :]  # terminal slice of the shared cube

    t0 = time.perf_counter()
    locvol_daily = locvol_cube(ids, z, lvmap, dates)
    locvol_gen_s = time.perf_counter() - t0
    locvol_terminal = locvol_daily[:, -1, :]

    def run_lanes(daily_cube: np.ndarray, terminal_cube: np.ndarray) -> tuple[dict, float, dict, float]:
        t = time.perf_counter()
        l1 = json.loads(
            fina_risk_cpp.run_cpp_parity(
                json.dumps(corpus["terminal"]), json.dumps(corpus["market"]), terminal_cube, bl.SEED, 0.01
            )
        )
        l1_s = time.perf_counter() - t
        t = time.perf_counter()
        l2 = json.loads(
            fina_risk_cpp.run_daily_termsheet_batch(
                json.dumps(corpus["daily"]), json.dumps(corpus["daily_market"]), daily_cube, dates
            )
        )
        l2_s = time.perf_counter() - t
        return l1, l1_s, l2, l2_s

    s1, s1_s, s2, s2_s = run_lanes(scalar_daily, scalar_terminal)
    v1, v1_s, v2, v2_s = run_lanes(locvol_daily, locvol_terminal)

    result = {
        "instruments": args.instruments,
        "paths": args.paths,
        "observations": obs,
        "vol_model": {"scalar": "conservative_surface_wing", "locvol": "dupire_from_eqvol"},
        "cube": {
            "scalar_gen_seconds": scalar_gen_s,
            "locvol_gen_seconds": locvol_gen_s,
            "calibration_seconds": calib_s,
            "locvol_slowdown_vs_scalar": locvol_gen_s / max(scalar_gen_s, 1e-9),
            "locvol_total_vs_scalar": (locvol_gen_s + calib_s) / max(scalar_gen_s, 1e-9),
        },
        "lanes": {
            "scalar": {
                "terminal_parity": {**s1, "wall_seconds": s1_s, "instruments_per_second": args.instruments / s1_s},
                "daily_eki_batch": {**s2, "wall_seconds": s2_s},
            },
            "locvol": {
                "terminal_parity": {**v1, "wall_seconds": v1_s, "instruments_per_second": args.instruments / v1_s},
                "daily_eki_batch": {**v2, "wall_seconds": v2_s},
            },
        },
        "pv_delta": {
            "terminal_pv_checksum": v1["pv_checksum"] - s1["pv_checksum"],
            "daily_mean_pv": v2["mean_pv"] - s2["mean_pv"],
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2))

    print(f"\n=== locvol vs scalar ({args.instruments:,} instruments x {args.paths:,} paths, obs={obs}) ===")
    print(f"surface calibration (4 names) : {calib_s:8.4f} s")
    print(f"scalar cube gen               : {scalar_gen_s:8.4f} s")
    print(f"locvol cube gen               : {locvol_gen_s:8.4f} s   ({locvol_gen_s / max(scalar_gen_s, 1e-9):.2f}x)")
    print(f"locvol + calibration vs scalar: {(locvol_gen_s + calib_s) / max(scalar_gen_s, 1e-9):.2f}x")
    print()
    print(f"{'lane':<18}{'scalar s':>10}{'locvol s':>10}{'ratio':>8}{'scalar PV':>14}{'locvol PV':>14}")
    print(
        f"{'terminal_parity':<18}{s1_s:>10.3f}{v1_s:>10.3f}{v1_s / max(s1_s, 1e-9):>8.2f}"
        f"{s1['pv_checksum']:>14.4f}{v1['pv_checksum']:>14.4f}"
    )
    print(
        f"{'daily_eki_batch':<18}{s2_s:>10.3f}{v2_s:>10.3f}{v2_s / max(s2_s, 1e-9):>8.2f}"
        f"{s2['mean_pv']:>14.6f}{v2['mean_pv']:>14.6f}"
    )
    print(
        f"\nPV delta (locvol - scalar): terminal={v1['pv_checksum'] - s1['pv_checksum']:+.4f} "
        f"daily_mean={v2['mean_pv'] - s2['mean_pv']:+.6f}"
    )
    print(f"[out] {args.out}")


if __name__ == "__main__":
    main()
