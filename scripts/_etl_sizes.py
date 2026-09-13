import json
import sys
import time

sys.path.insert(0, "scripts")
from fina_risk.benchmarking import build_benchmark_corpus
from fina_risk.etl import augment_termsheet as augment_termsheet_fn, compile_pricing_request

N_SAMPLE = 10_000
N = 100_000

def log(msg: str) -> None:
    print(msg, flush=True)

def scale(n: int, fn):
    t0 = time.perf_counter()
    out = fn(n)
    dt = time.perf_counter() - t0
    return out, dt

log("== ETL augment_termsheet (scaled) ==")
ag, dt = scale(N_SAMPLE, lambda n: augment_termsheet_fn(count=n, seed=20260909))
log(f"  {N_SAMPLE:>7,} variants: {dt:.3f}s -> full-100k est {100 * dt:.2f}s")
buf = json.dumps(ag).encode()
log(f"  bytes@10k: {len(buf):,} ({len(buf) / 1e6:.2f} MB) | per variant ~{len(buf) // N_SAMPLE:,} B")

log("== ETL compile_pricing_request (scaled) ==")
reqs, dt = scale(N_SAMPLE, lambda n: [compile_pricing_request(v) for v in ag])
log(f"  {N_SAMPLE:>7,} requests: {dt:.3f}s -> full-100k est {100 * dt:.2f}s")
cb = json.dumps(reqs).encode()
log(f"  bytes@10k: {len(cb):,} ({len(cb) / 1e6:.2f} MB) | per request ~{len(cb) // N_SAMPLE:,} B")

log("== corpus ingestion (geometry, no pricing) ==")
corpus, dt = scale(N_SAMPLE, lambda n: build_benchmark_corpus(
    instruments=n, underlyings=1200, paths=1200, factors=12, seed=20260909,
    unique_structure_count=min(n, 10000), structure_cache=True,
))
log(f"  corpus@10k: {dt:.3f}s")
print("  corpus keys:", list(corpus.keys()), flush=True)
