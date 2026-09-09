# 100k Instrument Taylor-Risk and OLAP Benchmark

## Executive result

The augmented portfolio generator produced **100,000 ELI/FCN instruments** over **1,200 underlyings** using the repository's legacy-inspired instrument and market conventions. The calculation ran with a reduced **128-path shared simulation cube** to target a minutes-scale SLA. The complete calculation, Parquet persistence, and two OLAP dashboard queries completed in **68.27 seconds** on the local CPU reference implementation.

The benchmark is a performance and dataflow validation. The reduced path count is not a production valuation setting. Barrier and memory-state products require higher path counts and event-aware validation before a production risk sign-off.

## Input preparation

| Item | Result |
|---|---:|
| Instruments generated | 100,000 |
| Underlyings generated | 1,200 |
| Instrument JSON size | 103 MB |
| Market JSON size | 14 MB |
| Instrument ingestion rows | 100,000 |
| Market ingestion rows | 1,200 |
| Input Parquet ingestion time | 9.64 seconds |
| Append policy | New immutable Parquet partition |

The generated market data includes spot, bid/ask, reference spot, dividends, equity volatility surfaces, FX spots, FX volatility surfaces, interest curves, and a 1,200-dimensional correlation matrix. Instruments include three underlyings, worst-of baskets, EKI knock-in, physical delivery, global memory-call variants, funding, and range-accrual coupon legs.

## Calculation configuration

| Setting | Value |
|---|---|
| Pricing kernel | `price_terminal_legs.v1` |
| Instruments | 100,000 |
| Unique payoff structures | 1,000 |
| Underlyings | 1,200 |
| Correlation factors | 12 |
| Shared paths | Yes |
| Paths | 128 |
| Structure reuse | Yes |
| Monitoring | Daily |
| Taylor scope | Second-order |
| Market reuse | Enabled |
| Curve bootstrap reuse | Enabled |
| Vol-surface interpolation | Enabled |
| Dividend projection | Enabled |
| FX conversion | Enabled |
| Range accrual state | Enabled |
| Memory-call state | Enabled |
| Payment-date discounting | Enabled |
| Physical delivery | Enabled |
| PUT, funding, and coupon legs | Enabled |

## Sensitivity scope

The run requested the broad risk scope:

| Risk component | Method label | Value/checksum |
|---|---|---:|
| Delta | `CRN_BUMP_REVALUE` | 110.3640 |
| Gamma | `CRN_BUMP_REVALUE` | -1.2239 |
| Vega | `CRN_BUMP_REVALUE` | -25,121.8947 |
| 1M bucket vega | `CRN_BUCKET_BUMP_REVALUE` | -2,512.1895 |
| 3M bucket vega | `CRN_BUCKET_BUMP_REVALUE` | -3,768.2842 |
| 6M bucket vega | `CRN_BUCKET_BUMP_REVALUE` | -5,024.3789 |
| 1Y bucket vega | `CRN_BUCKET_BUMP_REVALUE` | -7,536.5684 |
| 2Y bucket vega | `CRN_BUCKET_BUMP_REVALUE` | -6,280.4737 |
| IRPV01 | `CRN_BUMP_REVALUE` | -6.7620 |
| FX delta | `CRN_BUMP_REVALUE` | 67,625.0884 |
| Skew delta | `CRN_BUMP_REVALUE` | 5,158.1901 |
| Cross vega | `CRN_BUMP_REVALUE` | -19,958.3536 |

The current benchmark engine honestly labels this portfolio-wide scope as CRN bump revaluation. The metadata records `QuantLib-Risks/XAD_representative_only` for the representative fixed-branch AAD capability; it does not mislabel the full discontinuous 100k portfolio as AAD.

## Timing and throughput

| Phase | Time |
|---|---:|
| PV and sensitivity calculation | 64.81 seconds |
| Arrow/Parquet persistence | 3.40 seconds |
| Total calculation-to-OLAP workflow | 68.27 seconds |
| Calculation throughput | 1,543 instruments/second |
| Persisted risk rows | 300,000 |

Three rows were emitted per instrument, one for each underlying spot risk factor. Each wide row stores the factor's delta, dollar delta, delta P&L, gamma, gamma P&L, vega, vega P&L, IRPV01, rate P&L, and total Taylor P&L components.

## Parquet output

The calculation persisted:

```text
/tmp/fina-risk-100k-olap/risk_wide.parquet
/tmp/fina-risk-100k-olap/manifest.json
```

The compressed wide Parquet file was approximately **968 KB** because repeated structure and numeric columns compress efficiently. It contains 300,000 atomic rows. The benchmark currently emits the wide calculation dataset; method-level long observations should be enabled when full AAD and cross-check provenance is required for production audit.

## OLAP dashboard queries

The portfolio summary query grouped by `portfolio_id` and summed base PV, total Taylor P&L, delta P&L, and vega P&L:

| Portfolio | Base PV | Taylor P&L | Delta P&L | Vega P&L |
|---|---:|---:|---:|---:|
| BENCHMARK | 202,875.2653 | 54.9537 | 309.0763 | -251.2189 |

The factor-exposure query grouped by `underlying_id`, ordered by total Taylor P&L, and returned the top rows:

| Underlying | Taylor P&L |
|---|---:|
| EQ0027 US | 0.2283 |
| EQ0361 US | 0.2178 |
| EQ0604 US | 0.2073 |

Both results came from DuckDB queries over Parquet using AG Grid SSRM-compatible grouping, aggregation, sorting, and pagination semantics. No analyst-specific pre-aggregation was required.

## Interpretation and limitations

The result demonstrates that the normalized storage and query architecture can process a 100k-instrument scope within a minute-scale local reference run when structure reuse, shared paths, reduced paths, and a 12-factor correlation representation are enabled.

The benchmark's 128 paths are deliberately reduced for the SLA experiment. A production FCN/ELI valuation with EKI, memory call, range accrual, and physical delivery requires materially higher paths and validation around discontinuities. The current full-scope sensitivity engine uses CRN bump revaluation for the benchmark rows. Smooth fixed-branch AAD is available in the project for eligible calculations, while barriers, kinks, memory transitions, and physical-delivery branches retain explicit fallback labels.

The next performance step is to emit method-level long rows, batch structure-level bump calculations, and replace per-instrument scalar revaluation with a vectorized structure batch. Those changes should be made before treating the 1,543 instruments/second result as a production capacity number.

## Reproduction

Generate the input data outside the repository:

```bash
FINA_RISK_BENCHMARK_OUT=/tmp/fina-risk-100k-input \
FINA_RISK_BENCHMARK_INSTRUMENTS=100000 \
FINA_RISK_BENCHMARK_UNDERLYINGS=1200 \
uv run python scripts/generate_benchmark.py
```

Run the calculation, Parquet persistence, and OLAP queries:

```bash
uv run python scripts/run_100k_scope.py
```

The benchmark output is written to `/tmp/fina-risk-100k-benchmark.json`, and the Parquet result is written to `/tmp/fina-risk-100k-olap/`. Large generated inputs and outputs are intentionally kept outside Git.
