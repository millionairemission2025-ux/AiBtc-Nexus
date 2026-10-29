"""Provider side: sells services, negotiates prices, issues invoices, delivers.

State that must survive a restart (open negotiations, quotes, delivered results
and escrow jobs) lives in the ledger's SQLite database.

With a backend that supports hold invoices, services are sold in **escrow** by
default: the buyer's payment is locked in the HTLC, the provider runs the service,
and only then settles the invoice. If the service fails the invoice is canceled
and the buyer's funds return to it automatically.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from . import __version__, l402
from .errors import NexusError, ProtocolError, ServiceError
from .fees import FeeSchedule
from .identity import AgentIdentity
from .ledger import Ledger
from .lightning.base import InvoiceState, LightningBackend, preimage_matches
from .negotiation import ConcessionStrategy, NegotiationContext, Role, Strategy, decide_safely
from .protocol import Envelope, EnvelopeVerifier, MessageType, price_from, sign

log = logging.getLogger(__name__)

ServiceHandler = Callable[[dict], Awaitable[Any]]

SESSION, QUOTE, DELIVERY, ESCROW = "session", "quote", "delivery", "escrow"


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
    escrow: bool | None = None  # None: use escrow whenever the backend supports it

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


@dataclass
class HttpLikeResponse:
    status: int
    body: dict
    headers: dict[str, str] = field(default_factory=dict)


def _ctx_from(data: dict) -> NegotiationContext:
    data = dict(data)
    data["role"] = Role(data["role"])
    return NegotiationContext(**data)


def _ctx_to(ctx: NegotiationContext) -> dict:
    data = asdict(ctx)
    data["role"] = ctx.role.value
    return data


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
        max_open_escrows: int = 500,
        delivery_ttl: int = 7 * 86_400,
        verify_settlement: bool = True,
        escrow_poll_interval: float = 1.0,
        handler_timeout: float = 120.0,
    ):
        self.identity = identity
        self.backend = backend
        self.services = {s.service_id: s for s in services}
        for service in services:
            self.uses_escrow(service)  # fail fast on escrow=True without hold invoices
        self.ledger = ledger or Ledger()
        self.strategy = strategy or ConcessionStrategy(beta=0.8)
        self.l402_secret = l402_secret or os.urandom(32)
        self.fee_schedule = fee_schedule
        self.quote_ttl = quote_ttl
        self.invoice_expiry = invoice_expiry
        self.negotiation_ttl = negotiation_ttl
        self.max_sessions = max_sessions
        self.max_open_escrows = max_open_escrows
        self.delivery_ttl = delivery_ttl
        self.verify_settlement = verify_settlement
        self.escrow_poll_interval = escrow_poll_interval
        self.handler_timeout = handler_timeout
        self.verifier = EnvelopeVerifier()
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    # lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        """Resume escrow jobs left open by a previous run."""
        self.ledger.purge_expired_state()
        for payment_hash, job in self.ledger.list_state(ESCROW):
            if job["status"] in ("waiting", "running", "settling"):
                self._watch(payment_hash)

    async def aclose(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()

    async def wait_idle(self) -> None:
        """Wait until no escrow job is being watched (useful in tests)."""
        while pending := [t for t in self._tasks.values() if not t.done()]:
            await asyncio.gather(*pending, return_exceptions=True)

    # catalog -----------------------------------------------------------
    def uses_escrow(self, service: Service) -> bool:
        if service.escrow is None:
            return self.backend.supports_hold_invoices
        if service.escrow and not self.backend.supports_hold_invoices:
            raise NexusError(f"service {service.service_id}: backend cannot do escrow")
        return service.escrow

    def info(self) -> dict:
        return {
            "agent_id": self.identity.agent_id,
            "name": self.identity.name,
            "protocol_version": 1,
            "software": f"aibtc-nexus/{__version__}",
            "payment_schemes": ["L402"],
            "escrow": self.backend.supports_hold_invoices,
        }

    def catalog(self) -> list[dict]:
        return [
            {
                "service_id": s.service_id,
                "description": s.description,
                "list_price_sats": s.list_price_sats,
                "negotiable": s.negotiable,
                "escrow": self.uses_escrow(s),
            }
            for s in self.services.values()
        ]

    def _service(self, service_id: str) -> Service:
        try:
            return self.services[service_id]
        except KeyError:
            raise UnknownService(f"unknown service {service_id!r}") from None

    def _lock(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    # negotiation -------------------------------------------------------
    def _save_session(self, nid: str, session: dict) -> None:
        self.ledger.put_state(SESSION, nid, session, session["created_at"] + self.negotiation_ttl)

    def _reply(self, env: Envelope, type: str, body: dict) -> dict:
        return sign(self.identity, type, env.negotiation_id, env.round + 1, body).to_dict()

    def _issue_quote(self, env: Envelope, session: dict, ctx: NegotiationContext, price: int):
        now = int(time.time())
        quote = {
            "quote_id": os.urandom(16).hex(),
            "service_id": ctx.service_id,
            "price_sats": price,
            "buyer": session["buyer"],
            "expires_at": now + self.quote_ttl,
        }
        self.ledger.put_state(
            QUOTE, quote["quote_id"], {**quote, "challenge": None}, quote["expires_at"]
        )
        self.ledger.delete_state(SESSION, env.negotiation_id)
        self.ledger.record_negotiation(
            env.negotiation_id,
            Role.SELLER.value,
            session["buyer"],
            ctx.service_id,
            "agreed",
            price,
            ctx.round,
        )
        return self._reply(env, MessageType.ACCEPT, {"price_sats": price, "quote": quote})

    def _close(self, env: Envelope, session: dict, ctx: NegotiationContext, reason: str):
        self.ledger.delete_state(SESSION, env.negotiation_id)
        self.ledger.record_negotiation(
            env.negotiation_id,
            Role.SELLER.value,
            session["buyer"],
            ctx.service_id,
            "rejected",
            None,
            ctx.round,
        )
        return self._reply(env, MessageType.REJECT, {"reason": reason})

    async def handle_negotiation(self, data: Any) -> dict:
        env = self.verifier.verify(Envelope.from_dict(data))
        if len(env.negotiation_id) < 16 or len(env.negotiation_id) > 128:
            raise ProtocolError("negotiation_id must be 16-128 characters")
        async with self._lock(f"n:{env.negotiation_id}"):
            try:
                return await self._negotiate(env)
            finally:
                self._locks.pop(f"n:{env.negotiation_id}", None)

    async def _negotiate(self, env: Envelope) -> dict:
        if env.type == MessageType.RFQ:
            if env.round != 0 or self.ledger.get_state(SESSION, env.negotiation_id):
                raise ProtocolError("negotiation already exists or bad opening round")
            if self.ledger.count_state(SESSION) >= self.max_sessions:
                self.ledger.purge_expired_state()
                if self.ledger.count_state(SESSION) >= self.max_sessions:
                    raise ProtocolError("too many open negotiations, try again later")
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
            session = {
                "ctx": _ctx_to(ctx),
                "buyer": env.sender,
                "created_at": time.time(),
                "next_round": 2,
                "last_offer": service.list_price_sats,
            }
            self._save_session(env.negotiation_id, session)
            self.ledger.record_negotiation(
                env.negotiation_id, Role.SELLER.value, env.sender, service.service_id, "open"
            )
            return self._reply(
                env,
                MessageType.OFFER,
                {
                    "price_sats": service.list_price_sats,
                    "negotiable": service.negotiable,
                    "max_rounds": ctx.max_rounds,
                },
            )

        session = self.ledger.get_state(SESSION, env.negotiation_id)
        if session is None:
            raise ProtocolError("unknown or closed negotiation")
        if env.sender != session["buyer"]:
            raise ProtocolError("message is not from the negotiating buyer")
        if env.round != session["next_round"]:
            raise ProtocolError(f"expected round {session['next_round']}, got {env.round}")
        ctx = _ctx_from(session["ctx"])

        if env.type == MessageType.REJECT:
            return self._close(env, session, ctx, "buyer walked away")

        if env.type == MessageType.ACCEPT:
            if price_from(env) != session["last_offer"]:
                raise ProtocolError("buyer accepted a price that was not offered")
            return self._issue_quote(env, session, ctx, session["last_offer"])

        if env.type == MessageType.COUNTER:
            their = price_from(env)
            ctx.their_offers.append(their)
            decision = await decide_safely(self.strategy, ctx, their)
            if decision.action == "accept":
                return self._issue_quote(env, session, ctx, their)
            if decision.action == "reject":
                return self._close(env, session, ctx, decision.rationale or "no agreement")
            price = int(decision.price_sats or session["last_offer"])
            ctx.round += 1
            ctx.my_offers.append(price)
            session.update(ctx=_ctx_to(ctx), last_offer=price, next_round=env.round + 2)
            self._save_session(env.negotiation_id, session)
            return self._reply(
                env, MessageType.OFFER, {"price_sats": price, "rounds_left": ctx.rounds_left}
            )

        raise ProtocolError(f"unexpected {env.type} from buyer")

    # payment -----------------------------------------------------------
    async def _challenge(
        self, service: Service, price: int, quote: dict | None, params: dict
    ) -> tuple[str, str]:
        memo = f"aibtc-nexus:{service.service_id}"
        escrow = self.uses_escrow(service)
        if escrow:
            if self.ledger.count_state(ESCROW) >= self.max_open_escrows:
                self.ledger.purge_expired_state()
                if self.ledger.count_state(ESCROW) >= self.max_open_escrows:
                    raise ProtocolError("too many open escrow jobs, try again later")
            preimage = os.urandom(32).hex()
            payment_hash = hashlib.sha256(bytes.fromhex(preimage)).hexdigest()
            invoice = await self.backend.create_hold_invoice(
                price, payment_hash, memo, self.invoice_expiry
            )
        else:
            invoice = await self.backend.create_invoice(price, memo, self.invoice_expiry)
        caveats = l402.Caveats(
            payment_hash=invoice.payment_hash,
            service_id=service.service_id,
            price_sats=price,
            expires_at=invoice.expires_at + self.quote_ttl,
            quote_id=quote["quote_id"] if quote else "",
            buyer=quote["buyer"] if quote else "",
            escrow=escrow,
        )
        if escrow:
            job = {
                "service_id": service.service_id,
                "params": params,
                "preimage": preimage,
                "price_sats": price,
                "quote_id": caveats.quote_id,
                "buyer": caveats.buyer,
                "status": "waiting",
                "invoice_expires_at": invoice.expires_at,
                "error": None,
            }
            self.ledger.put_state(ESCROW, invoice.payment_hash, job, caveats.expires_at + 86_400)
            self._watch(invoice.payment_hash)
        return l402.mint_token(self.l402_secret, caveats), invoice.payment_request

    async def request_payment(
        self, service_id: str, quote_id: str | None = None, params: dict | None = None
    ) -> HttpLikeResponse:
        service = self._service(service_id)
        params = params or {}
        if quote_id:
            try:
                async with self._lock(f"q:{quote_id}"):
                    quote = self.ledger.get_state(QUOTE, quote_id)
                    if quote is None or quote["service_id"] != service_id:
                        raise ProtocolError("unknown, expired or mismatched quote")
                    # One invoice per quote, so a retried request never creates a second debt.
                    if quote["challenge"] is None:
                        quote["challenge"] = list(
                            await self._challenge(service, quote["price_sats"], quote, params)
                        )
                        self.ledger.put_state(QUOTE, quote_id, quote, quote["expires_at"])
            finally:
                self._locks.pop(f"q:{quote_id}", None)
            token, pr = quote["challenge"]
            price = quote["price_sats"]
        else:
            token, pr = await self._challenge(service, service.list_price_sats, None, params)
            price = service.list_price_sats
        return HttpLikeResponse(
            402,
            {
                "error": "payment_required",
                "price_sats": price,
                "payment_request": pr,
                "token": token,
                "escrow": l402.read_caveats(token).escrow,
            },
            {"WWW-Authenticate": l402.challenge_header(token, pr)},
        )

    # escrow ------------------------------------------------------------
    def _watch(self, payment_hash: str) -> None:
        if payment_hash in self._tasks:
            return
        task = asyncio.get_running_loop().create_task(self._run_escrow(payment_hash))
        self._tasks[payment_hash] = task
        task.add_done_callback(lambda _: self._tasks.pop(payment_hash, None))

    def _update_job(self, payment_hash: str, job: dict, **changes: Any) -> None:
        job.update(changes)
        self.ledger.put_state(ESCROW, payment_hash, job, job["invoice_expires_at"] + 86_400 * 2)
        if job["status"] in ("refunded", "expired", "canceled") and job["quote_id"]:
            # Let a retry on the same quote get a fresh invoice instead of a dead one.
            quote = self.ledger.get_state(QUOTE, job["quote_id"])
            if quote and quote["challenge"]:
                if l402.read_caveats(quote["challenge"][0]).payment_hash == payment_hash:
                    quote["challenge"] = None
                    self.ledger.put_state(QUOTE, job["quote_id"], quote, quote["expires_at"])

    async def _retry(self, what: str, call: Callable[[], Awaitable[Any]]) -> Any:
        """Retry a backend call until it succeeds: giving up would leave the buyer's
        funds locked in the HTLC."""
        delay = self.escrow_poll_interval
        while True:
            try:
                return await call()
            except NexusError as e:
                log.warning("escrow %s failed (%s); retrying in %.1fs", what, e, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60 * self.escrow_poll_interval)

    async def _wait_for_payment(self, payment_hash: str, job: dict) -> InvoiceState | None:
        """Poll until the HTLC is locked; back off while nothing happens."""
        delay = self.escrow_poll_interval / 5
        while time.time() < job["invoice_expires_at"]:
            try:
                state = await self.backend.invoice_state(payment_hash)
            except NexusError as e:
                log.warning("escrow poll failed: %s", e)
            else:
                if state is not InvoiceState.OPEN:
                    return state
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, self.escrow_poll_interval * 3)
        return None

    async def _run_escrow(self, payment_hash: str) -> None:
        job = self.ledger.get_state(ESCROW, payment_hash)
        if job is None:
            return
        try:
            if job["status"] == "waiting":
                state = await self._wait_for_payment(payment_hash, job)
                if state is None:
                    with contextlib.suppress(NexusError):
                        await self.backend.cancel_hold_invoice(payment_hash)
                    self._update_job(payment_hash, job, status="expired")
                    return
                if state is not InvoiceState.ACCEPTED:
                    self._update_job(payment_hash, job, status=state.value)
                    return
                self._update_job(payment_hash, job, status="running")

            if job["status"] == "running":
                service = self._service(job["service_id"])
                try:
                    result = await asyncio.wait_for(
                        service.handler(job["params"]), timeout=self.handler_timeout
                    )
                except Exception as e:  # refund the buyer
                    log.warning("escrow service %s failed: %s", job["service_id"], e)
                    await self._retry(
                        "cancel", lambda: self.backend.cancel_hold_invoice(payment_hash)
                    )
                    self._update_job(payment_hash, job, status="refunded", error=str(e)[:500])
                    return
                self._store_delivery(payment_hash, job["service_id"], job["price_sats"], result)
                self._update_job(payment_hash, job, status="settling")

            if job["status"] == "settling":
                await self._retry(
                    "settle", lambda: self.backend.settle_hold_invoice(job["preimage"])
                )
                self._record_sale(
                    payment_hash,
                    job["buyer"],
                    job["service_id"],
                    job["price_sats"],
                    job["preimage"],
                    job["quote_id"],
                )
                self._update_job(payment_hash, job, status="delivered")
        except asyncio.CancelledError:
            raise
        except Exception:
            # Left in its current status; start() resumes it after a restart.
            log.exception("escrow job %s interrupted", payment_hash)

    # delivery ----------------------------------------------------------
    def _store_delivery(self, payment_hash: str, service_id: str, price: int, result: Any):
        self.ledger.put_state(
            DELIVERY,
            payment_hash,
            {"service_id": service_id, "price_sats": price, "result": result},
            time.time() + self.delivery_ttl,
        )

    def _record_sale(self, payment_hash, buyer, service_id, price, preimage, quote_id) -> None:
        new = self.ledger.record_incoming(payment_hash, buyer, service_id, price, preimage)
        if new and self.fee_schedule:
            self.ledger.accrue_fee(payment_hash, self.fee_schedule.fee_for(price))
        if quote_id:
            self.ledger.delete_state(QUOTE, quote_id)

    async def deliver(self, service_id: str, authorization: str, params: dict) -> HttpLikeResponse:
        service = self._service(service_id)
        token, preimage = l402.parse_authorization(authorization)
        caveats = l402.verify_token(self.l402_secret, token)
        if caveats.service_id != service_id:
            raise ProtocolError("token was issued for a different service")
        if not preimage_matches(preimage, caveats.payment_hash):
            raise ProtocolError("preimage does not match the payment hash")

        key = f"d:{caveats.payment_hash}"
        try:
            async with self._lock(key):
                stored = self.ledger.get_state(DELIVERY, caveats.payment_hash)
                if stored is not None:
                    return HttpLikeResponse(200, self._delivery_body(caveats, stored["result"]))
                if caveats.escrow:
                    # The preimage is only released by settlement, which happens after
                    # the result is stored, so a missing result means it expired.
                    raise ProtocolError("no stored result for this escrow payment")
                if self.verify_settlement and not await self.backend.is_invoice_settled(
                    caveats.payment_hash
                ):
                    raise ProtocolError("invoice is not settled")
                try:
                    result = await asyncio.wait_for(
                        service.handler(params), timeout=self.handler_timeout
                    )
                except Exception as e:
                    log.exception("service %s failed", service_id)
                    raise ServiceError(f"service failed: {e}") from e
                self._store_delivery(caveats.payment_hash, service_id, caveats.price_sats, result)
                self._record_sale(
                    caveats.payment_hash,
                    caveats.buyer,
                    service_id,
                    caveats.price_sats,
                    preimage,
                    caveats.quote_id,
                )
                return HttpLikeResponse(200, self._delivery_body(caveats, result))
        finally:
            self._locks.pop(key, None)

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
            return await self.request_payment(service_id, quote_id, params)
        except UnknownService as e:
            return HttpLikeResponse(404, {"error": "not_found", "detail": str(e)})
        except ProtocolError as e:
            return HttpLikeResponse(
                401 if authorization else 400, {"error": "bad_request", "detail": str(e)}
            )
        except ServiceError as e:
            return HttpLikeResponse(502, {"error": "service_failed", "detail": str(e)})
        except NexusError as e:  # backend trouble creating an invoice
            log.error("invoke failed: %s", e)
            return HttpLikeResponse(503, {"error": "unavailable", "detail": "try again later"})

    def escrow_status(self, payment_hash: str) -> dict | None:
        job = self.ledger.get_state(ESCROW, payment_hash)
        if job is None:
            return None
        return {"status": job["status"], "error": job["error"]}

    def stats(self) -> dict:
        jobs = [job["status"] for _, job in self.ledger.list_state(ESCROW)]
        return {
            "sales": self.ledger.totals(),
            "negotiations": self.ledger.negotiation_stats(Role.SELLER.value),
            "platform_fees": self.ledger.fee_totals(),
            "escrow": {s: jobs.count(s) for s in sorted(set(jobs))},
        }
