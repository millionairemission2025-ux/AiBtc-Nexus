# Security

## Deploying safely

- **Start on regtest or signet.** Move to mainnet with small `NEXUS_MAX_PAYMENT_SATS`
  and `NEXUS_DAILY_LIMIT_SATS`, then raise them as you gain confidence.
- **Least-privilege credentials.** For LND, bake a macaroon limited to what the agent
  needs (`lncli bakemacaroon invoices:read invoices:write offchain:read offchain:write
  info:read`) instead of using `admin.macaroon`. A provider that only sells needs no
  payment permission at all (LNbits: the invoice key only).
- **Keep the hot wallet small.** The agent's node or wallet should hold only its
  working balance.
- **Protect the files** created at runtime: the identity key, `l402.secret` and the
  ledger database (all created with restrictive permissions where possible, and
  ignored by git). Losing `l402.secret` invalidates outstanding paid tokens.
- **Kill switch.** Set `NEXUS_FROZEN=1` (or call `PolicyEngine.freeze()`) to stop
  all outgoing payments immediately.
- **Pin providers** you trust with `expected_provider_id` or
  `NEXUS_ALLOWED_PROVIDERS`.
- **Put the provider behind TLS** (reverse proxy) and set `NEXUS_ADMIN_TOKEN` only
  if you need `/v1/admin/stats`.

## Threats the code handles

| Threat | Mitigation |
|---|---|
| Strategy/LLM overspends | `enforce_limits` clamps every decision to the budget |
| Prompt injection through offers | only numeric fields reach the LLM prompt |
| Forged or replayed messages | Ed25519 signatures, nonces, clock-skew window, round checks |
| Provider inflates the invoice | buyer checks amount, hash, service and quote before paying |
| Double payment | ledger records the payment as pending before it is sent |
| Unknown payment outcome (timeout) | payment stays pending and is never retried automatically |
| Paid but not delivered | proof of payment kept; `retry_delivery` collects without paying again |
| Fake proof of payment | provider checks the preimage and that its node settled the invoice |
| Forged tokens | HMAC-SHA256 with a provider secret |
| Negotiation flooding | session TTL and cap on open sessions; 256 KiB body limit |

## Known limitations

- Delivery after payment is not trustless: a dishonest provider can take the payment
  and not deliver. The buyer keeps a signed quote and a proof of payment for
  disputes, and should limit exposure per provider. Hold-invoice escrow is on the
  roadmap.
- Provider sessions and quotes live in memory and are lost on restart (paid tokens
  stay valid as long as `l402.secret` is kept).
- No rate limiting per IP; put the server behind a proxy that provides it.

## Reporting a vulnerability

Please open a private security advisory on GitHub rather than a public issue.
