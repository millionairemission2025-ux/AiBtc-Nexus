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
| Unknown payment outcome (timeout) | payment stays pending, blocks a second attempt, resolved by `reconcile_pending` |
| Paid but not delivered | escrow: funds locked until the result exists, refunded if the service fails; without escrow, proof of payment kept and `retry_delivery` collects without paying again |
| Node down while settling or refunding | escrow retries settle/cancel until the node is back; jobs resume after a restart |
| Fake proof of payment | provider checks the preimage and that its node settled the invoice |
| Forged tokens | HMAC-SHA256 with a provider secret |
| Flooding | per-IP rate limit on POST, caps on open negotiations and escrow jobs, 256 KiB body limit checked while reading |

## Known limitations

- Escrow proves that the provider produced *a* result before being paid, not that
  the result is correct. Keep per-provider limits and pin providers you trust.
- Escrow needs hold invoices (LND, simulated network). With LNbits the provider is
  paid first; the buyer keeps the signed quote and proof of payment for disputes.
- Escrow jobs are watched by polling the node (with backoff), so the number of
  unpaid hold invoices is capped (`max_open_escrows`).
- The L402 token is an HMAC caveat set rather than a full macaroon.
- Replay protection keeps nonces in memory; after a restart a message could be
  replayed within the 120 s skew window, but the round checks reject it.

## Reporting a vulnerability

Please open a private security advisory on GitHub rather than a public issue.
