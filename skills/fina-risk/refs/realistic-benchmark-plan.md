# Realistic FCN Benchmark Plan

## Purpose

The existing benchmark is a **kernel microbenchmark**, not a production-capacity estimate. It demonstrates shared-path and repeated-structure throughput, but it does not yet reproduce the full workload implied by the legacy term sheet and pricing JSON. This plan defines a benchmark that can be compared meaningfully with a production estate using many EC2 instances.

## What the legacy fixture implies

The fixture is not just a terminal worst-of put. It contains at least three economically distinct legs: a physically settled knock-in put, a principal/funding leg, and a range-accrual coupon. The coupon includes historical fixing counts (`N1`), total fixing counts (`N2`), unpaid-period selection, payment dates after fixing end dates, payment lag, memory/call state, and a range indicator. The option leg includes multiple underlyings, worst-of selection, EKI monitoring, a knock-in barrier, strike, reference spots, and physical delivery. Market data includes spots, curves, volatility surfaces, dividends, and correlations.

A realistic engine must therefore separate and measure the following stages:

| Stage | Realistic work to measure |
|---|---|
| Input normalization | Legacy JSON parsing, semantic mapping, validation, and date/calendar normalization |
| Market construction | Curves, discount factors, dividends, FX conversion, volatility surface interpolation, and correlation factorization |
| Structure compilation | Payoff graph, fixing/payment schedules, barrier states, coupon memory rules, and physical-delivery rules |
| Shared path generation | Daily paths for all required underlyings or factors, with common random numbers and reproducible seeds |
| Lifecycle/state evolution | Daily EKI monitoring, memory/call transitions, range observations, fixing counters, and call/accrual state |
| Leg pricing | PUT, funding, and coupon cash flows with leg signs and payment-date discounting |
| Sensitivities | AAD tape/graph construction and reverse sweep, pathwise/LRM methods, and FD fallback at discontinuities |
| P&L explain | First-order Taylor P&L, optional second-order terms, realized-vs-forecast residual, and attribution by risk factor |
| Aggregation | Portfolio netting, risk-factor reverse index, currency conversion, and output serialization |
| Operations | Chunking, retries, memory pressure, worker startup, network transfer, and cold/warm runtime |

## Distinct payoff structures: layman explanation

The previous benchmark contained 100,000 instrument records but only 1,000 unique payoff baskets. This means that the 100,000 rows were like 100 copies of each of 1,000 recipe cards. The computer can prepare one recipe, cache its instructions, and reuse it for many customers. That is useful when a real book genuinely contains repeated terms, but it can make the result look much faster than a book in which every trade has a different schedule or payoff.

A **payoff structure** is the recipe, not merely the trade identifier. Two instruments share a structure only when the parts that affect computation are equivalent, including the number and ordering of underlyings, worst-of or best-of rule, strike and barrier conventions, knock-in/knock-out monitoring, observation frequency, coupon rate and range bounds, memory rules, fixing/payment schedules, callability, settlement, and sensitivity scope. Different notionals or current spots can often reuse a compiled recipe; different barriers, schedules, memory rules, or monitoring calendars generally cannot reuse the same compiled state graph.

A realistic benchmark should report both:

1. **Record count**: how many instruments are priced.
2. **Structure count**: how many genuinely distinct payoff/schedule/state recipes are compiled.

It should include a reuse sweep such as 1, 10, 100, 1,000, and 10,000 unique structures for the same record count. It should also include a no-reuse control where every instrument has a unique schedule or barrier, so caching benefits are visible rather than hidden.

## Recommended benchmark matrix

The minimum production-comparable run should use 10,000 genuinely distinct instruments, 30,000 paths, daily monitoring over 252 steps, up to three underlyings per instrument, the full 1,200-underlying market universe, full correlation factorization, full coupon/range/memory state, EKI monitoring, payment-date discounting, deltas, and first-order Taylor P&L. A second-order run should be separate because it may materially increase tape memory and reverse-sweep cost.

Recommended toggles and runs:

| Run | Paths | Structures | State | Sensitivity | P&L | Purpose |
|---|---:|---:|---|---|---|---|
| Smoke | 1,000 | 100 | terminal | none | none | Validate input and kernel correctness |
| Shared-path baseline | 30,000 | 1,000 | daily | delta | Taylor-1 | Measure reuse-heavy production case |
| High-diversity | 30,000 | 10,000 | daily | delta | Taylor-1 | Remove most structure-cache advantage |
| Full risk | 30,000 | 10,000 | daily | AAD + fallback | Taylor-1/2 | Measure risk and tape cost |
| 100k capacity | 30,000 | 10,000–100,000 | daily | configurable | configurable | Measure chunking, memory, and scaling |
| No-reuse control | 30,000 | 100,000 | daily | configurable | configurable | Establish upper-bound structure compilation cost |

Every run should report path generation, state evolution, pricing, AAD/tape, P&L, aggregation, serialization, wall time, CPU time, peak RSS, tape memory, path memory, cache hit ratio, unique structure count, and error/timeout counts.

## Realistic 100k FCN estimation

A 100k-instrument result cannot be inferred from the earlier 100-path terminal benchmark. The earlier result used 100 paths, only 1,000 reused baskets, terminal rather than daily state, simplified coupon treatment, and shared-kernel pathwise deltas. It demonstrated that a lightweight arithmetic loop can be fast; it did not demonstrate production FCN capacity.

A reasonable planning estimate is that 100k full FCNs with 30k daily paths and daily path-dependent state are a **distributed batch workload**, not a single-server request. The first useful estimate should be empirical: run 1k and 10k distinct structures under full state, measure peak memory and wall time, then scale by structure compilation and state/path work separately. If path generation is shared, it should scale sublinearly with trade count; payoff/state evaluation, AAD tape construction, and output risk cells may scale nearly linearly or worse when structures and risk factors are unique.

The benchmark must not promise that 100k full FCNs finish in seconds. It should instead report a range by scope: PV-only, PV plus first-order delta, PV plus Taylor-1, and PV plus second-order/AAD. It should also report whether AAD is per instrument, per compiled structure, or representative only. Representative AAD must never be presented as portfolio-wide AAD.

## Acceptance criteria

A benchmark is production-comparable only if it consumes the full correlation and market inputs, uses daily state where the product requires it, prices all requested legs through the same execution kernel as the fixture, distinguishes unique structures from records, reports every disabled expensive component, and verifies PV against a known fixture or an independent reference implementation. The output must preserve enough metadata to reproduce the exact run.
