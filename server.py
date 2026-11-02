import asyncio
import hmac
import json
import logging
import os
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, get_args

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field

from btcpay import BtcpayClient, BtcpayError

logger = logging.getLogger("btcpay-mcp")

# The SDK types log_level as a closed set, so an unrecognised LOG_LEVEL has to
# be resolved to one of its levels here -- the raw env string is not
# assignable, and an unknown level falling back to INFO beats the import-time
# raise that the SDK would otherwise produce.
_LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
_LEVELS: tuple[_LogLevel, ...] = get_args(_LogLevel)


def _log_level() -> _LogLevel:
    requested = os.environ.get("LOG_LEVEL", "INFO").upper()
    return requested if requested in _LEVELS else "INFO"


def _env_float(name: str, default: float) -> float:
    """A float env var, or the default when unset or unparseable.

    A typo'd timeout should not stop the server from starting, so a bad value
    warns and falls back rather than raising at import.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("ignoring non-numeric %s=%r; using %s", name, raw, default)
        return default


mcp = MCPServer(
    "BTCPay",
    instructions=(
        "Read-only view of this host's BTCPay Server settlement engine: the "
        "stores, their enabled payment methods and rates, invoices and their "
        "per-rail payment state, the Bitcoin on-chain wallet, and payment "
        "requests. list_invoices answers 'what came in', get_invoice and "
        "get_invoice_payment_methods answer 'did it settle and on which rail', "
        "and get_wallet_balance / list_wallet_transactions read the on-chain "
        "wallet. Every tool is a read: the API key this server holds has four "
        "view permissions and cannot create, refund, pay out, generate an "
        "address or broadcast a transaction. BTCPay's server-level info "
        "endpoint needs an unrestricted key and is deliberately not exposed. "
        "The Monero plugin does not implement these wallet endpoints, so "
        "wallet tools name BTC-CHAIN; XMR payments still appear on invoices. "
        "Amounts are decimal strings, not floats."
    ),
    log_level=_log_level(),
)

# Every tool here only reads, and the key it holds cannot write. Saying so lets
# a well-behaved client skip a confirmation prompt before running one.
_BTCPAY_TOOLS = ToolAnnotations(read_only_hint=True, open_world_hint=False)

# Reached over the podman npm-network (the container name resolves without any
# host publishing); BTCPAY_URL overrides. The key is the read-only one the
# stack bootstrap mints, never the admin or backend runtime key.
BTCPAY_URL = os.environ.get("BTCPAY_URL", "http://btcpay:49392")
BTCPAY_API_KEY = os.environ.get("BTCPAY_API_KEY", "")
BTCPAY_STORE_ID = os.environ.get("BTCPAY_STORE_ID", "")
# BTCPay builds absolute links from the request's Host, so a call over the
# internal name returns `http://btcpay:49392/i/<id>`. When the stack sets the
# public origin, every string that starts with BTCPAY_URL is rewritten to it,
# which makes checkout/receipt links usable by a client instead of naming a
# container. Empty means "leave links as BTCPay returned them".
BTCPAY_PUBLIC_URL = os.environ.get("BTCPAY_PUBLIC_URL", "").rstrip("/")
BTCPAY_TIMEOUT = _env_float("BTCPAY_TIMEOUT", 30.0)

# Set to "1" to serve the endpoint with no token at all. Without it a missing
# token is a startup error, not a warning.
MCP_ALLOW_ANONYMOUS = os.environ.get("MCP_ALLOW_ANONYMOUS", "") == "1"

# Unauthenticated liveness path, for pollers that hold no token.
HEALTH_PATH = "/health"

# Readiness path: /health says the process is up, this says BTCPay answers.
# Deliberately separate -- a BTCPay outage must not fail a liveness probe, or
# the container restarts in a loop while the settlement engine recovers. It
# does not check the API key either: a 401 on every tool is a configuration
# problem for get_btcpay_health to report, not a reason to kill the process.
READY_PATH = "/ready"
READY_TIMEOUT = 3.0
# /ready is unauthenticated, and every probe is a request to BTCPay, so the
# answer is cached for a few seconds: a poller hammering the path then costs
# one round trip per TTL instead of one per request.
READY_CACHE_TTL = 5.0
_ready_cache: tuple[float, dict[str, Any], int] | None = None
# Created lazily, inside the running loop: an asyncio.Lock binds to the loop
# that first awaits it, and this server has exactly one in production.
_ready_lock: asyncio.Lock | None = None

# The env var the bearer token is read from (also the name in the quadlet
# unit's EnvironmentFile and in the client configs).
TOKEN_ENV = "BTCPAY_MCP_TOKEN"

client = BtcpayClient(BTCPAY_URL, BTCPAY_API_KEY, BTCPAY_TIMEOUT)

if not BTCPAY_API_KEY:
    logger.warning("BTCPAY_API_KEY is not set; every tool except get_btcpay_health will fail")
if not BTCPAY_STORE_ID:
    logger.warning("BTCPAY_STORE_ID is not set; store_id must be given on every store-scoped tool")

# --- output caps -------------------------------------------------------
# A list answer is what a model reads, so every list tool caps both the number
# of rows and the bytes. The defaults are what fits in one read; the maximum is
# the guard against a caller that asks for everything.
DEFAULT_LIMIT = 50
MAX_LIMIT = 100
MAX_TX_LIMIT = 200
MAX_RATES = 10
MAX_RESPONSE_BYTES = 512 * 1024
MAX_STRING_BYTES = 4096
MAX_TEXT = 200

# Fields that are credentials or key material when they appear in a BTCPay
# object. Names are matched case-insensitively because the API mixes
# `apiKey`/`connectionString` camel case with lower-case JSON.
_REDACT_KEYS = frozenset(
    {
        "preimage",
        "apikey",
        "api_key",
        "connectionstring",
        "connection_string",
        "password",
        "token",
        "secret",
        "privatekey",
        "private_key",
        "macaroon",
        "mnemonic",
        "seed",
        "rpcpassword",
    }
)

_ID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_PAYMENT_METHOD = re.compile(r"^[A-Za-z0-9-]{1,40}$")
_CURRENCY_PAIR = re.compile(r"^[A-Za-z0-9]{1,12}_[A-Za-z0-9]{1,12}$")
_STATUSES = {
    "new": "New",
    "processing": "Processing",
    "expired": "Expired",
    "invalid": "Invalid",
    "settled": "Settled",
}

# The fields worth returning from a list. BTCPay's full objects carry links,
# receipts and payment-method blobs; a list of fifty of them fills a context
# window without answering the question.
_INVOICE_FIELDS = (
    "id",
    "status",
    "amount",
    "currency",
    "paidAmount",
    "createdTime",
    "expirationTime",
    "monitoringExpiration",
    "metadata",
    "additionalStatus",
    "archived",
    "type",
    "checkoutLink",
)
_STORE_FIELDS = ("id", "name", "defaultCurrency", "defaultPaymentMethod", "archived")
_PAYMENT_METHOD_FIELDS = ("paymentMethodId", "enabled", "config")
_TRANSACTION_FIELDS = (
    "transactionHash",
    "amount",
    "status",
    "confirmations",
    "timestamp",
    "blockHeight",
    "labels",
    "comment",
)
_UTXO_FIELDS = (
    "outpoint",
    "amount",
    "confirmations",
    "address",
    "keyPath",
    "timestamp",
    "labels",
    "comment",
)


def _clamp(value: int, maximum: int) -> int:
    """Clamp a limit into the range the tools advertise."""
    return max(1, min(int(value), maximum))


def _render(payload: Any) -> str:
    """A tool result as text, with internal BTCPay links made public."""
    return json.dumps(_publicise(payload), indent=2, ensure_ascii=False)


def _publicise(value: Any) -> Any:
    """Rewrite this BTCPay instance's own base URL to its public origin.

    Every link BTCPay returns is absolute and built from the request Host. The
    stack calls it as `http://btcpay:49392`, a name no client can resolve, so
    with BTCPAY_PUBLIC_URL set a checkout link becomes the operator's real
    host. A string that does not start with the API base is untouched.
    """
    if not BTCPAY_PUBLIC_URL:
        return value
    if isinstance(value, dict):
        return {name: _publicise(item) for name, item in value.items()}
    if isinstance(value, list):
        return [_publicise(item) for item in value]
    if isinstance(value, str) and value.startswith(BTCPAY_URL):
        return BTCPAY_PUBLIC_URL + value[len(BTCPAY_URL) :]
    return value


def _truncate(value: str) -> str:
    """Cap one string at MAX_STRING_BYTES of UTF-8, never splitting a char."""
    raw = value.encode("utf-8")
    if len(raw) <= MAX_STRING_BYTES:
        return value
    kept = raw[:MAX_STRING_BYTES].decode("utf-8", "ignore")
    return f"{kept} … [truncated {len(raw) - MAX_STRING_BYTES} bytes]"


def _redact(value: Any, key: str | None = None) -> Any:
    """Remove credentials and cap long strings, recursively.

    The point is not to hide BTCPay's own data: it is that a few fields are
    secrets (an invoice preimage proves payment; an API key's own response
    echoes the key; a payment method's connection string can carry a macaroon)
    and a model should never be handed one by accident.
    """
    if isinstance(value, dict):
        return {
            str(name): (
                "[redacted]"
                if str(name).lower() in _REDACT_KEYS
                else _redact(item, str(name))
            )
            for name, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return _truncate(value)
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return _truncate(str(value))


def _project(row: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    """One API object reduced to the fields a list should return."""
    return {name: _redact(row[name]) for name in fields if name in row}


def _budget(items: list[Any], limit: int) -> tuple[list[Any], bool]:
    """Keep at most `limit` items and at most MAX_RESPONSE_BYTES of them.

    The item cap is the caller's; this is the second bound, because one
    payment method or one BOLT11 destination can be large regardless of how
    few rows were asked for. When the budget is hit the answer says so rather
    than silently returning less.
    """
    truncated = len(items) > limit
    kept: list[Any] = []
    size = 0
    for item in items[:limit]:
        encoded = json.dumps(item, ensure_ascii=False).encode("utf-8")
        if size + len(encoded) > MAX_RESPONSE_BYTES:
            truncated = True
            break
        kept.append(item)
        size += len(encoded)
    return kept, truncated


def _identifier(value: str, label: str) -> str:
    """A BTCPay id (store, invoice, payment method) or a ValueError naming it."""
    text = value.strip()
    if not _ID.match(text):
        raise ValueError(
            f"{label} must be a BTCPay id of 1-100 letters, digits, '_' or '-', not {value!r}"
        )
    return text


def _store_id(value: str | None) -> str:
    store = value.strip() if value else BTCPAY_STORE_ID.strip()
    if not store:
        raise ValueError(
            "store_id is required because BTCPAY_STORE_ID is not set; use "
            "list_stores to see the stores this key can read"
        )
    return _identifier(store, "store_id")


def _payment_method(value: str) -> str:
    method = value.strip()
    if not _PAYMENT_METHOD.match(method):
        raise ValueError(
            "payment_method must be a BTCPay payment method id like BTC-CHAIN "
            f"or BTC-LN, not {value!r}"
        )
    return method


def _currency_pairs(value: str) -> list[str]:
    pairs = [part.strip() for part in value.split(",") if part.strip()]
    if not pairs:
        raise ValueError("currency_pairs must name at least one pair like BTC_USD")
    if len(pairs) > MAX_RATES:
        raise ValueError(f"currency_pairs is capped at {MAX_RATES} pairs")
    for pair in pairs:
        if not _CURRENCY_PAIR.match(pair):
            raise ValueError(
                f"currency_pairs entries must look like BTC_USD, not {pair!r}"
            )
    return pairs


def _statuses(value: str | None) -> list[str] | None:
    if not value:
        return None
    parts = [part.strip() for part in value.split(",") if part.strip()]
    if not parts:
        return None
    statuses: list[str] = []
    for part in parts:
        canonical = _STATUSES.get(part.lower())
        if canonical is None:
            raise ValueError(
                "status must be one of New, Processing, Expired, Invalid or "
                f"Settled (comma-separated for several), not {part!r}"
            )
        if canonical not in statuses:
            statuses.append(canonical)
    return statuses


def _timestamp(value: str | int, label: str) -> int:
    """A Unix timestamp as BTCPay's list filters want it.

    Greenfield's `startDate`/`endDate` are `UnixTimestamp`, not ISO strings
    (the live API answers 422 for an RFC3339 value). An int is passed through,
    which is what `agent-mcp`'s CLI produces for an unquoted number; a string
    may be whole seconds or an RFC3339 instant, converted as a convenience
    because a model is more likely to have a date than an epoch.
    """
    if isinstance(value, int):
        if value < 0:
            raise ValueError(f"{label} must not be negative")
        return value
    text = value.strip()
    if len(text) > 40:
        raise ValueError(f"{label} is capped at 40 characters")
    if text.isdigit():
        return int(text)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as err:
        raise ValueError(
            f"{label} must be Unix seconds like 1793573278, or an RFC3339 "
            f"instant like 2026-11-02T10:00:00Z, not {value!r}"
        ) from err
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp())


def _text(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if len(text) > MAX_TEXT:
        raise ValueError(f"{label} is capped at {MAX_TEXT} characters")
    return text


def _list_answer(key: str, rows: list[Any], limit: int, **extra: Any) -> dict[str, Any]:
    """A consistent list envelope: key, returned, truncation hint."""
    kept, truncated = _budget(rows, limit)
    payload: dict[str, Any] = {key: kept, "returned": len(kept), **extra}
    # BTCPay's list endpoints do not return a total, so the honest hint is
    # "the page is full": the caller should page or narrow the filters.
    if truncated or len(rows) >= limit:
        payload["truncated"] = True
    return payload


def _stores(limit: int) -> dict[str, Any]:
    rows = [_project(row, _STORE_FIELDS) for row in client.stores()]
    return _list_answer("stores", rows, _clamp(limit, MAX_LIMIT))


def _store(store_id: str | None) -> dict[str, Any]:
    return _redact(client.store(_store_id(store_id)))


def _payment_methods(store_id: str | None, limit: int) -> dict[str, Any]:
    store = _store_id(store_id)
    rows = [
        _project(row, _PAYMENT_METHOD_FIELDS)
        for row in client.payment_methods(store)
    ]
    return _list_answer(
        "payment_methods", rows, _clamp(limit, MAX_LIMIT), store_id=store
    )


def _rates(store_id: str | None, currency_pairs: str) -> dict[str, Any]:
    store = _store_id(store_id)
    pairs = _currency_pairs(currency_pairs)
    rows = [_redact(row) for row in client.rates(store, pairs)]
    return _list_answer("rates", rows, MAX_RATES, store_id=store, currency_pairs=pairs)


def _invoices(
    store_id: str | None,
    status: str | None,
    text_search: str | None,
    start_date: str | int | None,
    end_date: str | int | None,
    skip: int,
    take: int,
    include_payment_methods: bool,
) -> dict[str, Any]:
    store = _store_id(store_id)
    params: dict[str, Any] = {
        "status": _statuses(status),
        "textSearch": _text(text_search, "text_search"),
        "startDate": _timestamp(start_date, "start_date") if start_date else None,
        "endDate": _timestamp(end_date, "end_date") if end_date else None,
        "skip": skip,
        "take": _clamp(take, MAX_LIMIT),
        "includePaymentMethods": "true" if include_payment_methods else None,
    }
    rows: list[dict[str, Any]] = []
    for row in client.invoices(store, params):
        projected = _project(row, _INVOICE_FIELDS)
        if include_payment_methods:
            # `paymentMethods` is not in _INVOICE_FIELDS (it is opt-in), so it
            # has to come from the original row.
            projected["paymentMethods"] = _redact(row.get("paymentMethods"))
        rows.append(projected)
    payload = _list_answer(
        "invoices",
        rows,
        _clamp(take, MAX_LIMIT),
        store_id=store,
        pagination={"skip": skip, "take": _clamp(take, MAX_LIMIT)},
    )
    applied = {
        name: value
        for name, value in params.items()
        if value not in (None, "", False)
    }
    payload["filters"] = applied
    return payload


def _invoice(invoice_id: str) -> dict[str, Any]:
    invoice = _redact(client.invoice(_identifier(invoice_id, "invoice_id")))
    if isinstance(invoice, dict):
        methods = invoice.get("paymentMethods")
        if isinstance(methods, list):
            invoice["paymentMethods"] = methods[:MAX_LIMIT]
            if len(methods) > MAX_LIMIT:
                invoice["paymentMethods_truncated"] = True
    return invoice


def _invoice_payment_methods(invoice_id: str, limit: int) -> dict[str, Any]:
    invoice = _identifier(invoice_id, "invoice_id")
    rows = [_redact(row) for row in client.invoice_payment_methods(invoice)]
    return _list_answer(
        "payment_methods", rows, _clamp(limit, MAX_LIMIT), invoice_id=invoice
    )


def _wallet(store_id: str | None, payment_method: str) -> dict[str, Any]:
    store = _store_id(store_id)
    method = _payment_method(payment_method)
    return {
        "store_id": store,
        "payment_method": method,
        "wallet": _redact(client.wallet(store, method)),
    }


def _wallet_transactions(
    store_id: str | None, payment_method: str, skip: int, limit: int
) -> dict[str, Any]:
    store = _store_id(store_id)
    method = _payment_method(payment_method)
    capped = _clamp(limit, MAX_TX_LIMIT)
    rows = [
        _project(row, _TRANSACTION_FIELDS)
        for row in client.wallet_transactions(
            store, method, {"skip": skip, "limit": capped}
        )
    ]
    return _list_answer(
        "transactions",
        rows,
        capped,
        store_id=store,
        payment_method=method,
        pagination={"skip": skip, "limit": capped},
    )


def _wallet_utxos(store_id: str | None, payment_method: str, limit: int) -> dict[str, Any]:
    store = _store_id(store_id)
    method = _payment_method(payment_method)
    rows = [
        _project(row, _UTXO_FIELDS) for row in client.wallet_utxos(store, method)
    ]
    return _list_answer(
        "utxos", rows, _clamp(limit, MAX_TX_LIMIT), store_id=store, payment_method=method
    )


def _payment_requests(
    store_id: str | None, include_archived: bool, limit: int
) -> dict[str, Any]:
    store = _store_id(store_id)
    rows = [
        _redact(row)
        for row in client.payment_requests(store, include_archived=include_archived)
    ]
    return _list_answer(
        "payment_requests",
        rows,
        _clamp(limit, MAX_LIMIT),
        store_id=store,
        include_archived=include_archived,
    )


def _health() -> dict[str, Any]:
    """BTCPay is answering, the key works, and the default store is readable.

    A report, not a tool error: asking about a misconfigured key is a
    legitimate question, and the answer says which of the three failed. The
    key's own `apiKey` field is redacted; only its label, id and permissions
    are returned.
    """
    try:
        health = client.health()
    except BtcpayError as err:
        return {"status": "down", "api": "down", "error": err.message}

    synchronized = health.get("synchronized")
    report: dict[str, Any] = {
        "api": "ok",
        "synchronized": synchronized if isinstance(synchronized, bool) else None,
    }
    if not BTCPAY_API_KEY:
        report["status"] = "unconfigured"
        report["api_key"] = {"configured": False}
        return report

    try:
        info = client.api_key_info()
    except BtcpayError as err:
        report["status"] = "degraded"
        report["api_key"] = {"configured": True, "error": err.message}
        return report

    permissions = info.get("permissions")
    report["api_key"] = {
        "configured": True,
        "label": info.get("label"),
        "id": info.get("id"),
        "permissions": permissions if isinstance(permissions, list) else [],
    }

    if BTCPAY_STORE_ID:
        try:
            store = client.store(_store_id(None))
        except (BtcpayError, ValueError) as err:
            report["status"] = "degraded"
            report["store"] = {"error": getattr(err, "message", str(err))}
            return report
        report["store"] = {"id": store.get("id"), "name": store.get("name")}

    report["status"] = "ok" if report["synchronized"] is not False else "degraded"
    return report


def _guard(label: str, action: Callable[[], Any]) -> str:
    """Run a tool body, turning its known failures into a tool error.

    `label` names the intent that failed, so a log line says what broke
    without every tool repeating the same three `except` clauses. Failures
    raise `ToolError`, which the SDK returns as `is_error=True` with the same
    text the model would otherwise have read -- so a client can tell "BTCPay
    did not answer" from "no invoices matched".
    """
    try:
        return _render(action())
    except ValueError as err:
        raise ToolError(str(err)) from err
    except BtcpayError as err:
        logger.warning("%s failed: %s", label, err.message)
        raise ToolError(err.message) from err
    except ToolError:
        raise
    except Exception as err:  # noqa: BLE001 - surfaced as text, never hidden
        logger.exception("unexpected error in %s", label)
        raise ToolError(f"Unexpected error calling {label}: {err}") from err


@mcp.tool(structured_output=False, annotations=_BTCPAY_TOOLS)
def get_btcpay_health() -> str:
    """Report whether BTCPay answers and whether this server's key can read it.

    Three questions, because they fail differently: `api` says the HTTP
    endpoint answers, `api_key` says the read-only key is accepted and names
    its permissions, and `store` says the configured store is readable.
    `unconfigured` means no key is set; `degraded` means the key or store was
    refused; `down` means BTCPay itself did not answer. `synchronized` is
    BTCPay's own view of its chain sync. This is the first call to make when
    every other tool returns 403.
    """
    return _guard("get_btcpay_health", _health)


@mcp.tool(structured_output=False, annotations=_BTCPAY_TOOLS)
def list_stores(
    limit: Annotated[int, Field(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> str:
    """List the stores this key can read.

    The stack's key is scoped to the BoltMesh store, so the normal answer is
    one row. Each row carries `id`, `name`, `defaultCurrency`,
    `defaultPaymentMethod` and `archived`; use a row's `id` as `store_id` on
    the other tools, or rely on `BTCPAY_STORE_ID` and omit it.

    Args:
        limit: Maximum stores to return (1-100, default 50).
    """
    return _guard("list_stores", lambda: _stores(limit))


@mcp.tool(structured_output=False, annotations=_BTCPAY_TOOLS)
def get_store(
    store_id: Annotated[str | None, Field(max_length=100)] = None,
) -> str:
    """Read one store's settings.

    The full store object: name, default currency, invoice expiration, speed
    policy, payment tolerance, receipt/checkout preferences and the rest.
    Amounts are strings; nothing here is a credential. Use
    list_payment_methods for the rails it accepts.

    Args:
        store_id: Store id (default: BTCPAY_STORE_ID).
    """
    return _guard("get_store", lambda: _store(store_id))


@mcp.tool(structured_output=False, annotations=_BTCPAY_TOOLS)
def list_payment_methods(
    store_id: Annotated[str | None, Field(max_length=100)] = None,
    limit: Annotated[int, Field(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> str:
    """List the payment rails enabled on a store.

    One row per rail (`BTC-CHAIN`, `BTC-LN`, `XMR-CHAIN`, ...) with `enabled`.
    BTCPay only returns a rail's `config` to a key with store-modify
    permission, which this server does not hold; credentials would be
    redacted even if it did. A rail that is listed but not enabled is one
    BTCPay will not offer at checkout.

    Args:
        store_id: Store id (default: BTCPAY_STORE_ID).
        limit: Maximum rails to return (1-100, default 50).
    """
    return _guard("list_payment_methods", lambda: _payment_methods(store_id, limit))


@mcp.tool(structured_output=False, annotations=_BTCPAY_TOOLS)
def get_store_rates(
    currency_pairs: Annotated[str, Field(min_length=1, max_length=MAX_TEXT)],
    store_id: Annotated[str | None, Field(max_length=100)] = None,
) -> str:
    """Read the exchange rates BTCPay uses to price invoices.

    A pair is `CRYPTO_FIAT`, e.g. `BTC_USD`; several can be comma-separated
    (`BTC_USD,BTC_EUR`), up to ten. Each answer carries `currencyPair`,
    `rate` (a decimal string) and any `errors` the rate source reported. An
    empty list means BTCPay has no rate configured for those pairs.

    Args:
        currency_pairs: One or more `CRYPTO_FIAT` pairs, comma-separated.
        store_id: Store id (default: BTCPAY_STORE_ID).
    """
    return _guard("get_store_rates", lambda: _rates(store_id, currency_pairs))


@mcp.tool(structured_output=False, annotations=_BTCPAY_TOOLS)
def list_invoices(
    store_id: Annotated[str | None, Field(max_length=100)] = None,
    status: Annotated[str | None, Field(max_length=MAX_TEXT)] = None,
    text_search: Annotated[str | None, Field(max_length=MAX_TEXT)] = None,
    start_date: Annotated[str | int | None, Field(description="Unix seconds or RFC3339")] = None,
    end_date: Annotated[str | int | None, Field(description="Unix seconds or RFC3339")] = None,
    skip: Annotated[int, Field(ge=0, le=100_000)] = 0,
    take: Annotated[int, Field(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    include_payment_methods: bool = False,
) -> str:
    """List a store's invoices, newest first, with optional filters.

    The settlement question: `status` filters one or more of `New`,
    `Processing`, `Expired`, `Invalid`, `Settled` (comma-separated);
    `start_date`/`end_date` are Unix seconds (an RFC3339 instant also works
    and is converted); `text_search` matches the invoice's text fields. Rows
    carry `amount`, `paidAmount`, `currency`, `status`, `createdTime` and
    `metadata`; `createdTime` and the other timestamps are Unix seconds. With
    `include_payment_methods=true` each row also carries its per-rail
    destinations and payments (preimages redacted).

    Page with `skip`/`take`. A full page sets `truncated: true`, because
    BTCPay's list endpoint returns no total -- page until it stops being set.

    Args:
        store_id: Store id (default: BTCPAY_STORE_ID).
        status: One or more statuses, comma-separated.
        text_search: Free-text filter (order id, buyer email, ...).
        start_date: Only invoices created at/after this Unix timestamp (or
            RFC3339 instant).
        end_date: Only invoices created at/before this Unix timestamp (or
            RFC3339 instant).
        skip: Rows to skip (default 0).
        take: Rows to return (1-100, default 50).
        include_payment_methods: Include per-rail payment details.
    """
    return _guard(
        "list_invoices",
        lambda: _invoices(
            store_id,
            status,
            text_search,
            start_date,
            end_date,
            skip,
            take,
            include_payment_methods,
        ),
    )


@mcp.tool(structured_output=False, annotations=_BTCPAY_TOOLS)
def get_invoice(
    invoice_id: Annotated[str, Field(min_length=1, max_length=100)],
) -> str:
    """Read one invoice in full.

    The invoice object: status, amount, paidAmount, currency, checkout and
    receipt links, metadata and timestamps (Unix seconds). Payment-rail detail
    is *not* included here on this BTCPay build (the field comes back empty);
    use get_invoice_payment_methods for destinations, rates and individual
    payments. Preimages are redacted.

    Args:
        invoice_id: BTCPay invoice id, e.g. `AzCB7tdFKukm8dFCAuC8jc`.
    """
    return _guard("get_invoice", lambda: _invoice(invoice_id))


@mcp.tool(structured_output=False, annotations=_BTCPAY_TOOLS)
def get_invoice_payment_methods(
    invoice_id: Annotated[str, Field(min_length=1, max_length=100)],
    limit: Annotated[int, Field(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> str:
    """Read an invoice's per-rail payment state.

    One row per rail the invoice offered: `paymentMethodId` (`BTC-LN`,
    `BTC-CHAIN`, `XMR-CHAIN`), `amount`, `due`, `totalPaid`,
    `paymentMethodPaid`, `rate`, the `destination` (BOLT11 invoice or
    address), `activated` and the individual `payments`. This is where "did
    it settle, and on which rail" is answered. `additionalData.preimage` is
    redacted.

    Args:
        invoice_id: BTCPay invoice id.
        limit: Maximum rails to return (1-100, default 50).
    """
    return _guard(
        "get_invoice_payment_methods",
        lambda: _invoice_payment_methods(invoice_id, limit),
    )


@mcp.tool(structured_output=False, annotations=_BTCPAY_TOOLS)
def get_wallet_balance(
    store_id: Annotated[str | None, Field(max_length=100)] = None,
    payment_method: Annotated[str, Field(min_length=1, max_length=40)] = "BTC-CHAIN",
) -> str:
    """Read a store's on-chain wallet balance.

    `balance`, `confirmedBalance` and `unconfirmedBalance` in the wallet's
    own unit (BTC), as decimal strings, plus the wallet's `label`. This reads
    the store's BTCPay-managed wallet, not the node's, and not the Monero
    plugin's wallet -- the Monero plugin does not implement this endpoint.

    Args:
        store_id: Store id (default: BTCPAY_STORE_ID).
        payment_method: On-chain rail id (default: BTC-CHAIN).
    """
    return _guard("get_wallet_balance", lambda: _wallet(store_id, payment_method))


@mcp.tool(structured_output=False, annotations=_BTCPAY_TOOLS)
def list_wallet_transactions(
    store_id: Annotated[str | None, Field(max_length=100)] = None,
    payment_method: Annotated[str, Field(min_length=1, max_length=40)] = "BTC-CHAIN",
    skip: Annotated[int, Field(ge=0, le=100_000)] = 0,
    limit: Annotated[int, Field(ge=1, le=MAX_TX_LIMIT)] = DEFAULT_LIMIT,
) -> str:
    """List a store's on-chain wallet transactions, newest first.

    Each row carries `transactionHash`, `amount` (signed, in BTC),
    `confirmations`, `status`, `timestamp`, `blockHeight`, `labels` and
    `comment`. Use it to answer "did the on-chain payment arrive" and "what
    moved out". Nothing here signs or broadcasts.

    Args:
        store_id: Store id (default: BTCPAY_STORE_ID).
        payment_method: On-chain rail id (default: BTC-CHAIN).
        skip: Rows to skip (default 0).
        limit: Rows to return (1-200, default 50).
    """
    return _guard(
        "list_wallet_transactions",
        lambda: _wallet_transactions(store_id, payment_method, skip, limit),
    )


@mcp.tool(structured_output=False, annotations=_BTCPAY_TOOLS)
def list_wallet_utxos(
    store_id: Annotated[str | None, Field(max_length=100)] = None,
    payment_method: Annotated[str, Field(min_length=1, max_length=40)] = "BTC-CHAIN",
    limit: Annotated[int, Field(ge=1, le=MAX_TX_LIMIT)] = DEFAULT_LIMIT,
) -> str:
    """List a store's on-chain wallet UTXOs.

    Each row carries `outpoint` (`txid:vout`), `amount` (BTC), `address`,
    `confirmations`, `keyPath`, `timestamp`, `labels` and `comment`. It is
    the spendable set behind get_wallet_balance, for "what coins are there"
    rather than "what arrived".

    Args:
        store_id: Store id (default: BTCPAY_STORE_ID).
        payment_method: On-chain rail id (default: BTC-CHAIN).
        limit: Maximum UTXOs to return (1-200, default 50).
    """
    return _guard(
        "list_wallet_utxos", lambda: _wallet_utxos(store_id, payment_method, limit)
    )


@mcp.tool(structured_output=False, annotations=_BTCPAY_TOOLS)
def list_payment_requests(
    store_id: Annotated[str | None, Field(max_length=100)] = None,
    include_archived: bool = False,
    limit: Annotated[int, Field(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> str:
    """List a store's payment requests.

    Payment requests are BTCPay's reusable "pay this amount" objects (a
    donation page, a deposit request), separate from invoices. Each row
    carries its title, amount, currency, expiry and form metadata. Archived
    ones are hidden unless `include_archived` is set. Payment *payouts* and
    pull payments are deliberately not exposed: listing them needs a write
    permission, and this server holds none.

    Args:
        store_id: Store id (default: BTCPAY_STORE_ID).
        include_archived: Include archived payment requests.
        limit: Maximum rows to return (1-100, default 50).
    """
    return _guard(
        "list_payment_requests",
        lambda: _payment_requests(store_id, include_archived, limit),
    )


async def _readiness() -> tuple[dict[str, Any], int]:
    """`/ready`'s answer, cached for READY_CACHE_TTL seconds.

    The path is unauthenticated, so without the cache any client could turn
    one HTTP request into one BTCPay request. The cache is a plain module
    global: a five-second-old liveness answer is still the right answer. The
    lock means a burst of concurrent requests produces one probe rather than
    one each.
    """
    global _ready_cache, _ready_lock
    if _ready_lock is None:
        _ready_lock = asyncio.Lock()
    async with _ready_lock:
        cached = _ready_cache
        if cached is not None and time.monotonic() - cached[0] < READY_CACHE_TTL:
            return cached[1], cached[2]
        body, status_code = await _probe_readiness()
        _ready_cache = (time.monotonic(), body, status_code)
        return body, status_code


def _btcpay_probe() -> None:
    """One cheap read, bounded by READY_TIMEOUT rather than the tool timeout.

    A separate client on purpose: the readiness answer must not wait thirty
    seconds on a wedged BTCPay, and it must not inherit whatever timeout the
    tools were configured with. The health endpoint is unauthenticated, so
    this says "BTCPay answers" and not "the key is right" -- a key problem
    must not restart a perfectly healthy container.
    """
    BtcpayClient(BTCPAY_URL, "", READY_TIMEOUT).health()


async def _probe_readiness() -> tuple[dict[str, Any], int]:
    """Ask BTCPay for its health, off the event loop with a short timeout."""
    try:
        await asyncio.to_thread(_btcpay_probe)
    except BtcpayError as err:
        logger.warning("/ready: BTCPay is not answering: %s", err.message)
        return {"status": "unready", "error": err.message}, 503
    except Exception as err:  # noqa: BLE001 - reported, never raised at a probe
        logger.exception("/ready: unexpected error")
        return {"status": "unready", "error": str(err)}, 503
    return {"status": "ready", "btcpay": "ok"}, 200


async def _json_response(
    scope: Any,
    receive: Any,
    send: Any,
    body: dict[str, Any],
    status_code: int,
    headers: dict[str, str] | None = None,
) -> None:
    """Send one JSON response, with no body for a HEAD request."""
    from starlette.responses import JSONResponse

    response = JSONResponse(body, status_code=status_code, headers=headers)
    if scope.get("method") == "HEAD":
        # JSONResponse has already set Content-Length for the body it would
        # have sent; HEAD promises the headers and no body.
        response.body = b""
    await response(scope, receive, send)


class BearerAuthMiddleware:
    """Pure-ASGI bearer auth in front of the streamable-HTTP app.

    The endpoint is published on the LAN through nginx-proxy-manager, so a
    missing token is a hard error rather than a warning: serving every tool
    unauthenticated for as long as the env var is absent is not a failure mode
    worth keeping. Set MCP_ALLOW_ANONYMOUS=1 to run it open on purpose, which
    is what debugging from inside the host wants.

    GET /health stays unauthenticated so a poller can say "up" without holding
    the token; it says nothing else. GET /ready does the same and adds whether
    BTCPay answers, on a short timeout, for the same reason. Both answer only
    GET and HEAD -- a write method on a status path is a client bug, not
    something to answer 200 to.
    """

    def __init__(self, app: Any, token: str | None) -> None:
        self.app = app
        self.token = token or ""
        if not self.token:
            if not MCP_ALLOW_ANONYMOUS:
                raise RuntimeError(
                    f"{TOKEN_ENV} is not set, and this endpoint is published "
                    "on the LAN. Set the token, or set MCP_ALLOW_ANONYMOUS=1 "
                    "to serve without authentication."
                )
            logger.warning(
                "%s not set and MCP_ALLOW_ANONYMOUS=1: endpoint is UNAUTHENTICATED",
                TOKEN_ENV,
            )

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if scope.get("path") in (HEALTH_PATH, READY_PATH):
            if scope.get("method") not in ("GET", "HEAD"):
                await _json_response(
                    scope,
                    receive,
                    send,
                    {"error": "method not allowed"},
                    405,
                    {"allow": "GET, HEAD"},
                )
                return
            if scope.get("path") == HEALTH_PATH:
                await _json_response(scope, receive, send, {"status": "ok"}, 200)
                return
            body, status_code = await _readiness()
            await _json_response(scope, receive, send, body, status_code)
            return

        if self.token and not self._authorised(scope):
            await _json_response(scope, receive, send, {"error": "unauthorized"}, 401)
            return

        await self.app(scope, receive, send)

    def _authorised(self, scope: Any) -> bool:
        """Compare the presented credential in constant time."""
        headers = dict(scope.get("headers") or [])
        header = headers.get(b"authorization", b"").decode("latin-1").strip()
        # RFC 7235 makes the scheme case-insensitive; a client sending
        # "bearer <token>" is not a typo worth a 401, and neither is a stray
        # space inside the header.
        scheme, _, presented = header.partition(" ")
        if scheme.lower() != "bearer":
            return False
        return hmac.compare_digest(presented.strip().encode(), self.token.encode())


if __name__ == "__main__":
    import uvicorn

    # Accept any Host header: DNS-rebinding protection is disabled because the
    # bearer token gates every request.
    app = BearerAuthMiddleware(
        mcp.streamable_http_app(
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False)
        ),
        os.environ.get(TOKEN_ENV),
    )

    # 0.0.0.0 is what the container publishes; loopback is what running it for
    # a smoke test wants, hence the env overrides.
    uvicorn.run(
        app,
        host=os.environ.get("MCP_HOST", "0.0.0.0"),
        port=int(os.environ.get("MCP_PORT", "8000")),
        log_level=_log_level().lower(),
    )
