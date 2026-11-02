"""What the server advertises in `tools/list`.

The schema is the contract a client validates against, and the tool set is
also this server's security boundary: every tool is a read, and the set is
exactly these twelve. A write name appearing here -- `create_invoice`,
`refund`, `payout`, `create_address` -- is the change this file exists to
catch, so it fails the build rather than shipping.
"""

from __future__ import annotations

import anyio
import pytest
from mcp import Client

import server

TOOLS = {
    "get_btcpay_health",
    "list_stores",
    "get_store",
    "list_payment_methods",
    "get_store_rates",
    "list_invoices",
    "get_invoice",
    "get_invoice_payment_methods",
    "get_wallet_balance",
    "list_wallet_transactions",
    "list_wallet_utxos",
    "list_payment_requests",
}


@pytest.fixture
def advertised() -> dict:
    """The tools/list result, fetched once per test through a real client."""

    async def _list():
        async with Client(server.mcp) as c:
            return await c.list_tools()

    listing = anyio.run(_list)
    return {tool.name: tool for tool in listing.tools}


def test_the_tool_set_is_read_only(advertised: dict) -> None:
    assert set(advertised) == TOOLS, (
        "the tool set changed; every tool here must be a read, and the key "
        "this server holds cannot create, refund, pay out or broadcast"
    )


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_text_tools_publish_no_output_schema(advertised: dict, name: str) -> None:
    assert advertised[name].output_schema is None, (
        f"{name} publishes an output schema, which means its str return is "
        "serialised twice; pass structured_output=False to @mcp.tool()"
    )


def test_every_tool_declares_itself_read_only(advertised: dict) -> None:
    for name, tool in advertised.items():
        assert tool.annotations is not None, f"{name} publishes no annotations"
        assert tool.annotations.read_only_hint is True, f"{name} is not marked read-only"
        # This is the host's own BTCPay instance, not an open-world network.
        assert tool.annotations.open_world_hint is False, f"{name} claims open-world"


def test_list_limits_are_bounded(advertised: dict) -> None:
    limit = advertised["list_stores"].input_schema["properties"]["limit"]
    assert (limit["minimum"], limit["maximum"], limit["default"]) == (1, 100, 50)

    take = advertised["list_invoices"].input_schema["properties"]["take"]
    assert (take["minimum"], take["maximum"], take["default"]) == (1, 100, 50)
    assert advertised["list_invoices"].input_schema["properties"]["skip"]["minimum"] == 0

    tx_limit = advertised["list_wallet_transactions"].input_schema["properties"]["limit"]
    assert (tx_limit["minimum"], tx_limit["maximum"], tx_limit["default"]) == (1, 200, 50)


def test_rates_require_the_pair(advertised: dict) -> None:
    schema = advertised["get_store_rates"].input_schema
    assert schema.get("required") == ["currency_pairs"]
    assert schema["properties"]["currency_pairs"]["minLength"] == 1


def test_invoice_ids_are_required(advertised: dict) -> None:
    for name in ("get_invoice", "get_invoice_payment_methods"):
        schema = advertised[name].input_schema
        assert schema.get("required") == ["invoice_id"]
        assert schema["properties"]["invoice_id"]["maxLength"] == 100


def test_store_scoped_tools_make_the_store_optional(advertised: dict) -> None:
    # BTCPAY_STORE_ID is the default; the tool argument is an override.
    for name in ("get_store", "list_payment_methods", "get_store_rates", "list_invoices"):
        required = advertised[name].input_schema.get("required", [])
        assert "store_id" not in required, name


def test_wallet_tools_default_to_btc_chain(advertised: dict) -> None:
    for name in ("get_wallet_balance", "list_wallet_transactions", "list_wallet_utxos"):
        method = advertised[name].input_schema["properties"]["payment_method"]
        assert method["default"] == "BTC-CHAIN", name
