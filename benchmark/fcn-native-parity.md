# Canonical FCN Native Engine Conformance

## Scope

This evidence records the native **FCN/RakiPlus** pricing lane exposed by the `quote.price` FastMCP operation. The adapter accepts the canonical `pricing-request` envelope with explicit `fcn_terms`; it does not price by inferring positions in a legacy `Chunk.Jobs` array. The migration helper `compile_fcn_native_request` is confined to source ingestion and produces the canonical envelope before the engine boundary.

The C++ engine marker is **`cpp_fcn_rakiplus_v1`**. It returns the three economic legs `FUNDING`, `COUPON`, and `PUT / Terminal Optionality`, as well as expected cashflows, lifecycle transition exemplars, selected branch, request/process IDs, source revision, result and terms schema labels, and evidence status.

## Native design

The native implementation preserves the existing `fina_risk_cpp` ABI and benchmark/daily-term-sheet parity lanes. The canonical lane is added through `run_fcn_rakiplus_json` and the pybind function `price_fcn_rakiplus`. Its typed implementation is divided into terms, observations, lifecycle state, coupon arithmetic, barrier comparison, terminal optionality, funding, settlement, schedule lookup, result assembly, and engine orchestration components under `cpp/include/fina_risk/` and `cpp/src/fina_risk/`.

JSON parsing, result-envelope assembly, MCP transport, and canonical-request compilation occur outside the path × observation loop. The loop reuses its state workspace and only maintains preallocated aggregate arrays. The legacy benchmark targets retain `-O3 -march=native -mtune=native -ffast-math`; `engine.cpp` explicitly uses `-fno-fast-math` so NaN/missing-fixing detection and barrier equality semantics are not optimized away.

## Implemented semantics

| Area | Implemented rule |
|---|---|
| Coupon | Fixed coupon plus range-accrual rate; lower/upper equality is request-configurable; historical paid fixing counts are respected. |
| Coupon memory | Unpaid coupon memory carries when enabled and releases through a qualifying subsequent period. |
| KO | Local and global KO are evaluated on discrete observations; same-day precedence is explicit in `same_day_ko_precedence`. |
| Memory KO | Supported only with explicit `memory_ko_mode: per_underlying_ever`; other interpretations are rejected as ambiguous. |
| KI and terminal payoff | European KI state is evaluated at final fixing; a KI residual PUT is paid only on non-terminated paths. |
| Funding and discounting | Funding is paid at KO or maturity and discounted to evaluation; coupon payment dates are discounted per period. |
| Settlement | Cash/physical settlement metadata is returned; physical delivery is flagged only when the residual terminal optionality is positive. |
| Failures | Missing/non-finite fixings are `unresolved`; continuous monitoring, non-worst-of performance, and undeclared memory-KO modes are explicit unsupported/ambiguous outcomes. |

## Deterministic conformance

The focused native suite covers ordinary coupon days; in-range and out-of-range days; coupon-memory release; local KO; global KO; same-day global precedence; KI/no-KI maturity; per-underlying memory locks; final fixing; payment-date discounting; physical-delivery flags; inclusive range bounds; missing fixings; and ambiguous semantics. A scalar canonical reference oracle is deliberately separate from the C++ lane and compares leg PVs, total PV, KI/KO probabilities, and selected branch on deterministic fixtures.

The full test command completed with **53 passed**. The real stdio MCP E2E initialized a spawned `fina-risk-mcp` server, discovered `quote.price`, called it with a canonical FCN request, required `native: true`, and validated the result against `fcn-native-pricing-result.schema.json`. The evidence manifest was then validated by `fina-skills/scripts/validate_model_registry.py`.

### Termsheet1 30,000-path native stdio result

| Metric | Result |
|---|---:|
| Native engine marker | `cpp_fcn_rakiplus_v1` |
| Canonical quote PV | 1.2578822378779018 |
| Funding leg PV | 0.9907992959786480 |
| Coupon leg PV | 0.2868424186303910 |
| PUT / Terminal Optionality PV | -0.019759476731137312 |
| PUT price (absolute) | 0.019759476731137312 |
| Reference PUT price | 0.02112 |
| Difference | -0.001360523268862688 |
| Acceptance tolerance | 0.002000000000000000 |
| KI probability | 0.12243333333333334 |
| Global-KO probability | 0.5475666666666666 |

The quote is within the stated Monte-Carlo conformance tolerance of the supplied `0.02112` reference. The preserved legacy native fixture adapter, using the same 30,000 paths and seed, prices its PUT at `0.020334585483020577`; its total fixture PV is bit-for-bit unchanged at `1.473866861364184` after the refactor.

## Before/after benchmark

The comparison used a clean detached worktree at baseline revision `61351d4d3e15f6498256cf072da8700684ce59c4`, the generated 2,000-instrument / 1,200-underlying corpus, seed `20260909`, 1,000 paths, and the same Release compiler policy. Timing is subject to ordinary host noise; the controlled results show that linking the typed canonical engine did not regress the preserved benchmark lane.

| Metric | Baseline | Refactored | Change |
|---|---:|---:|---:|
| Outer wall time | 0.610 s | 0.570 s | -6.56% |
| Benchmark throughput | 73,250.15 instruments/s | 94,435.37 instruments/s | +28.92% |
| Native benchmark pricing time | 0.008899 s | 0.006549 s | -26.41% |
| Native benchmark total time | 0.027304 s | 0.021179 s | -22.43% |
| Peak shared path cube | 16,944,000 bytes | 16,944,000 bytes | 0 bytes |
| Price checksum | 1,987,442.6269004024 | 1,987,442.6269004028 | relative drift 2.01e-16 |
| Legacy fixture PV | 1.473866861364184 | 1.473866861364184 | 0 |

## Commands

```bash
uv sync --all-groups --python python3.12
cmake -S cpp -B cpp/build -DCMAKE_BUILD_TYPE=Release
cmake --build cpp/build -j2
PYTHONPATH="$PWD/src:$PWD/cpp/build" uv run pytest -q
PYTHONPATH="$PWD/src:$PWD/cpp/build" uv run python scripts/fcn_stdio_e2e.py \
  --paths 30000 \
  --out benchmark/fcn-stdio-e2e-30k.json \
  --manifest benchmark/fcn-stdio-e2e-30k.manifest.json
```

## Open contract questions and assumptions

The canonical engine intentionally does not claim continuous monitoring, best-of/average/kth-best performance, or unstated memory-KO semantics. `per_underlying_ever` is the only implemented memory-KO convention. The request's path builder uses the canonical market snapshot, NYSE observation grid, downside surface interpolation, deterministic seed, and resolved correlation. Dividends and a full Dupire local-volatility evolution remain outside this first canonical path-builder scope; callers that require them must supply an externally generated valid path cube.

The FastMCP operation is a server-side logical quote operation and never exposes an MCP URL, native module choice, or credentials to a browser. FinAP wiring was not edited because the supplied FinA checkout contains an uninitialized, empty `finap` submodule directory; the adapter is ready for an authenticated FinAP server action to spawn the stdio tool, but repository-level submodule wiring requires an accessible FinAP worktree.
