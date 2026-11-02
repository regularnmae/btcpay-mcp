"""Minimal BTCPay Greenfield client: the store, invoice, wallet and payment
read surface this server exposes.

Stdlib only, for the same reason as the sibling servers' clients: this process
reads one HTTP API on one podman network, so its whole transport is an HTTP
GET -- no third-party client.

Two credentials are involved and they are not the same one:

  this module's `key`   a BTCPay API key, restricted by the stack bootstrap to
                        four read permissions (`btcpay.store.canview*`). It is
                        sent as `Authorization: token <key>`.
  the MCP bearer token  a separate credential this server itself checks. It
                        never reaches BTCPay.

Shapes pinned (BTCPay 2.4.5, Greenfield v1):

  GET /api/v1/health
      `{"synchronized": bool}`, unauthenticated
  GET /api/v1/api-keys/current
      `{id, label, permissions, apiKey, created}` -- `apiKey` is the key
      itself, so server.py redacts it before a tool sees it
  GET /api/v1/stores
      a JSON array
  GET /api/v1/stores/{id}/invoices
      a JSON array; `take`/`skip` page it, `includePaymentMethods` expands it
  GET /api/v1/invoices/{id}/payment-methods
      a JSON array; `additionalData.preimage` is a secret
  GET .../wallet/transactions
      a JSON array; `skip`/`limit` page it

Amounts and rates are JSON *strings* (`"0.00008995"`); this module passes them
through untouched, and so do the tools.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_TIMEOUT = 30.0

# One answer is read into memory whole, so it needs a ceiling: a wedged or
# hostile peer must not make this process allocate without bound. The tools cap
# their own lists far below this; this is the transport's last line.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class BtcpayError(Exception):
    """A failed BTCPay request.

    `message` is what a model should read -- BTCPay's own text when it sent
    one, or this module's description of a transport failure. `status` is the
    HTTP status when there was a response.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def _error_detail(raw: bytes) -> str:
    """BTCPay's own message out of its `{"code":..,"message":..}` body."""
    text = raw.decode("utf-8", "replace").strip()
    try:
        body = json.loads(text)
    except ValueError:
        return text[:200] or "no detail"
    if isinstance(body, dict):
        message = body.get("message")
        if isinstance(message, str) and message:
            return message[:300]
        code = body.get("code")
        if isinstance(code, str) and code:
            return code[:300]
    return "no detail"


class BtcpayClient:
    """The Greenfield API, addressed by base URL and keyed by an API key."""

    def __init__(self, url: str, key: str, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.url = url.rstrip("/")
        self.key = key
        self.timeout = timeout

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET one path, mapping every failure onto BtcpayError.

        List-valued parameters are encoded as repeated keys (`status=New&
        status=Settled`), which is what ASP.NET Core model binding wants;
        `urlencode(..., doseq=True)` does that.
        """
        url = f"{self.url}{path}"
        if params:
            filtered = {name: value for name, value in params.items() if value not in (None, "")}
            if filtered:
                url = f"{url}?{urllib.parse.urlencode(filtered, doseq=True)}"

        headers = {"Accept": "application/json"}
        if self.key:
            # Greenfield's scheme is `token`, not Bearer. The MCP's own bearer
            # token is a different credential and never reaches this header.
            headers["Authorization"] = f"token {self.key}"

        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body: bytes = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as err:
            detail = _error_detail(err.read(MAX_RESPONSE_BYTES))
            if err.code in (401, 403):
                raise BtcpayError(
                    f"BTCPay refused the API key ({err.code}): {detail}. This "
                    "server needs the read-only key the stack bootstrap mints "
                    "(BTCPAY_MCP_KEY); the admin key and the backend runtime "
                    "key are different credentials.",
                    err.code,
                ) from err
            if err.code == 404:
                raise BtcpayError(f"BTCPay has no such resource for {path}: {detail}", 404) from err
            raise BtcpayError(f"BTCPay answered {err.code} for {path}: {detail}", err.code) from err
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            raise BtcpayError(f"BTCPay is unreachable at {self.url}: {err}") from err

        if len(body) > MAX_RESPONSE_BYTES:
            raise BtcpayError(
                f"BTCPay answered {path} with more than {MAX_RESPONSE_BYTES} "
                "bytes; that is not a response this server should read whole"
            )
        if not body.strip():
            return None
        try:
            return json.loads(body.decode("utf-8", "replace"))
        except ValueError as err:
            raise BtcpayError(f"BTCPay answered {path} with something that is not JSON") from err

    def _list(self, path: str, params: dict[str, Any] | None = None, what: str = "") -> list[Any]:
        body = self._get(path, params)
        if body is None:
            return []
        if not isinstance(body, list):
            raise BtcpayError(
                f"BTCPay answered {what or path} with {type(body).__name__}, not a list"
            )
        return [row for row in body if isinstance(row, dict)]

    def _dict(self, path: str, what: str = "") -> dict[str, Any]:
        body = self._get(path)
        if not isinstance(body, dict):
            raise BtcpayError(
                f"BTCPay answered {what or path} with {type(body).__name__}, not an object"
            )
        return body

    # -- server / key -----------------------------------------------------

    def health(self) -> dict[str, Any]:
        """The unauthenticated liveness endpoint (`{"synchronized": bool}`)."""
        return self._dict("/api/v1/health", "/api/v1/health")

    def api_key_info(self) -> dict[str, Any]:
        """The key's own metadata. `apiKey` is in the answer and is a secret."""
        return self._dict("/api/v1/api-keys/current", "/api/v1/api-keys/current")

    # -- stores -----------------------------------------------------------

    def stores(self) -> list[dict[str, Any]]:
        return self._list("/api/v1/stores", what="/api/v1/stores")

    def store(self, store_id: str) -> dict[str, Any]:
        return self._dict(f"/api/v1/stores/{_segment(store_id)}", "the store")

    def payment_methods(self, store_id: str) -> list[dict[str, Any]]:
        return self._list(
            f"/api/v1/stores/{_segment(store_id)}/payment-methods",
            what="the store's payment methods",
        )

    def rates(self, store_id: str, currency_pairs: list[str]) -> list[dict[str, Any]]:
        return self._list(
            f"/api/v1/stores/{_segment(store_id)}/rates",
            {"currencyPair": currency_pairs},
            "the store's rates",
        )

    # -- invoices ---------------------------------------------------------

    def invoices(self, store_id: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        return self._list(
            f"/api/v1/stores/{_segment(store_id)}/invoices",
            params,
            "the invoice list",
        )

    def invoice(self, invoice_id: str) -> dict[str, Any]:
        return self._dict(f"/api/v1/invoices/{_segment(invoice_id)}", "the invoice")

    def invoice_payment_methods(self, invoice_id: str) -> list[dict[str, Any]]:
        return self._list(
            f"/api/v1/invoices/{_segment(invoice_id)}/payment-methods",
            what="the invoice's payment methods",
        )

    # -- on-chain wallet --------------------------------------------------

    def wallet(self, store_id: str, payment_method: str) -> dict[str, Any]:
        return self._dict(
            f"/api/v1/stores/{_segment(store_id)}/payment-methods/"
            f"{_segment(payment_method)}/wallet",
            "the wallet",
        )

    def wallet_transactions(
        self, store_id: str, payment_method: str, params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        return self._list(
            f"/api/v1/stores/{_segment(store_id)}/payment-methods/"
            f"{_segment(payment_method)}/wallet/transactions",
            params,
            "the wallet transactions",
        )

    def wallet_utxos(self, store_id: str, payment_method: str) -> list[dict[str, Any]]:
        return self._list(
            f"/api/v1/stores/{_segment(store_id)}/payment-methods/"
            f"{_segment(payment_method)}/wallet/utxos",
            what="the wallet UTXOs",
        )

    # -- payment requests -------------------------------------------------

    def payment_requests(
        self, store_id: str, include_archived: bool = False
    ) -> list[dict[str, Any]]:
        return self._list(
            f"/api/v1/stores/{_segment(store_id)}/payment-requests",
            {"includeArchived": "true" if include_archived else None},
            "the payment requests",
        )


def _segment(value: str) -> str:
    """One path segment, quoted.

    The server validates ids before they get here; this is the second guard,
    and the one that makes a typo'd slash a 404 rather than a request to a
    different endpoint.
    """
    return urllib.parse.quote(value, safe="")
