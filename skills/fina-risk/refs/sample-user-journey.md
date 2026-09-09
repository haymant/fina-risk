# Sample User Journey: End-to-End Portfolio P&L Forecast

## Purpose

This document defines the agent journey for a request such as:

> Use the `fina-risk` skill to conduct an end-to-end portfolio P&L forecast based on exposed risk factors.

The journey uses small MCP operations that an agent can compose. It does not require a pre-generated analyst report. It ingests normalized atomic records, calculates the requested PV and sensitivities, appends results to Parquet, and queries those results through DuckDB using AG Grid Server-Side Row Model (SSRM) semantics.

The initial presentation can be Markdown tables in the agent response. The same query contract can later drive an AG Grid dashboard without changing the storage or risk-calculation contract.

## 1. Operating modes

| Mode | HOT state | WARM state | COLD state | Appropriate use |
|---|---|---|---|---|
| Local stdio | In-process path cubes, AAD tape, compiled payoff graphs, pipeline state | Optional local metadata | Local Parquet files | Development, fixture validation, interactive agent runs |
| Streamable HTTP | Request-local compute and bounded caches | Redis-backed job, metadata, and manifest state | GCS/S3-compatible Parquet | Stateless API requests with recoverable state |
| Worker-backed production | Shared memory or accelerator state | Redis and job queue | Partitioned object-store Parquet | Large portfolios, repeated recalculation, multi-user dashboards |

The transport does not change the DTOs. Stdio keeps HOT state in memory. HTTP requires the caller to supply or recover a `pipeline_id`; the WARM implementation can map that identifier to Redis. Large risk results must not be copied into Redis.

## 2. Atomic MCP call sequence

### Step 0: Establish pipeline state

Call `pipeline_state` first when the run may span multiple requests.

```json
{
  "pipeline_id": "pnl-forecast-20260909",
  "mcp_transport": "stdio",
  "state_backend": "memory"
}
```

For HTTP deployment, use the same DTO with `mcp_transport="streamable_http"` and `state_backend="redis"`. The current local reference reports that Redis is an integration boundary; it does not silently pretend that an unavailable Redis connection is active.

### Step 1: Ingest instruments

Call `ingest_instruments` with either the benchmark JSON object or a list of instrument records.

```json
{
  "root": "/tmp/fina-risk-olap",
  "instruments": [
    {
      "instrumentId": "ELI-BENCH-00000",
      "productType": "ELIFCN_KI",
      "currency": "AUD",
      "notional": 250000,
      "underlyings": ["EQ0000 US", "EQ0137 US", "EQ0274 US"],
      "features": {"worstOf": true, "kiMonitoring": "EKI"},
      "legs": [],
      "legacySemantics": {"quotedSpotRelativeBump": 0.01, "commonRandomNumbers": true}
    }
  ]
}
```

The tool writes an append-only Parquet partition under `instruments/`. It preserves nested economics as JSON columns while promoting the keys required for indexing and joins. Re-ingesting a corrected snapshot creates a new partition rather than rewriting the existing partition.

### Step 2: Ingest market data

Call `ingest_market_data` with the benchmark market object or its `underlyings` list.

```json
{
  "root": "/tmp/fina-risk-olap",
  "evaluationDate": 46274,
  "underlyings": [
    {
      "id": "EQ0000 US",
      "currency": "AUD",
      "spot": 849.191157,
      "referenceSpot": 833.2973891727507,
      "bid": 849.181157,
      "ask": 849.201157,
      "dividend": []
    }
  ]
}
```

The normalized market record contains the underlying key, currency, spot, reference spot, bid, ask, dividends, and serialized volatility-surface payload. The market snapshot identifier and evaluation date belong in the manifest and in every downstream calculation-run record.

### Step 3: Read risk metadata

Call `read_risk_metadata` before selecting sensitivities. The result defines the composite risk-factor key:

```text
portfolio_id|instrument_id|leg_id|risk_factor_id|greek|bucket_id
```

The metadata also defines the wide Taylor components, method labels, units, fallback policy, and the two persistent datasets:

| Dataset | Grain | Purpose |
|---|---|---|
| `risk_wide` | One row per position, leg, risk factor, and Greek | Fast Taylor P&L arithmetic and factor-centric drilldowns |
| `risk_long` | One row per method observation | AAD, pathwise, FD, and cross-check auditability |

The agent must not infer risk-factor identity from a display label. It must use the metadata key and retain the method and transition-treatment columns.

### Step 4: Read dashboard metadata

Call `read_dashboard_metadata` to discover the top views and their allowed drill paths.

The initial views are:

| View | Dataset | Drill path | Primary measures |
|---|---|---|---|
| Portfolio summary | `risk_wide` | Portfolio → instrument → leg → factor | PV, Taylor P&L, delta P&L, gamma P&L, vega P&L, rate P&L |
| Factor exposure | `risk_wide` | Factor type → underlying → risk factor | Delta, dollar delta, Taylor P&L |
| Method audit | `risk_long` | Risk-factor key → method → status | Sensitivity value, dollar value |

The global UI driver shares filters, selected rows, drill path, sort model, and filter model across views. In the first version, the agent can represent this state as Markdown query descriptions and tables. A future web application can bind the same state to AG Grid instances.

### Step 5: Plan computation and storage

Call `plan_pnl_forecast` with the requested universe and Greek scope.

```json
{
  "instruments": 2000,
  "underlyings": 1200,
  "paths": 30000,
  "sensitivities": ["delta", "gamma", "vega", "bucket_vega", "irpv01", "fx_delta", "skew_delta", "cross_vega"]
}
```

The plan must expose cost drivers before calculation. It should identify the following reusable objects:

| Reusable object | Why it matters |
|---|---|
| Market snapshot | Avoids repeating market normalization for each trade |
| Structure signature | Batches instruments with equivalent payoff topology |
| Simulation path cube | Enables common-random-number bumps and shared scenarios |
| AAD tape | Computes many smooth-factor derivatives in one reverse pass |
| Reverse indexes | Maps risk factors to affected positions without scanning all records |
| Parquet partitions | Allows append-only writes and predicate pushdown |

The agent should prefer AAD for smooth fixed-branch factors, pathwise methods where payoff derivatives are available, and CRN finite differences for barriers, kinks, memory transitions, and other discontinuities. Expensive Greeks should be enabled only when the user asks for them or when a trigger policy requires them.

### Step 6: Trigger PV and sensitivities

Call `trigger_pnl_forecast` after the plan is accepted by the agent’s execution policy.

For the canonical fixture, pass `request` and `paths`. For the augmented benchmark, pass `instruments`, `underlyings`, `paths`, `sensitivities`, `pnl`, and execution toggles.

The output must include:

- Calculation-run identifier.
- PV by position and leg.
- Sensitivity value, unit, method, and fallback reason.
- Risk-factor key.
- Taylor decomposition components.
- Simulation path count, seed, model, and common-random-number state.
- Quality and transition flags.

The PUT option, funding, and coupon legs remain separate. Their signed PVs must reconcile to the instrument PV before the agent reports portfolio totals.

### Step 7: Append results to Parquet

The forecast trigger writes normalized risk results through the Arrow/Parquet storage path. The current local implementation creates append-only partitions for ingestion and writes `risk_wide.parquet` and `risk_long.parquet` for the calculation result. A production writer should use partition directories such as:

```text
risk_wide/valuation_date=2026-09-09/run_id=.../part-....parquet
risk_long/valuation_date=2026-09-09/run_id=.../part-....parquet
```

The append contract is important. A writer must not rewrite a 100k-instrument dataset for every new valuation. It should add a partition, update a compact manifest, and let DuckDB query a parquet glob. Object-store uploads should be immutable and versioned.

### Step 8: Query and present P&L analysis

Use `olap_query` for every dashboard-like question. The request can contain `rowGroupCols`, `valueCols`, `groupKeys`, `filterModel`, `sortModel`, `startRow`, and `endRow`.

Example portfolio-level query:

```json
{
  "dataset": "risk_wide",
  "startRow": 0,
  "endRow": 50,
  "rowGroupCols": [{"field": "portfolio_id"}],
  "valueCols": [
    {"field": "base_pv", "aggFunc": "sum"},
    {"field": "total_taylor_pnl", "aggFunc": "sum"},
    {"field": "delta_pnl", "aggFunc": "sum"},
    {"field": "vega_pnl", "aggFunc": "sum"}
  ],
  "sortModel": [{"colId": "total_taylor_pnl", "sort": "desc"}]
}
```

For a factor drilldown, retain the same filters and replace the grouping path with `underlying_id`, `risk_factor_id`, and `leg_id`. For an audit view, query `risk_long` and group by `risk_factor_key` and `method`.

The agent can then render a concise Markdown table:

| Portfolio | Base PV | Taylor P&L | Delta P&L | Vega P&L |
|---|---:|---:|---:|---:|
| PORT-A | 0.00 | 0.00 | 0.00 | 0.00 |

The table is a presentation layer. The durable source remains the normalized Parquet rows and the SSRM query payload.

## 3. DTO and storage contracts

### Hot DTOs

| DTO | Required fields | Storage |
|---|---|---|
| `PipelineState` | `pipeline_id`, transport, state backend | Process memory or Redis reference |
| `SimulationUniverse` | structure IDs, factor IDs, path settings | RAM/GPU |
| `PathCubeMetadata` | seed, paths, time grid, model, CRN ID | RAM/GPU with recoverable metadata |
| `AadTapeMetadata` | tape ID, eligible factors, branch policy | RAM/GPU |

### Warm DTOs

| DTO | Required fields | Storage |
|---|---|---|
| `ExecutionPlan` | scope, paths, sensitivities, reuse policy, cost drivers | Redis or manifest |
| `RiskMetadata` | RFK, measures, methods, units, schemas | Redis or versioned JSON |
| `DashboardMetadata` | views, dimensions, measures, shared UI state | Redis or versioned JSON |
| `PipelineManifest` | run ID, input versions, output partitions, counts | Redis plus object-store manifest |

### Cold datasets

| Dataset | Required key | Storage rule |
|---|---|---|
| Instruments | `instrument_id` | Append-only Parquet partitions |
| Market | `underlying_id`, valuation date | Append-only Parquet partitions |
| Risk wide | composite risk-factor key | Parquet with predicate-pushdown-friendly partitions |
| Risk long | composite key plus method | Parquet for audit and method comparison |
| P&L explain | run ID plus position and factor | Parquet, derived only when requested |

## 4. Performance rules

The first performance rule is to normalize once and reuse everywhere. The second is to batch by structure and market dependencies. The third is to calculate only the risk scope required by the user or trigger policy. The fourth is to append immutable Parquet partitions rather than rewrite historical data.

For a 100k-instrument portfolio, the agent should first calculate PV and spot delta for all positions, use AAD for eligible smooth factors, and defer bucket vega, skew delta, cross-vega, and discontinuity-sensitive Greeks to targeted subsets or event-triggered recalculation. The calculation response should expose timing and row-count metrics so the user can distinguish pricing cost, sensitivity cost, serialization cost, and SSRM query cost.

## 5. Missing concerns that the journey explicitly adds

The journey adds several controls beyond the initial eight steps. It records input and calculation versions, validates signed leg reconciliation, preserves method provenance, separates HOT/WARM/COLD state, keeps UI state independent from calculation rows, supports stale-data detection, and prevents a dashboard query from becoming an accidental full-table materialization. It also makes the transport choice explicit so local in-memory operation and Redis-backed HTTP operation use the same agent-facing tool contract.

## 6. Self-test prompt and expected tool sequence

Use this prompt to exercise the journey:

> Use the `fina-risk` skill to conduct an end-to-end portfolio P&L forecast based on exposed risk factors. Ingest `benchmark/instruments.json` and `benchmark/market.json`, use 30,000 paths, calculate delta and gamma first, persist normalized risk rows, and show portfolio and factor-level Taylor P&L tables.

Expected sequence:

1. `pipeline_state`
2. `ingest_instruments`
3. `ingest_market_data`
4. `read_risk_metadata`
5. `read_dashboard_metadata`
6. `plan_pnl_forecast`
7. `trigger_pnl_forecast`
8. `olap_query` for portfolio summary
9. `olap_query` for factor drilldown
10. `link_olap_views` if multiple views must share filters

The agent should report the calculation run, row counts, method labels, storage partitions, query filters, and any fallback or reconciliation warnings.

## References

[1]: https://duckdb.org/docs/stable/data/parquet/overview.html "DuckDB Parquet documentation"

[2]: https://arrow.apache.org/docs/python/parquet.html "Apache Arrow Python Parquet documentation"

[3]: https://www.ag-grid.com/javascript-data-grid/server-side-model/ "AG Grid Server-Side Row Model documentation"
