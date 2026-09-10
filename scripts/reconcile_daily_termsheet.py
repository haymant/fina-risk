from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from fina_risk.daily_termsheet import price_daily_termsheet, weekday_serials
from fina_risk.pricing import common_from_job, load_legacy_request


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("fixture")
    ap.add_argument("daily_paths")
    args = ap.parse_args()
    root = json.loads(Path(args.fixture).read_text())
    jobs = load_legacy_request(root)
    meta = json.loads(Path(args.daily_paths + ".meta.json").read_text())
    spots = np.fromfile(args.daily_paths, dtype=np.float64).reshape(
        meta["paths"], meta["observations"], meta["underlyings"]
    )
    def canonical(value: np.ndarray) -> float:
        rs = []
        for job in jobs:
            common = common_from_job(job)
            expiry = int(common["dealData"].get("expiryDate", common["dealData"].get("maturityDate")))
            count = len(weekday_serials(int(common["marketData"]["evaluationDate"]), expiry))
            rs.append(price_daily_termsheet(common, value[:, :count, :]))
        return rs[0].pv - rs[0].coupon_pv + rs[2].coupon_pv

    results = []
    for job in jobs:
        common = common_from_job(job)
        expiry = int(common["dealData"].get("expiryDate", common["dealData"].get("maturityDate")))
        count = len(weekday_serials(int(common["marketData"]["evaluationDate"]), expiry))
        results.append(price_daily_termsheet(common, spots[:, :count, :]))
    first = results[0]
    coupon = results[2].coupon_pv if len(results) >= 3 else first.coupon_pv
    canonical_pv = first.pv - first.coupon_pv + coupon
    deltas = []
    gammas = []
    for i in range(spots.shape[2]):
        up = spots.copy()
        down = spots.copy()
        up[:, :, i] *= 1.01
        down[:, :, i] *= 0.99
        pu, pd = canonical(up), canonical(down)
        deltas.append((pu - pd) / (0.02 * 1.0))
        gammas.append((pu - 2.0 * canonical_pv + pd) / 0.01**2)
    summary = {
        "engine": "python_daily_termsheet",
        "daily_observations": first.daily_observations,
        "observation_dates": first.observation_dates,
        "job_pvs": [r.pv for r in results],
        "job_put_prices": [r.put_price for r in results],
        "job_coupon_pvs": [r.coupon_pv for r in results],
        "pv": canonical_pv,
        "put_price": first.put_price,
        "coupon_pv": coupon,
        "relative_delta": deltas,
        "relative_gamma": gammas,
        "ki_probability": first.ki_probability,
        "ko_probability": first.ko_probability,
        "coupon_fixings": results[2].coupon_fixings if len(results) >= 3 else first.coupon_fixings,
        "memory_carry": results[2].memory_carry if len(results) >= 3 else first.memory_carry,
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
