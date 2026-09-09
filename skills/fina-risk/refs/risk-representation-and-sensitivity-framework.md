# Risk Representation and Sensitivity Calculation Framework

## Executive recommendation

Use two representations for the same risk run. Store a **long observation table** as the canonical, auditable record. Present a **wide factor-centric table** as the primary user interface for Taylor P&L attribution. The long table preserves every method observation, while the wide table makes the relationship between market factor, Greek exposure, realized shock, and P&L contribution immediately visible.

The calculation policy should be **AAD-first, not AAD-only**. Use true reverse-mode AAD for differentiable model and payoff arithmetic. Use fuzzy or sigmoid-smoothed AAD for explicitly smoothed barrier and conditional-state risk. Use common-random-number finite differences, pathwise estimators, analytic formulas, or likelihood-ratio/Malliavin estimators only where AAD cannot represent the required transition or model derivative. Every row must report its method, scope, transition treatment, units, and quality status.

## 1. Risk representation view

### 1.a Wide factor-centric table

A wide row represents one portfolio, instrument, leg, and primary risk factor. Its columns combine exposure, market shock, Greek values, and Taylor P&L. This is the most intuitive view for a risk manager because all contributions associated with an underlying spot or curve node appear together.

The table below defines the recommended columns. Nullable columns are intentional because not every factor has every Greek.

| Column | Meaning | Layman description | Taylor P&L role |
|---|---|---|---|
| `as_of` | Valuation timestamp | When the risk was measured | Anchors the market shock and P&L comparison |
| `run_id` | Immutable calculation run | Which calculation produced the row | Links attribution to a reproducible run |
| `portfolio_id` | Portfolio identifier | Which book owns the risk | Enables portfolio aggregation |
| `instrument_id` | Trade or instrument identifier | Which product created the risk | Explains trade-level contribution |
| `leg_id` | PUT, FUNDING, COUPON, or other leg | Which economic component creates the exposure | Separates option, funding, and coupon P&L |
| `risk_factor_id` | Canonical factor name | For example, `EQ:ADBE:SPOT` | Identifies the market move being attributed |
| `risk_factor_type` | Spot, volatility, curve, FX, skew, correlation | What kind of market input moved | Selects the relevant shock and Greek columns |
| `underlying_id` | Equity, index, or currency identifier | Which asset is involved | Groups all ADBE or AMZN effects together |
| `curve_id` | Interest-rate curve identifier | Which rate curve moved | Supports curve-level P&L attribution |
| `bucket_id` | Tenor or volatility bucket | Which maturity segment moved | Prevents mixing 1Y Vega with 5Y Vega |
| `base_pv` | Base valuation | Value before the market move | Starting point for the forecast |
| `spot` | Quoted spot level | Current asset price | Converts normalized delta into dollar hedge value |
| `spot_shock` | Realized or hypothetical spot change | How much spot moved | Multiplied by delta for first-order P&L |
| `delta` | First derivative of PV | Directional exposure to the factor | `delta_pnl = delta × shock` |
| `delta_dollar` | Hedge-scaled delta | Dollar amount needed to hedge the factor | Hedge measure, not a P&L measure |
| `delta_pnl` | First-order delta attribution | P&L explained by directional exposure | Main first-order Taylor term |
| `gamma` | Second derivative | Curvature of the exposure | `gamma_pnl = 0.5 × gamma × shock²` |
| `gamma_pnl` | Second-order attribution | Extra P&L caused by curvature | Adds convexity to Taylor forecast |
| `vega` | Volatility sensitivity | Effect of a volatility move | `vega_pnl = vega × vol_shock` |
| `vol_shock` | Volatility move | Change in quoted or model volatility | Input to Vega attribution |
| `bucket_vega` | Vega by expiry bucket | Which part of the volatility surface moved | Supports maturity-specific P&L |
| `irpv01` | PV change per one basis point | Rate exposure in currency per bp | `rate_pnl = IRPV01 × rate_shock_bp` |
| `rate_shock_bp` | Rate move in basis points | How much the curve moved | Input to rate attribution |
| `fx_delta` | FX spot sensitivity | Effect of currency conversion movement | FX P&L attribution |
| `fx_shock` | FX movement | Change in FX spot | Input to FX delta P&L |
| `skew_delta` | Sensitivity to skew | Effect of a surface-shape move | Skew P&L attribution |
| `cross_factor_id` | Second factor for cross-Greeks | The other factor in cross risk | Makes cross terms unambiguous |
| `cross_greek` | Cross sensitivity | Joint effect of two factor moves | `cross_pnl = cross_greek × shock_a × shock_b` |
| `total_taylor_pnl` | Sum of displayed Taylor terms | Forecast P&L explained by the row | Direct comparison with actual P&L |
| `actual_pnl` | Realized P&L | What actually happened | Validation target for Taylor forecast |
| `unexplained_pnl` | Actual less forecast P&L | What the model did not explain | Highlights missing Greeks, jumps, and model error |
| `selected_method` | Method used for the displayed value | AAD, smoothed AAD, FD, pathwise, or LRM | Tells the user how much to trust the number |
| `cross_check_method` | Independent validation method | Usually CRN finite difference | Shows whether the selected result was checked |
| `method_agreement` | Relative difference between methods | How closely methods agree | Flags unstable or discontinuous risk |
| `transition_treatment` | Hard, frozen, fuzzy, sigmoid, LR, or fallback | How barriers and state changes were handled | Distinguishes smooth risk from event risk |
| `quality_flag` | Good, conditional, smoothed estimate, fallback, unavailable | Plain-language confidence status | Prevents conditional risk from looking exact |
| `risk_factor_key` | Stable composite identity | Full lookup key for the row | Supports joins, caching, and aggregation |

The wide table should use separate columns for raw sensitivity, hedge value, and P&L contribution. For spot risk:

```text
raw_delta      = dV / dS
delta_dollar   = raw_delta × quoted_spot × notional
delta_pnl      = raw_delta × realized_spot_shock
```

A hedge dollar delta is not the P&L from a 1% move. Mixing those values is a common source of apparent under- or overstatement.

The first-order and second-order Taylor terms should be explicit:

```text
spot_delta_pnl = delta × dS
spot_gamma_pnl = 0.5 × gamma × dS²
vega_pnl       = vega × dVol
rate_pnl       = IRPV01 × dRateBp
cross_pnl      = cross_greek × dFactorA × dFactorB
```

### 1.b Long observation table

A long row represents one method observation for one composite risk factor. The canonical key is:

```text
portfolio_id|instrument_id|leg_id|risk_factor_id|greek|bucket_id
```

The long table should preserve separate component columns as well as the composite key. A concatenated key alone is not sufficient for querying, validation, or aggregation.

The long format is rational because different methods can produce different observations for the same risk factor. For example, the same ADBE spot delta may have an AAD row, a CRN finite-difference row, and a pathwise fallback row. Keeping all rows enables method comparison, audit replay, convergence studies, and later method-selection improvements without overwriting history.

The long table is particularly useful for RiskCube storage, Parquet partitioning, DuckDB analysis, method diagnostics, portfolio netting, and regulatory or model-validation evidence. It is less intuitive for a human reading a single factor’s P&L, which is why the wide view should be generated from it rather than used as the only storage format.

The long record should include `value`, `unit`, `dollar_value`, `shock_size`, `shock_mode`, `method`, `method_status`, `aad_scope`, `transition_treatment`, `smoothing_width`, `paths`, `seed`, `confidence_interval`, `quality_flag`, and provenance identifiers.

## 2. Calculation methods

### 2.a Method definitions

**Reverse-mode AAD** records a calculation using differentiated scalar values and propagates derivatives backward from the PV. It is preferred when the model path, discounting, payoff arithmetic, and state logic are differentiable. Its major benefit is that many first-order input sensitivities can be produced at a cost that is largely independent of the number of inputs.

**Fixed-branch AAD** tapes the arithmetic of the branch selected by the base scenario. It is valid as a local derivative away from a branch boundary. It does not capture the movement of Monte Carlo paths across a digital, barrier, worst-of, knock-in, knock-out, memory, or delivery boundary.

**Smoothed AAD** replaces a hard transition with a differentiable approximation. A call-spread or fuzzy condition evaluator is appropriate for continuous inequalities. A sigmoid barrier is another option. The result is a derivative of the smoothed valuation and must report the smoothing convention and width.

**Fuzzy conditional evaluation** evaluates both branches in the smoothing region, stores only affected variables, and recombines the branch states using the degree of truth. Nested conditions multiply degrees of truth. Boolean, counter, and memory conditions require discrete-domain interpolation and must not be treated as ordinary continuous call spreads.

**Pathwise differentiation** differentiates the payoff along each simulated path. It is efficient for smooth payoff regions and often useful for spot risk. It misses distributional transition effects when the payoff contains a hard indicator or branch.

**Finite difference** recomputes PV after a bump. Common-random-number finite difference reuses the same random paths for the up and down runs, reducing Monte Carlo noise. It remains the practical fallback for discontinuous event-state risk, but the bump size must be recorded and convergence must be checked.

**Analytic sensitivity** uses a closed-form or semi-closed-form derivative where a valid formula exists. It is fast and accurate for supported smooth components, but it is not a generic replacement for the product-level risk engine.

**Likelihood-ratio or Malliavin estimation** differentiates the probability distribution rather than the discontinuous payoff. It can recover event risk without smoothing, but the weights are model- and discretization-specific. It should only be used when separately derived and validated for the exact simulation model.

### 2.b Preferred method mapping

AAD is always preferred when the relevant calculation is genuinely differentiable and the tape includes the required market input. Otherwise, the fallback is selected based on the source of the non-smoothness and the performance objective.

| Sensitivity | Preferred method | Fallback | Reason and required label |
|---|---|---|---|
| Spot delta, smooth leg | Reverse-mode AAD | Pathwise | AAD reuses one reverse sweep across many spot inputs |
| Spot delta, hard barrier or KI | Smoothed AAD if enabled | CRN FD or LRM | Hard event movement is not captured by fixed-branch AAD |
| Hedge dollar delta | AAD or selected delta, then unit conversion | Same as delta | Store separately from P&L contribution |
| Gamma | Second-order AAD | CRN central FD | First-order XAD alone cannot provide gamma |
| Vega | AAD through simulated volatility | CRN FD | Requires volatility to be a taped model input |
| Bucket Vega | AAD through bucketed surface parameter | Bucket CRN FD | Requires maturity-bucket surface handles |
| IRPV01 | AAD through curve and discounting | CRN rate bump | Curve nodes and discount factors must be taped |
| FX delta | AAD through FX conversion | CRN FX bump | Requires FX spot as an input in the payoff and conversion tape |
| Skew delta | AAD through SVI or surface parameter | Surface bump | The surface parameter convention must be explicit |
| Cross Vega | Mixed or second-order AAD | Joint CRN bump | Requires a two-factor surface parameterization |
| Coupon range delta | Smoothed AAD | CRN FD | Range membership is discontinuous |
| Memory/call-state delta | Discrete-domain fuzzy AAD | CRN FD | Boolean and counter states need domain-aware treatment |
| Physical-delivery delta | Smoothed AAD with delivery state | CRN FD | Settlement regime can change discontinuously |
| Barrier hit probability | LRM/Malliavin or smoothed AAD | Scenario analysis | Plain payoff AAD is not an event-probability estimator |
| First-order Taylor P&L | Selected AAD or fallback Greeks | Method-specific | Record method per component |
| Second-order Taylor P&L | Second-order AAD | FD gamma and cross terms | Requires consistent second-order units and coverage |

## 3. Analysis methodology and performance

A 100,000-instrument live portfolio should not calculate every risk measure at the same frequency. The analysis framework should match the risk calculation to the market condition, product lifecycle, and business decision.

The basic principle is to maintain a **tiered risk service**. A fast tier monitors the most immediate exposures. A scheduled tier refreshes expensive surface, curve, and transition risks. An event tier activates additional calculations when a market or lifecycle trigger is reached.

### Tiered calculation schedule

| Tier | Typical frequency | Main objective | Recommended calculations |
|---|---|---|---|
| Intraday fast | Seconds to minutes | Explain current P&L and hedge direction | PV, spot delta, dollar delta, first-order Taylor P&L |
| Intraday standard | Five to thirty minutes | Keep key nonlinear and funding risk current | Gamma, IRPV01, FX delta, major Vega buckets |
| Event-driven | Immediately after trigger | Capture jump and transition risk | Barrier proximity, KI/KO state, memory state, smoothed AAD or CRN FD |
| Observation-date | Before and after fixing | Explain coupon, barrier, and call-state jumps | Full leg risk, barrier hit probabilities, coupon memory, transition fallbacks |
| End-of-day | Daily | Complete risk and model controls | Full requested Greek vector, method cross-checks, convergence diagnostics |
| Overnight | Daily or weekly | Expensive diagnostics and calibration | Full surface sensitivities, cross-Greeks, second-order risk, LRM studies |

For most FCN portfolios, spot delta and dollar delta are the first monitoring priority because they explain immediate hedge exposure and much of short-horizon P&L. Around an observation date or when an underlying approaches a barrier or strike, the priority changes. Barrier-state risk, gamma, coupon memory, volatility, and event probabilities require a higher calculation tier.

### Trigger conditions

A trigger should raise the calculation tier when any of the following occurs:

| Trigger | Required response |
|---|---|
| Underlying moves materially relative to the last run | Refresh spot delta, dollar delta, and Taylor P&L |
| Underlying approaches a barrier or strike | Calculate gamma, event probability, smoothed AAD or CRN FD |
| Fixing or observation date is near | Refresh full leg risk and lifecycle state |
| Implied volatility moves materially | Refresh Vega and bucket Vega |
| Volatility surface skew changes | Refresh skew and surface-parameter risk |
| Curve moves beyond threshold | Refresh IRPV01 and curve-node risk |
| FX moves beyond threshold | Refresh FX delta and converted P&L |
| Memory or call state changes | Reprice affected instruments and recompute state-sensitive risk |
| AAD and fallback methods diverge | Mark the factor conditional and schedule a higher-fidelity run |

Performance should be measured by **risk cells and tape reuse**, not only by instrument count. Instruments sharing market factors, payoff structures, simulation paths, and tape structure should be grouped together. A 100,000-instrument run with high structure reuse is materially different from 100,000 unique stateful scripts.

## 4. Decision framework and implementation priorities

### 4.a Method and timing decision

For each requested risk factor, the engine should make the following decisions in order:

1. Is the input present in the market and model graph?
2. Is the calculation smooth under the current lifecycle and payoff state?
3. Is the input represented on the AAD tape?
4. Is the requested result raw, smoothed, or event-state risk?
5. Is the calculation within the current latency budget?
6. If AAD is unavailable or invalid, is a pathwise, FD, analytic, or LRM method approved?
7. What quality label, fallback reason, shock size, and convergence evidence must be returned?

The decision must be stored with the risk row. It must not remain an implicit behavior of the implementation.

### 4.b Priority ranking

| Priority | Risk output | Why it matters | Preferred method | Presentation |
|---:|---|---|---|---|
| 1 | PV and first-order Taylor P&L | Required for live P&L monitoring | Shared paths plus AAD where smooth | Wide factor view |
| 2 | Spot delta and dollar delta by underlying | Direct hedge exposure | Reverse AAD or pathwise | Wide and long |
| 3 | Event-state and barrier proximity | Detects discontinuity risk before it jumps | Smoothed AAD plus CRN FD cross-check | Wide with quality flag |
| 4 | Gamma | Important near strikes and barriers | Second-order AAD or CRN FD | Wide |
| 5 | IRPV01 and FX delta | Funding and currency attribution | AAD when curve/FX are taped | Wide and long |
| 6 | Vega and bucket Vega | Important near observation dates and volatility moves | AAD through surface, otherwise CRN FD | Wide by bucket |
| 7 | Skew and SVI parameters | Needed for surface-shape risk | Surface-parameter AAD | Long plus selected wide columns |
| 8 | Memory/coupon-state Greeks | Important near coupon and call events | Discrete fuzzy AAD or CRN FD | Wide by coupon leg |
| 9 | Cross-Greeks | Useful for nonlinear scenario explanation | Second-order AAD or joint CRN FD | Separate cross-risk table |
| 10 | Full diagnostic vector | Model validation and overnight analysis | Full AAD/fallback suite | Long RiskCube partitions |

The first production aggregation should therefore materialize a wide table containing base PV, spot, spot shock, delta, dollar delta, delta P&L, method, cross-check method, method agreement, transition treatment, and quality flag. It should also retain the long AAD and CRN-FD observations for every factor.

### 4.c Cross-factor presentation

A cross-Greek must contain two factor identities. For example:

```text
risk_factor_id  = EQ:ADBE:VOL
cross_factor_id = EQ:AMZN:VOL
greek           = CROSS_VEGA
```

The factor ordering must be deterministic so the same pair cannot appear twice. Cross-P&L should be shown in a dedicated cross-risk table or a separate section of the wide view rather than hidden inside either factor’s ordinary Vega.

## Relationship to fina-pricer schemas

The fina-pricer schemas remain relevant after introducing AAD. AAD changes the calculation engine and tape metadata. It does not replace the need for stable identity, market coordinates, lifecycle state, leg decomposition, RiskCube axes, and persistence references.

The fina-pricer design provides several important foundations:

| Existing fina-pricer concept | Enhancement recommended for AAD-era fina-risk |
|---|---|
| `RiskFactorKey` | Preserve as the canonical factor coordinate and add a stable string `risk_factor_id` |
| `RiskCube` axes | Retain the axes and add method, tape scope, transition treatment, unit, shock, and P&L contribution metadata |
| Explicit legs | Use `leg_id` in every wide and long risk row |
| AAD-first policy | Extend it with `aad_scope`, `tape_inputs`, `tape_id`, `backend`, and `aad_eligibility_reason` |
| Barrier smoothing | Add `smoothing_mode`, `smoothing_width`, and `hard_reference_pv` |
| Full Greek vector | Store a separate method observation for each Greek rather than forcing all Greeks into one scalar cell |
| Lifecycle state | Add `state_snapshot_id`, `barrier_state`, `memory_state`, and `transition_count` |
| Error estimates | Add Monte Carlo standard error, method agreement, bump convergence, and smoothing-width convergence |
| S3/Parquet persistence | Partition by run, portfolio, factor type, and valuation date; keep compact summaries in the trade system |

The previous conclusion should therefore be refined: **the old schemas are not less relevant because of AAD**. They are the contract around the AAD calculation. What should change is the amount of AAD provenance and transition metadata attached to each RiskCube cell. The tape is the execution artifact; the RiskFactorKey, lifecycle, leg, and RiskCube schemas are the identity and reporting artifacts.

The strongest enhancement is to separate three layers:

1. **Calculation graph:** model handles, path cube, payoff graph, state graph, and AAD tape.
2. **Risk observations:** long rows for every method and factor.
3. **Presentation and aggregation:** wide factor rows and portfolio summaries for P&L attribution.

This separation allows the engine to replace a CRN-FD fallback with smoothed AAD later without changing the user-facing risk key or the portfolio aggregation contract.

## References

[1]: https://github.com/haymant/fina-pricer/blob/main/skills/fina-pricer/SKILL.md "Fina-pricer AAD-first FCN workflow and RiskCube contract"

[2]: https://github.com/haymant/fina-pricer/tree/main/skills/fina-pricer/schema "Fina-pricer JSON Schemas"

[3]: https://github.com/asavine/Scripting/tree/Book-V1 "Book-V1 fuzzy evaluator sample implementation"

[4]: https://github.com/auto-differentiation/ "XAD automatic differentiation project"

[5]: /home/ubuntu/upload/1.md "User-provided book excerpt on fuzzy logic and risk sensitivities"
