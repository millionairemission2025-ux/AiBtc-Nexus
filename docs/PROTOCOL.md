# AiBtc-Nexus protocol, version 1

## Identities

Each agent has an Ed25519 key pair. Its **agent id** is the hex-encoded 32-byte
public key. Buyers can pin a provider's agent id (`expected_provider_id`) to
detect impersonation.

## Envelope

Every negotiation message is a JSON object:

```json
{
  "version": 1,
  "type": "rfq | offer | counter | accept | reject",
  "negotiation_id": "32 hex chars, chosen by the buyer",
  "round": 0,
  "sender": "<agent id>",
  "timestamp": 1793289599,
  "nonce": "32 hex chars",
  "body": { },
  "signature": "<hex Ed25519 signature>"
}
```

The signature covers the canonical JSON (sorted keys, no whitespace, UTF-8) of all
fields except `signature`. Receivers reject a message when:

- the signature does not verify against `sender`;
- `|now - timestamp| > 120 s`;
- the `(sender, nonce)` pair was already seen;
- `round` is not the next expected one (buyer messages are even, provider replies
  odd: a reply to round *n* has round *n + 1*);
- the sender is not the party that opened the negotiation.

## Messages

| Type | Sender | Body |
|---|---|---|
| `rfq` | buyer | `{"service_id"}` (round 0) |
| `offer` | provider | `{"price_sats", "negotiable"?, "max_rounds"?, "rounds_left"?}` |
| `counter` | buyer | `{"price_sats"}` |
| `accept` | buyer | `{"price_sats"}`: must equal the provider's last offer |
| `accept` | provider | `{"price_sats", "quote": {"quote_id", "service_id", "price_sats", "buyer", "expires_at"}}` |
| `reject` | either | `{"reason"}` |

The provider's signed `accept` is a binding quote. The buyer checks that its
price is exactly the price it proposed or accepted.

## Payment (L402-style)

1. `POST /v1/services/{service_id}/invoke` with `X-Nexus-Quote: <quote_id>`
   (or without it, at list price) and body `{"params": {...}}`.
2. The provider answers `402 Payment Required` with
   `WWW-Authenticate: L402 macaroon="<token>", invoice="<bolt11>"`. Requests for
   the same quote always return the same invoice.
3. The token carries caveats (`payment_hash`, `service_id`, `price_sats`,
   `quote_id`, `buyer`, `expires_at`) and an HMAC-SHA256 by the provider. Clients
   can read the caveats; only the provider can mint tokens.
4. The buyer verifies that the invoice amount equals the agreed price, that its
   payment hash equals the token's, and that service and quote match; then pays.
5. The buyer repeats the request with `Authorization: L402 <token>:<preimage hex>`.
6. The provider verifies the HMAC, the expiry, the service, that
   `sha256(preimage) == payment_hash` and that its node shows the invoice as
   settled, then runs the service. Repeating the same authorization returns the
   same result without charging again.

### Escrow (hold invoices)

When `escrow` is set in the token's caveats (and in `/v1/services`), the invoice is
a **hold invoice**: the provider generates the preimage and keeps it secret.

1. The buyer's first request carries the service `params`.
2. The buyer pays; its funds are locked in the HTLC (state `ACCEPTED`).
3. The provider sees the locked payment, runs the service with those params and
   stores the result.
4. Only then does it settle the invoice, which reveals the preimage to the buyer.
   If the service fails or exceeds its timeout, the provider cancels the invoice
   and the HTLC fails back to the buyer: no funds are spent.
5. The buyer calls again with `Authorization: L402 <token>:<preimage>` and gets
   the stored result.

`GET /v1/escrow/{payment_hash}` returns the job status: `waiting`, `running`,
`settling`, `delivered`, `refunded`, `expired` or `canceled`.

The token is not a full macaroon yet; clients treat it as opaque, so a macaroon
implementation can replace it without changing the wire format.

## Platform fees

A provider configured with a fee schedule (basis points, optional minimum and
maximum) accrues a fee per settled sale in its ledger. A `FeeSettler` asks the
platform for an invoice (`POST /v1/platform/fee-invoice {"amount_sats",
"provider_id"}`), checks that its amount equals the accrued total, pays it and
marks the fees settled.
