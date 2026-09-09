---
name: fina-risk
description: High-level orchestration skill for structured product pricing, AAD-first risk cube generation, shared Monte Carlo path simulation, portfolio risk aggregation, P&L explain/forecast and lifecycle processing of equity-linked notes (ELI/FCN). Use when turning a term sheet or legacy pricing request into decomposed legs and PV/Greeks, validating against the legacy engine's bump conventions, wiring Vercel Redis/GCS warm-cold storage, or bridging the pricing result into the fina-trade repository.
---

# fina-risk

fina-risk is a high-level orchestration skill. It does **not** implement pricing logic directly; it orchestrates a collection of MCP tools and pushes detailed specifications into `refs/` and `schema/` so context consumption stays bounded.

Scope:

- Structured product pricing (equity-linked notes / FCN, worst-of baskets)
- Risk cube generation
- AAD-based sensitivities
- Monte Carlo simulation, GPU acceleration, and path reuse
- Batch pricing and portfolio risk aggregation
- P&L explain and forecasting
- Scenario analysis
- Lifecycle state processing and corporate action adjustment
- Normalized DuckDB/Arrow/Parquet OLAP with AG Grid SSRM query semantics

## Design principles

Universe-first: never price trade-by-trade.

```text
Trade Universe → Risk Universe → Simulation Universe → Shared Path Cube
```

Adjoint-first risk generation priority:

```text
AAD → Pathwise → Likelihood Ratio → Finite Difference
```

GPU-first simulation: GPU, fallback SIMD CPU, fallback scalar CPU.

## Runtime architecture

```text
Client Agent
    ↓
fina-risk Skill
    ↓
MCP Server            (fina-pricer riskcube = currently executable reference)
    ↓
Coordinator ── Market Engine · Universe Engine · Simulation Engine
              · State Engine · Payoff Graph Engine · AAD Engine
              · Risk Engine · PnL Engine · Portfolio Engine · Storage Engine
```

### Deployment

| Target | Runtime | State |
|---|---|---|
| Local | Long-running Python MCP server with a native C++ pricing library (`stdio` / `http-stream`) | HOT in-memory caches: curve, vol, path cube, state cube, AAD tape |
| Vercel | Stateless serverless ASGI/Streamable HTTP endpoint (see fina-pricer) | WARM = Redis; COLD = GCS Parquet |

**Vercel host setup follows `fina-pricer/`** (`api/index.py` + `vercel.json` rewriting `/mcp` → `/api`, entrypoint `api.index:app`). Warm recoverable metadata (universe cache, path/state/risk cube metadata, job status, graph cache) lives in **Redis**; large immutable artifacts (path cubes, risk cubes, PnL) live in compressed **Parquet on GCS** via DuckDB S3 interoperability. Configure `ALLOWED_HOSTS`, `S3_API_KEY`, `S3_API_SECRET`, `S3_BUCKET_NAME` (`s3://fina-riskcube`), `S3_ENDPOINT` (`storage.googleapis.com`) as deployment environment variables, never in source. For serverless latency keep path/step counts conservative; a separate long-running worker handles large production portfolios.

## Native C++ pricing and risk stack

The `cpp/` implementation is the performance lane and deliberately owns no MCP transport. The Python MCP server and DTOs remain the compatibility boundary; Python calls the native library through pybind11 in the same style as the DuckDB Python wrapper.

1. **QuantLib C++** supplies calendars, curves, volatility surfaces, processes, schedules, payoffs, instruments, and model conventions.
2. **XAD C++** supplies reverse-mode tapes for smooth fixed-branch valuation and Greeks. Discontinuous barrier, KI/KO, worst-of, and memory transitions retain explicit pathwise/CRN fallback labels.
3. **DuckDB + Arrow + Parquet + the official AWS S3 SDK** provide OLAP and immutable COLD persistence. Redis remains the WARM metadata adapter for the Python MCP deployment.
4. **pybind11** exposes typed pricing, risk, P&L, and persistence functions to Python. It does not expose MCP tools; the Python server continues to share the exact tool names and schemas.
5. The CMake build treats these libraries as optional for local bootstrap and Vercel packaging. When present, `FINA_RISK_HAS_QUANTLIB_XAD` and `FINA_RISK_HAS_DUCKDB` select the production adapters; the deterministic native reference kernel remains available without them.

Never call `QuantLib.Option.delta()` or an FD loop "AAD". Background: `fina-pricer/docs/aad_research.md` and `fina-pricer/skills/fina-pricer/references/xad_quantlib_notes.md`.

## MCP tool groups

Load only the subsystem spec you need from `refs/*`; validate objects against `schema/*`.

| # | Group | Ref | Tools | Key DTOs |
|---|---|---|---|---|
| 1 | Market | `refs/market/*` | load/store/diff/freeze/validate_market | MarketSnapshot, MarketDiff, CurveDefinition, VolSurfaceDefinition |
| 2 | Universe | `refs/universe/*` | discover/merge/split_universe, estimate_cost | TradeUniverse, RiskUniverse, SimulationUniverse |
| 3 | Compiler | `refs/compiler/*` | compile_trade, compile_portfolio | CompiledTrade, CompiledLeg, CompiledFeature |
| 4 | QuantLib | `refs/quantlib/*` | build_quantlib_market, build_processes/schedules/payoffs | QlModelDefinition, ProcessDefinition |
| 5 | Simulation | `refs/simulation/*` | build/reuse/inspect_path_cube, build_time_grid | SimulationRequest, PathCubeMetadata |
| 6 | GPU | `refs/gpu/*` | gpu_status/allocate/release, tune_batch_size, kernel_profile | GpuProfile, BatchPlan, KernelProfile |
| 7 | State | `refs/state/*` | build/update/inspect_state_cube | StateCube, StateTransition |
| 8 | Payoff | `refs/payoff/*` | compile/inspect/evaluate_payoff_graph | PayoffGraph, GraphNode, GraphEdge |
| 9 | AAD | `refs/aad/*` | build_aad_graph, check_aad_eligibility, run_adjoint, inspect_gradient | AadGraph, AdjointNode, GradientEntry |
| 10 | Risk | `refs/risk/*` | generate/aggregate_risk_cube, generate_greeks, compare_methods | RiskRequest, SensitivityResult, RiskCube |
| 11 | PnL | `refs/pnl/*` | forecast/explain_pnl, taylor_decomposition | PnLForecast, PnLExplain, TaylorBreakdown |
| 12 | Portfolio | `refs/portfolio/*` | aggregate_portfolio, net_sensitivities, portfolio_scenarios | PortfolioView, PortfolioRisk |
| 13 | Scheduler | `refs/scheduler/*` | submit/cancel/rebalance/inspect_job | JobRequest, JobStatus |
| 14 | OLAP / Storage | `refs/olap/*` | write_risk_store, olap_query, storage_status, resolve_s3_dataset, link_olap_views | RiskStoreManifest, SSRMQuery, LinkedViewState |
| 15 | E2E Pipeline | `refs/sample-user-journey.md` | ingest_instruments, ingest_market_data, read_risk_metadata, read_dashboard_metadata, plan_pnl_forecast, trigger_pnl_forecast, pipeline_state | InstrumentSnapshot, MarketSnapshot, ExecutionPlan, PipelineState |

The **executable reference implementation** of these tools is the `fina-pricer` riskcube MCP server, which exposes `pricing_and_sensitivity`, `scenario_*`, `olap_query`, `gcs_read_parquet`, `storage_status`, and `set_storage_mode`, with typed schemas in `fina-pricer/skills/fina-pricer/schema/`.

### Normalized OLAP principle

The OLAP layer stores atomic risk-factor components rather than pre-generated analyst views. `risk_wide.parquet` contains one row per `{portfolio, instrument, leg, risk_factor}` with Taylor components such as delta, gamma, vega, rate P&L and method provenance kept together. `risk_long.parquet` preserves method observations and cross-checks for audit. A Taylor decomposition is therefore a DuckDB join/filter/aggregation over atomic rows, not a batch of materialized reports. Every storage and query operation is exposed as an MCP tool so an agent can create the store, issue AG Grid SSRM requests, inspect storage, resolve S3/GCS-compatible locations, and coordinate linked dashboard views.

For an end-to-end portfolio P&L request, agents should follow `refs/sample-user-journey.md`. The journey is composed from atomic MCP calls rather than a monolithic endpoint. Use stdio when process-local HOT state is sufficient. Use Streamable HTTP with Redis-backed WARM metadata when state must survive requests, and keep large immutable COLD artifacts in S3-compatible Parquet.

For layman explanations of structure reuse, correlation-factor compression, and the boundary between fixed-branch AAD, smoothed AAD, and CRN fallback, load `refs/uniqueness-factor-compression-and-aad.md`.

The benchmark hybrid lane uses cached structure-level method selection: XAD `AAD_FIXED_BRANCH` for eligible stable spot delta, vega, bucket vega, IRPV01, FX delta, and skew paths; `PATHWISE` fallback in transition bands; optional explicitly labeled `AAD_SMOOTHED`; and CRN bump fallback for unsupported gamma and cross-factor components.

## Canonical DTO hierarchy

```text
Trade
├── Instrument ├── Underlyings ├── Basket ├── Legs ├── Features
├── Schedules ├── Settlement ├── Lifecycle └── PayoffGraph
```

Runtime pipeline: `MarketSnapshot → SimulationUniverse → PathCube → StateCube → AADGraph → RiskCube → PortfolioRisk`.

## Storage layout

Defined around storage boundaries (see `schema/README.md`):

- **HOT** (RAM/GPU, mutable): `ProcessCache`, `PathCube`, `StateCube`, `AadTape`
- **WARM** (Redis, recoverable): `UniverseCache`, `GraphCache`, `RiskCache`, `JobCache`
- **COLD** (Parquet + object storage, immutable): `PathCubeArchive`, `RiskCube`, `RiskCell`, `PnLExplain`, `PortfolioRisk`

Every object validates against its JSON Schema in `schema/`:

| Layer | File |
|---|---|
| HOT | `schema/process-cache.schema.json`, `schema/path-cube.schema.json`, `schema/state-cube.schema.json`, `schema/aad-tape.schema.json` |
| WARM | `schema/simulation-universe.schema.json`, `schema/payoff-graph.schema.json`, `schema/risk-cache.schema.json`, `schema/job-status.schema.json` |
| COLD | `schema/path-cube-archive.schema.json`, `schema/risk-cube.schema.json`, `schema/risk-cell.schema.json`, `schema/pnl-explain.schema.json`, `schema/portfolio-risk.schema.json` |
| Pipeline | `schema/pipeline-state.schema.json`, `schema/execution-plan.schema.json`, `schema/storage-boundaries.schema.json` |

Risk report cells carry the originating RFK, measure, value, method (`AAD`/`PATHWISE`/`LRM`/`FD`) and fallback reason.

## Executable and testable

The skill is executable and testable against the bundled term-sheet fixture and the fina-pricer reference engine.

### Canonical fixture

- `refs/termsheet1.md` — sanitized **full term sheet**: USD non-principal-protected ELI, 9-month daily-memory-callable note, final-fixing-date knock-in (EKI), worst-of 2-asset basket, physical delivery on knock-in.
- `refs/termsheet1.md.json` — the **same product as a legacy pricing engine request**. It carries `marketData`, `instrument economics`, and the **PV pricing bumping settings**, and breaks the single instrument into its **three legs, one per `finaRefJobID`**.
- PV - the legacy engine generates price 0.02113 for the PUT option, without any bumping.

### Legacy request anatomy

Three jobs share one market snapshot and differ only in `dealData`:

| Job | Leg | legId | multiplier | Role |
|---|---|---|---|---|
| 0 | PUT | 1 | -1 | short intrinsic option (knock-in, delivery) |
| 1 | FUNDING | 3 | +1 | principal funding at par (`notionalReturn=true`) |
| 2 | COUPON | 2 | +1 | range-accrual coupon strip |

**marketData**: equity quotes for `ADBE UW` (spot 267.885) and `AMZN UW` (spot 258.355) with bid/ask, cash dividends (Excel serial `exDate`), pair correlation `0.4593248180…`, `eqVol` strike×maturity volatility grids per underlying (`strike_type=fixed`, `Reference` spot), `estCurves`/`discCurves` `USD Std Curve` (Actual/365), `MCPara.numPaths=30000`, and `evaluationDate` 46272 (serial). Serial dates are Excel-style: 46136=2026-04-24 (trade/initial fixing), 46419=2027-02-01 (final fixing), 46421=2027-02-03 (expected expiry), 46174=2026-06-01 (ADBE memory call date).

**instrument economics (ELIFCN_KI)**:

- Underlyings: `spot` 239.97 / 260.00, `strikePrice` 187.1766 / 202.80 (**78%** exercise), `barrierPrice` 263.967 / 286.00 (**110%** call/memory).
- `KIKOSelect`: `knockIn` EKI (final fixing only), `knockInStar.KIBarrier` **0.70** (knock-in price), `strikeKI2`/`maturBarrier` **0.78** (exercise), `ITMPayment=Delivery` (worst-of physical delivery), `GKOLocked [true,false]` / `GKODate 46174` = ADBE already memorised on 2026-06-01 (AMZN not yet) → memory-carry state, `alreadyKnockIn` per leg.
- Coupon leg `RGACCLKO`: per-period `endDate`/`paymentDate` arrays (incl. short initial stub), global KO coupon handling, `accIndicator=WPS`.

**PV pricing bumping settings**: each job runs five `Tasks`:

| Task | data_type | data_index | amount | Meaning |
|---|---|---|---|---|
| 0 | -1 | 0 | 0.0 | base PV scenario |
| 1 | 1 | 0 | +2.67885 | ADBE spot +1% (`267.885 × 0.01`) |
| 2 | 1 | 0 | -2.67885 | ADBE spot -1% |
| 3 | 1 | 1 | +2.58355 | AMZN spot +1% (`258.355 × 0.01`) |
| 4 | 1 | 1 | -2.58355 | AMZN spot -1% |

So the legacy convention is **1% relative bumps** on each equity spot — the equivalent of fina-pricer `parameters.bump_size=0.01` with `bump_mode="relative"` and common-random-number finite differences. Bump the quoted market spots (267.885/258.355), not the instrument reference prices (239.97/260.00).

### How to execute

1. Map the legacy JSON onto the fina-pricer `PricingRequest` contract (`InstrumentKey`, `UnwindMapRaw`, `RiskFactorKeys`, `MarketDataSnapshot`, `UpdatedLifecycle`, `parameters`, `CommonEconomics`, `Legs`) — see `fina-pricer/skills/fina-pricer/schema/pricing_request.schema.json`. Prefer explicit `Legs` with `leg_type` `intrinsic_option` / `funding` / `coupon`, `ki_monitoring="EKI"`, `ki_enabled` explicit on the coupon leg, product `already_knock_in` state, and `memory_ko` from `GKOLocked`/`GKODate`.
2. Run the reference engine:

```bash
uv run --project /path/to/fina-pricer riskcube-mcp                    # MCP stdio
uv run --project /path/to/fina-pricer uvicorn riskcube_mcp.server:app # Streamable HTTP /mcp + /healthz
```

3. Regression gates:

```bash
uv run --project /path/to/fina-pricer pytest -q
uv run --project /path/to/fina-pricer ruff check .
uv run --project /path/to/fina-pricer mypy src
```

### Verification criteria

- **Leg decomposition**: aggregate PV equals the signed sum of the three leg PVs (funding at par + short intrinsic option + coupon strip). PUT leg PV must be negative (`multiplier=-1`).
- **Bump parity**: per-RFK spot delta reproduces the $\pm$1% bump differences from the legacy `Tasks`; report `bump_size=0.01`, relative mode, and method per cell.
- **Payoff consistency**: worst-of, strike-normalized basket; EKI knockdown only when the worst performing asset closes ≤ 70% of initial spot on the final fixing date; redemption capped at 100% of notional after KI; memory call when both underlyings close ≥ 110% (then all `GKOLocked`).
- **Lifecycle / memory carry**: coupon leg reflects the ADBE `GKOLocked` memory event on 2026-06-01 and pays accrued unpaid coupons on call.
- **Explainability**: report model, path/step counts, seed, moneyness, time to expiry, barrier events/hit probability, applied fixings, and coupon-memory carry, matching fina-pricer's `explainability` object.

Use the fina-pricer demo for a broader matrix (OTM/ATM/ITM, memory on/off, global/local KI/KO): `fina-pricer/skills/fina-pricer/scripts/demo.py`.

## C++ parity lane

`cpp/include/fina_risk_cpp.hpp` defines the non-MCP native boundary. `cpp/src/fina_risk_cpp.cpp` currently implements the deterministic fixture and shared-factor benchmark kernel, with the same signed PUT/FUNDING/COUPON decomposition and benchmark fields as Python. The next production adapters must preserve these DTOs while replacing the fallback internals with QuantLib/XAD and Arrow/DuckDB/S3.

The native `fina-risk-cpp-e2e` target follows `refs/sample-user-journey.md` from
instrument/market ingestion through structure compilation, shared paths, hybrid
risk rows, Parquet materialization, and DuckDB aggregation. Method provenance is
honest: without discoverable QuantLib C++ and XAD C++ libraries it emits
`PATHWISE_NATIVE_FALLBACK`; with both adapters it selects `AAD_FIXED_BRANCH` for
smooth fixed-branch factors and retains pathwise/CRN fallback for transitions.
The 100k-record benchmark uses 1,000 paths when requested and reports ingestion,
compile, path, risk, serialization, and OLAP timings separately.

Run the native lane locally:

```bash
cmake -S cpp -B cpp/build -DCMAKE_BUILD_TYPE=Release
cmake --build cpp/build -j2
./cpp/build/fina-risk-cpp-benchmark benchmark/instruments.json benchmark/market.json
```

The MCP layer remains Python on both local and Vercel. Vercel runs the Python fixture/HTTP regression suite; native C++ benchmarking runs in CI or a Linux build image because Vercel Python functions are not a suitable home for QuantLib/XAD toolchains.

## fina-trade integration (draft)

The draft trade repository at `modules/fina-trade` (`skills/fina-trade/SKILL.md`, `schema/postgres.sql`, `schema/trade.schema.json`) provides a deterministic `TradeRepository` and a `PostgresTradeRepository` with the RFQ → quote → trade → lifecycle flow. After the riskcube path is validated (**test the risk cube first**, per fina-pricer), fina-risk adapts the trade-boundary flow:

```text
rfq_create → pricing_and_sensitivity → quote_persist → trade_accept
   → trade_amend / fixing / corporate event → lifecycle event
   → scheduler subscription → re-price → new quote version
```

Persistence boundary: keep large RiskCube cells and Parquet partitions in the pricer-owned S3/GCS store; persist only compact quote summaries and immutable object/instance references in the trade Postgres database. Never duplicate the full RiskCube into trade storage.

## Success criteria

- 100k+ structured products, shared path simulation, GPU execution
- AAD risk cube generation with an honest per-cell method label
- Portfolio aggregation and Taylor PnL decomposition
- Event-risk analytics, lifecycle processing, QuantLib integration
- MCP tool orchestration; local stateful and cloud stateless deployment
- Reproducible validation of the bundled term-sheet fixture against the legacy engine's bumping convention

Implementation guidance stays externalized in `refs/*` and `schema/*` so agents load only the required subsystem.
