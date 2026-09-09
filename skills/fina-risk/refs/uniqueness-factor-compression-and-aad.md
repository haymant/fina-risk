# Payoff-Structure Uniqueness, Correlation Factors, and AAD Boundaries

## 1. Why 100,000 instruments can have 1,000 unique payoff structures

The number of instruments and the number of payoff structures describe different things. An instrument is a trade instance with its own identifier, notional, underlyings, strikes, barriers, coupon rates, dates, currency, and lifecycle state. A payoff structure is the reusable calculation recipe that explains how those fields are transformed into cash flows and risk.

A useful layman analogy is a restaurant. There may be 100,000 customer orders, but the kitchen may only have 1,000 recipes. The orders are different because the quantities, customers, and ingredients differ. The recipes are reusable because the cooking logic is the same.

For an ELI or FCN, the recipe can include the number of underlyings, worst-of or best-of logic, final-fixing knock-in, call barrier, memory-call behavior, physical delivery, range-accrual coupon rules, payment lag, and the PUT/FUNDING/COUPON leg composition.

Two trades can therefore be different instruments while sharing one structure:

| Attribute | Instrument A | Instrument B |
|---|---|---|
| Instrument identifier | `ELI-00001` | `ELI-00002` |
| Underlyings | ADBE, AMZN, MSFT | BMW, SAP, Siemens |
| Notional | 250,000 | 1,000,000 |
| Exercise strike | 78% | 82% |
| Knock-in barrier | 70% | 65% |
| Coupon rate | 1.0% | 1.4% |
| Payoff recipe | Three-asset worst-of EKI FCN | Three-asset worst-of EKI FCN |
| Leg recipe | PUT + FUNDING + COUPON | PUT + FUNDING + COUPON |

The market data and economics differ, but the engine can reuse the payoff graph, schedule logic, state-machine layout, simulation mapping, risk-factor mapping, and AAD tape layout where the structure is eligible.

The benchmark's 1,000 structures are therefore a controlled assumption about portfolio diversity. They do not mean that every real portfolio has exactly 1,000 structures. A standardized issuance program may have fewer. A bespoke exotics book may have more.

## 2. Why 1,200 underlyings can use 12 correlation factors

The 1,200 underlyings are still individual securities. The 12 factors are a compressed representation of their common movement.

A simple factor model writes an underlying return as:

```text
individual return = common-factor exposure + residual return
```

For example, ADBE and AMZN may both have high exposure to a broad equity factor and a technology factor. BMW and SAP may have high exposure to a European factor. Each name still has its own spot, volatility, dividend schedule, currency, sector, factor loadings, and residual movement.

The engine can simulate 12 common factors and map them to all 1,200 underlyings. This avoids generating a fully independent 1,200-dimensional random vector for every path and branch.

A full 1,200-by-1,200 correlation matrix contains 1,440,000 entries. A 12-factor loading matrix contains approximately 14,400 loading values before residual and calibration metadata. The factor representation is therefore substantially cheaper while retaining broad co-movement.

The approximation has limits. It may not capture a particular pair's unusual dependence, a single-name event, correlation breakdown, or tail dependence. Concentrated or stressed books may require more factors, pair-specific adjustments, or a richer dependence model.

## 3. The benchmark's CRN label does not mean AAD is impossible

The benchmark currently labels all reported portfolio Greeks as CRN bump revaluation because that is what the implemented 100k benchmark path actually computes. This is an implementation status, not a mathematical statement that no Greek can be calculated with AAD.

The benchmark includes discontinuous and stateful features:

- Final-fixing EKI knock-in.
- Barrier and kinked worst-of payoff logic.
- Global memory-call transitions.
- Range-accrual state.
- Physical-delivery branch selection.
- Payment and lifecycle state changes.

Those features make a single exact reverse-mode derivative invalid across every simulated path. A path can change branch under a small market perturbation. The derivative within one frozen branch is meaningful, but it is not the complete derivative of the discontinuous product at the transition.

## 4. Which Greeks can use AAD

The correct answer depends on the scope of the derivative and the treatment of transitions.

| Greek or risk | True fixed-branch AAD | Smoothed AAD | Honest fallback for current discontinuous benchmark |
|---|---|---|---|
| Spot delta | Yes for smooth paths and frozen branches | Yes with smoothed barriers or soft worst-of | CRN FD/pathwise near transitions |
| Spot gamma | Yes for smooth twice-differentiable branches | Sometimes, but sensitive to smoothing width | CRN second difference or local branch-aware method |
| Vega | Yes when volatility enters a differentiable diffusion/payoff path | Yes if barrier/state transitions are smoothed | CRN vol bump or likelihood-ratio method |
| Bucket vega | Yes for differentiable surface interpolation and fixed maturity buckets | Yes with smooth bucket interpolation | Bucketed CRN vol bump |
| IRPV01 | Yes for smooth discount factors and payment cash flows | Usually no smoothing is needed for the curve itself | Curve bump when payment/state branches change |
| FX delta | Yes for smooth FX conversion and foreign-market inputs | Yes if FX-linked barriers are smoothed | CRN FX bump |
| Skew delta | Yes if the volatility-surface parameterization is differentiable | Yes with smooth smile interpolation | CRN skew parameter bump |
| Cross vega | Yes in a differentiable multi-factor tape | Possible but expensive and approximation-sensitive | CRN cross bump |
| Barrier-event probability risk | Not as an ordinary path derivative | Smoothed probability derivative is possible | Likelihood-ratio, conditional expectation, or scenario analysis |
| Memory-call transition risk | Not exactly at the discrete transition | Soft-state approximation is possible | Event-aware bump or scenario split |
| Physical-delivery branch risk | Not exactly at the branch boundary | Soft settlement approximation is possible but changes economics | Branch-aware FD or scenario analysis |

The strongest candidates for immediate AAD are:

1. Fixed-branch spot delta.
2. Fixed-branch spot gamma where the payoff is twice differentiable.
3. Smooth volatility and volatility-surface parameters.
4. Discount-curve sensitivities for fixed payment schedules.
5. Smooth FX conversion factors.

The benchmark should therefore be described as **AAD-capable in selected subgraphs**, not as fully AAD-calculated today.

## 5. What fuzzy logic or smoothing actually does

A barrier indicator is discontinuous:

```text
indicator = 1 if spot <= barrier else 0
```

A smoothed replacement may be:

```text
soft_indicator = sigmoid((barrier - spot) / epsilon)
```

This gives a derivative near the barrier. The derivative is useful for an approximate sensitivity, but it is not the derivative of the original hard-barrier product. The result depends on the smoothing width `epsilon`.

Smoothing can be useful when:

- The user explicitly requests a stable approximation.
- The smoothing width is recorded in the risk metadata.
- The result is compared against hard-branch CRN or conditional methods.
- The product is monitored rather than used for exact legal valuation.

Smoothing must not be silently presented as exact AAD. A suitable label is:

```text
method = AAD_SMOOTHED
transition_treatment = sigmoid_barrier
smoothing_width = epsilon
quality_flag = approximation
```

Fuzzy logic is not a free solution to discrete lifecycle state. A memory call is a discrete event with accrued-coupon consequences. Replacing it by a soft probability produces a different economic model unless the approximation is explicitly approved.

## 6. Recommended benchmark implementation split

The 100k benchmark should eventually be split into calculation lanes:

| Lane | Population | Preferred method | Output label |
|---|---|---|---|
| Smooth structure and away from transition | Large majority of ordinary paths | AAD | `AAD_FIXED_BRANCH` |
| Smooth structure with controlled transition approximation | Selected monitoring or forecast use | Smoothed AAD | `AAD_SMOOTHED` |
| Barrier, kink, memory, and delivery transitions | Paths or trades near events | CRN FD, conditional, or scenario method | `CRN_FD` / `CONDITIONAL` |
| Cross-method validation sample | Small representative sample | AAD plus CRN FD | `AAD_CROSS_CHECKED` |

This preserves performance because the expensive fallback is applied only where transition risk is material. The full portfolio need not use the most expensive method uniformly.

## 7. How the wide and long risk tables should record this

The wide table should contain the selected production or monitoring value:

| Column | Example |
|---|---|
| `selected_method` | `AAD_FIXED_BRANCH` |
| `cross_check_method` | `CRN_FD` |
| `transition_treatment` | `frozen_branch` |
| `smoothing_width` | `null` |
| `quality_flag` | `conditional` |
| `total_taylor_pnl` | arithmetic sum of selected components |

The long table should preserve every observation:

| Column | Example |
|---|---|
| `risk_factor_key` | `P1\|I1\|PUT\|SPOT:ADBE\|DELTA\|` |
| `method` | `AAD_FIXED_BRANCH` |
| `method_status` | `selected` or `cross_check` |
| `aad_scope` | `fixed_branch` |
| `transition_treatment` | `frozen_branch` |
| `smoothing_width` | `null` |
| `value` | derivative value |
| `quality_flag` | `conditional` |

This allows a dashboard to show the fast selected risk while an audit view explains where it came from and whether it was validated by CRN or another method.

## 8. Practical conclusion

Some benchmark Greeks can absolutely use AAD. The current all-CRN result reflects the present benchmark implementation and the need to be honest about discontinuities. A production-quality implementation should use AAD for smooth fixed-branch deltas, gammas, vegas, bucket vegas, IRPV01, FX delta, skew, and cross-vega where the underlying market and payoff graph are differentiable.

Smoothing can extend AAD coverage for monitoring and forecasting, but it creates an approximation and must carry a smoothing width and transition label. It does not make hard barriers, memory events, and physical-delivery branches exactly differentiable.

The right architecture is therefore hybrid:

```text
AAD for the smooth majority
+ smoothed AAD for approved approximations
+ CRN/conditional/scenario fallback near discontinuities
+ explicit method and quality metadata for every risk cell
```

## References

[1]: https://www.quantlib.org/ "QuantLib project documentation"

[2]: https://xad.readthedocs.io/ "XAD automatic differentiation documentation"

[3]: https://en.wikipedia.org/wiki/Automatic_differentiation "Automatic differentiation overview"
