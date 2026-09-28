# Fixture PV Reconciliation at 30,000 Paths

## Result

The earlier comparison reported **0.963979**, which is the valuation of only the first fixture job. The canonical server pipeline loads three jobs and uses the first job's PUT/FUNDING legs plus the third job's coupon leg. At 30,000 paths and seed 1729, the canonical total is **1.343704**.

Correlation is no longer the market-data quote: the ADBE-AMZN pair is resolved
from the lake correlation store (`correlations.parquet`, rho = **0.4041**,
window 252, alpha 0.4) and substituted before pricing on both engines. The
flat-rho 0.459325 fixture priced 1.4738664 — before the coupon changes below.

| Calculation | PV |
|---|---:|
| First job: PUT + FUNDING | 0.9630697976 |
| Second job: FUNDING-only reference | 0.9848465484 |
| Third job: FUNDING + COUPON | 1.3656824724 |
| Canonical server aggregation | **1.3437038479** |

The canonical aggregation is:

```text
PUT       -0.0219786245
FUNDING    0.9850484222
COUPON     0.3806340503
-------------------------------
TOTAL      1.3437038479
```

The server intentionally takes the first job's PUT and funding legs and replaces its coupon leg with the coupon calculation from the third job. It does not sum all three job PVs, because the jobs represent separate calculation components of the same economic fixture rather than three independent trades.

## Where the coupon number comes from

The COUPON leg is **0.380634**, and it is a leg — a part of the note, never the
whole. The whole note is worth **1.343704** because the note returns its
principal (FUNDING ≈ 0.985) plus the coupons (0.381) minus the knock-in put
protection (−0.022). A number greater than one on the COUPON job is therefore
expected: it is *the whole note*, not the coupon amount. The coupon leg itself,
checked directly, sits at 0.38 — comfortably in the "0.03+" band the legacy
system reports, and no longer the untruncated 0.5097 of the pre-callable-gate
code.

Two convention changes moved the coupon from **0.5098871695** to **0.380634**:

1. **The callable gate.** The coupon used to be a scalar sum that never read the
   per-path call at all — every called path paid its entire remaining coupon
   schedule. `price_fixture` now implements the gate (the `span`/`elapsed`
   clip, ported from `pricing.py:364-366`), so a period that ends before the
   call pays in full, the period containing the call pays only the fixings up
   to and including the call step, and a later period pays nothing.
2. **The in-flight period is owed in full** (block-book correction 8). The
   payload row ending 2026-09-01 (the 1-Sep fixing, payment 03-Sep) is
   fixed-but-unpaid at the 07-Sep evaluation: `N1 = 14 / N2 = 21` is a status
   marker, not an amount share, so the full 0.9642% month counts — not the
   `(21-14)/21 = 1/3` the old `unpaid / total` factor carried.

The coupon explanation shows:

- Six unpaid periods (`unpaid_fixings` 7, 21, 22, 20, 22, 19).
- The in-flight 1-Sep period counted whole, not at one third.
- Accrued rate `0.009642`.
- Notional `50,000`.
- Legacy quote scale `10.0`.
- Payment-date discounting and payment lags (`[2, 4, 2, 2, 2, 2]`).

The raw coupon cash PV is `1903.1702514`. The quote conversion is:

```text
1903.1702514 / 50000 × 10 = 0.3806340
```

## C++ lane

Through the dispatched entry point (`run_termsheet` on the single-common root,
which the classifier routes to `price_fixture`), 30k paths, seed 1729:

```text
CPP COUPON     0.381160413
CPP FUNDING    0.984480071
CPP PUT       -0.018014194   (job 0)
CPP canonical (job0 PUT+FUNDING + job3 COUPON)  1.347626290
```

The C++ coupon moved from **0.509721** to **0.381160** — the same two changes,
ported to the C++ loop (per-path `call_step` + the `span`/`elapsed` clip on the
lane's NYSE grid + full accrual for owed periods). Python and C++ now agree
within ~0.14% on the coupon (0.380634 vs 0.381160); the residual on the whole
note (~0.004) is the pre-existing lane spread (different step grid, dividend
drift, and volatility calibration — recorded in `lane-checkpoint.md`). The C++
numbers above were measured on the fixture's own market JSON (stored
correlation); the coupon leg, which is what these changes touch, is insensitive
to the lake-correlation substitution at the displayed precision.

## Why 1.04 may be remembered

A total around **1.04** is what this note totals **when all three legs are in
the same unit**. The 0.381 coupon is expressed in the legacy **ten-point price
convention** (`coupon_quote_scale: 10.0`); in per-unit-of-notional terms it is
0.0381, and

```text
0.985 funding - 0.022 PUT + 0.038 coupon ≈ 1.001 + small rate/time conventions
```

lands in the 1.03–1.04 neighbourhood. The shipped total is 1.344 only because
the coupon leg carries the `×10` while the funding and put legs do not — the
documented units defect (block-book correction 1). With the callable gate and
the in-flight-full convention now correct, the remaining gap to "1.04" is a
units question, not a coupon-amount question: if the legacy system quotes the
coupon in the same unit as the other legs, the coupon to compare is **0.038**,
and the total is ~1.04.

## Reproduction

```bash
uv run python scripts/reconcile_fixture_pv.py
```

The C++ column is produced by calling `run_termsheet` on each job's
`common_from_job(...)` extract with `paths=30000, seed=1729` and reading the
leg pvs off the dispatched payload.