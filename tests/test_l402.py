import time

import pytest

from aibtc_nexus import l402
from aibtc_nexus.errors import ProtocolError

SECRET = b"s" * 32


def caveats(**kw):
    base = dict(
        payment_hash="ab" * 32, service_id="svc", price_sats=10, expires_at=int(time.time()) + 60
    )
    base.update(kw)
    return l402.Caveats(**base)


def test_token_roundtrip_and_tamper():
    token = l402.mint_token(SECRET, caveats())
    assert l402.verify_token(SECRET, token).price_sats == 10
    payload, mac = token.split(".")
    forged = l402.mint_token(b"other", caveats(price_sats=1)).split(".")[0] + "." + mac
    with pytest.raises(ProtocolError, match="signature"):
        l402.verify_token(SECRET, forged)
    with pytest.raises(ProtocolError):
        l402.verify_token(SECRET, "garbage")


def test_token_expiry():
    token = l402.mint_token(SECRET, caveats(expires_at=int(time.time()) - 1))
    with pytest.raises(ProtocolError, match="expired"):
        l402.verify_token(SECRET, token)


def test_headers_roundtrip():
    token = l402.mint_token(SECRET, caveats())
    header = l402.challenge_header(token, "lnbc1xyz")
    assert header.startswith("L402 ")
    assert l402.parse_challenge(header) == (token, "lnbc1xyz")
    assert l402.parse_challenge(header.replace("L402", "LSAT")) == (token, "lnbc1xyz")
    auth = l402.authorization_header(token, "cd" * 32)
    assert l402.parse_authorization(auth) == (token, "cd" * 32)
    for bad in ("Bearer x", "L402 nocolon", "L402 :x"):
        with pytest.raises(ProtocolError):
            l402.parse_authorization(bad)
    with pytest.raises(ProtocolError):
        l402.parse_challenge('L402 macaroon="x"')
