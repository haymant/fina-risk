# Fixture PV Reconciliation at 30,000 Paths

## Result

The earlier comparison reported **0.963979**, which is the valuation of only the first fixture job. The canonical server pipeline loads three jobs and uses the first job's PUT/FUNDING legs plus the third job's coupon leg. At 30,000 paths and seed 1729, the canonical total is **1.473866**.

| Calculation | PV |
|---|---:|
| First job: PUT + FUNDING | 0.9639793053 |
| Second job: FUNDING-only reference | 0.9848465484 |
| Third job: FUNDING + COUPON | 1.4949355916 |
| Canonical server aggregation | **1.4738664748** |

The canonical aggregation is:

```text
PUT       -0.0210691168
FUNDING    0.9850484222
COUPON     0.5098871695
--------------------------------
TOTAL      1.4738664748
```

The server intentionally takes the first job's PUT and funding legs and replaces its coupon leg with the coupon calculation from the third job. It does not sum all three job PVs, because the jobs represent separate calculation components of the same economic fixture rather than three independent trades.

## Why 1.04 may be remembered

A total around **1.04** would be consistent with a coupon PV around **0.076**:

```text
0.985 funding - 0.021 PUT + approximately 0.076 coupon ≈ 1.04
```

The current repository fixture instead calculates a coupon PV of **0.509887**. Its coupon explanation shows:

- Six unpaid periods.
- Seven unpaid fixings in the first unpaid period.
- Full future fixing counts in the remaining periods.
- Accrued rate `0.009642`.
- Notional `50,000`.
- Legacy quote scale `10.0`.
- Payment-date discounting and payment lags.

The raw coupon cash PV is `2549.4358473`. The current quote conversion is:

```text
2549.4358473 / 50000 × 10 = 0.5098872
```

Therefore, **0.964 is not the canonical total**, and **1.04 is not the current canonical result**. The current code produces 1.474 because it includes the current coupon convention and quote scale.

If 1.04 is the intended legacy parity value, the coupon leg's historical quote scale, unpaid-fixing convention, or source fixture revision must be identified before changing the pricing code. The 30k AAD/CRN comparison itself used the first job's PUT price, so its sensitivity comparison is unaffected by the coupon aggregation discrepancy.

## Reproduction

```bash
uv run python scripts/reconcile_fixture_pv.py
```
