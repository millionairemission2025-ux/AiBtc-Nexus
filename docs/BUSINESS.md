# Modello di business e roadmap

## Il problema

Gli agenti AI hanno bisogno di comprare risorse (dati, API, calcolo, inferenza) senza
un umano che inserisca una carta di credito. Le carte hanno costi fissi alti, richiedono
account e KYC per ogni fornitore e non funzionano per micropagamenti da pochi centesimi.
Lightning permette pagamenti istantanei da 1 satoshi in su, senza account.

Gli standard esistenti (L402 di Lightning Labs, x402 di Coinbase) risolvono il
**pagamento** a prezzo fisso. Nessuno copre bene la **negoziazione automatica del
prezzo** con limiti di spesa garantiti: è il punto di differenza di AiBtc-Nexus.

## Fonti di ricavo (in ordine di attivazione)

| # | Fonte | Come funziona nel codice | Note |
|---|---|---|---|
| 1 | **Commissione di piattaforma** | `FeeSchedule` (basis points) + `FeeSettler`: il fornitore matura una fee per ogni vendita e la paga in batch alla piattaforma (`serve-platform`) | Inizio: 1% (100 bps). Si può mettere un minimo/massimo |
| 2 | **Gateway gestito "Lightning paywall per API"** | `serve-provider` + handler `http`: chi ha un'API la mette a pagamento senza modificarla | Abbonamento mensile + commissione; target: fornitori di dati, API AI |
| 3 | **Wallet per agenti (MCP)** | `aibtc-nexus mcp`: wallet con limiti di spesa per Claude e altri agenti | Freemium: gratis fino a X sats/mese, poi abbonamento per dashboard, report, multi-agente |
| 4 | **Marketplace / directory** dei fornitori | da costruire: elenco di `/v1/info` + `/v1/services` verificati | Commissione sulle vendite + posizionamento a pagamento |
| 5 | **Enterprise** | policy avanzate, audit del ledger, SSO, supporto | Licenza annuale |

### Esempio di conti (ipotesi da verificare)

- 50 fornitori attivi × 20.000 vendite/mese × 500 sats medi = 500 milioni sats/mese
  di volume (circa 5 BTC).
- Con una fee dell'1%: circa 5 milioni sats/mese (0,05 BTC) di commissioni.
- Più abbonamenti al gateway: per esempio 50 × 29 €/mese.

Il volume dipende interamente dall'adozione: la priorità iniziale è portare fornitori
e agenti sulla rete, non massimizzare la fee.

## Cosa serve ancora per incassare (fuori dal codice)

1. **Nodo Lightning di produzione** per la piattaforma (LND o LNbits su un provider come
   Voltage), con canali e liquidità in entrata per ricevere le fee.
2. **Verifica legale** in base al paese. Il modello attuale è **non-custodial**: ogni
   agente usa il proprio nodo o wallet e la piattaforma riceve solo le proprie fee.
   Questo riduce molto gli obblighi (MiCA/CASP in UE). Se in futuro offrirai wallet
   custodial (gestisci tu i fondi degli utenti), servirà quasi certamente una licenza.
   Fatti consigliare da un avvocato specializzato in cripto prima del lancio.
3. **Marchio**: esiste già un progetto chiamato "AIBTC" nell'ecosistema Bitcoin/Stacks
   per agenti AI. Verifica il rischio di confusione e valuta un nome diverso prima di
   investire nel brand.
4. **Fiscalità**: le fee incassate in BTC sono ricavi; serve un commercialista per la
   contabilizzazione e la conversione.
5. **Primi clienti**: 3–5 fornitori di dati o API AI disposti a provare il gateway
   gratis per 3 mesi in cambio di feedback e visibilità.

## Roadmap

### Fatto in questa versione (0.1)
- Protocollo di negoziazione firmato, a più round, con protezione da replay.
- Limiti rigidi su ogni decisione, strategia deterministica e strategia con Claude.
- Backend LND e LNbits, rete simulata per test.
- Pagamenti in stile L402 legati al prezzo negoziato, consegna idempotente, recupero
  dopo una consegna fallita senza ripagare.
- Policy di spesa, ledger SQLite, commissioni di piattaforma.
- Server HTTP, CLI, server MCP, Dockerfile, CI, 85 test.

### Prossimi passi
1. **Validazione su regtest/signet** con nodi veri (`tests/test_regtest_lnd.py`),
   poi mainnet con limiti bassi.
2. **Pubblicazione su PyPI** (`pip install aibtc-nexus`) e annuncio nelle community
   Bitcoin/Lightning e degli sviluppatori di agenti.
3. **Escrow con hold invoice**: il fornitore incassa solo dopo la consegna, per
   eliminare il rischio di mancata consegna.
4. **Macaroon veri** al posto del token HMAC (compatibilità piena con client L402).
5. **Reputazione dei fornitori**: punteggi basati sulle consegne riuscite, firmati.
6. **Directory pubblica / marketplace** dei servizi.
7. **Dashboard web** per wallet e fornitori (spesa, vendite, fee, kill switch).
8. **Altri backend**: Nostr Wallet Connect (NWC), Core Lightning, Breez SDK.
9. **Integrazioni** con i framework di agenti (LangChain, CrewAI), oltre a MCP.
10. **Persistenza delle sessioni** del fornitore (oggi sono in memoria) e rate limiting.
