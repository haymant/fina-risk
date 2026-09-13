import json
import time

from fina_risk.benchmarking import build_benchmark_corpus

N = 100_000

t0 = time.perf_counter()
corp = build_benchmark_corpus(
    instruments=N, underlyings=1200, paths=3000, factors=12, seed=20260909,
    unique_structure_count=N, structure_cache=True,
)
print(f"corpus geometry build: {time.perf_counter() - t0:.3f}s")
print("  keys:", list(corp.keys()))
spots = corp["spots"]
idx = corp["basket_idx"]
strikes = corp["strikes"]
u = corp["unique_baskets"]
terminal = corp["terminal"]


def jelly_spot_market() -> dict:
    return {"underlyings": [{"id": f"EQ{i:04d} US", "spot": float(spots[i])} for i in range(spots.size)]}


def jelly_instruments() -> dict:
    recs = []
    for i in range(N):
        key = i % u
        ii = idx[key]
        recs.append(
            {
                "instrumentId": f"ELI-BENCH-{i:05d}",
                "underlyings": [f"EQ{int(j):04d} US" for j in ii],
                "legs": [
                    {
                        "leg_id": 1,
                        "leg_type": "intrinsic_option",
                        "leg_name": "PUT",
                        "multiplier": -1,
                        "payoff": {"basket": "worst_of", "strike": float(strikes[key])},
                    }
                ],
            }
        )
    return {"instruments": recs}


t0 = time.perf_counter()
market = jelly_spot_market()
t1 = time.perf_counter()
mb = json.dumps(market, separators=(",", ":")).encode()
t2 = time.perf_counter()
inst = jelly_instruments()
t3 = time.perf_counter()
ib = json.dumps(inst, separators=(",", ":")).encode()
t4 = time.perf_counter()
print(f"build market json: {t1 - t0:.3f}s  dumps: {t2 - t1:.3f}s  -> {len(mb):,} B ({len(mb) / 1e6:.2f} MB)")
print(f"build instruments json (100k term-sheet-like): {t3 - t2:.3f}s  dumps: {t4 - t3:.3f}s  -> {len(ib):,} B ({len(ib) / 1e6:.2f} MB)")
print(f"total ETL json input (market+instruments): {len(mb) + len(ib):,} B ({(len(mb) + len(ib)) / 1e6:.2f} MB)  build wall ~{t4 - t0:.3f}s")
print(f"terminal cube: {terminal.nbytes:,} B ({terminal.nbytes / 1e6:.0f} MB)")
