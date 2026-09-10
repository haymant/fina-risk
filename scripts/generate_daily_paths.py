from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from fina_risk.daily_termsheet import weekday_serials
from fina_risk.pricing import common_from_job, load_legacy_request, market_from_legacy


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("fixture")
    ap.add_argument("output")
    ap.add_argument("--paths", type=int, default=30000)
    ap.add_argument("--seed", type=int, default=1729)
    args = ap.parse_args()
    root = json.loads(Path(args.fixture).read_text())
    common = common_from_job(load_legacy_request(root)[0])
    market = market_from_legacy(common, paths=args.paths, seed=args.seed)
    expiry = max(
        int(j["commonData"]["dealData"].get("expiryDate", j["commonData"]["dealData"].get("maturityDate")))
        for j in root["Chunk"]["Jobs"]
    )
    dates = weekday_serials(market.evaluation_date, expiry)
    steps = len(dates)
    rng = np.random.default_rng(args.seed)
    z1 = rng.standard_normal((args.paths, steps))
    corr_scale = np.sqrt(max(1.0 - market.correlation**2, 0.0))
    z2 = market.correlation * z1 + corr_scale * rng.standard_normal((args.paths, steps))
    dt = 1.0 / 252.0
    shocks = np.stack((z1, z2), axis=2)
    drift = (market.rate - 0.5 * market.vols**2)[None, None, :] * dt
    diffusion = market.vols[None, None, :] * np.sqrt(dt) * shocks
    increments = drift + diffusion
    log_spots = np.log(market.quoted_spots)[None, None, :] + np.cumsum(increments, axis=1)
    spots = np.exp(log_spots).astype(np.float64)
    spots.tofile(args.output)
    meta = {"paths": args.paths, "observations": steps, "underlyings": 2, "seed": args.seed, "dates": dates.tolist()}
    Path(args.output + ".meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    summary = {"output": args.output, "paths": args.paths, "observations": steps, "dates": dates.tolist()}
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
