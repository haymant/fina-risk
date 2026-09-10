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

This is daily scheduled weekday monitoring, not a claim that the source file contains an explicit exchange holiday calendar. The next calendar-hardening step is to replace the weekday schedule with QuantLib NYSE calendar dates for exact production fixing eligibility.
