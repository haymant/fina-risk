# fina-risk

High-level orchestration skill and MCP server skeleton for structured product
pricing, AAD-first risk cube generation, shared Monte Carlo path simulation,
portfolio risk aggregation, and P&L explain/forecast of equity-linked notes
(ELI/FCN).

- `skills/fina-risk/SKILL.md` — agent contract (frontmatter + orchestration spec).
- `skills/fina-risk/schema/` — hot/warm/cold JSON Schemas.
- `skills/fina-risk/refs/termsheet1.md` / `termsheet1.md.json` — full term sheet
  and the legacy engine pricing request (market data, instrument economics, 1%
  relative bumping settings, 3-leg PUT/FUNDING/COUPON decomposition).
- `src/fina_risk/server.py` — MCP orchestration server with a working local
  pricing/risk path and explicit artifact metadata.
- `src/fina_risk/pricing.py` — deterministic, common-random-number NumPy
  reference engine for the bundled three-leg ELI fixture. It preserves the
  legacy quoted-spot bump convention and emits explainability metadata.
- `skills/fina-risk/schema/pricing-request.schema.json` and
  `term-sheet-conventions.schema.json` — extensible contracts covering legacy
  JSON semantics, payoff conventions, lifecycle/memory state, coupon schedules,
  bumping, and shared batch-computation metadata.

## Run

```bash
uv sync --dev
uv run fina-risk-mcp                          # MCP stdio
uv run uvicorn fina_risk.server:app           # Streamable HTTP at /mcp and /healthz
uv run pytest -q
uv run ruff check .
uv run mypy src
```

The local reference path uses NumPy-batched correlated GBM and shared random
numbers for spot bumps. It is intentionally the correctness baseline for a
future QuantLib/XAD or GPU adapter; Vercel storage and GPU execution remain
lower-priority adapters over the same DTOs.

## Benchmark corpus

The benchmark generator creates 2,000 heterogeneous three-leg instruments over
1,200 underlyings. It retains the full per-underlying spot, dividend, volatility
surface, FX, interest-curve, and 1,200-by-1,200 correlation data rather than
compressing the workload into a small representative sample:

```bash
uv run python scripts/generate_benchmark.py
uv run python scripts/run_benchmark.py \
  benchmark/instruments.json benchmark/market.json
```

Generated JSON files under `benchmark/` are ignored by Git. The local ZIP archive
contains the generated corpus and benchmark result, but is intentionally not
committed by default. The data loader accepts either a normal path or a virtual
ZIP member path such as:

```text
benchmark/fina-risk-benchmark.zip/instruments.json
```

Using a shared 30,000-path, 252-step, 12-factor float32 path representation, the
benchmark priced all 2,000 instruments in approximately **3.05 seconds** on the
local CPU reference backend, or approximately **656 instruments/second**. The
benchmark reports the factorized path memory footprint and uses reverse-indexed
underlyings and batches of 100 instruments.

Fixture and benchmark leg PVs both execute through the shared
`price_terminal_legs.v1` kernel. Their adapters differ only in legacy/augmented
input normalization and coupon cash-flow preparation; PUT, FUNDING, aggregate
PV, and leg-sign conventions are not duplicated in the benchmark runner.

The production-comparable workload matrix, reuse-control design, and realistic
100k FCN estimation guidance are documented in
[`skills/fina-risk/refs/realistic-benchmark-plan.md`](skills/fina-risk/refs/realistic-benchmark-plan.md).
The canonical request schema exposes execution toggles for daily state,
correlation factorization, curve/vol/dividend/FX preparation, AAD scope,
Taylor P&L order, structure caching, and output metrics.

The Python CPU reference now includes a **QuantLib-Risks/XAD first-order adjoint
path** for fixed-branch smooth payoff arithmetic. Its outputs distinguish methods
explicitly: the PUT uses AAD with a pathwise transition fallback, funding is
AAD-eligible, and worst-of/knock-in/physical-delivery and memory-coupon
transitions retain explicit fallback reasons. Both the fixture result and
benchmark result include first-order Taylor P&L components (`forecastPnl`,
`actualPnl`, and `unexplainedPnl`) with AAD-based forecast contributions where
the tape is eligible.

## Vercel

Mirrors the `fina-pricer` deployment practice: `api/index.py` rewrites the
Vercel `/api` path onto FastMCP's `/mcp` route, `vercel.json` maps `/mcp` to
`/api`, and `[tool.vercel] entrypoint = "api.index:app"` is set in
`pyproject.toml`. Configure `ALLOWED_HOSTS`, and for warm/cold storage `REDIS_URL`
and `S3_API_KEY`/`S3_API_SECRET`/`S3_BUCKET_NAME` as deployment environment
variables (see `.env.example`). Warm metadata lives in Redis; large immutable
artifacts are Parquet on GCS.

## Troubleshooting: MCP 421 "Invalid Host header"

The mcp transport security gate only accepts hosts listed in `allowed_hosts`
(exact names plus `host:*` port wildcards; there is no `*.vercel.app` subdomain
wildcard support). A 421 on `https://<host>/mcp` means the deployment hostname
is not allowed — this is the same failure `fina-pricer` fixed by adding its
production hostname to the default list.

Two ways to fix:

1. **Set `ALLOWED_HOSTS` in Vercel Project Settings → Environment Variables** to
   the exact deployment hostname, e.g. `fina-risk-zmrl.vercel.app` (add any
   production/custom domain as well, comma-separated). This overrides the default.
2. **Add the hostname to the default list** in `src/fina_risk/server.py`
   `_resolve_allowed_hosts()` (the deployed preview host
   `fina-risk-zmrl.vercel.app` is already included). Note Vercel preview hosts
   are randomly generated per deployment, so prefer a stable production domain
   or the env-var override.
