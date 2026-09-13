# Pricing lane checkpoint

Snapshot of the fina-risk pricing lanes, the C++ kernel changes, and the
reference numbers so future work does not re-derive them. All runs are AAD
**disabled** (the native lane is finite-difference / CRN by design).

## Kernel changes made

`cpp/src/fina_risk_cpp.cpp` + `cpp/include/fina_risk_cpp.hpp` + `cpp/bindings/fina_risk_pybind.cpp`:

1. **Type-error fixes at the pybind boundary.**
   - `price_fixture`: `paths` accepts `double` (a JSON `30000.0` no longer raises
     `TypeError: incompatible function arguments`).
   - `run_cpp_parity`: `terminal` accepts a float32 array-like **or** a raw
     float32 `bytes` buffer (both normalise to the same cube).
2. **Daily lifecycle lane exposed to Python.**
   - `run_daily_termsheet(request_json, paths(P,O,U), dates, bump)` — faithful
     single-term-sheet daily pricing.
   - `run_daily_termsheet_batch(instruments_json, market_json, paths(P,O,U), dates)`
     — batched version returning checksums + throughput.
   - Both observe **daily**: per accrual period they count the daily in-range
     observations (`lowRange ≤ worst ≤ upRange`), net out already-paid `N1`,
     carry memory forward, use `N2` as the denominator, and stop accruing once
     global KO has fired.
   - Knock-in is **EKI** (final fixing only) — the old `daily_termsheet.cpp`
     app used continuous `any`-day KI and was not faithful to `knockInType=EKI`.

Rebuild:

```bash
cd FinA/fina-risk
cmake -S cpp -B cpp/build -DCMAKE_BUILD_TYPE=Release \
  -Dpybind11_DIR="$(.venv/bin/python -c 'import pybind11; print(pybind11.get_cmake_dir())')"
cmake --build cpp/build -j2 --target fina_risk_cpp
cp cpp/build/fina_risk_cpp.cpython-312-x86_64-linux-gnu.so \
   .venv/lib/python3.12/site-packages/
```

## Lanes

| lane | binding | observation | features | speed |
|---|---|---|---|---|
| terminal parity | `run_cpp_parity` | terminal cube | worst-of KI PUT + funding; delta/gamma/Taylor-2 | fastest |
| daily EKI batch | `run_daily_termsheet_batch` | daily cube | **N1/N2 daily fixings, memory carry, global-KO termination, EKI PUT** | ~30x slower |
| daily EKI single | `run_daily_termsheet` | daily cube | same as above, one term sheet | — |
| python mirror | `fina_risk.cpp_parity` / `pricing.price_fixture` | terminal | same maths as terminal lane (RNG differs) | slow |
| legacy daily app | `cpp/build/fina-risk-cpp-daily-termsheet` | daily cube | daily N1/N2/memory/KO, but **continuous KI** | — |

## Reference numbers — termsheet1 fixture

`skills/fina-risk/refs/termsheet1.md.json`, 30k paths, 108 weekday observations,
seed 1729, ρ=0.459325, AAD off.

| lane | PUT | COUPON | FUNDING | PV | KI prob |
|---|---:|---:|---:|---:|---:|
| terminal `price_fixture` (static N1/N2) | 0.021435 | 0.000000 | 0.985048 | 0.963614 | — |
| **daily EKI kernel (`KIBarrier=0.70`)** | **0.016186** | **0.275130** | 0.985048 | **1.243993** | 0.1207 |
| daily continuous KI (old app / python) | 0.018290 | 0.275130 | 0.985048 | 1.241889 | 0.2018 |
| legacy engine reported | ~0.02113 | — | — | — | — |

Daily EKI detail: `coupon_fixings=[19,22,21,24,20]`, `memory_carry=[1.576,0,0,0,0]`,
`ko_probability=0.5298`, `relative_delta=[-0.2108,-1.3694]`, `relative_gamma=[0.558,-9.919]`.

## Reference numbers — 100k benchmark

`scripts/benchmark_lanes.py`, 100k worst-of ELIFCN_KI, 3k paths, ρ=0.459325, AAD off.

| lane | wall s | inst/s | mean PV | notes |
|---|---:|---:|---:|---|
| terminal parity | 1.50 | 66,765 | 0.970200 | delta$ −16,867.18, gamma 3.2164, Taylor² forecast −162.90, unexplained −1.105 |
| daily EKI batch | 47.55 | 2,226 | 1.341249 | put 11,608.96, coupon 47,424.74, KI 0.4685, KO 0.2507 |

Upstream ETL (Rust sonic-rs, `scripts/run_100k_rust_cpp.py`): **1.28 s** to convert
100k legacy term-sheet records to fina-native (100k master + 200k unwound parquet
rows); generation 1.4 s; terminal C++ lane 1.38 s.

## Open convention decisions

1. **EKI level.** The kernel gates on `knockInStar.KIBarrier == 0.70`, giving PUT
   0.01619. Legacy reports ~0.02113, which matches gating at
   `maturBarrier == strikeKI2 == 0.78` (no-op gate, i.e. the 0.70–0.78 band is
   not knocked out). Pick one and make it explicit.
2. **Daily coupon denominator.** Currently `N2` (fixture), numerator
   `max(observed_daily_fixings − N1, 0) + memory`, capped at 1.
3. **Terminal lane vs daily lane.** The terminal parity lane is fast but does not
   observe daily; only the daily variants count KO/memory/N1/N2. Keep both and
   label the method in the report.
