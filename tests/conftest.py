"""Shared fixtures.

Every test talks to the server through the SDK's in-memory client, so no test
opens a socket. BTCPay itself is replaced by `fake_client`, which records the
calls each tool makes and answers with scripted objects -- the tools, their
filters, their projections and their redactions are what is under test, not a
live settlement engine.

The in-memory session is opened inside `anyio.run` rather than by an async
fixture: pytest 9 refuses to resolve an async fixture for a sync test, and
these tests are sync on purpose (a tool call is one blocking round trip).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import anyio
import pytest
from mcp import Client

import server
from btcpay import BtcpayError


def _tool_call(name: str, arguments: dict[str, Any]) -> Any:
    """One tool call over a fresh in-memory MCP session."""

    async def _run() -> Any:
        async with Client(server.mcp, raise_exceptions=True) as c:
            return await c.call_tool(name, arguments)

    return anyio.run(_run)


class FakeBtcpay:
    """Stands in for `server.client`.

    Records `(method, args, kwargs)` for every call and answers with the
    scripted attribute of the same name. `error` makes every method raise,
    which is how a test simulates a down or refusing BTCPay.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.error: Exception | None = None
        self.health_data: dict[str, Any] = {"synchronized": True}
        self.api_key_data: dict[str, Any] = {
            "id": "key-id",
            "label": "boltmesh-mcp",
            "permissions": [
                "btcpay.store.canviewstoresettings:store1",
                "btcpay.store.canviewinvoices:store1",
                "btcpay.store.canviewpaymentrequests:store1",
                "btcpay.store.canviewwallet:store1",
            ],
            "apiKey": "the-key-itself-must-never-be-returned",
        }
        self.stores_data: list[dict[str, Any]] = [
            {
                "id": "store1",
                "name": "BoltMesh",
                "defaultCurrency": "USD",
                "defaultPaymentMethod": "BTC-LN",
                "archived": False,
                "logoUrl": "https://example.invalid/logo.png",
            }
        ]
        self.store_data: dict[str, Any] = {
            "id": "store1",
            "name": "BoltMesh",
            "defaultCurrency": "USD",
        }
        self.payment_methods_data: list[dict[str, Any]] = [
            {
                "paymentMethodId": "BTC-LN",
                "enabled": True,
                "config": {"connectionString": "type=clightning;macaroon=SECRET"},
            },
            {"paymentMethodId": "BTC-CHAIN", "enabled": True, "config": {}},
        ]
        self.rates_data: list[dict[str, Any]] = [
            {"currencyPair": "BTC_USD", "rate": "77772.5", "errors": []}
        ]
        self.invoices_data: list[dict[str, Any]] = [
            {
                "id": "inv1",
                "status": "Settled",
                "amount": "0.00008995",
                "currency": "BTC",
                "paidAmount": "0.00008995",
                "createdTime": "2026-11-02T10:00:00Z",
                "checkoutLink": "http://btcpay:49392/i/inv1",
                "paymentMethods": [
                    {
                        "paymentMethodId": "BTC-LN",
                        "payments": [{"preimage": "SECRET-PREIMAGE"}],
                    }
                ],
                "metadata": {"orderId": "order-1"},
            }
        ]
        self.invoice_data: dict[str, Any] = {
            "id": "inv1",
            "status": "Settled",
            "amount": "0.00008995",
            "paymentMethods": [],
        }
        self.invoice_payment_methods_data: list[dict[str, Any]] = [
            {
                "paymentMethodId": "BTC-LN",
                "destination": "lnbc1…",
                "additionalData": {"preimage": "SECRET-PREIMAGE"},
                "payments": [{"preimage": "SECRET-PREIMAGE", "status": "Settled"}],
            }
        ]
        self.wallet_data: dict[str, Any] = {
            "balance": "0.00000000",
            "confirmedBalance": "0.00000000",
            "unconfirmedBalance": "0.00000000",
            "label": "",
        }
        self.wallet_transactions_data: list[dict[str, Any]] = [
            {
                "transactionHash": "a" * 64,
                "amount": "-0.00001000",
                "status": "Confirmed",
                "confirmations": 6,
                "timestamp": 1762000000,
                "extra": "must not be echoed",
            }
        ]
        self.wallet_utxos_data: list[dict[str, Any]] = [
            {
                "outpoint": "b" * 64 + ":0",
                "amount": "0.00008995",
                "address": "bc1q…",
                "confirmations": 3,
            }
        ]
        self.payment_requests_data: list[dict[str, Any]] = [
            {
                "id": "pr1",
                "title": "Deposit",
                "amount": "10.00",
                "currency": "USD",
                "status": "Active",
            }
        ]

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))
        if self.error is not None:
            raise self.error

    def health(self) -> dict[str, Any]:
        self._record("health")
        return self.health_data

    def api_key_info(self) -> dict[str, Any]:
        self._record("api_key_info")
        return self.api_key_data

    def stores(self) -> list[dict[str, Any]]:
        self._record("stores")
        return self.stores_data

    def store(self, store_id: str) -> dict[str, Any]:
        self._record("store", store_id)
        return self.store_data

    def payment_methods(self, store_id: str) -> list[dict[str, Any]]:
        self._record("payment_methods", store_id)
        return self.payment_methods_data

    def rates(self, store_id: str, currency_pairs: list[str]) -> list[dict[str, Any]]:
        self._record("rates", store_id, currency_pairs)
        return self.rates_data

    def invoices(self, store_id: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        self._record("invoices", store_id, params)
        return self.invoices_data

    def invoice(self, invoice_id: str) -> dict[str, Any]:
        self._record("invoice", invoice_id)
        return self.invoice_data

    def invoice_payment_methods(self, invoice_id: str) -> list[dict[str, Any]]:
        self._record("invoice_payment_methods", invoice_id)
        return self.invoice_payment_methods_data

    def wallet(self, store_id: str, payment_method: str) -> dict[str, Any]:
        self._record("wallet", store_id, payment_method)
        return self.wallet_data

    def wallet_transactions(
        self, store_id: str, payment_method: str, params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        self._record("wallet_transactions", store_id, payment_method, params)
        return self.wallet_transactions_data

    def wallet_utxos(self, store_id: str, payment_method: str) -> list[dict[str, Any]]:
        self._record("wallet_utxos", store_id, payment_method)
        return self.wallet_utxos_data

    def payment_requests(
        self, store_id: str, include_archived: bool = False
    ) -> list[dict[str, Any]]:
        self._record("payment_requests", store_id, include_archived)
        return self.payment_requests_data


@pytest.fixture(autouse=True)
def _deterministic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the store and key so a host env cannot change the expected calls."""
    monkeypatch.setattr(server, "BTCPAY_STORE_ID", "store1")
    monkeypatch.setattr(server, "BTCPAY_API_KEY", "test-key")


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> FakeBtcpay:
    """Replace the live client with a scripted fake, restoring it after."""
    fake = FakeBtcpay()
    monkeypatch.setattr(server, "client", fake)
    return fake


@pytest.fixture
def call() -> Callable[..., str]:
    """Call a tool and hand back the text the model would read."""

    def _call(name: str, **arguments: Any) -> str:
        result = _tool_call(name, arguments)
        assert not result.is_error, f"tool {name} failed: {result.content}"
        return result.content[0].text  # type: ignore[union-attr]

    return _call


@pytest.fixture
def error_call() -> Callable[..., str]:
    """Call a tool that is expected to fail, and return its error text."""

    def _call(name: str, **arguments: Any) -> str:
        result = _tool_call(name, arguments)
        assert result.is_error, f"tool {name} succeeded, want a tool error"
        return result.content[0].text  # type: ignore[union-attr]

    return _call


__all__ = ["BtcpayError", "FakeBtcpay", "call", "error_call", "fake_client"]
