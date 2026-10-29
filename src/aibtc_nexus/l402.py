"""L402-style HTTP 402 payment challenges.

The challenge format follows L402 (``WWW-Authenticate: L402 macaroon="…",
invoice="…"`` and ``Authorization: L402 <token>:<preimage>``). The token is an
HMAC-signed, base64url JSON caveat set rather than a full macaroon; clients
treat it as opaque, so a macaroon implementation can replace it later without
changing the wire format.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from dataclasses import asdict, dataclass

from .errors import ProtocolError

SCHEME = "L402"
_CHALLENGE_RE = re.compile(r'(\w+)="([^"]*)"')


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _b64d(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


@dataclass(frozen=True)
class Caveats:
    payment_hash: str
    service_id: str
    price_sats: int
    expires_at: int
    quote_id: str = ""
    buyer: str = ""
    escrow: bool = False  # hold invoice: the provider settles only after delivering


def mint_token(secret: bytes, caveats: Caveats) -> str:
    payload = _b64e(json.dumps(asdict(caveats), sort_keys=True).encode())
    mac = hmac.new(secret, payload.encode(), hashlib.sha256).digest()
    return f"{payload}.{_b64e(mac)}"


def read_caveats(token: str) -> Caveats:
    """Read caveats without verifying (clients use this to cross-check invoices)."""
    try:
        payload, _ = token.split(".", 1)
        return Caveats(**json.loads(_b64d(payload)))
    except (ValueError, TypeError) as e:
        raise ProtocolError(f"malformed L402 token: {e}") from e


def verify_token(secret: bytes, token: str, now: float | None = None) -> Caveats:
    try:
        payload, mac = token.split(".", 1)
        expected = hmac.new(secret, payload.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64d(mac)):
            raise ProtocolError("invalid L402 token signature")
    except ValueError as e:
        raise ProtocolError(f"malformed L402 token: {e}") from e
    caveats = read_caveats(token)
    if (now if now is not None else time.time()) >= caveats.expires_at:
        raise ProtocolError("L402 token expired")
    return caveats


def challenge_header(token: str, payment_request: str) -> str:
    return f'{SCHEME} macaroon="{token}", invoice="{payment_request}"'


def parse_challenge(header: str) -> tuple[str, str]:
    scheme, _, rest = header.strip().partition(" ")
    if scheme.upper() not in (SCHEME, "LSAT"):
        raise ProtocolError(f"unsupported payment challenge scheme {scheme!r}")
    fields = dict(_CHALLENGE_RE.findall(rest))
    try:
        return fields["macaroon"], fields["invoice"]
    except KeyError as e:
        raise ProtocolError(f"payment challenge missing {e}") from e


def authorization_header(token: str, preimage_hex: str) -> str:
    return f"{SCHEME} {token}:{preimage_hex}"


def parse_authorization(header: str) -> tuple[str, str]:
    scheme, _, rest = header.strip().partition(" ")
    if scheme.upper() not in (SCHEME, "LSAT"):
        raise ProtocolError("unsupported authorization scheme")
    token, sep, preimage = rest.strip().rpartition(":")
    if not sep or not token or not preimage:
        raise ProtocolError("malformed L402 authorization")
    return token, preimage
