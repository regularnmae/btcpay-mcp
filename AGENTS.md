# btcpay-mcp — agent notes

Flat Python MCP server (not a package). `server.py` is the app — config, tools,
auth middleware, projections and redactions; `btcpay.py` is its **stdlib-only**
Greenfield HTTP client (urllib, json — no HTTP library); `healthcheck.py` is
the container healthcheck probe. Shipped as a rootless podman/quadlet
container; the runtime — its unit, the `npm-network` it shares and the env
wiring — is owned by the `stack` repo
(https://github.com/regularnmae/stack), not here. Toolchain is uv only — no pip
in CI, in the build, or in the image. `mcp` is the 2.x line: the app object is
`MCPServer` (the v2 rename of `FastMCP`).

This is a sibling of `bitcoin-node-mcp`, `monero-node-mcp`, `monero-wallet-mcp`,
`lightning-mcp`, `fulcrum-mcp`, `searxng-mcp`, `victoria-logs-mcp`,
`victoriametrics-mcp` and `crowdsec-mcp`. Follow their structure and
conventions. Env vars, tool list and the security model are documented in
`README.md`; don't re-derive them from the code.

## Commands

```sh
uv run ruff check .            # lint (CI gate). No `ruff format`; line-length 100, E,F,I,UP,B,SIM
uv run pyright                 # typecheck (CI gate); include list in [tool.pyright]
uv run pytest -q               # no network, no BTCPay
uv run python scripts/smoke.py # boots the real app on loopback: /health, /ready, 401s, handshake
uv add <pkg>                   # edits pyproject.toml AND re-locks — commit the uv.lock change
uv lock --check                # CI drift gate
podman build -t localhost/btcpay-mcp:latest .   # single-stage Containerfile
```

Deployment is owned by the `stack` repo: `cd ~/build/stack && ./deploy.sh
--only btcpay-mcp` rebuilds the image, restarts `btcpay-mcp.service` and waits
for "(healthy)"; `./deploy.sh` deploys the whole stack. `./deploy.sh --only
btcpay` must have run first, because it is what mints the read-only key and
writes `config/btcpay-mcp/upstream.env`.

## Facts that are easy to get wrong

- **Greenfield's auth scheme is `token`, not `Bearer`.** `Authorization: token
  <BTCPAY_API_KEY>` is BTCPay's; `Bearer <BTCPAY_MCP_TOKEN>` is this server's
  own client credential. They are different layers and must never be crossed.
- **The key is read-only and scope-limited, and that is the security model.**
  It carries exactly `btcpay.store.canviewstoresettings`,
  `canviewinvoices`, `canviewpaymentrequests` and `canviewwallet`, minted by
  `scripts/btcpay_bootstrap.py` in the stack repo. Do not "fix" a 403 by
  widening it. The admin key and the backend runtime key both have write
  authority and are never used here.
- **`GET /api/v1/server/info` requires `unrestricted`.** There is no server
  info tool; `get_btcpay_health` uses the anonymous `/api/v1/health` plus the
  key's own metadata instead.
- **`GET /stores/{id}/payment-methods?includeConfig=true` requires
  `btcpay.store.canmodifystoresettings`.** The server does not request configs;
  BTCPay returns only `paymentMethodId`/`enabled` to a view key. Config is
  still redacted if a future response includes it.
- **The Monero plugin does not implement the Greenfield wallet endpoints.**
  `XMR-CHAIN/wallet*` answers `404 unknown-paymentMethodId`; wallet tools
  default to `BTC-CHAIN`. XMR payments still appear on invoices and in
  `get_invoice_payment_methods`, because those are store-level invoice data.
- **`get_invoice` does not include `paymentMethods` on BTCPay 2.4.5** (the
  field is empty). `/api/v1/invoices/{id}/payment-methods` is the endpoint that
  has destinations, rates and payments — hence the separate tool.
- **`get_invoice_payment_methods` carries the payment preimage.** It is a
  secret: whoever knows it can claim the payment. `_redact` replaces it with
  `"[redacted]"`, as it does `apiKey` (the API-key endpoint echoes the key
  itself) and connection strings. Add a new sensitive field to `_REDACT_KEYS`,
  not a one-off.
- **Amounts, rates and balances are strings**, and invoice timestamps are Unix
  seconds. Never `float()` an amount or reformat a timestamp; `"0.00008995"`
  and `1793573278` are what the API promises.
- **`list_invoices` has no total.** A full page sets `"truncated": true`; page
  with `skip`/`take`. Do not present a page as the whole history.
- **BTCPay builds links from the request Host**, so a call over
  `http://btcpay:49392` returns `http://btcpay:49392/i/<id>`. `_publicise`
  rewrites any string starting with `BTCPAY_URL` to `BTCPAY_PUBLIC_URL`; the
  stack sets that to the public origin. Without it links are left alone.
- **`list_stores` with a store-scoped key is expected to return one row.** If a
  scoped key ever leaks stores outside its scope, filter the tool to
  `BTCPAY_STORE_ID` rather than widening the key.
- **Caps live in `server.py`**: `DEFAULT_LIMIT` 50, `MAX_LIMIT` 100,
  `MAX_TX_LIMIT` 200, `MAX_RATES` 10, `MAX_RESPONSE_BYTES` 512 KiB,
  `MAX_STRING_BYTES` 4 KiB, `MAX_TEXT` 200. The schema's `Field(le=…)` and the
  `_clamp` call must agree; `tests/test_schemas.py` pins the advertised bounds.
- `_budget` is the second bound: it keeps whole items only and sets
  `"truncated": true` when the byte budget is hit, so a response never claims
  to be complete when it is not.
- Every `@mcp.tool` passes `structured_output=False` and
  `annotations=_BTCPAY_TOOLS`; `tests/test_schemas.py` asserts this and the
  exact set of tool names — adding a tool means updating `TOOLS` there and
  proving it is a read.
- `_guard` raises `ToolError` rather than returning error text, so a failure is
  `is_error=True` with the same message. `get_btcpay_health` is the exception:
  it returns a report (`ok`/`degraded`/`unconfigured`/`down`), because asking
  about a broken key is a legitimate question.
- Tool bodies are synchronous (`def`, not `async def`); anyio runs them on
  worker threads. `BtcpayClient` keeps no state between calls, so that is safe.
- A new top-level module must be added to the `Containerfile`'s `COPY` lines
  **and** pyright's `include`, or it won't ship or typecheck.
- `.containerignore` excludes `tests/`, `scripts/`, `*.md`, `.git`.
- The image declares no `HEALTHCHECK`; the `stack` unit supplies
  `HealthCmd=python /app/healthcheck.py`. `stack` writes this server's env files
  (`token.env`, `upstream.env`); see that repo for the unit, the env-file
  location and the host port (8114).
- The server binds `0.0.0.0:8000` and serves `/mcp` (token-gated); `/health` is
  open and answers exactly `{"status":"ok"}`. `/ready` is also open and probes
  BTCPay's `/api/v1/health` (`200` when it answers, `503` otherwise, 3 s
  timeout), cached for `READY_CACHE_TTL` (5 s). Both answer `GET`/`HEAD` only
  and `405` anything else. The podman healthcheck is `healthcheck.py` against
  `/mcp` (an authenticated initialize) and must not be moved to `/ready`, or a
  BTCPay outage restart-loops the container. `/ready` also does not check the
  API key for the same reason.
- The bearer-token env var is **`BTCPAY_MCP_TOKEN`**, not a shared name: each
  sibling server has its own token.

## Testing quirks

- Tests talk to the server through the SDK's in-memory `Client(mcp)` — no test
  opens a socket. `fake_client` (in `tests/conftest.py`) replaces
  `server.client` with a scripted fake that records `(method, args, kwargs)`;
  the autouse `_deterministic_env` pins `BTCPAY_STORE_ID=store1` and
  `BTCPAY_API_KEY=test-key` so a host env cannot change the expected calls.
- `tests/test_btcpay.py` fakes the transport by monkeypatching
  `urllib.request.urlopen`, so URL quoting, query encoding and error mapping
  are covered without BTCPay.
- `pytest` runs with `pythonpath = ["."]`, so `import server` and
  `import btcpay` work as top-level modules. Do **not** `from conftest import
  ...` in a test — use the fixtures.
- `tests/test_tools.py` asserts the **redacted** value (`"[redacted]"`), never
  the secret; keep it that way if you add a sensitive field.
