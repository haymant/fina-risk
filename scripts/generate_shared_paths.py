from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("market")
    parser.add_argument("output")
    parser.add_argument("--paths", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=20260909)
    args = parser.parse_args()
    market = json.loads(Path(args.market).read_text())
    underlyings = market["underlyings"]
    n = len(underlyings)
    factors = int(market["simulation"]["factorCount"])
    rng = np.random.default_rng(args.seed)
    factor_terminal = rng.standard_normal((args.paths, factors), dtype=np.float32)
    loadings = rng.normal(0.0, 0.08, (n, factors)).astype(np.float32)
    spots = np.asarray([u["spot"] for u in underlyings], dtype=np.float32)
    terminal = spots[None, :] * np.exp(factor_terminal @ loadings.T)
    terminal.astype(np.float32, copy=False).tofile(args.output)
    meta = {"paths": args.paths, "underlyings": n, "factors": factors, "seed": args.seed}
    Path(args.output + ".meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    summary = {
        "output": args.output,
        "paths": args.paths,
        "underlyings": n,
        "factors": factors,
        "bytes": int(terminal.nbytes),
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
