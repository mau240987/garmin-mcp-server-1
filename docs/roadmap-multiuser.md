# Fase 2 — Server remoto multi-utente (solo progettazione)

> Fuori scope della Fase 1 (brief §4). Nessuna implementazione. Evidenze come in
> `docs/api-coverage.md`: **[DOC]** documentato, **[NON VERIFICATO]**, **[DEDOTTO]**.

## Perché serve

- ChatGPT (e i client web) usano server MCP remoti via HTTPS (`docs/clients.md`).
- Un servizio per più atleti non può usare chiavi Personal: il Personal API "non è pensato per
  app commerciali che servono altre persone" [DOC].

## Requisiti

| Area | Requisito | Note |
|---|---|---|
| Accesso Garmin | Account **Starter** (business) e app approvata | Entità business, privacy policy, elenco processori AI [NON VERIFICATO]; utenti limitati [DOC]. |
| Dati in lettura | Starter è **solo push** (webhook) [DOC] | Endpoint HTTPS pubblico che riceve e salva i dati per utente; niente storico precedente alla connessione [DOC] → il profilo iniziale è povero. |
| Scritture | Training API (Starter) [DOC] | Il backend `OfficialStarterBackend` è già predisposto (A4). |
| Autenticazione utenti | OAuth 2.1 verso il server MCP (l'SDK MCP supporta `auth`/`token_verifier`) | Ogni token mappa un atleta; nessun accesso incrociato. |
| Consenso AI | Esplicito, informato e revocabile prima di inviare dati a un modello | Clausola §15.10 del contratto [NON VERIFICATO]; serve UI di consenso e registro. |
| Attribuzione | "Garmin [modello]" sui dati, frase per i derivati | Già implementata (API Brand Guidelines, letto). |
| Divieto di rilancio | Niente API proprie che espongano i dati Garmin a terzi | §5.2(o) [NON VERIFICATO]: il server MCP deve servire solo l'atleta stesso. |
| GDPR | Dati sanitari = categoria particolare (art. 9) | Base giuridica (consenso esplicito), DPIA, DPA con i processori (hosting, provider AI), minimizzazione, retention, cancellazione su revoca, registro dei trattamenti. |
| Sicurezza | Segreti in un secret manager, cifratura at-rest dei dati per utente, rate limiting, log senza dati personali | Il server attuale è single-user e senza auth: **non va esposto** così com'è. |

## Architettura proposta

```
client MCP (ChatGPT/Claude web) ──OAuth──▶ gateway MCP (HTTPS, multi-tenant)
                                              │  per utente: CoachStore isolato (DB)
                                              ├─▶ OfficialStarterBackend (Training API)
Garmin ──webhook push──▶ ingest (HTTPS) ──────┘  dati attività/salute per utente
```

- Il motore del piano, il profilo e la revisione restano invariati (funzioni pure).
- `CoachStore` passa da file JSON locali a un DB con chiave utente.
- Il router per capacità resta: in Fase 2 solo backend ufficiali, nessuno non ufficiale.

## Decisioni da prendere prima di iniziare

1. Soggetto giuridico titolare del trattamento e richiesta di accesso Starter.
2. Hosting (UE) e provider AI ammessi nell'elenco processori.
3. Testo del consenso AI e della privacy policy (con un legale).
