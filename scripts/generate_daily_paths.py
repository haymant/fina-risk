from __future__ import annotations

import argparse
import json
from pathlib import Path

from fina_risk.daily_termsheet import build_daily_path_cube


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("fixture")
    ap.add_argument("output", help="raw float64 (paths, observations, underlyings) cube file; meta is written to <output>.meta.json")
    ap.add_argument("--paths", type=int, default=30000)
    ap.add_argument("--seed", type=int, default=1729)
    ap.add_argument("--locvol", action="store_true", help="per-step Dupire local vol instead of flat scalar surface")
    args = ap.parse_args()
    spots, meta = build_daily_path_cube(args.fixture, paths=args.paths, seed=args.seed, locvol=args.locvol)
    spots.tofile(args.output)
    Path(args.output + ".meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    summary = {
        "output": args.output,
        "paths": meta["paths"],
        "observations": meta["observations"],
        "vols": meta["vols"],
        "calendar": meta["calendar"],
        "dates": meta["dates"],
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()