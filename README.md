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
- `src/fina_risk/server.py` — MCP server skeleton. All tools are placeholders
  returning `{"status": "to be done"}`.

## Run

```bash
uv sync --dev
uv run fina-risk-mcp                          # MCP stdio
uv run uvicorn fina_risk.server:app           # Streamable HTTP at /mcp and /healthz
uv run pytest -q
uv run ruff check .
uv run mypy src
```

## Vercel

Mirrors the `fina-pricer` deployment practice: `api/index.py` rewrites the
Vercel `/api` path onto FastMCP's `/mcp` route, `vercel.json` maps `/mcp` to
`/api`, and `[tool.vercel] entrypoint = "api.index:app"` is set in
`pyproject.toml`. Configure `ALLOWED_HOSTS`, and for warm/cold storage `REDIS_URL`
and `S3_API_KEY`/`S3_API_SECRET`/`S3_BUCKET_NAME` as deployment environment
variables (see `.env.example`). Warm metadata lives in Redis; large immutable
artifacts are Parquet on GCS.