from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("instruments")
    parser.add_argument("market")
    parser.add_argument("paths")
    parser.add_argument("--bump", type=float, default=0.01)
    args = parser.parse_args()
    started = time.perf_counter()
    instruments = json.loads(Path(args.instruments).read_text())["instruments"]
    market = json.loads(Path(args.market).read_text())
    terminal = np.fromfile(args.paths, dtype=np.float32).reshape(-1, len(market["underlyings"]))
    spots = np.asarray([u["spot"] for u in market["underlyings"]], dtype=np.float64)
    id_to_idx = {u["id"]: i for i, u in enumerate(market["underlyings"])}
    ingestion = time.perf_counter()
    pv_sum = delta_sum = gamma_sum = forecast_sum = actual_sum = 0.0
    delta_dollar_sum = 0.0
    for inst in instruments:
        idx = np.asarray([id_to_idx[x] for x in inst["underlyings"]], dtype=np.int64)
        strike = float(inst["legs"][0]["payoff"]["strike"])
        ratio = terminal[:, idx] / spots[idx][None, :]
        worst = ratio.min(axis=1)
        base = np.maximum(strike - worst, 0.0)
        deltas = np.zeros(len(idx), dtype=np.float64)
        gammas = np.zeros(len(idx), dtype=np.float64)
        for k in range(len(idx)):
            up = ratio.copy()
            down = ratio.copy()
            up[:, k] *= 1.0 + args.bump
            down[:, k] *= 1.0 - args.bump
            up_pv = np.maximum(strike - up.min(axis=1), 0.0).mean()
            down_pv = np.maximum(strike - down.min(axis=1), 0.0).mean()
            deltas[k] = (up_pv - down_pv) / (2.0 * args.bump * spots[idx[k]])
            gammas[k] = (up_pv - 2.0 * base.mean() + down_pv) / (args.bump * spots[idx[k]]) ** 2
        base_pv = 1.0 - base.mean()
        forecast = float(np.sum(deltas * spots[idx] * args.bump + 0.5 * gammas * (spots[idx] * args.bump) ** 2))
        shocked = np.maximum(strike - (worst * (1.0 + args.bump)), 0.0).mean()
        actual = float(shocked - base.mean())
        pv_sum += base_pv
        delta_sum += float(deltas.sum())
        delta_dollar_sum += float(np.sum(deltas * spots[idx]))
        gamma_sum += float(gammas.sum())
        forecast_sum += forecast
        actual_sum += actual
    finished = time.perf_counter()
    summary = {
        "instruments": len(instruments),
        "underlyings": len(spots),
        "paths": int(terminal.shape[0]),
        "pv_checksum": pv_sum,
        "delta_checksum": delta_sum,
        "delta_dollar_checksum": delta_dollar_sum,
        "gamma_checksum": gamma_sum,
        "taylor_forecast_checksum": forecast_sum,
        "taylor_actual_checksum": actual_sum,
        "taylor_unexplained_checksum": actual_sum - forecast_sum,
        "elapsed_seconds": finished - started,
        "instruments_per_second": len(instruments) / max(finished - started, 1e-12),
        "shared_path_cube": str(args.paths),
        "stage_ingestion_seconds": ingestion - started,
        "stage_compute_seconds": finished - ingestion,
        "method": "CRN_BUMP_REVALUE_WITH_PATHWISE_DELTA",
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
