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

## Conservative fixture lane (surface vol + NYSE calendar)

`price_fixture` in `cpp/src/fina_risk_cpp.cpp` was upgraded from the flat-ATM,
weekday-stepped, unconditional-put lane to a faithful conservative EKI lane:

1. **Full vol surface.** The `eqVol` grid is now interpolated (linear in strike;
   linear in total variance across the two bracketing maturity pillars) at both
   the exercise (0.78x initial) and knock-in (0.70x initial) moneyness, and the
   **higher downside-wing vol** per underlying is used. The old read collapsed
   the grid to the single ATM point (262.47 -> 44.95% for ADBE, 258.48 -> 35.62%
   for AMZN); the conservative read is 47.48% / 40.06% (AMZN 0.70x = 40.06%).
2. **Real NYSE calendar.** `nyse_schedule` (Mon-Fri minus US market holidays:
   New Year, MLK, Presidents, Good Friday, Memorial, Juneteenth, Independence,
   Labor, Thanksgiving, Christmas, with observed-date shifting) drives the
   `dt = 1/252` grid. It removes the 5 holiday observations the weekday
   placeholder counted in this window (2026-09-07 Labor Day, 11-26, 12-25,
   2027-01-01, 01-18) — fewer steps, slightly less variance.
3. **Curve interpolation** to the option expiry (was `curve[0]`, the 1M pillar)
   and **dividend yield** in the drift `(r - q)` from the equity cash-dividend
   schedule.
4. **EKI gate + global KO.** The put now pays only when the final-fixing worst
   ratio `<= KIBarrier` and the note was not called. Pre-locked `GKOLocked`
   memory state is deliberately not credited (under-detecting calls raises the
   short-put reserve — the conservative sell-side choice).

The Python mirror (`fina_risk.daily_termsheet`) gained matching `nyse_serials`
and now uses EKI (terminal-only) rather than continuous `any`-day monitoring.

## Reference numbers — termsheet1 fixture

`skills/fina-risk/refs/termsheet1.md.json`, 30k paths, seed 1729, AAD off.
Correlation is now resolved from the lake store (ADBE-AMZN = **0.4041**, was
0.459325 in the market data) and substituted on both engines. The daily lane
uses the real NYSE calendar (101 fixings to the 2027-02-01 option expiry on a
103-observation cube) and conservative surface vols (ADBE 0.47463, AMZN 0.40003).

| lane | PUT | COUPON | FUNDING | PV | KI prob |
|---|---:|---:|---:|---:|---:|
| terminal `price_fixture` (python, resolved corr, **default = KI-gated Dupire LV**) | **0.021979** | 0.000000 | 0.985048 | 0.959124 | — |
| terminal `price_fixture` (`locvol=False`, KI-gated scalar ATM) | 0.016970 | 0.000000 | 0.985048 | 0.968079 | — |
| terminal `price_fixture` (`locvol=False`, **vanilla** — regression baseline) | 0.021429 | 0.000000 | 0.985048 | 0.963619 | — |
| C++ `price_fixture` (surface + NYSE, resolved corr) | 0.020735 | 0.000000 | 0.984260 | 0.963745 | — |
| canonical PV (PUT+FUNDING job1 + COUPON job3) | py 1.4735064 · cpp 1.4734669 · Δ 3.95e-05 | | | | |
| **daily EKI (python == cpp, NYSE + surface + resolved corr)** | **0.020218** | **0.263057** | 0.984260 | **1.227888** | 0.1486 |
| legacy engine reported | ~0.02113 | — | — | — | — |

The canonical terminal PV moved from **1.4738664** (flat ρ=0.4593) to
**1.4735064** (ρ=0.4041) — the lower lake correlation raises the worst-of PUT
(+0.00036). The daily EKI lane is bit-exact Python==C++ (residual ~1e-14).
Terminal C++ vs python residual (4e-05) is the pre-existing MC-model spread
(C++ fixtures on NYSE day-steps + dividends + EKI/KO gates; python reference on
a continuous 101-step, no-dividend kernel).

Daily EKI detail: `coupon_fixings=[18,22,20,22,19]`,
`memory_carry=[2.2904,0,0,0,0]`, `ko_probability=0.5412`,
`relative_delta=[-0.271883,-1.294368]`.

### PUT contract — European knock-in gate (terminal lane)

The fixture PUT is the **ELIFCN_KI down-and-in**, not a vanilla put: it pays
`max(strike − worst, 0)` **only when the final-fixing worst ratio ≤ KIBarrier
(0.70)**. `price_fixture` now enforces this with
`price_terminal_legs(..., knock_in=ki)` where
`ki = worst(final) <= knockInStar.KIBarrier`, and the XAD lane takes the same
mask (`aad_put_sensitivity(..., knock_in=ki)`) so `aad.value == put_option_price`
still holds.

**Why this matters (the regression trap):** the terminal lane was introduced
(commit `85332b6`) computing `ki` but using it only for
`barrier_hit_probability`; the payoff was `df·max(strike−worst,0).mean()` —
**vanilla**. Commit `1079f57` moved that unconditional payoff into the shared
kernel `price_terminal_legs`, so the terminal lane stayed vanilla by
construction, while only `fina_risk.daily_termsheet` gated (`ki_hit`). Two lanes
therefore priced two different products and the "canonical PUT" (0.021429)
silently dropped the no-knock-in branch. Wiring Dupire local vol into that
vanilla lane pushed it up (0.025924) instead of reconciling. **Do not price the
terminal PUT without `knock_in`.**

EKI-gated PUT ladder, termsheet1 fixture, 30k paths, seed 1729 (legacy PY
reference **0.02113**):

| vol read | PUT |
|---|---:|
| ATM point (`_parse_vol`) | 0.016970 |
| surface @ 78% moneyness | 0.020133 |
| surface @ 76% / 74% moneyness | 0.020979 / 0.021848 |
| **Dupire LV (full smile)** | **0.021979** |
| daily EKI lane (gate + memory-KO, conservative vols) | 0.020218 |

The KI gate + Dupire LV (0.021979) sits ~4% above the legacy 0.02113; the
residual is the legacy's surface interpolation at the moneyness. The `vol_ratio`
argument (`bump_result` / `pricing_and_sensitivity`) selects the scalar surface
read so this can be reconciled empirically.

**Default:** the pricing lane now defaults to `locvol=True` (KI-gated Dupire LV),
so `price_fixture` / `bump_result` / `pricing_and_sensitivity` return **0.021979**
for the fixture PUT unless the caller passes `locvol=False` (scalar) or a
`vol_ratio` (scalar surface read).

## Reference numbers — 100k benchmark

`scripts/benchmark_lanes.py`, 100k worst-of ELIFCN_KI, 3k paths, ρ=0.459325, AAD off.

| lane | wall s | inst/s | mean PV | notes |
|---|---|---:|---:|---|
| terminal parity (flat ATM, weekday) | 1.50 | 66,765 | 0.970200 | prior baseline |
| daily EKI batch (flat ATM, weekday) | 47.55 | 2,226 | 1.341249 | put 11,608.96, coupon 47,424.74, KI 0.4685, KO 0.2507 |
| terminal parity (surface + NYSE) | 1.08 | 92,927 | 0.962096 | delta$ −18,990.66, gamma 3.0649 |
| daily EKI batch (surface + NYSE + schedule variants) | 31.80 | 3,433 | 1.305912 | put 11,938.09, coupon 44,220.18, KI 0.4764, KO 0.2623 |
| **daily EKI batch (+ looked-up correlation)** | **31.40** | **3,477** | **1.309409** | put 12,032.17, coupon 44,663.98, KI 0.4812, KO 0.2532 |

**Correlation lookup (replaces the hard-coded `RHO=0.459325`).** The universe
matrix is read from the lake store `<lake>/correlations.parquet`
(**1,301,691 rows**, 1,613 legs, as-of 2026-09-06, window 252, alpha 0.4) via
`fina_risk.correlation.resolve_correlation_matrix`. Resolved ADBE/AMZN/MSFT/NVDA:

```
[[1.0000 0.4041 0.5415 0.2138]
 [0.4041 1.0000 0.4849 0.4253]
 [0.5415 0.4849 1.0000 0.4278]
 [0.2138 0.4253 0.4278 1.0000]]
```

Design choice — **resolve upstream, pack the resolved matrix, never ship the
table**: one filtered DuckDB columnar scan (31.5 ms for 4 legs; ~0.31 s for a
300-leg universe = 44,850 pairs) produces a U×U PSD-clamped matrix that is
packed into the market payload and Cholesky-factored once for the shared cube.
The engine consumes U² floats, not 1.3M rows. Per-instrument baskets drawn from
one batch share a single simulated universe, so a per-universe matrix is exact;
per-*instrument* correlations with a shared cube would require the shock-cube
variant (shared standard normals + per-trade factor transform).

Schedule variants added to the corpus (and the kernel): per-trade
`coupon_periods` ∈ 2..9 (evenly ~12.5k each), observation `stride` ∈ {1 daily,
5 weekly, 21 monthly} with a random phase (~33k each), an explicit EKI
`ki_obs` date shifted 0..5 days before expiry, and per-period `start` dates
replacing the hard-coded ~one-month tail. The kernel counts in-range
observations on this per-period schedule; default fields reproduce the old
behaviour exactly (fixture parity residual 3e-17).

Upstream ETL (Rust sonic-rs, `scripts/run_100k_rust_cpp.py`): **1.28 s** to convert
100k legacy term-sheet records to fina-native (100k master + 200k unwound parquet
rows); generation 1.4 s; terminal C++ lane 1.38 s.

## Open convention decisions

1. **EKI level.** Resolved in `price_fixture`: it gates on
   `knockInStar.KIBarrier == 0.70` *and* reads the full vol surface, giving PUT
   0.02034 (legacy ~0.02113). The old flat-ATM gate at 0.70 gave 0.01619; the
   0.70–0.78 band is not knocked out. The residual ~4% vs legacy is a candidate
   for an explicit conservative vol margin, not a barrier-convention change.
2. **Daily coupon denominator.** Currently `N2` (fixture), numerator
   `max(observed_daily_fixings − N1, 0) + memory`, capped at 1.
3. **Terminal lane vs daily lane.** The terminal parity lane is fast but does not
   observe daily; only the daily variants count KO/memory/N1/N2. Keep both and
   label the method in the report.
