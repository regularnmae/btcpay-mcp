"""The stdlib BTCPay client: transport, query encoding and error mapping.

`urllib.request.urlopen` is monkeypatched, so the contract under test is what
this module sends and what it does with the answer -- no BTCPay is involved.
The credential header is the interesting one: Greenfield uses `token`, not
`Bearer`, and the MCP bearer token must never appear here.
"""

from __future__ import annotations

import email.message
import io
import json
import urllib.error
import urllib.parse
import urllib.request

import pytest

from btcpay import BtcpayClient, BtcpayError


class _Response:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self.body = body
        self.status = status

    def read(self, size: int = -1) -> bytes:
        return self.body if size is None or size < 0 else self.body[:size]

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _capture(monkeypatch: pytest.MonkeyPatch, response: object) -> list[urllib.request.Request]:
    """Answer every urlopen with `response`, recording the requests asked for."""
    requests: list[urllib.request.Request] = []

    def fake_urlopen(request: urllib.request.Request, timeout: float | None = None):
        requests.append(request)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return requests


def _http_error(code: int, message: str) -> urllib.error.HTTPError:
    body = io.BytesIO(json.dumps({"code": "error-code", "message": message}).encode())
    return urllib.error.HTTPError(
        "http://127.0.0.1:49392/api/v1/stores", code, message, email.message.Message(), body
    )


def test_the_key_is_sent_as_a_token_header(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = _capture(monkeypatch, _Response(b"[]"))
    BtcpayClient("http://127.0.0.1:49392", "api-key").stores()
    assert requests[0].get_header("Authorization") == "token api-key"


def test_an_empty_key_sends_no_authorization_header(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = _capture(monkeypatch, _Response(b'{"synchronized": true}'))
    BtcpayClient("http://127.0.0.1:49392", "").health()
    assert requests[0].get_header("Authorization") is None


def test_invoices_send_paging_and_array_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = _capture(monkeypatch, _Response(b"[]"))
    BtcpayClient("http://127.0.0.1:49392", "k").invoices(
        "store1",
        {
            "status": ["New", "Settled"],
            "textSearch": "order-1",
            "skip": 50,
            "take": 25,
            "includePaymentMethods": "true",
        },
    )
    parsed = urllib.parse.urlparse(requests[0].full_url)
    assert parsed.path == "/api/v1/stores/store1/invoices"
    query = urllib.parse.parse_qs(parsed.query)
    assert query["status"] == ["New", "Settled"]
    assert query["textSearch"] == ["order-1"]
    assert query["skip"] == ["50"]
    assert query["take"] == ["25"]
    assert query["includePaymentMethods"] == ["true"]


def test_a_zero_skip_is_sent_as_zero_not_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = _capture(monkeypatch, _Response(b"[]"))
    BtcpayClient("http://127.0.0.1:49392", "k").invoices(
        "store1", {"skip": 0, "take": 5}
    )
    query = urllib.parse.parse_qs(urllib.parse.urlparse(requests[0].full_url).query)
    assert query == {"skip": ["0"], "take": ["5"]}


def test_path_segments_are_quoted(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = _capture(monkeypatch, _Response(b"{}"))
    BtcpayClient("http://127.0.0.1:49392", "k").store("a/b c")
    assert urllib.parse.urlparse(requests[0].full_url).path == "/api/v1/stores/a%2Fb%20c"


def test_a_refused_key_names_the_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    _capture(monkeypatch, _http_error(403, "access forbidden"))
    with pytest.raises(BtcpayError) as caught:
        BtcpayClient("http://127.0.0.1:49392", "wrong").stores()
    assert caught.value.status == 403
    assert "refused the API key" in caught.value.message
    assert "read-only key" in caught.value.message


def test_a_missing_resource_is_a_404_message(monkeypatch: pytest.MonkeyPatch) -> None:
    _capture(monkeypatch, _http_error(404, "This store doesn't exist"))
    with pytest.raises(BtcpayError) as caught:
        BtcpayClient("http://127.0.0.1:49392", "k").store("nope")
    assert caught.value.status == 404
    assert "no such resource" in caught.value.message


def test_an_unreachable_btcpay_names_the_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    _capture(monkeypatch, urllib.error.URLError("connection refused"))
    with pytest.raises(BtcpayError) as caught:
        BtcpayClient("http://btcpay:49392", "k").health()
    assert "unreachable at http://btcpay:49392" in caught.value.message


def test_a_body_that_is_not_json_is_a_protocol_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _capture(monkeypatch, _Response(b"<html>oops</html>"))
    with pytest.raises(BtcpayError) as caught:
        BtcpayClient("http://127.0.0.1:49392", "k").stores()
    assert "not JSON" in caught.value.message


def test_a_body_that_is_not_a_list_is_a_protocol_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _capture(monkeypatch, _Response(b'{"stores": []}'))
    with pytest.raises(BtcpayError) as caught:
        BtcpayClient("http://127.0.0.1:49392", "k").stores()
    assert "not a list" in caught.value.message


def test_a_body_that_is_not_an_object_is_a_protocol_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _capture(monkeypatch, _Response(b"[]"))
    with pytest.raises(BtcpayError) as caught:
        BtcpayClient("http://127.0.0.1:49392", "k").store("store1")
    assert "not an object" in caught.value.message


def test_payment_requests_can_include_archived(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = _capture(monkeypatch, _Response(b"[]"))
    BtcpayClient("http://127.0.0.1:49392", "k").payment_requests(
        "store1", include_archived=True
    )
    query = urllib.parse.parse_qs(urllib.parse.urlparse(requests[0].full_url).query)
    assert query == {"includeArchived": ["true"]}


def test_wallet_endpoints_name_the_payment_method(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = _capture(monkeypatch, _Response(b'{"balance": "0"}'))
    BtcpayClient("http://127.0.0.1:49392", "k").wallet("store1", "BTC-CHAIN")
    assert urllib.parse.urlparse(requests[0].full_url).path == (
        "/api/v1/stores/store1/payment-methods/BTC-CHAIN/wallet"
    )
