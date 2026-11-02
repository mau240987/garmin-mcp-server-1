# Requisiti per il backend Starter (A4)

> Stato: **stub** (`src/garmin_coach_mcp/backends/official_starter.py`), nessuna funzionalità.
> Data: 2026-11-02. Livelli di evidenza come in `docs/api-coverage.md` §1: **[DOC]** pagina
> ufficiale letta · **[DEDOTTO]** inferenza · **[NON VERIFICATO]** fonte secondaria/brief.

## 1. Perché Starter

È l'unica via **ufficiale** per sostituire le scritture del backend non ufficiale
(`push_workout`, `push_training_plan`, `delete_*`): il Personal API è solo lettura [DOC + REALE].

## 2. Cosa sappiamo

| Punto | Contenuto | Evidenza |
|---|---|---|
| API incluse | Activity (JSON e FIT), Courses, Health, **Training**, Women's Health | [DOC] overview |
| Consegna dati | **Solo push** (webhook) | [DOC] overview |
| Dati storici | Non inclusi (solo dati successivi alla connessione dell'utente) | [DOC] overview |
| Capacità utenti | "Limited"; il valore ~1.000 non è confermato | [DOC] / [NON VERIFICATO] |
| Accesso | Richiesta tramite il Garmin Developer Portal ("Apply…", "Request now") | [DOC] overview |
| Requisiti di ammissione | Entità business, privacy policy, elenco dei processori AI | [NON VERIFICATO] (portale non leggibile senza account) |
| Autorizzazione utente | Flusso OAuth dell'utente (consenso e revoca) | [DEDOTTO] |
| Contratto | Developer Program Agreement Rev. B: §15.10 (uso per AI: trasparenza + consenso esplicito e revocabile), §6.4 (attribuzione), §5.2(o) (nessuna esposizione a terzi tramite proprie API) | [NON VERIFICATO] |
| Pagina Enterprise | `developer.garmin.com/gc-developer-program/enterprise/` è renderizzata lato client e non espone testo pubblico | verificato il 2026-11-02 |

## 3. Implicazioni architetturali

1. **Scritture (Training API):** chiamate partner → Garmin. Compatibili con un server locale a
   patto di gestire il consenso OAuth dell'utente (serve un redirect URI: in locale
   un'URL `http://127.0.0.1:<porta>/callback` temporanea, da confermare con i requisiti del
   portale) [DEDOTTO].
2. **Letture (Activity/Health):** solo push → serve un **endpoint HTTPS pubblico** che riceva i
   webhook e uno storage. Non compatibile con il server stdio single-user della Fase 1: le
   letture restano sul backend **Personal**. Starter per le letture ha senso solo nella Fase 2
   (server remoto multi-utente).
3. **Nessuno storico:** i dati precedenti alla connessione non arrivano; per il profilo atleta
   (12 settimane) il Personal API resta comunque necessario.
4. **Conformità (se il contratto è confermato):** consenso AI esplicito e revocabile prima di
   inviare dati a Claude/ChatGPT/Gemini; attribuzione Garmin su dati, export e derivati;
   nessun relay dei dati verso terzi.

## 4. Punto di innesto nel codice (già pronto)

- `OfficialStarterBackend` è `official = True`. Quando dichiarerà le capacità
  `WORKOUT_CREATE/SCHEDULE/DELETE` (vedi `PLANNED_CAPABILITIES`), il router la sceglierà
  **prima** del backend non ufficiale, senza modifiche ai tool. Questo comportamento è già
  coperto da un test (`test_starter_takes_writes_once_it_declares_them`).
- Le letture continueranno a passare dal Personal API, perché Starter non dichiarerà capacità
  di lettura nella Fase 1.
- `StarterCredentials` contiene segnaposto (`client_id`, `client_secret`, token utente): i nomi
  reali dei parametri vanno presi dalla documentazione del portale.

## 5. Da fare quando ci sarà un account Starter

1. Leggere i termini e la documentazione del Training API nel portale. Confermare o correggere
   la §2 e il formato dei workout: le unità (V-1) e il target di passo (V-2) di
   `docs/api-coverage.md` §6.
2. Implementare il flusso OAuth locale con memorizzazione sicura del token (keychain o file
   `0600`), revoca e rinnovo.
3. Implementare `create_running_workout`, `schedule_workout` e `delete_workout`, con test sulle
   fixture ricavate dalla specifica ufficiale. Poi dichiarare le capacità.
4. Aggiungere la verifica empirica, sul modello di `scripts/verify_personal_api.py`.
5. Aggiornare `docs/api-coverage.md` §3 (tier necessario → disponibile).
