"""The tools: the calls they build, the fields they return, the redactions.

`fake_client` records every call, so these tests pin the query a tool sends --
the Greenfield parameter names are the contract, and a filter that silently
changes shape (a missing `take`, an empty status where "any" was meant) is the
bug worth catching without a live BTCPay.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

import server
from btcpay import BtcpayError

PREIMAGE = "SECRET-PREIMAGE"


def _payload(text: str) -> dict[str, Any]:
    return json.loads(text)


def test_list_stores_projects_the_useful_fields(call, fake_client) -> None:
    payload = _payload(call("list_stores"))
    assert payload["returned"] == 1
    assert payload["stores"] == [
        {
            "id": "store1",
            "name": "BoltMesh",
            "defaultCurrency": "USD",
            "defaultPaymentMethod": "BTC-LN",
            "archived": False,
        }
    ]
    assert "logoUrl" not in payload["stores"][0]


def test_get_store_uses_the_configured_store(call, fake_client) -> None:
    payload = _payload(call("get_store"))
    assert payload["id"] == "store1"
    assert fake_client.calls[-1] == ("store", ("store1",), {})


def test_payment_methods_redact_connection_strings(call, fake_client) -> None:
    payload = _payload(call("list_payment_methods"))
    rows = {row["paymentMethodId"]: row for row in payload["payment_methods"]}
    assert rows["BTC-LN"]["config"]["connectionString"] == "[redacted]"
    assert "SECRET" not in json.dumps(payload)


def test_rates_need_a_currency_pair(error_call, fake_client) -> None:
    text = error_call("get_store_rates", currency_pairs="BTC")
    assert "BTC_USD" in text
    assert fake_client.calls == []


def test_rates_are_returned_as_strings(call, fake_client) -> None:
    payload = _payload(call("get_store_rates", currency_pairs="BTC_USD,BTC_EUR"))
    assert payload["rates"][0]["rate"] == "77772.5"
    assert fake_client.calls[-1] == ("rates", ("store1", ["BTC_USD", "BTC_EUR"]), {})


def test_invoices_build_the_greenfield_filters(call, fake_client) -> None:
    payload = _payload(
        call(
            "list_invoices",
            status="settled,expired",
            text_search="order-1",
            start_date="2026-11-01T00:00:00Z",
            end_date="2026-11-02T00:00:00+00:00",
            skip=5,
            take=10,
            include_payment_methods=True,
        )
    )
    _, args, _ = fake_client.calls[-1]
    store, params = args
    assert store == "store1"
    assert params["status"] == ["Settled", "Expired"]
    assert params["textSearch"] == "order-1"
    assert params["startDate"] == 1793491200
    assert params["endDate"] == 1793577600
    assert params["skip"] == 5
    assert params["take"] == 10
    assert params["includePaymentMethods"] == "true"
    assert payload["filters"]["status"] == ["Settled", "Expired"]


def test_invoices_accept_a_unix_timestamp(call, fake_client) -> None:
    _payload(call("list_invoices", start_date="1793491200"))
    _, args, _ = fake_client.calls[-1]
    assert args[1]["startDate"] == 1793491200


def test_an_invalid_date_is_a_tool_error(error_call, fake_client) -> None:
    text = error_call("list_invoices", start_date="last tuesday")
    assert "Unix seconds" in text
    assert fake_client.calls == []


def test_invoices_redact_payment_preimages(call, fake_client) -> None:
    text = call("list_invoices", include_payment_methods=True)
    assert PREIMAGE not in text
    payload = _payload(text)
    payments = payload["invoices"][0]["paymentMethods"][0]["payments"]
    assert payments[0]["preimage"] == "[redacted]"


def test_invoices_leave_payment_methods_out_by_default(call, fake_client) -> None:
    payload = _payload(call("list_invoices"))
    assert "paymentMethods" not in payload["invoices"][0]


def test_internal_links_are_rewritten_to_the_public_origin(
    call, fake_client, monkeypatch
) -> None:
    monkeypatch.setattr(server, "BTCPAY_PUBLIC_URL", "https://btcpay.boltmesh.net")
    payload = _payload(call("list_invoices"))
    assert payload["invoices"][0]["checkoutLink"] == (
        "https://btcpay.boltmesh.net/i/inv1"
    )


def test_invoices_hint_when_the_page_is_full(call, fake_client) -> None:
    fake_client.invoices_data = fake_client.invoices_data * 2
    payload = _payload(call("list_invoices", take=2))
    assert payload["truncated"] is True


def test_invoice_payment_methods_redact_the_preimage(call, fake_client) -> None:
    text = call("get_invoice_payment_methods", invoice_id="inv1")
    assert PREIMAGE not in text
    payload = _payload(text)
    row = payload["payment_methods"][0]
    assert row["additionalData"]["preimage"] == "[redacted]"
    assert row["payments"][0]["preimage"] == "[redacted]"
    assert row["destination"] == "lnbc1…"


def test_an_invalid_invoice_id_is_a_tool_error(error_call, fake_client) -> None:
    text = error_call("get_invoice", invoice_id="../etc/passwd")
    assert "invoice_id must be" in text
    assert fake_client.calls == []


def test_an_invalid_store_id_is_a_tool_error(error_call, fake_client) -> None:
    text = error_call("get_store", store_id="../etc")
    assert "store_id must be" in text
    assert fake_client.calls == []


def test_an_invalid_status_is_a_tool_error(error_call, fake_client) -> None:
    text = error_call("list_invoices", status="paid")
    assert "New, Processing, Expired, Invalid or Settled" in text
    assert fake_client.calls == []


def test_health_never_returns_the_api_key(call, fake_client) -> None:
    text = call("get_btcpay_health")
    assert "the-key-itself-must-never-be-returned" not in text
    payload = _payload(text)
    assert payload["status"] == "ok"
    assert payload["api_key"]["label"] == "boltmesh-mcp"
    assert payload["api_key"]["permissions"]
    assert "apiKey" not in payload["api_key"]
    assert payload["store"] == {"id": "store1", "name": "BoltMesh"}


def test_health_reports_an_unconfigured_key(call, fake_client, monkeypatch) -> None:
    monkeypatch.setattr(server, "BTCPAY_API_KEY", "")
    monkeypatch.setattr(server, "BTCPAY_STORE_ID", "")
    payload = _payload(call("get_btcpay_health"))
    assert payload["status"] == "unconfigured"
    assert payload["api_key"] == {"configured": False}
    assert [name for name, _, _ in fake_client.calls] == ["health"]


def test_health_reports_a_refused_key(call, fake_client, monkeypatch) -> None:
    def boom() -> dict[str, Any]:
        raise BtcpayError("BTCPay refused the API key (403): forbidden", 403)

    monkeypatch.setattr(fake_client, "api_key_info", boom)
    payload = _payload(call("get_btcpay_health"))
    assert payload["status"] == "degraded"
    assert "refused the API key" in payload["api_key"]["error"]


def test_health_reports_btcpay_down(call, fake_client) -> None:
    fake_client.error = BtcpayError("BTCPay is unreachable at http://btcpay:49392: refused")
    text = call("get_btcpay_health")
    payload = _payload(text)
    assert payload["status"] == "down"
    assert "unreachable" in payload["error"]


def test_wallet_tools_default_to_the_onchain_rail(call, fake_client) -> None:
    payload = _payload(call("get_wallet_balance"))
    assert payload["payment_method"] == "BTC-CHAIN"
    assert fake_client.calls[-1] == ("wallet", ("store1", "BTC-CHAIN"), {})


def test_wallet_transactions_page_and_project(call, fake_client) -> None:
    payload = _payload(call("list_wallet_transactions", skip=10, limit=5))
    _, args, _ = fake_client.calls[-1]
    assert args == ("store1", "BTC-CHAIN", {"skip": 10, "limit": 5})
    assert payload["transactions"][0]["transactionHash"] == "a" * 64
    assert "extra" not in payload["transactions"][0]


def test_wallet_utxos_project_the_useful_fields(call, fake_client) -> None:
    payload = _payload(call("list_wallet_utxos"))
    assert payload["utxos"][0]["outpoint"] == "b" * 64 + ":0"
    assert payload["utxos"][0]["confirmations"] == 3


def test_payment_requests_can_include_archived(call, fake_client) -> None:
    payload = _payload(call("list_payment_requests", include_archived=True))
    assert payload["include_archived"] is True
    assert fake_client.calls[-1] == ("payment_requests", ("store1", True), {})


def test_a_btcpay_failure_becomes_a_tool_error(error_call, fake_client) -> None:
    fake_client.error = BtcpayError("BTCPay is unreachable at http://btcpay:49392: refused")
    assert "unreachable" in error_call("list_stores")


def test_amounts_stay_strings(call, fake_client, monkeypatch) -> None:
    # The invoice fields are projected, so the value must be the API's string,
    # not a float a JSON round trip would produce.
    payload = _payload(call("list_invoices"))
    assert payload["invoices"][0]["amount"] == "0.00008995"
    assert isinstance(payload["invoices"][0]["amount"], str)


@pytest.mark.parametrize("bad", ["abc/def", "a b", "x" * 50 + "/" + "x" * 49])
def test_store_id_validation_is_shared(error_call, fake_client, bad: str) -> None:
    text = error_call("get_store", store_id=bad)
    assert "store_id must be" in text
