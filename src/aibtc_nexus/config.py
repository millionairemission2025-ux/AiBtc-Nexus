"""Build components from environment variables (twelve-factor style)."""

from __future__ import annotations

import os

from .errors import NexusError
from .lightning import FakeLightningNetwork, LightningBackend, LnbitsBackend, LndRestBackend
from .policy import SpendingPolicy

_fake_network: FakeLightningNetwork | None = None


def _env(name: str, default: str | None = None, required: bool = False) -> str | None:
    value = os.environ.get(name, default)
    if required and not value:
        raise NexusError(f"environment variable {name} is required")
    return value


def backend_from_env(
    node_name: str = "local", fake_balance_sats: int = 100_000
) -> LightningBackend:
    """``NEXUS_LN_BACKEND`` = ``fake`` (default) | ``lnd`` | ``lnbits``."""
    kind = (_env("NEXUS_LN_BACKEND", "fake") or "fake").lower()
    if kind == "lnd":
        return LndRestBackend(
            url=_env("LND_REST_URL", required=True),
            macaroon_hex=_env("LND_MACAROON_HEX", required=True),
            tls_cert_path=_env("LND_TLS_CERT_PATH"),
        )
    if kind == "lnbits":
        return LnbitsBackend(
            url=_env("LNBITS_URL", required=True),
            invoice_key=_env("LNBITS_INVOICE_KEY", required=True),
            admin_key=_env("LNBITS_ADMIN_KEY"),
        )
    if kind == "fake":
        global _fake_network
        _fake_network = _fake_network or FakeLightningNetwork()
        existing = _fake_network.nodes.get(node_name)
        return existing or _fake_network.create_node(node_name, fake_balance_sats)
    raise NexusError(f"unknown NEXUS_LN_BACKEND {kind!r}")


def policy_from_env() -> SpendingPolicy:
    def as_int(name: str, default: int) -> int:
        return int(_env(name, str(default)))

    allow = _env("NEXUS_ALLOWED_PROVIDERS")
    per_cp = _env("NEXUS_PER_PROVIDER_DAILY_SATS")
    return SpendingPolicy(
        max_payment_sats=as_int("NEXUS_MAX_PAYMENT_SATS", 10_000),
        daily_limit_sats=as_int("NEXUS_DAILY_LIMIT_SATS", 100_000),
        per_counterparty_daily_sats=int(per_cp) if per_cp else None,
        max_routing_fee_sats=as_int("NEXUS_MAX_ROUTING_FEE_SATS", 50),
        allowed_counterparties={a.strip() for a in allow.split(",") if a.strip()}
        if allow
        else None,
        frozen=_env("NEXUS_FROZEN", "0") in ("1", "true", "yes"),
    )
