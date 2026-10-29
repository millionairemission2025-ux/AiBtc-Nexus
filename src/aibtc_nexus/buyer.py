"""Buyer side: negotiates a price, verifies the invoice, pays within policy, collects."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

from . import l402
from .client import ProviderClient
from .errors import (
    BackendError,
    DeliveryFailed,
    InvoiceMismatch,
    NegotiationFailed,
    PaymentError,
    ProtocolError,
)
from .identity import AgentIdentity
from .ledger import Ledger
from .lightning.base import LightningBackend, PaymentState, preimage_matches
from .negotiation import ConcessionStrategy, NegotiationContext, Role, Strategy, decide_safely
from .policy import PolicyEngine, SpendingPolicy
from .protocol import Envelope, EnvelopeVerifier, MessageType, price_from, sign

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgreedQuote:
    provider_id: str
    service_id: str
    negotiation_id: str
    quote_id: str
    price_sats: int
    list_price_sats: int
    rounds: int
    transcript: tuple[dict, ...]


@dataclass(frozen=True)
class PurchaseResult:
    provider_id: str
    service_id: str
    price_sats: int
    list_price_sats: int
    routing_fee_sats: int
    payment_hash: str
    preimage: str  # proof of payment
    result: Any
    rounds: int

    @property
    def saved_sats(self) -> int:
        return self.list_price_sats - self.price_sats


def _header(headers: dict[str, str], name: str) -> str | None:
    for k, v in headers.items():
        if k.lower() == name.lower():
            return v
    return None


class BuyerAgent:
    def __init__(
        self,
        identity: AgentIdentity,
        backend: LightningBackend,
        policy: SpendingPolicy | None = None,
        ledger: Ledger | None = None,
        strategy: Strategy | None = None,
        delivery_retries: int = 3,
    ):
        self.identity = identity
        self.backend = backend
        self.ledger = ledger or Ledger()
        self.policy = PolicyEngine(policy or SpendingPolicy(), self.ledger)
        self.strategy = strategy or ConcessionStrategy()
        self.delivery_retries = delivery_retries
        self.verifier = EnvelopeVerifier()
        self._pay_lock = asyncio.Lock()

    # negotiation -------------------------------------------------------
    async def negotiate(
        self,
        client: ProviderClient,
        service_id: str,
        max_price_sats: int,
        target_price_sats: int | None = None,
        max_rounds: int = 5,
        expected_provider_id: str | None = None,
    ) -> AgreedQuote:
        info = await client.info()
        provider_id = str(info.get("agent_id", ""))
        if expected_provider_id and provider_id != expected_provider_id:
            raise ProtocolError("provider identity does not match the pinned agent id")
        self.policy.check_counterparty(provider_id)
        max_price = min(max_price_sats, self.policy.policy.max_payment_sats)
        if max_price <= 0:
            raise NegotiationFailed("no budget")

        negotiation_id = os.urandom(16).hex()
        transcript: list[dict] = []
        round_no = 0

        async def send(type: str, body: dict) -> Envelope:
            nonlocal round_no
            out = sign(self.identity, type, negotiation_id, round_no, body)
            transcript.append(out.to_dict())
            reply_data = await client.negotiate(out.to_dict())
            reply = self.verifier.verify(
                Envelope.from_dict(reply_data), expected_sender=provider_id
            )
            if reply.negotiation_id != negotiation_id or reply.round != round_no + 1:
                raise ProtocolError("reply is out of sequence")
            transcript.append(reply.to_dict())
            round_no += 2
            return reply

        def record(status: str, price: int | None, rounds: int) -> None:
            self.ledger.record_negotiation(
                negotiation_id, Role.BUYER.value, provider_id, service_id, status, price, rounds
            )

        reply = await send(MessageType.RFQ, {"service_id": service_id})
        if reply.type == MessageType.REJECT:
            record("rejected", None, 0)
            raise NegotiationFailed(f"provider declined: {reply.body.get('reason', '')}")
        if reply.type != MessageType.OFFER:
            raise ProtocolError(f"expected offer, got {reply.type}")
        list_price = their = price_from(reply)

        target = target_price_sats or max(1, int(min(list_price, max_price) * 0.7))
        ctx = NegotiationContext(
            role=Role.BUYER,
            service_id=service_id,
            limit_sats=max_price,
            target_sats=min(target, max_price),
            max_rounds=max_rounds if reply.body.get("negotiable", True) else 0,
        )
        proposed: int | None = None

        for _ in range(max_rounds + 2):
            ctx.their_offers.append(their)
            decision = await decide_safely(self.strategy, ctx, their)
            if decision.action == "reject":
                await send(MessageType.REJECT, {"reason": decision.rationale})
                record("rejected", None, ctx.round)
                raise NegotiationFailed(
                    f"no agreement: provider wants {their} sats, limit is {max_price}"
                )
            if decision.action == "accept":
                proposed = their
                reply = await send(MessageType.ACCEPT, {"price_sats": their})
            else:
                proposed = int(decision.price_sats)
                ctx.round += 1
                ctx.my_offers.append(proposed)
                reply = await send(MessageType.COUNTER, {"price_sats": proposed})

            if reply.type == MessageType.ACCEPT:
                price = price_from(reply)
                quote = reply.body.get("quote") or {}
                if price != proposed or int(quote.get("price_sats", -1)) != proposed:
                    raise ProtocolError("provider accepted a different price than proposed")
                if quote.get("service_id") != service_id or not quote.get("quote_id"):
                    raise ProtocolError("provider sent an invalid quote")
                record("agreed", price, ctx.round)
                return AgreedQuote(
                    provider_id,
                    service_id,
                    negotiation_id,
                    str(quote["quote_id"]),
                    price,
                    list_price,
                    ctx.round,
                    tuple(transcript),
                )
            if reply.type == MessageType.REJECT:
                record("rejected", None, ctx.round)
                raise NegotiationFailed(
                    f"provider ended negotiation: {reply.body.get('reason', '')}"
                )
            if reply.type != MessageType.OFFER:
                raise ProtocolError(f"unexpected {reply.type} from provider")
            their = price_from(reply)

        record("rejected", None, ctx.round)
        raise NegotiationFailed("negotiation did not converge")

    # payment -----------------------------------------------------------
    async def pay_and_collect(
        self,
        client: ProviderClient,
        provider_id: str,
        service_id: str,
        params: dict,
        price_sats: int,
        quote_id: str | None = None,
        list_price_sats: int | None = None,
        rounds: int = 0,
    ) -> PurchaseResult:
        resp = await client.invoke(service_id, params, quote_id=quote_id)
        if resp.status != 402:
            raise ProtocolError(f"expected a 402 payment challenge, got {resp.status}: {resp.body}")
        header = _header(resp.headers, "WWW-Authenticate")
        if not header:
            raise ProtocolError("402 response without a WWW-Authenticate challenge")
        token, payment_request = l402.parse_challenge(header)

        caveats = l402.read_caveats(token)
        invoice = await self.backend.decode_invoice(payment_request)
        if invoice.amount_sats != price_sats or caveats.price_sats != price_sats:
            raise InvoiceMismatch(f"invoice is for {invoice.amount_sats} sats, agreed {price_sats}")
        if invoice.payment_hash != caveats.payment_hash:
            raise InvoiceMismatch("invoice payment hash does not match the token")
        if caveats.service_id != service_id or (quote_id and caveats.quote_id != quote_id):
            raise InvoiceMismatch("token is for a different service or quote")
        if invoice.is_expired():
            raise InvoiceMismatch("invoice already expired")

        fee_limit = self.policy.policy.routing_fee_limit(price_sats)
        async with self._pay_lock:
            self.policy.check(price_sats, provider_id, fee_limit)
            self.ledger.begin_payment(
                "out", invoice.payment_hash, provider_id, service_id, price_sats, fee_limit
            )
        try:
            payment = await self.backend.pay_invoice(payment_request, fee_limit)
        except PaymentError as e:
            self.ledger.fail_payment("out", invoice.payment_hash)
            if caveats.escrow:
                raise PaymentError(f"escrow payment not completed, no funds spent: {e}") from e
            raise
        except BackendError:
            # Outcome unknown: leave it pending so it is never paid twice.
            # reconcile_pending() resolves it once the node knows the outcome.
            log.error("payment %s outcome unknown; left pending", invoice.payment_hash)
            raise
        if not preimage_matches(payment.preimage, invoice.payment_hash):
            raise PaymentError("backend returned an invalid preimage")
        if payment.fee_sats > fee_limit:
            log.warning("routing fee %s sats above the %s limit", payment.fee_sats, fee_limit)
        self.ledger.complete_payment(
            "out", invoice.payment_hash, payment.preimage, payment.fee_sats
        )

        context = {
            "provider_id": provider_id,
            "service_id": service_id,
            "params": params,
            "quote_id": quote_id,
            "authorization": l402.authorization_header(token, payment.preimage),
            "price_sats": price_sats,
            "list_price_sats": list_price_sats or price_sats,
            "routing_fee_sats": payment.fee_sats,
            "payment_hash": invoice.payment_hash,
            "preimage": payment.preimage,
            "rounds": rounds,
        }
        return await self._collect(client, context)

    async def retry_delivery(
        self, client: ProviderClient, failure: DeliveryFailed
    ) -> PurchaseResult:
        """Collect a service that was paid for but not delivered, without paying again."""
        return await self._collect(client, failure.context)

    async def _collect(self, client: ProviderClient, c: dict) -> PurchaseResult:
        last_error = ""
        for attempt in range(self.delivery_retries):
            try:
                delivered = await client.invoke(
                    c["service_id"],
                    c["params"],
                    quote_id=c["quote_id"],
                    authorization=c["authorization"],
                )
            except Exception as e:  # network errors: delivery is idempotent, retry
                last_error = str(e)
            else:
                if delivered.status == 200:
                    return PurchaseResult(
                        c["provider_id"],
                        c["service_id"],
                        c["price_sats"],
                        c["list_price_sats"],
                        c["routing_fee_sats"],
                        c["payment_hash"],
                        c["preimage"],
                        delivered.body.get("result"),
                        c["rounds"],
                    )
                last_error = f"{delivered.status}: {delivered.body}"
                if delivered.status < 500:
                    break
            if attempt + 1 < self.delivery_retries:
                await asyncio.sleep(min(2**attempt, 8) * 0.25)
        raise DeliveryFailed(
            f"paid (payment_hash={c['payment_hash']}, preimage={c['preimage']}) "
            f"but delivery failed: {last_error}",
            payment_hash=c["payment_hash"],
            preimage=c["preimage"],
            context=c,
        )

    async def purchase(
        self,
        client: ProviderClient,
        service_id: str,
        params: dict | None = None,
        max_price_sats: int = 1_000,
        target_price_sats: int | None = None,
        max_rounds: int = 5,
        expected_provider_id: str | None = None,
    ) -> PurchaseResult:
        """Negotiate, pay and collect in one call."""
        started = time.monotonic()
        quote = await self.negotiate(
            client, service_id, max_price_sats, target_price_sats, max_rounds, expected_provider_id
        )
        result = await self.pay_and_collect(
            client,
            quote.provider_id,
            service_id,
            params or {},
            quote.price_sats,
            quote.quote_id,
            quote.list_price_sats,
            quote.rounds,
        )
        log.info(
            "bought %s for %s sats (list %s) in %.2fs",
            service_id,
            result.price_sats,
            result.list_price_sats,
            time.monotonic() - started,
        )
        return result

    async def reconcile_pending(self) -> dict[str, str]:
        """Ask the backend about payments whose outcome was unknown and settle the
        ledger accordingly. Returns {payment_hash: new status}."""
        resolved: dict[str, str] = {}
        for record in self.ledger.pending_outgoing():
            status = await self.backend.payment_status(record.payment_hash)
            if status.state is PaymentState.SUCCEEDED and preimage_matches(
                status.preimage, record.payment_hash
            ):
                self.ledger.complete_payment(
                    "out", record.payment_hash, status.preimage, status.fee_sats
                )
                resolved[record.payment_hash] = "succeeded"
            elif status.state in (PaymentState.FAILED, PaymentState.NOT_FOUND):
                self.ledger.fail_payment("out", record.payment_hash)
                resolved[record.payment_hash] = "failed"
        return resolved

    def report(self) -> dict:
        return {
            "policy": self.policy.report(),
            "totals": self.ledger.totals(),
            "negotiations": self.ledger.negotiation_stats(Role.BUYER.value),
        }
