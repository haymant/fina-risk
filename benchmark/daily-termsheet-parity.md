# Faithful daily termsheet parity

The previous terminal parity path was retained as a regression benchmark. A new daily termsheet path now consumes a shared weekday observation cube and applies the fixture lifecycle state in both Python and C++.

## Implemented state

The daily engine now performs cumulative discrete EKI monitoring, global KO detection when both underlyings meet the global barrier, option termination after KO, terminal physical-delivery PUT payoff only for KI-hit and non-KO paths, per-period range fixing counts, unpaid fixing counters, memory carry, KO termination of future coupon accrual, and payment-date discounting.

The fixture’s shared path cube contains 30,000 paths, 108 weekday observations through the latest job maturity, and two underlyings. The canonical option job itself expires earlier and uses 106 observations; the extra two dates are required by the coupon/funding job maturity.

## Results

| Metric | Python | C++ | Residual |
|---|---:|---:|---:|
| Canonical PV | 1.2418887593575048 | 1.2418887593575000 | 4.66e-15 |
| PUT price | 0.01829008113756282 | 0.01829008113756280 | 1.73e-17 |
| Coupon PV | 0.2751304183222766 | 0.2751304183222720 | 4.61e-15 |
| KI probability | 0.20183333333333334 | 0.20183333333333334 | 0 |
| Global KO probability | 0.5298333333333334 | 0.5298333333333334 | 0 |
| ADBE relative delta | -0.20837947912478727 | -0.20837947912469845 | 8.88e-14 |
| AMZN relative delta | -1.3541503664047272 | -1.3541503664042830 | 4.44e-13 |
| ADBE relative gamma | 0.978009120473633 | 0.9780091204780739 | 4.44e-12 |
| AMZN relative gamma | -10.18544498479601 | -10.18544498479157 | 4.44e-12 |

Coupon fixing averages for the five post-valuation periods are `[19, 22, 21, 24, 20]`, with average memory carry `[1.5758667, 0, 0, 0, 0]` after each period.

This is daily scheduled weekday monitoring, not a claim that the source file contains an explicit exchange holiday calendar.

## Current state (NYSE calendar + conservative surface vols + lake correlation)

The calendar was hardened to the real NYSE schedule (`nyse_serials`, 103 fixings
to the latest job maturity) and the cube is generated with conservative
full-surface vols (exercise + knock-in moneyness, higher wing) and correlation
resolved from the lake store (ADBE-AMZN = 0.4041). Same shared cube for both engines.

Re-run: `scripts/generate_daily_paths.py skills/fina-risk/refs/termsheet1.md.json <out> --paths 30000 --seed 1729` then `scripts/reconcile_daily_termsheet.py`.

| Metric | Python | C++ | Residual |
|---|---:|---:|---:|
| Canonical PV | 1.2278879049922349 | 1.2278879049922349 | -1.02e-14 |
| PUT price | 0.020217522451669773 | 0.020217522451669773 | 3.47e-17 |
| Coupon PV | 0.26305700527111353 | 0.26305700527111353 | 0 |
| KI probability | 0.14863333333333334 | — | — |
| Global KO probability | 0.5412 | — | — |
| ADBE relative delta | -0.2718826869612889 | -0.2718826869612889 | 0 |
| AMZN relative delta | -1.2943681319655287 | -1.2943681319655287 | 8.88e-16 |

Option-job observations: **101** (NYSE serials to the option expiry 46419; the
103-observation cube covers the coupon/funding job maturity 46421). Coupon
fixing averages `[18, 22, 20, 22, 19]`, memory carry `[2.2904, 0, 0, 0, 0]`.
Vols used: ADBE 0.474628, AMZN 0.400025.
