# Fixture AAD versus CRN Comparison at 30,000 Paths

## Configuration

The canonical `termsheet1.md.json` fixture was priced with the repository's shared pricing kernel using 30,000 paths, seed `1729`, and the fixture's standard 194-step simulation. The comparison uses common random numbers for the finite-difference scenarios.

| Item | Value |
|---|---:|
| Paths | 30,000 |
| Seed | 1729 |
| Fixture PV | 0.9639793053 |
| PUT option price | 0.0210691168 |
| Pricing model | Correlated GBM terminal/reference CPU |
| Spot bump | 1% relative |
| AAD tape | XAD first-order fixed branch |

## Spot sensitivities

| Risk factor | Fixed-branch AAD | Pathwise | CRN FD | AAD minus CRN | Relative difference |
|---|---:|---:|---:|---:|---:|
| ADBE UW spot | -0.0002541994 | -0.0002541994 | -0.0002538051 | -0.0000003944 | -0.1554% |
| AMZN UW spot | -0.0003364056 | -0.0003364056 | -0.0003376572 | 0.0000012516 | 0.3707% |

The AAD and pathwise results match to floating-point precision for this fixture. CRN finite difference is close, with sub-0.4% differences at a 1% bump. The remaining difference is expected because the finite-difference estimate includes paths that move across the payoff kink or transition boundary, while fixed-branch AAD and pathwise differentiation retain the base path's branch.

## Other market inputs

| Risk | AAD fixed branch | CRN fallback | Difference | Status |
|---|---:|---:|---:|---|
| Volatility scale / vega | 0.0643278734 | 0.0642992345 | 0.0000286388 | AAD and CRN agree closely |
| Discount / IRPV01 | -0.0000021388915 | -0.0000021388915 | Approximately zero | AAD and CRN agree to numerical precision |
| FX delta | 0.0210691168 | Not applicable | — | Legacy fixture is USD/USD and has no FX factor |
| Skew delta | 0.0 | 0.0 | 0.0 | Zero-skew control; no independent fixture skew input |

The vega AAD value is a local derivative of the volatility tangent tape around the base volatility scale. The IRPV01 AAD value differentiates the discount factor and applies the same rate-bump convention as the CRN comparison.

## Method interpretation

The fixture's method metadata remains:

| Leg | Method | Reason |
|---|---|---|
| PUT | `AAD_WITH_PATHWISE_TRANSITION_FALLBACK` | Fixed-branch XAD with worst-of, EKI, and physical-delivery transition fallback |
| FUNDING | `AAD` | Smooth discount/funding arithmetic |
| COUPON | `FD` | Range, memory, and call-state transitions |

The comparison supports the hybrid method policy. AAD is a good local method for smooth arithmetic and fixed branches. Pathwise differentiation provides the same local spot result in this fixture. CRN bumping remains the economic fallback when the derivative must include branch-probability movement or a market input is not represented inside the tape.

## Reproduction

```bash
uv run python scripts/compare_fixture_aad_30k.py
```

The script writes the machine-readable result to `/tmp/fina-risk-fixture-aad-30k.json`.
