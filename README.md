# AiBtc-Nexus

Autonomous AI-to-AI price negotiation and Bitcoin payments over the Lightning
Network: the financial layer for the machine economy.

An agent that needs a service (data, compute, inference, any API) asks a provider
agent for a price, haggles over signed messages within a budget it can never exceed,
pays a Lightning invoice and gets the result together with a cryptographic proof of
payment. Providers put any existing API behind a Lightning paywall in minutes.

```
Buyer agent                                        Provider agent
    | -- rfq (signed) ------------------------------> |
    | <------------------------------ offer 1000 sat  |
    | -- counter 560 -------------------------------> |
    | <------------------------------- offer 953 sat  |
    |                     ...                         |
    | -- counter 740 -------------------------------> |
    | <------------------- accept 740 + signed quote  |
    | -- POST /invoke  (X-Nexus-Quote, params) -----> |
    | <---- 402 + L402 challenge (token, hold invoice) |
    |  check invoice == 740 sat, check policy, pay    |
    |  ...funds locked in the HTLC (escrow)...        |
    |                      provider runs the service, |
    |                 then settles (or refunds on fail)|
    | -- POST /invoke  (Authorization: L402 tok:pre)> |
    | <------------------------------ 200 + result    |
```

## Features

- **Negotiation protocol**: multi-round, Ed25519-signed messages with replay and
  clock-skew protection ([docs/PROTOCOL.md](docs/PROTOCOL.md)).
- **Hard limits**: whatever a strategy proposes (deterministic or AI), a guard
  ensures a buyer never offers or accepts more than its maximum and a seller never
  goes below its floor. A randomized test hammers this with a "chaos" strategy.
- **AI negotiation with Claude** (`ClaudeStrategy`), with automatic fallback to
  the deterministic strategy on errors or refusals. Only numbers from the
  counterparty reach the model, so it cannot be prompt-injected through offers.
- **Real Lightning backends**: LND (REST, router v2, hold invoices, payment
  tracking) and LNbits, plus an in-memory network with real hash/preimage and
  hold-invoice semantics for tests and demos.
- **L402-style HTTP 402 payments**: compatible header format, invoices bound to
  the negotiated quote, single invoice per quote, idempotent delivery.
- **Spending policy**: per-payment, daily and per-provider limits, routing-fee
  caps, allow/block lists and a kill switch. Payments are recorded before they are
  sent, so the same invoice is never paid twice.
- **Escrow with hold invoices** (default on LND and the simulated network): the
  buyer's payment stays locked until the provider has produced the result; if the
  service fails or times out the invoice is canceled and the buyer is refunded
  automatically. The provider is paid only for work it delivered.
- **Buyer protection**: the buyer verifies amount, payment hash, service and
  expiry before paying, and verifies the preimage after. Without escrow, if
  delivery fails after payment, `retry_delivery` collects the result without
  paying again.
- **Never pays twice, never loses track**: a payment whose outcome is unknown
  (lost connection, payment in flight) stays pending and blocks a second attempt;
  `reconcile_pending` (CLI `aibtc-nexus reconcile`) asks the node how it ended.
- **Restart-safe provider**: open negotiations, quotes, delivered results and
  escrow jobs live in SQLite; escrow jobs resume automatically after a restart.
- **Hardened HTTP server**: per-IP rate limiting, request size limit, caps on
  open negotiations and escrow jobs.
- **Revenue for the operator**: configurable platform fee (basis points), batched
  and settled to the platform over Lightning.
- **MCP server**: any MCP-capable agent (Claude Code, Claude Desktop, …) can list,
  negotiate and buy services, within the operator's spending policy.
- SQLite ledger, CLI, Dockerfile, CI on Python 3.10–3.13 with ≥90% test coverage.

## Quick start

```bash
pip install -e ".[all]"     # or: pip install -e .  for the core only
aibtc-nexus demo            # two agents on a simulated network
aibtc-nexus demo --llm      # the buyer negotiates with Claude (needs ANTHROPIC_API_KEY)
```

Example output:

```
Agent_A budget: 800 sats | list price: 1000 sats | provider floor (private): 650 sats

  [0] Agent_A  rfq
  [1] Provider offer   1000
  [2] Agent_A  counter 560
  ...
  [8] Agent_A  counter 740
  [9] Provider accept  740

Paid 740 sats (+1 routing), saved 260 sats vs list price.
Proof of payment (preimage): 6abde860…
Escrow: delivered (the provider was paid only after delivering)
```

### Use it from Python

```python
from aibtc_nexus.buyer import BuyerAgent
from aibtc_nexus.client import HttpProviderClient
from aibtc_nexus.identity import AgentIdentity
from aibtc_nexus.lightning import LndRestBackend
from aibtc_nexus.policy import SpendingPolicy

buyer = BuyerAgent(
    AgentIdentity.load_or_create("buyer.key"),
    LndRestBackend("https://127.0.0.1:8080", macaroon_hex, "tls.cert"),
    policy=SpendingPolicy(max_payment_sats=2_000, daily_limit_sats=20_000),
)
async with HttpProviderClient("https://provider.example") as provider:
    result = await buyer.purchase(provider, "market-data", {"pair": "BTC/USD"}, max_price_sats=800)
print(result.result, result.price_sats, result.preimage)
```

See [examples/two_agents.py](examples/two_agents.py) for a runnable script.

### Sell a service

1. Describe your services (see [examples/services.json](examples/services.json)).
   A handler is `"echo"` or `{"type": "http", "url": …}`, which forwards the
   buyer's parameters to your existing API. `"escrow"` is `true`, `false` or
   omitted (escrow whenever the backend supports hold invoices).
2. Configure a Lightning backend (see [.env.example](.env.example)).
3. Run:

```bash
export NEXUS_LN_BACKEND=lnd LND_REST_URL=https://127.0.0.1:8080 \
       LND_MACAROON_HEX=... LND_TLS_CERT_PATH=~/.lnd/tls.cert
aibtc-nexus serve-provider --config services.json --host 0.0.0.0 --port 8402
# behind a reverse proxy that sets X-Forwarded-For: add --trust-proxy
```

HTTP API:

| Method | Path | Purpose |
|---|---|---|
| GET | `/v1/info` | provider agent id and capabilities |
| GET | `/v1/services` | catalog with list prices (floors stay private) |
| POST | `/v1/negotiate` | signed negotiation messages |
| POST | `/v1/services/{id}/invoke` | 402 challenge, then delivery with `Authorization: L402 …` |
| GET | `/v1/escrow/{payment_hash}` | escrow job status (waiting, running, delivered, refunded, expired) |
| GET | `/v1/admin/stats` | sales and fees (bearer `NEXUS_ADMIN_TOKEN`) |

### Buy from the command line

```bash
aibtc-nexus buy https://provider.example market-data --max-price 800 --params '{"pair":"BTC/USD"}'
aibtc-nexus reconcile   # resolve payments interrupted by a lost connection
```

### Give an AI agent a wallet (MCP)

```json
{
  "mcpServers": {
    "aibtc-nexus": {
      "command": "aibtc-nexus",
      "args": ["mcp"],
      "env": {
        "NEXUS_LN_BACKEND": "lnbits",
        "LNBITS_URL": "https://your-lnbits.example",
        "LNBITS_INVOICE_KEY": "...",
        "LNBITS_ADMIN_KEY": "...",
        "NEXUS_MAX_PAYMENT_SATS": "2000",
        "NEXUS_DAILY_LIMIT_SATS": "20000"
      }
    }
  }
}
```

Tools: `list_services`, `buy_service`, `wallet_status`, `reconcile_payments`. The
limits are enforced by the server; the model cannot raise them.

## Development

```bash
pip install -e ".[dev]"
ruff check src tests examples && ruff format --check src tests examples
pytest
```

`tests/test_regtest_lnd.py` runs a real payment between two LND nodes (for
example on [Polar](https://lightningpolar.com) regtest) when the `NEXUS_IT_*`
variables documented in the file are set; otherwise it is skipped.

## Status

Alpha. The simulated network and the mocked LND/LNbits APIs are exercised in CI;
the LND and LNbits backends have not yet been run against real nodes. Start on regtest or
signet, then mainnet with small limits. Read [SECURITY.md](SECURITY.md) first.

Business model and roadmap: [docs/BUSINESS.md](docs/BUSINESS.md).

## License

MIT
