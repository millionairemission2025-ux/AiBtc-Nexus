# Guida al lancio

Tutto ciò che si fa con il codice è pronto e verificato, anche con pagamenti
Lightning reali su regtest. Qui sotto ci sono solo i passi che richiedono **i tuoi
account, i tuoi soldi o le tue decisioni**, in ordine.

## 1. Pubblicare su PyPI (15 minuti, gratis)

Serve una configurazione da fare una sola volta; poi ogni nuova versione si
pubblica da sola.

1. Crea un account su [pypi.org](https://pypi.org/account/register/) e attiva la
   verifica in due passaggi (obbligatoria).
2. Vai su **Account → Publishing → Add a new pending publisher** e compila:
   - PyPI Project Name: `aibtc-nexus`
   - Owner: `millionairemission2025-ux`
   - Repository name: `AiBtc-Nexus`
   - Workflow name: `release.yml`
   - Environment name: `pypi`
3. Su GitHub: **Settings → Environments → New environment** chiamato `pypi`.
4. Dimmi "fatto": creo io il tag `v0.2.0` e GitHub pubblica il pacchetto. Da quel
   momento chiunque lo installa con `pip install aibtc-nexus`.

(Il nome `aibtc-nexus` oggi è libero su PyPI. Se decidi di cambiare nome, fallo
**prima** di pubblicare: vedi il punto 5.)

## 2. Nodo Lightning di produzione (1 ora, qualche decina di euro)

Opzione più semplice: un nodo LND ospitato su [Voltage](https://voltage.cloud).

1. Crea un nodo LND **mainnet**.
2. Apri un canale verso un nodo ben connesso e procurati liquidità in entrata
   (Voltage la vende integrata). Senza liquidità in entrata non puoi ricevere
   pagamenti.
3. Crea un macaroon con soli permessi necessari, non usare `admin.macaroon`:
   ```bash
   lncli bakemacaroon invoices:read invoices:write offchain:read offchain:write info:read
   ```
4. Mettilo in `.env` (vedi `.env.example`) con `NEXUS_LN_BACKEND=lnd`.
5. **Limiti bassi all'inizio**: `NEXUS_MAX_PAYMENT_SATS=1000`,
   `NEXUS_DAILY_LIMIT_SATS=10000`.

Ricorda la riserva del canale (circa 1% della capacità): finché il saldo non la
supera, le commissioni di piattaforma restano maturate e vengono ritentate.

## 3. Mettere online il primo servizio

Sul server (anche una VPS da 5 €/mese):

```bash
pip install "aibtc-nexus[server]"
aibtc-nexus serve-provider --config services.json --host 0.0.0.0 --port 8402
```

Mettilo dietro un reverse proxy con HTTPS (Caddy lo fa in automatico) e aggiungi
`--trust-proxy`. Oppure usa il `Dockerfile` del repository.

## 4. Legale e fiscale (prima di incassare da terzi)

- **Avvocato** esperto di cripto-attività: chiedi conferma che il modello
  *non-custodial* (ognuno usa il proprio wallet, la piattaforma incassa solo le
  proprie commissioni) non richiede autorizzazioni MiCA/CASP o registrazione OAM
  nel tuo caso.
- **Commercialista**: contabilizzazione delle commissioni incassate in BTC e
  trattamento fiscale.

## 5. Nome

"AIBTC" è già usato da un progetto di agenti AI su Bitcoin/Stacks. Se vuoi evitare
confusione, alcune alternative ancora libere su PyPI (verificato il 29/10/2026):
`satshaggle`, `haggle402`, `agentbazaar`, `bargainbolt`, `dealbot402`. Controlla
anche marchi registrati (EUIPO) e domini prima di scegliere. Se cambi nome, dimmelo:
rinomino io pacchetto, comandi e documentazione.

## 6. Primi clienti

Obiettivo: 3–5 fornitori di dati o API che provano il gateway gratis per 3 mesi.
Dove trovarli: chi vende già API a pagamento per sviluppatori di agenti AI, dati di
mercato crypto, progetti nell'ecosistema Lightning (L402, Nostr).

### Messaggio ai fornitori (italiano)

> Ciao [nome], ho visto [la vostra API]. Sto lanciando AiBtc-Nexus, uno strumento
> open source che permette agli agenti AI di comprare servizi come il vostro e
> pagarli in bitcoin via Lightning, con negoziazione automatica del prezzo.
>
> Per voi: la vostra API resta com'è, noi la mettiamo dietro un paywall Lightning
> in pochi minuti. Incassate in sats per ogni chiamata, senza account, carte o
> chargeback, e con l'escrow il cliente paga solo se il servizio risponde.
>
> Cerco 3–5 fornitori per un test gratuito di 3 mesi, in cambio di feedback. Ti va
> una call di 15 minuti?

### Message to providers (English)

> Hi [name], I came across [your API]. I'm launching AiBtc-Nexus, an open-source
> toolkit that lets AI agents buy services like yours and pay in bitcoin over
> Lightning, with automatic price negotiation.
>
> For you: your API stays as it is; we put it behind a Lightning paywall in
> minutes. You earn sats per call with no accounts, cards or chargebacks, and with
> escrow the client only pays when your service actually answers.
>
> I'm looking for 3–5 providers for a free 3-month pilot in exchange for feedback.
> Up for a 15-minute call?

### Annuncio per community (Nostr, X, Reddit, Discord)

> AiBtc-Nexus: AI agents negotiate prices with each other and pay over Lightning,
> with hold-invoice escrow (the seller only gets paid after delivering) and hard
> spending limits the AI cannot override. L402-style, MCP server included, so
> Claude and other agents can use it out of the box. Open source (MIT).
> pip install aibtc-nexus — github.com/millionairemission2025-ux/AiBtc-Nexus
