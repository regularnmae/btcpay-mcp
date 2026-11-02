# btcpay-mcp

A read-only [Model Context Protocol](https://modelcontextprotocol.io/) server
for this stack's [BTCPay Server](https://btcpayserver.org/) settlement engine:
stores, payment rails, invoices and their per-rail payment state, the Bitcoin
on-chain wallet, exchange rates and payment requests.

It is one of the stack's MCP servers. The runtime — the podman/quadlet unit,
the `npm-network` it shares, the NPM path prefix and the env files — is owned by
the [`stack`](https://github.com/regularnmae/stack) repo, not here. This repo
ships code, tests, CI and a `Containerfile`.

## Read-only by construction

Two credentials are involved and neither is a write credential:

| Where | What | Why |
| --- | --- | --- |
| Client → this server | `BTCPAY_MCP_TOKEN`, a bearer token | the same auth model as every sibling MCP server |
| This server → BTCPay | `BTCPAY_API_KEY`, a store-scoped API key | the stack bootstrap mints it with four `canview*` permissions only |

The key's permissions are:

```
btcpay.store.canviewstoresettings
btcpay.store.canviewinvoices
btcpay.store.canviewpaymentrequests
btcpay.store.canviewwallet
```

It cannot create or modify an invoice, refund, pay out, generate a receiving
address, sign or broadcast a transaction. The server itself only issues GETs, so
a bug cannot turn into a write either. The admin key (`BTCPAY_ADMIN_KEY`) and
the BoltMesh backend's runtime key (`BTCPAY_API_KEY` in `config/btcpay/`) are
different credentials and are never given to this server.

A few fields are redacted before a model sees them: `additionalData.preimage`
(knowing it proves payment), `apiKey` (the API-key endpoint echoes the key
itself), and `connectionString`/macaroon/password/token fields in any config.
Internal `http://btcpay:49392/...` links are rewritten to `BTCPAY_PUBLIC_URL`
when the stack sets it, so a checkout link is usable.

## Tools

| Tool | Answers | BTCPay call |
| --- | --- | --- |
| `get_btcpay_health` | Is BTCPay up, is the key accepted, is the store readable? | `/api/v1/health`, `/api/v1/api-keys/current`, `/stores/{id}` |
| `list_stores` | Which stores can this key read? | `/api/v1/stores` |
| `get_store` | One store's settings | `/stores/{id}` |
| `list_payment_methods` | Which rails are enabled (`BTC-LN`, `BTC-CHAIN`, `XMR-CHAIN`) | `/stores/{id}/payment-methods` |
| `get_store_rates` | The exchange rate BTCPay uses | `/stores/{id}/rates?currencyPair=…` |
| `list_invoices` | The settlement list, filtered and paged | `/stores/{id}/invoices` |
| `get_invoice` | One invoice in full | `/api/v1/invoices/{id}` |
| `get_invoice_payment_methods` | Per-rail destination, amount and payments | `/api/v1/invoices/{id}/payment-methods` |
| `get_wallet_balance` | The store's on-chain wallet balance | `…/wallet` |
| `list_wallet_transactions` | On-chain wallet history | `…/wallet/transactions` |
| `list_wallet_utxos` | The spendable on-chain set | `…/wallet/utxos` |
| `list_payment_requests` | Reusable "pay this amount" objects | `/stores/{id}/payment-requests` |

Deliberately absent, because each needs a write permission or an unrestricted
key: invoice creation/refunds/status changes, payouts, pull payments, wallet
address generation, transaction broadcast, webhooks, store settings changes,
server info, and the store's Lightning node (the `lightning` MCP server covers
CLN).

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `BTCPAY_MCP_TOKEN` | — | bearer token clients must present; required unless `MCP_ALLOW_ANONYMOUS=1` |
| `BTCPAY_URL` | `http://btcpay:49392` | Greenfield base URL (on `npm-network`) |
| `BTCPAY_API_KEY` | — | the read-only key; a missing key is reported by `get_btcpay_health` |
| `BTCPAY_STORE_ID` | — | default store for the store-scoped tools |
| `BTCPAY_PUBLIC_URL` | — | public origin used to rewrite internal links (e.g. `https://btcpay.example.net`) |
| `BTCPAY_TIMEOUT` | `30` | HTTP timeout in seconds |
| `MCP_HOST` / `MCP_PORT` | `0.0.0.0` / `8000` | bind address inside the container |
| `LOG_LEVEL` | `INFO` | the stack unit sets `WARNING` |
| `MCP_ALLOW_ANONYMOUS` | — | `1` serves `/mcp` with no token (local debugging only) |

## Endpoints

- `POST /mcp` — Streamable HTTP, bearer-gated.
- `GET /health` — unauthenticated liveness, `{"status":"ok"}`. GET/HEAD only.
- `GET /ready` — unauthenticated, probes BTCPay's `/api/v1/health` on a 3 s
  timeout and caches the answer for 5 s. It deliberately does **not** check the
  API key: a key problem is a tool error, not a reason to restart a healthy
  container.

## Development

```sh
uv run ruff check .            # lint (CI gate)
uv run pyright                 # typecheck (CI gate)
uv run pytest -q               # no network, no BTCPay
uv run python scripts/smoke.py # boot the real app on loopback
uv lock --check                # CI drift gate
```

The image is a single-stage `Containerfile`; build it with
`podman build -t localhost/btcpay-mcp:latest .`.

## Limits

Every list is bounded: 100 invoices/stores/payment requests per call (50 by
default), 200 wallet transactions/UTXOs, 512 KiB per response. A full page sets
`"truncated": true`, because BTCPay's list endpoints return no total — page with
`skip`/`take`. Amounts, rates and balances are BTCPay's decimal **strings**;
timestamps are Unix seconds.

## Deployment

The stack owns it:

```sh
cd ~/build/stack
./deploy.sh --only btcpay        # mints/refreshes BTCPAY_MCP_KEY, writes the env
./deploy.sh --only btcpay-mcp    # build, start, wait for (healthy)
```

The public URL is `https://mcp.boltmesh.net/btcpay`. See the stack's README and
`AGENTS.md` for the unit, the key lifecycle and the rotation story.
