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