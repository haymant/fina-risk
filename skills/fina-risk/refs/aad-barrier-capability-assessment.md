# AAD Capability Assessment for Barriers and Discontinuous FCN Payoffs

## Executive conclusion

The book’s central conclusion is directly applicable to fina-risk: **ordinary reverse-mode AAD does not repair a discontinuous Monte Carlo payoff**. A tape differentiates the executed arithmetic and therefore returns a local derivative of the selected branch. It does not recover the derivative of the probability mass crossing a digital, barrier, knock-in, knock-out, memory, or physical-delivery boundary.

The book’s practical solution is to evaluate the unmodified script with a **fuzzy condition evaluator**. Continuous conditions are replaced by call-spread degrees of truth. Conditional state is evaluated on both branches and recombined using the degree of truth. Boolean and other discrete conditions require domain-aware interpolation rather than a continuous call spread. AAD can then differentiate the resulting smoothed arithmetic, while unsmoothed discontinuity risk remains a separate likelihood-ratio, analytic, or bump-and-revalue problem.

The current fina-risk implementation has a working **first-order XAD tape for a fixed worst-of branch**, plus finite-difference and pathwise fallbacks. It does not yet implement the book’s fuzzy condition evaluator, affected-variable analysis, discrete-domain treatment, or a full pathwise model tape. Consequently, it currently supports local AAD only for smooth fixed-branch arithmetic and does not support reliable full-product AAD across barrier and state transitions.

## What the sample code demonstrates

The `Book-V1` sample code uses a templated evaluator whose numeric type can be a differentiated type. Its `FuzzyEvaluator` overrides conditional evaluation rather than changing the script. It computes a call-spread degree of truth for continuous inequalities, evaluates both branches only when the degree is between zero and one, stores only variables affected by the conditional block, and recombines branch states. The `IfProcessor` precomputes affected variables, including variables changed by nested conditions, so fuzzy evaluation does not copy the entire variable store for every `if` statement.

This design is important for fina-risk because the relevant discontinuities are not limited to a final `max` operation. They occur in event conditions, barrier monitoring, knock-in state, call/memory state, range accrual, physical delivery, and conditional coupon cash flows. Smoothing only the final payoff would not correctly smooth the state transitions.

## Current fina-risk AAD implementation

The current adapter in `src/fina_risk/aad.py` records quoted spots as XAD first-order inputs. It freezes the base-path worst-of branch and differentiates the arithmetic payoff on that branch. The implementation explicitly reports the limitation: worst-of and strike kinks require pathwise or finite-difference treatment at transitions.

The current fixture output therefore has three distinct concepts:

| Output | Meaning | Current status |
|---|---|---|
| `aad.deltas` | First-order XAD derivative on the frozen base branch | Implemented for the PUT arithmetic branch |
| `fd_sensitivities` | Common-random-number symmetric bump of the fixture PUT | Implemented per quoted underlying |
| `dollar_delta` | `normalized_delta × quoted_spot × notional` for hedge sizing | Implemented and verified |

The fixture’s current XAD values agree closely with its finite-difference values, but that agreement is local. It is not evidence that the XAD tape captures the derivative of knock-in or worst-of transition probabilities.

## Greek capability matrix

The following matrix distinguishes **mathematically possible with the book’s method**, **currently implemented in fina-risk**, and **what remains necessary for production-quality AAD**.

| Sensitivity | AAD after smooth/fuzzy treatment | Current fina-risk AAD | Reliable current method | Main limitation |
|---|---|---|---|---|
| Spot delta | Yes | Partial | XAD fixed branch plus CRN FD fallback | Worst-of, EKI, and delivery transitions are not taped with fuzzy state logic |
| Dollar spot delta | Yes | Partial | Convert normalized delta using spot × notional | Unit conversion is implemented; transition risk still needs smoothing or fallback |
| Gamma | Yes in principle | No | CRN second-order bump currently used in benchmark | Requires second-order tape or nested/second-order AAD and smoothed kinks |
| Parallel vega | Yes | No full-product AAD | CRN bump/revalue benchmark | Current XAD tape does not differentiate simulated volatility dynamics |
| Bucket vega | Yes | No | CRN bucket bump proxy/revalue | Requires maturity-bucket volatility inputs and a bucketed model tape |
| IRPV01 | Yes | No | CRN rate/discount bump benchmark | Requires rates inside the simulated cash-flow and discounting tape |
| FX delta | Yes | No | CRN FX bump benchmark | Current fixture is USD/USD and has no FX risk factor in the tape |
| Skew delta | Yes | No | CRN skew-shape bump benchmark | Requires a differentiable volatility-surface parameterization |
| Cross vega | Yes | No | CRN joint volatility/skew bump benchmark | Requires explicit cross-factor parameterization and a stable mixed derivative method |
| Coupon delta/vega | Yes after state smoothing | No | FD | Coupon range, memory, and call state are discontinuous/discrete |
| Barrier delta/vega | Yes after fuzzy barrier treatment | No | FD or pathwise fallback | A hard barrier produces a digital transition in the path event |
| Knock-in probability risk | Not from plain payoff AAD | No | LR/Malliavin, analytic, or smoothed estimator | The derivative is a distribution/probability effect, not a branch derivative |
| Physical-delivery delta | Only with fuzzy delivery/settlement treatment | No | FD | Delivery changes the payoff regime and can create a discontinuity |
| Memory/call-state Greeks | Yes after discrete-domain fuzzy treatment | No | FD | Boolean state cannot be smoothed as an ordinary continuous condition |
| Taylor first-order P&L | Yes using valid deltas | Partial | AAD where eligible, FD/pathwise fallback elsewhere | Forecast quality is limited by transition and model-factor coverage |
| Taylor second-order P&L | Yes with gamma and cross-gamma | No | Not currently produced as AAD | Needs second-order sensitivities and consistent factor units |

## What can be implemented with AAD next

### Smooth fixed-branch model Greeks

The existing XAD approach can be extended to record the underlying model inputs rather than only quoted spots. For a smooth fixed branch, this can produce first-order spot delta, rate delta, volatility vega, FX delta, and sensitivities to differentiable surface parameters. The implementation must tape the simulation map, discount factors, terminal state, and payoff arithmetic using the same XAD scalar type.

This method is appropriate for a **local smooth component**. It is not sufficient for a hard barrier or state transition.

### Fuzzy-condition AAD for continuous conditions

The book’s `FuzzyEvaluator` can be adapted to fina-risk’s compiled leg/state representation. For each continuous condition such as `spot - barrier > 0`, the evaluator would calculate a call-spread degree of truth. It would evaluate both conditional branches only in the fuzzy band and recombine affected state variables. The resulting smoothed script can then run through XAD.

This can provide stable first-order AAD for smoothed:

- digital-style payoff conditions;
- discretely monitored barriers;
- knock-in and knock-out conditions represented as continuous market inequalities;
- smooth conditional coupons;
- smooth call and memory transitions when their conditions depend directly on continuous market quantities.

The smoothing width must be an explicit execution-plan parameter. Price bias, sensitivity stability, and convergence must be tested across multiple widths.

### Discrete-domain fuzzy AAD

The book’s Chapter 20 treatment is required for variables such as `alive`, fixing counters, memory flags, and range-accrual counts. A continuous call spread applied directly to a boolean variable is biased and mathematically wrong. The engine must infer the crisp domain of each condition and use the appropriate constant, boolean, binary, or general discrete interpolation rule.

This is the route to stable AAD for memory and stateful FCNs. It is materially more work than adding a smooth `if` function because it requires domain analysis before simulation and state-aware conditional evaluation during simulation.

## What should not be claimed as AAD

Plain AAD of a hard branch is not a reliable barrier Greek. It differentiates the branch selected on each path and misses the movement of paths across the branch boundary. The same issue affects finite differences with too-small bumps, although larger common-random-number bumps can estimate the economic scenario change at the cost of bump-size bias.

Likelihood-ratio or Malliavin estimators can target the distributional derivative of a discontinuous payoff, but they are model-dependent. The book explicitly notes that the weights depend on the drift, diffusion, discretization, and implementation of each model. They should not be represented by a generic AAD tape without model-specific derivation and validation.

Gamma and cross-gamma are especially unsafe to label as AAD in the current system. First-order XAD is installed and exercised, but second-order tape support, nested adjoints, and discontinuity treatment are not implemented in the current project.

## Recommended implementation sequence

The next AAD work should proceed in four controlled stages. First, add an execution-plan option for `smoothing.enabled`, `smoothing.width`, and `smoothing.mode`, and return both raw and smoothed valuations. Second, implement continuous-condition fuzzy evaluation with affected-variable storage and nested-condition handling. Third, add crisp-domain inference and discrete interpolation for booleans, counters, memory state, and fixing counts. Fourth, tape the model and all smooth payoff/state arithmetic with XAD and compare AAD against CRN finite differences over a smoothing-width convergence grid.

The acceptance criteria should include fixture PV parity, PUT parity, dollar-delta parity, barrier-hit probability stability, smooth-versus-sharp price bias, and AAD-versus-CRN-FD agreement. Each Greek result should report its method, smoothing mode, fallback reason, and whether it includes transition risk.

## Final assessment

Fina-risk can currently provide **first-order local AAD for smooth fixed-branch PUT arithmetic** and can provide hedge dollar deltas after the correct notional conversion. It can provide the broader requested Greek set through CRN bump-and-revalue, but those benchmark Greeks are not full-product AAD.

After implementing the book’s continuous and discrete fuzzy evaluators, fina-risk should be able to provide defensible first-order AAD for smoothed spot, rate, volatility, FX, coupon, and barrier sensitivities. It should still report a separate fallback or model-specific estimator for hard transition risk. Full second-order Greeks, cross-gamma, and reliable unsmoothed barrier AAD remain outside the current implementation and require additional second-order tape support or a validated likelihood-ratio/Malliavin implementation.

## References

[1]: https://github.com/asavine/Scripting/tree/Book-V1 "Antoine Savine, Modern Computational Finance scripting sample code, Book-V1 branch"

[2]: https://github.com/asavine/Scripting "Antoine Savine, Scripting repository"

[3]: https://github.com/auto-differentiation/ "XAD automatic differentiation project reference"

[4]: https://github.com/QuantLib/QuantLib "QuantLib reference library"

[5]: /home/ubuntu/upload/1.md "User-provided book excerpt, Part IV: Fuzzy Logic and Risk Sensitivities"
