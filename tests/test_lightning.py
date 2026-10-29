import json

import httpx
import pytest

from aibtc_nexus.errors import BackendError, PaymentError, PaymentPending
from aibtc_nexus.lightning import (
    FakeLightningNetwork,
    InvoiceState,
    LnbitsBackend,
    LndRestBackend,
    PaymentState,
    preimage_matches,
)

PREIMAGE = "11" * 32
HASH = "02d449a31fbb267c8f352e9968a79e3e5fc95c1bbeaa502fd6454ebde5a4bedc"


def test_preimage_matches():
    assert preimage_matches(PREIMAGE, HASH)
    assert preimage_matches(PREIMAGE, HASH.upper())
    assert not preimage_matches("22" * 32, HASH)
    assert not preimage_matches("not hex", HASH)


async def test_fake_network_payment_flow():
    net = FakeLightningNetwork(routing_fee_sats=2)
    alice, bob = net.create_node("alice", 1000), net.create_node("bob")
    inv = await bob.create_invoice(300, "coffee")
    decoded = await alice.decode_invoice(inv.payment_request)
    assert decoded == inv
    result = await alice.pay_invoice(inv.payment_request, max_fee_sats=5)
    assert preimage_matches(result.preimage, inv.payment_hash)
    assert (await alice.get_balance_sats(), await bob.get_balance_sats()) == (698, 300)
    assert await bob.is_invoice_settled(inv.payment_hash)
    with pytest.raises(PaymentError, match="already settled"):
        await alice.pay_invoice(inv.payment_request, 5)


async def test_fake_network_failures():
    net = FakeLightningNetwork(routing_fee_sats=10)
    alice, bob = net.create_node("alice", 100), net.create_node("bob")
    with pytest.raises(PaymentError, match="fee"):
        await alice.pay_invoice((await bob.create_invoice(50)).payment_request, 5)
    with pytest.raises(PaymentError, match="insufficient"):
        await alice.pay_invoice((await bob.create_invoice(95)).payment_request, 50)
    expired = await bob.create_invoice(10, expiry=0)
    with pytest.raises(PaymentError, match="expired"):
        await alice.pay_invoice(expired.payment_request, 50)
    with pytest.raises(BackendError):
        await alice.decode_invoice("lnbc1garbage")
    with pytest.raises(BackendError):
        await bob.create_invoice(0)


def lnd_transport(pay_status="SUCCEEDED", preimage=PREIMAGE):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Grpc-Metadata-macaroon"] == "abcd"
        path = request.url.path
        if path == "/v1/invoices":
            assert json.loads(request.content) == {"value": "500", "memo": "m", "expiry": "600"}
            return httpx.Response(200, json={"r_hash": "AA==", "payment_request": "lnbc5u1test"})
        if path == "/v1/payreq/lnbc5u1test":
            return httpx.Response(
                200,
                json={
                    "payment_hash": HASH,
                    "num_satoshis": "500",
                    "timestamp": "1700000000",
                    "expiry": "600",
                    "description": "m",
                    "destination": "02ab",
                },
            )
        if path == "/v2/router/send":
            body = json.loads(request.content)
            assert body["fee_limit_sat"] == "7" and body["no_inflight_updates"] is True
            line = json.dumps(
                {
                    "result": {
                        "status": pay_status,
                        "payment_hash": HASH,
                        "payment_preimage": preimage,
                        "value_sat": "500",
                        "fee_sat": "3",
                        "failure_reason": "FAILURE_REASON_NO_ROUTE",
                    }
                }
            )
            return httpx.Response(200, content=line + "\n")
        if path == f"/v1/invoice/{HASH}":
            return httpx.Response(200, json={"state": "SETTLED"})
        if path == "/v1/balance/channels":
            return httpx.Response(200, json={"local_balance": {"sat": "12345", "msat": "12345000"}})
        return httpx.Response(404, json={"message": "not found"})

    return httpx.MockTransport(handler)


async def test_lnd_backend():
    lnd = LndRestBackend("https://lnd:8080", "abcd", transport=lnd_transport())
    inv = await lnd.create_invoice(500, "m")
    assert (inv.payment_hash, inv.amount_sats, inv.payee) == (HASH, 500, "02ab")
    paid = await lnd.pay_invoice("lnbc5u1test", 7)
    assert (paid.preimage, paid.fee_sats, paid.amount_sats) == (PREIMAGE, 3, 500)
    assert await lnd.is_invoice_settled(HASH)
    assert await lnd.get_balance_sats() == 12345
    with pytest.raises(BackendError):
        await lnd.decode_invoice("unknown")
    await lnd.aclose()


async def test_lnd_payment_failure_and_bad_preimage():
    lnd = LndRestBackend("https://lnd:8080", "abcd", transport=lnd_transport(pay_status="FAILED"))
    with pytest.raises(PaymentError, match="NO_ROUTE"):
        await lnd.pay_invoice("lnbc5u1test", 7)
    lnd = LndRestBackend("https://lnd:8080", "abcd", transport=lnd_transport(preimage="22" * 32))
    with pytest.raises(PaymentError, match="preimage"):
        await lnd.pay_invoice("lnbc5u1test", 7)


def lnbits_transport(fee_msat=-2000):
    def handler(request: httpx.Request) -> httpx.Response:
        key = request.headers["X-Api-Key"]
        path = request.url.path
        if path == "/api/v1/payments" and request.method == "POST":
            body = json.loads(request.content)
            if body["out"]:
                assert key == "admin"
                return httpx.Response(201, json={"payment_hash": HASH})
            assert key == "invoice"
            return httpx.Response(
                201, json={"payment_hash": HASH, "payment_request": "lnbc5u1test"}
            )
        if path == "/api/v1/payments/decode":
            return httpx.Response(
                200,
                json={
                    "payment_hash": HASH,
                    "amount_msat": 500_000,
                    "date": 1700000000,
                    "expiry": 600,
                    "description": "m",
                    "payee": "02ab",
                },
            )
        if path == f"/api/v1/payments/{HASH}":
            return httpx.Response(
                200, json={"paid": True, "preimage": PREIMAGE, "details": {"fee": fee_msat}}
            )
        if path == "/api/v1/wallet":
            return httpx.Response(200, json={"balance": 9_000_000})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


async def test_lnbits_backend():
    lnbits = LnbitsBackend("https://lnbits", "invoice", "admin", transport=lnbits_transport())
    inv = await lnbits.create_invoice(500, "m")
    assert (inv.payment_hash, inv.amount_sats) == (HASH, 500)
    paid = await lnbits.pay_invoice("lnbc5u1test", 5)
    assert (paid.preimage, paid.fee_sats) == (PREIMAGE, 2)
    assert await lnbits.is_invoice_settled(HASH)
    assert await lnbits.get_balance_sats() == 9000
    await lnbits.aclose()


async def test_lnbits_requires_admin_key_and_fee_overrun_is_not_a_failure():
    with pytest.raises(PaymentError, match="admin key"):
        await LnbitsBackend("https://lnbits", "invoice", transport=lnbits_transport()).pay_invoice(
            "x", 5
        )
    # The payment went through: report it with its real fee instead of losing it.
    lnbits = LnbitsBackend("https://lnbits", "invoice", "admin", transport=lnbits_transport(-9000))
    paid = await lnbits.pay_invoice("lnbc5u1test", 5)
    assert paid.fee_sats == 9


async def test_lnd_hold_invoices_and_tracking():
    import base64

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        calls.append((path, body))
        if path == "/v2/invoices/hodl":
            assert base64.b64decode(body["hash"]).hex() == HASH and body["value"] == "500"
            return httpx.Response(200, json={"payment_request": "lnbc5u1test"})
        if path == "/v1/payreq/lnbc5u1test":
            return httpx.Response(
                200,
                json={
                    "payment_hash": HASH,
                    "num_satoshis": "500",
                    "timestamp": "1700000000",
                    "expiry": "600",
                },
            )
        if path in ("/v2/invoices/settle", "/v2/invoices/cancel"):
            return httpx.Response(200, json={})
        if path == f"/v1/invoice/{HASH}":
            return httpx.Response(200, json={"state": "ACCEPTED"})
        if path.startswith("/v2/router/track/"):
            encoded = path.rsplit("/", 1)[1]
            if base64.urlsafe_b64decode(encoded).hex() != HASH:
                return httpx.Response(404, json={"message": "payment isn't initiated"})
            line = json.dumps(
                {
                    "result": {
                        "status": "SUCCEEDED",
                        "payment_hash": HASH,
                        "payment_preimage": PREIMAGE,
                        "fee_sat": "2",
                    }
                }
            )
            return httpx.Response(200, content=line + "\n")
        return httpx.Response(404)

    lnd = LndRestBackend("https://lnd", "abcd", transport=httpx.MockTransport(handler))
    assert lnd.supports_hold_invoices
    inv = await lnd.create_hold_invoice(500, HASH, "m")
    assert inv.payment_hash == HASH
    assert await lnd.invoice_state(HASH) is InvoiceState.ACCEPTED
    await lnd.settle_hold_invoice(PREIMAGE)
    await lnd.cancel_hold_invoice(HASH)
    assert base64.b64decode(calls[-2][1]["preimage"]).hex() == PREIMAGE
    assert base64.b64decode(calls[-1][1]["payment_hash"]).hex() == HASH
    status = await lnd.payment_status(HASH)
    assert (status.state, status.preimage, status.fee_sats) == (PaymentState.SUCCEEDED, PREIMAGE, 2)
    assert (await lnd.payment_status("00" * 32)).state is PaymentState.NOT_FOUND


@pytest.mark.parametrize("mode", ["in_flight", "disconnect", "empty"])
async def test_lnd_unknown_outcome_is_pending(mode):
    def handler(request: httpx.Request) -> httpx.Response:
        if mode == "disconnect":
            raise httpx.ReadError("connection reset")
        if mode == "empty":
            return httpx.Response(200, content=b"")
        return httpx.Response(200, content=json.dumps({"result": {"status": "IN_FLIGHT"}}) + "\n")

    lnd = LndRestBackend("https://lnd", "abcd", transport=httpx.MockTransport(handler))
    with pytest.raises(PaymentPending):
        await lnd.pay_invoice("lnbc1x", 5)


async def test_lnbits_payment_status_and_pending():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/payments/decode":
            return httpx.Response(200, json={"payment_hash": HASH, "amount_msat": 1000})
        if path == "/api/v1/payments" and request.method == "POST":
            return httpx.Response(502, text="gateway timeout")
        if path == f"/api/v1/payments/{HASH}":
            return httpx.Response(200, json={"paid": False, "status": "pending"})
        return httpx.Response(404, json={"detail": "not found"})

    lnbits = LnbitsBackend("https://l", "i", "a", transport=httpx.MockTransport(handler))
    with pytest.raises(PaymentPending):
        await lnbits.pay_invoice("lnbc1x", 5)
    assert (await lnbits.payment_status(HASH)).state is PaymentState.PENDING
    assert (await lnbits.payment_status("00" * 32)).state is PaymentState.NOT_FOUND
