# Changelog

## 0.2.0

### Added
- Escrow with hold invoices (LND and the simulated network, on by default when the
  backend supports it): the provider runs the service while the buyer's payment is
  locked, settles only after storing the result, and cancels (refunding the buyer)
  when the service fails or times out. `GET /v1/escrow/{payment_hash}` reports the
  job status.
- Provider state (negotiations, quotes, delivered results, escrow jobs) persisted in
  SQLite; escrow jobs resume on startup.
- `BuyerAgent.reconcile_pending()`, `aibtc-nexus reconcile` and the MCP tool
  `reconcile_payments` resolve payments whose outcome was unknown.
- Backend `payment_status`, `invoice_state` and hold-invoice calls (LND).
- Per-IP rate limiting (`--rate-limit`, `--trust-proxy`), caps on open escrow jobs,
  handler timeout, 413 for oversized bodies.
- CI enforces ≥ 90% test coverage; tests have a timeout.
- `scripts/regtest.sh` and a CI job that make real payments between two LND nodes
  on regtest (escrow purchase, escrow refund, direct payment, reconciliation).
- Release workflow publishing to PyPI with trusted publishing on `v*` tags.

### Fixed
- LND: TLS certificate passed to httpx in the non-deprecated way.
- Escrow payment errors no longer blame the service when the payment failed for
  another reason (e.g. insufficient balance).
- LNbits: a routing fee above the limit was reported as a failed payment although
  the payment had gone through, so the buyer lost the service it paid for.
- LND and LNbits: an in-flight payment or a lost connection was treated as a
  failure; it is now `PaymentPending` and stays pending until reconciled.
- The request body is no longer read entirely before checking its size.
- Locks were not released when a service failed.
- After a refund, a quote kept pointing at the canceled invoice; a retry now gets a
  fresh one.

## 0.1.0

First working version: signed negotiation protocol, deterministic and Claude
strategies with hard limits, LND/LNbits/simulated backends, L402-style payments,
spending policy, ledger, platform fees, HTTP servers, CLI, MCP server.
