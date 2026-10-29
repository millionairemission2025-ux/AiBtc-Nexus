"""Provider side: sells services, negotiates prices, issues invoices, delivers."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from . import __version__, l402
from .errors import NexusError, ProtocolError, ServiceError
from .fees import FeeSchedule
from .identity import AgentIdentity
from .ledger import Ledger
from .lightning.base import LightningBackend, preimage_matches
from .negotiation import ConcessionStrategy, NegotiationContext, Role, Strategy, decide_safely
from .protocol import Envelope, EnvelopeVerifier, MessageType, price_from, sign

log = logging.getLogger(__name__)

ServiceHandler = Callable[[dict], Awaitable[Any]]


class UnknownService(NexusError):
    pass


@dataclass
class Service:
    service_id: str
    description: str
    list_price_sats: int
    handler: ServiceHandler
    floor_price_sats: int | None = None  # None: fixed price, not negotiable
    max_rounds: int = 5

    def __post_init__(self) -> None:
        if self.list_price_sats <= 0:
            raise ValueError("list price must be positive")
        if (
            self.floor_price_sats is not None
            and not 0 < self.floor_price_sats <= self.list_price_sats
        ):
            raise ValueError("floor price must be positive and not above the list price")

    @property
    def negotiable(self) -> bool:
        return self.floor_price_sats is not None and self.floor_price_sats < self.list_price_sats

    def public(self) -> dict:
        return {
            "service_id": self.service_id,
            "description": self.description,
            "list_price_sats": self.list_price_sats,
            "negotiable": self.negotiable,
        }


@dataclass
class Quote:
    quote_id: str
    service_id: str
    price_sats: int
    buyer: str
    expires_at: int
    challenge: tuple[str, str] | None = None  # (token, payment_request) once invoiced

    def public(self) -> dict:
        return {
            "quote_id": self.quote_id,
            "service_id": self.service_id,
            "price_sats": self.price_sats,
            "buyer": self.buyer,
            "expires_at": self.expires_at,
        }


@dataclass
class _Session:
    ctx: NegotiationContext
    buyer: str
    created_at: float
    next_round: int
    last_offer: int
    status: str = "open"


@dataclass
class HttpLikeResponse:
    status: int
    body: dict
    headers: dict[str, str] = field(default_factory=dict)


class ProviderAgent:
    def __init__(
        self,
        identity: AgentIdentity,
        backend: LightningBackend,
        services: list[Service],
        ledger: Ledger | None = None,
        strategy: Strategy | None = None,
        l402_secret: bytes | None = None,
        fee_schedule: FeeSchedule | None = None,
        quote_ttl: int = 600,
        invoice_expiry: int = 600,
        negotiation_ttl: int = 600,
        max_sessions: int = 10_000,
        verify_settlement: bool = True,
    ):
        self.identity = identity
        self.backend = backend
        self.services = {s.service_id: s for s in services}
        self.ledger = ledger or Ledger()
        self.strategy = strategy or ConcessionStrategy(beta=0.8)
        self.l402_secret = l402_secret or os.urandom(32)
        self.fee_schedule = fee_schedule
        self.quote_ttl = quote_ttl
        self.invoice_expiry = invoice_expiry
        self.negotiation_ttl = negotiation_ttl
        self.max_sessions = max_sessions
        self.verify_settlement = verify_settlement
        self.verifier = EnvelopeVerifier()
        self._sessions: OrderedDict[str, _Session] = OrderedDict()
        self._quotes: dict[str, Quote] = {}
        self._delivered: OrderedDict[str, Any] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}

    # catalog -----------------------------------------------------------
    def info(self) -> dict:
        return {
            "agent_id": self.identity.agent_id,
            "name": self.identity.name,
            "protocol_version": 1,
            "software": f"aibtc-nexus/{__version__}",
            "payment_schemes": ["L402"],
        }

    def catalog(self) -> list[dict]:
        return [s.public() for s in self.services.values()]

    def _service(self, service_id: str) -> Service:
        try:
            return self.services[service_id]
        except KeyError:
            raise UnknownService(f"unknown service {service_id!r}") from None

    # negotiation -------------------------------------------------------
    def _expire(self) -> None:
        now = time.time()
        while self._sessions:
            nid, session = next(iter(self._sessions.items()))
            if (
                now - session.created_at < self.negotiation_ttl
                and len(self._sessions) <= self.max_sessions
            ):
                break
            self._sessions.pop(nid)
        for qid in [q for q, quote in self._quotes.items() if quote.expires_at <= now]:
            self._quotes.pop(qid)

    def _reply(self, env: Envelope, session: _Session, type: str, body: dict) -> dict:
        out = sign(self.identity, type, env.negotiation_id, env.round + 1, body)
        session.next_round = env.round + 2
        return out.to_dict()

    def _issue_quote(self, env: Envelope, session: _Session, price: int) -> dict:
        quote = Quote(
            quote_id=os.urandom(16).hex(),
            service_id=session.ctx.service_id,
            price_sats=price,
            buyer=session.buyer,
            expires_at=int(time.time()) + self.quote_ttl,
        )
        self._quotes[quote.quote_id] = quote
        session.status = "agreed"
        self.ledger.record_negotiation(
            env.negotiation_id,
            Role.SELLER.value,
            session.buyer,
            quote.service_id,
            "agreed",
            price,
            session.ctx.round,
        )
        self._sessions.pop(env.negotiation_id, None)
        return self._reply(
            env, session, MessageType.ACCEPT, {"price_sats": price, "quote": quote.public()}
        )

    def _close(self, env: Envelope, session: _Session, reason: str) -> dict:
        session.status = "rejected"
        self.ledger.record_negotiation(
            env.negotiation_id,
            Role.SELLER.value,
            session.buyer,
            session.ctx.service_id,
            "rejected",
            None,
            session.ctx.round,
        )
        self._sessions.pop(env.negotiation_id, None)
        return self._reply(env, session, MessageType.REJECT, {"reason": reason})

    async def handle_negotiation(self, data: Any) -> dict:
        env = self.verifier.verify(Envelope.from_dict(data))
        self._expire()

        if env.type == MessageType.RFQ:
            if env.negotiation_id in self._sessions or env.round != 0:
                raise ProtocolError("negotiation already exists or bad opening round")
            if len(env.negotiation_id) < 16:
                raise ProtocolError("negotiation_id too short")
            service = self._service(str(env.body.get("service_id", "")))
            floor = service.floor_price_sats if service.negotiable else service.list_price_sats
            ctx = NegotiationContext(
                role=Role.SELLER,
                service_id=service.service_id,
                limit_sats=floor,
                target_sats=service.list_price_sats,
                max_rounds=service.max_rounds if service.negotiable else 0,
                my_offers=[service.list_price_sats],
            )
            session = _Session(ctx, env.sender, time.time(), 0, service.list_price_sats)
            self._sessions[env.negotiation_id] = session
            self.ledger.record_negotiation(
                env.negotiation_id, Role.SELLER.value, env.sender, service.service_id, "open"
            )
            return self._reply(
                env,
                session,
                MessageType.OFFER,
                {
                    "price_sats": service.list_price_sats,
                    "negotiable": service.negotiable,
                    "max_rounds": ctx.max_rounds,
                },
            )

        session = self._sessions.get(env.negotiation_id)
        if session is None or session.status != "open":
            raise ProtocolError("unknown or closed negotiation")
        if env.sender != session.buyer:
            raise ProtocolError("message is not from the negotiating buyer")
        if env.round != session.next_round:
            raise ProtocolError(f"expected round {session.next_round}, got {env.round}")

        if env.type == MessageType.REJECT:
            return self._close(env, session, "buyer walked away")

        if env.type == MessageType.ACCEPT:
            if price_from(env) != session.last_offer:
                raise ProtocolError("buyer accepted a price that was not offered")
            return self._issue_quote(env, session, session.last_offer)

        if env.type == MessageType.COUNTER:
            their = price_from(env)
            session.ctx.their_offers.append(their)
            decision = await decide_safely(self.strategy, session.ctx, their)
            if decision.action == "accept":
                return self._issue_quote(env, session, their)
            if decision.action == "reject":
                return self._close(env, session, decision.rationale or "no agreement")
            price = int(decision.price_sats or session.last_offer)
            session.ctx.round += 1
            session.ctx.my_offers.append(price)
            session.last_offer = price
            return self._reply(
                env,
                session,
                MessageType.OFFER,
                {"price_sats": price, "rounds_left": session.ctx.rounds_left},
            )

        raise ProtocolError(f"unexpected {env.type} from buyer")

    # payment + delivery ------------------------------------------------
    async def _challenge(
        self, service: Service, price: int, quote: Quote | None
    ) -> tuple[str, str]:
        invoice = await self.backend.create_invoice(
            price, memo=f"aibtc-nexus:{service.service_id}", expiry=self.invoice_expiry
        )
        caveats = l402.Caveats(
            payment_hash=invoice.payment_hash,
            service_id=service.service_id,
            price_sats=price,
            expires_at=invoice.expires_at + self.quote_ttl,
            quote_id=quote.quote_id if quote else "",
            buyer=quote.buyer if quote else "",
        )
        return l402.mint_token(self.l402_secret, caveats), invoice.payment_request

    async def request_payment(
        self, service_id: str, quote_id: str | None = None
    ) -> HttpLikeResponse:
        service = self._service(service_id)
        if quote_id:
            quote = self._quotes.get(quote_id)
            if quote is None or quote.service_id != service_id or quote.expires_at <= time.time():
                raise ProtocolError("unknown, expired or mismatched quote")
            # One invoice per quote, so a retried request never creates a second debt.
            if quote.challenge is None:
                quote.challenge = await self._challenge(service, quote.price_sats, quote)
            token, pr = quote.challenge
            price = quote.price_sats
        else:
            token, pr = await self._challenge(service, service.list_price_sats, None)
            price = service.list_price_sats
        return HttpLikeResponse(
            402,
            {
                "error": "payment_required",
                "price_sats": price,
                "payment_request": pr,
                "token": token,
            },
            {"WWW-Authenticate": l402.challenge_header(token, pr)},
        )

    def _lock(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    async def deliver(self, service_id: str, authorization: str, params: dict) -> HttpLikeResponse:
        service = self._service(service_id)
        token, preimage = l402.parse_authorization(authorization)
        caveats = l402.verify_token(self.l402_secret, token)
        if caveats.service_id != service_id:
            raise ProtocolError("token was issued for a different service")
        if not preimage_matches(preimage, caveats.payment_hash):
            raise ProtocolError("preimage does not match the payment hash")

        async with self._lock(caveats.payment_hash):
            if caveats.payment_hash in self._delivered:
                result = self._delivered[caveats.payment_hash]
                return HttpLikeResponse(200, self._delivery_body(caveats, result))
            if self.verify_settlement and not await self.backend.is_invoice_settled(
                caveats.payment_hash
            ):
                raise ProtocolError("invoice is not settled")
            try:
                result = await service.handler(params)
            except Exception as e:
                log.exception("service %s failed", service_id)
                raise ServiceError(f"service failed: {e}") from e
            new = self.ledger.record_incoming(
                caveats.payment_hash, caveats.buyer, service_id, caveats.price_sats, preimage
            )
            if new and self.fee_schedule:
                self.ledger.accrue_fee(
                    caveats.payment_hash, self.fee_schedule.fee_for(caveats.price_sats)
                )
            if caveats.quote_id:
                self._quotes.pop(caveats.quote_id, None)
            self._delivered[caveats.payment_hash] = result
            while len(self._delivered) > 10_000:
                self._delivered.popitem(last=False)
        self._locks.pop(caveats.payment_hash, None)
        return HttpLikeResponse(200, self._delivery_body(caveats, result))

    @staticmethod
    def _delivery_body(caveats: l402.Caveats, result: Any) -> dict:
        return {
            "service_id": caveats.service_id,
            "price_sats": caveats.price_sats,
            "payment_hash": caveats.payment_hash,
            "result": result,
        }

    async def invoke(
        self,
        service_id: str,
        params: dict,
        quote_id: str | None = None,
        authorization: str | None = None,
    ) -> HttpLikeResponse:
        """Single entry point for transports: 402 challenge, or delivery once paid."""
        try:
            if authorization:
                return await self.deliver(service_id, authorization, params)
            return await self.request_payment(service_id, quote_id)
        except UnknownService as e:
            return HttpLikeResponse(404, {"error": "not_found", "detail": str(e)})
        except ProtocolError as e:
            return HttpLikeResponse(
                401 if authorization else 400, {"error": "bad_request", "detail": str(e)}
            )
        except ServiceError as e:
            return HttpLikeResponse(502, {"error": "service_failed", "detail": str(e)})

    def stats(self) -> dict:
        return {
            "sales": self.ledger.totals(),
            "negotiations": self.ledger.negotiation_stats(Role.SELLER.value),
            "platform_fees": self.ledger.fee_totals(),
        }
