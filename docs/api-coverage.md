# Copertura API Garmin — Gate G0

> Stato: **bozza in attesa di approvazione** · Data analisi: 2026-11-02
> Scopo: decidere quali dei 9 tool possono passare all'API ufficiale **Personal** e cosa resta
> fuori, prima di scrivere i backend (A2–A4).

## 1. Fonti e livello di evidenza

| Sigla | Significato |
|---|---|
| **[SPEC]** | Letto nella specifica OpenAPI ufficiale del Personal API (vedi sotto). |
| **[DOC]** | Letto nelle pagine ufficiali developer.garmin.com (overview, Personal API, FAQ). |
| **[DEDOTTO]** | Inferenza ragionata dalla documentazione, non verificata con chiamata reale. |
| **[NON VERIFICATO]** | Informazione da fonti secondarie (brief del progetto) non confermata. |
| **[REALE]** | Verificato con chiamata reale. **Nessuna voce ha questo livello oggi** (vedi §1.2). |

### 1.1 Specifica ufficiale

- Pagina: `https://developer.garmin.com/gc-developer-program/personal/swagger-docs/`
- La specifica non è un file scaricabile: è incorporata nel bundle JS della pagina Swagger
  (`component---src-pages-gc-developer-program-personal-swagger-docs-js-715115a6210d45afc282.js`,
  `JSON.parse('{"openapi":"3.0.1",…}')`). Estratta così com'è, senza ricostruzioni.
- Titolo/versione: **Garmin Connect Personal API 1.0.0**, OpenAPI 3.0.1.
- Server: `https://personal.connectapi.garmin.com` e `https://personal.connectapi.garmin.cn`.
- Contenuto: **26 operazioni, tutte `GET`**; 0 `POST/PUT/DELETE`; nessun `securityScheme`
  dichiarato. SHA-256 (primi 16 hex) del JSON estratto: `84f408b2866a84ef`.
- La specifica **non è stata copiata nel repository** (contenuto Garmin; termini di riuso non
  verificati). Per rigenerarla: scaricare il bundle indicato ed estrarre l'argomento di
  `JSON.parse(...)` che inizia con `{"openapi"`.

### 1.2 Verifica empirica: non eseguita

L'ambiente di sviluppo usato per questa analisi **non può raggiungere**
`personal.connectapi.garmin.com` (connessione rifiutata dal proxy di rete della sandbox), quindi
non è stato possibile alcun test reale, nemmeno avendo una chiave. Per chiudere i punti aperti:

```bash
export GARMIN_PERSONAL_API_KEY=...        # mai in chat, mai nel repo
python3 scripts/verify_personal_api.py 2026-10-30 > personal-api-report.md
```

Lo script fa solo `GET` (più il `POST` di emissione token documentato), stampa codici HTTP e
**presenza** dei campi (mai i valori). Il report va rivisto prima di condividerlo.

## 2. Il Personal API in sintesi [SPEC][DOC]

| Area | Endpoint (`GET`) | Note |
|---|---|---|
| Attività | `/activitylist-service/activities/search/activities` | filtri `start`, `limit` (default 99), `startDate`, `endDate`, `activityType`, … → `SummarizedActivityDTO[]` |
| | `/activity-service/activity/{activityId}` | `ActivitySummaryDTO` con `summaryDTO` + `splitSummaries` |
| | `/activity-service/activity/{activityId}/workouts` | workout strutturato eseguito nell'attività (step, target) |
| | `/download-service/files/activity/{activityId}` | file originale (zip, FIT) |
| | `/wellnessactivity-service/activity/summary/range/{from}/{until}` | attività wellness con `timeInZoneList` |
| Salute | `/usersummary-service/usersummary/daily?calendarDate=` | passi, RHR, RHR media 7gg, stress, Body Battery (max/min/al risveglio), SpO2 |
| | `/usersummary-service/v2/dailysummaries?startDate&endDate` | come sopra, su intervallo |
| | `/sleep-service/sleep/dailySleep?date=` · `/sleep-service/sleep/dailySleepSummariesByDate` | `DailySleepDTO` (durata, fasi, `sleepScores`, `sleepNeed`) |
| | `/hrv-service/hrv/{date}` · `/hrv-service/hrv/daily/{from}/{to}` | `hrvSummary` (`lastNightAvg`, `weeklyAvg`, `baseline`, `status`) |
| | `/wellness-service/wellness/bodyBattery/reports/daily` | report Body Battery su intervallo |
| | `/wellness-service/wellness/dailyHeartRate?date=` | FC giornaliera, RHR |
| | `/biometric-service/heartRateZones/v2` | **zone FC configurate** (floor zone 1–5, max HR, LTHR, per sport) |
| | `/usersummary-service/stats/{steps,stress,hydration}/daily/{start}/{end}` | serie giornaliere |
| | sonno, peso, idratazione, nutrizione, SpO2/acclimatazione, fitness age, obiettivo peso | |
| Profilo | `/userprofile-service/userprofile/settings` | include `availableTrainingDays`, `preferredLongTrainingDays` |
| Women's Health | `/periodichealth-service/menstrualcycle/{summary,dayview}/{date}` | |

Autenticazione [DOC]: chiave creata dall'utente in Garmin Connect Web (scadenza a scelta
30/90/180/365 giorni, mostrata una sola volta, email di avviso ~1 settimana prima della scadenza).
Token: `POST /personalapitoken-service/personal-api/personal-api-key/token` con header
`Authorization: ApiKey <key>`; access token valido **24 h** (o fino alla scadenza della chiave),
**nessun refresh token** — se ne chiede uno nuovo. Lo schema della risposta del token **non è
documentato** e il formato dell'header per le chiamate successive (`Bearer`) è **[DEDOTTO]** da
"OAuth2 access tokens" nella FAQ. Rate limit: **non documentati** nelle pagine lette.

**Assente dalla specifica** [SPEC]: Training Readiness, Training Status, carico acuto/cronico,
endpoint dei massimali (VO2max giornaliero), libreria workout, calendario, qualsiasi scrittura,
lap per giro / serie temporali dettagliate dell'attività (disponibili solo dentro il file FIT).

## 3. Matrice di copertura dei 9 tool

| Tool | Endpoint ufficiale (Personal) | Campi coperti | Campi mancanti | Tier necessario | Verdetto |
|---|---|---|---|---|---|
| `get_activities` | `GET /activitylist-service/activities/search/activities` | id, nome, data locale, tipo, distanza, durata, FC media/max, calorie; **in più** `hrTimeInZone_1..5`, `aerobicTrainingEffect`, `anaerobicTrainingEffect`, `activityTrainingLoad`, `vO2MaxValue`, `splitSummaries`, `lapCount` | — | Personal | **Coperto** [SPEC] |
| `get_activity_details` | `GET /activity-service/activity/{id}` (+ `/{id}/workouts`, + download FIT) | `summaryDTO` completo (FC, Training Effect, carico, dinamiche di corsa: GCT, stride, oscillazione), `splitSummaries` per tipo di split | lap per giro e serie temporali (solo da FIT, da parsare); FIT forse privato di campi proprietari [NON VERIFICATO] | Personal | **Coperto** per il payload attuale; **Parziale** per il bisogno coaching "passo per giro" [SPEC][DEDOTTO] |
| `get_health_summary` | `usersummary/daily` + `sleep/dailySleep` + `hrv/{date}` | passi, RHR, RHR 7gg, stress medio, Body Battery max/min/al risveglio, SpO2, sonno (durata, score, fasi), HRV (notte, settimana, baseline, status) | **Training Readiness** | Personal | **Parziale** [SPEC] |
| `get_training_status` | `activities/search/activities` con `startDate/endDate` | tutto ciò che il tool calcola oggi (volume, sessioni, durata, passo medio, ultime corse) | Training Status / Readiness / carico acuto-cronico **nativi Garmin**; VO2max giornaliero | Personal (volume); nessun tier documentato per lo stato nativo | **Coperto** per il comportamento attuale; **Non coperto** per le metriche native [SPEC] |
| `get_workouts` | nessuno | — | tutta la libreria workout | Starter (Training API) [DEDOTTO] | **Non coperto** [SPEC] |
| `push_workout` | nessuno (Personal è solo GET) | — | creazione + pianificazione | Starter (Training API) [DOC: Training API inclusa in Starter] | **Non coperto** [SPEC] |
| `push_training_plan` | nessuno | — | idem | Starter (Training API) | **Non coperto** [SPEC] |
| `delete_workout` | nessuno | — | cancellazione | Starter (Training API) [DEDOTTO] | **Non coperto** [SPEC] |
| `delete_plan_workouts` | nessuno | — | elenco + cancellazione | Starter (Training API) [DEDOTTO] | **Non coperto** [SPEC] |

Le ipotesi di partenza del brief sono **confermate** dalla specifica, con due precisazioni:
`get_training_status` è coperto *nella forma attuale* (che calcola il volume dalle attività),
mentre non lo sarebbe se lo si volesse estendere allo stato nativo Garmin; `get_activity_details`
è coperto ma i lap per giro richiedono il parsing del FIT.

### 3.1 Punti utili per Race Coach che la specifica offre (non presenti oggi)

- **Zone FC ufficiali** (`heartRateZones/v2`): permettono di classificare "lento corso in Z3–Z4"
  con le zone dell'atleta invece di soglie stimate.
- **Tempo in zona per attività** (`hrTimeInZone_1..5`) direttamente nell'elenco attività: il
  calcolo della distribuzione per zona di `get_athlete_profile` non richiede chiamate per attività.
- **`activityTrainingLoad` per attività**: consente di calcolare in codice deterministico un carico
  acuto (7 gg) / cronico (28 gg). Sarà un indicatore **derivato**, non il valore Garmin.
- **`/activity/{id}/workouts`**: il workout pianificato eseguito nell'attività → base per
  `review_week` (pianificato vs svolto).
- **`availableTrainingDays` / `preferredLongTrainingDays`** nel profilo: default per i giorni di
  allenamento in `set_race_goal`.
- **Serie su intervallo** (sonno, HRV, Body Battery, daily summaries v2): trend RHR/HRV/sonno con
  poche chiamate.

## 4. Verifica delle segnalazioni della community (§1 del brief)

| Segnalazione | Esito | Evidenza |
|---|---|---|
| Personal non espone Training Readiness, Training Status, carico acuto/cronico | **Confermato** (nessun endpoint) | [SPEC] |
| VO2max non disponibile | **Parzialmente smentito**: `vO2MaxValue` per attività è documentato nell'elenco attività; nessun VO2max giornaliero. Che il campo sia valorizzato va verificato | [SPEC] / da verificare [REALE] |
| Nessuna scrittura con chiave Personal | **Confermato** (solo GET) | [SPEC] |
| Base URL `personal.connectapi.garmin.com` | **Confermato** (+ `.cn`) | [SPEC][DOC] |
| Download FIT `/download-service/files/activity/{activityId}` | **Confermato** (risposta zip) | [SPEC] |
| FIT privati di campi proprietari | **Non verificabile** senza chiamata reale | — |
| Chiave 30 gg–1 anno, token 24 h, niente refresh | **Confermato** (30/90/180/365 gg) | [DOC] |
| "Non pensata per app commerciali che servono altre persone" | **Confermato** | [DOC] FAQ |
| Starter: push only, +Training e Courses, niente storico, utenti limitati | **Confermato**; "~1.000 utenti" non confermato (la pagina dice "Limited") | [DOC] overview |
| Enterprise: +Golf, push o ping/pull, storico 1 mese+, illimitato | **Confermato**; prezzo (1.000 $ + per utente) **non confermato** | [DOC] overview |
| Starter richiede entità business, privacy policy, elenco processori AI | **Non verificato** (il form non è leggibile senza account) | — |

## 5. Rischi contrattuali e di compliance

| # | Rischio | Stato evidenza | Impatto sul progetto |
|---|---|---|---|
| R1 | **Termini del Personal API** accettati alla creazione della chiave (testo leggibile solo da loggati) | **NON letto** | Da leggere dall'utente prima della Fase 2: in particolare eventuali clausole su invio dati a fornitori AI (Claude/ChatGPT/Gemini). |
| R2 | Developer Program Agreement Rev. B — §15.10 (AI: trasparenza + consenso esplicito e revocabile), §6.4 (attribuzione), §5.2(o) (no esposizione a terzi via proprie API) | **NON verificato** (testo non pubblicamente raggiungibile dalle pagine lette) | Se confermato vale per Starter/Enterprise; per il Fase 1 single-user progettiamo comunque già consenso esplicito e attribuzione. Il server remoto multi-utente (Fase 2) ricadrebbe in §5.2(o). |
| R3 | API Brand Guidelines: requisiti di attribuzione per display, schermate secondarie, **dati esportati e dati derivati** | Esistenza dei requisiti **confermata** [DOC]; dettaglio nel PDF scaricabile **non letto** | Ogni output del backend ufficiale ed export (`md/ics/fit`) e il profilo derivato devono riportare l'attribuzione Garmin. Testo esatto da prendere dal PDF. |
| R4 | **Backend non ufficiale** (`garminconnect`): login con credenziali Garmin, endpoint interni | Fatto | Possibile violazione dei Termini d'uso di Garmin Connect [NON VERIFICATO], rischio blocco account, rotture frequenti: in questa sessione abbiamo osservato rotture reali (vedi §6). |
| R5 | Personal = solo dati propri, no app commerciali multi-utente | [DOC] | Coerente con Fase 1 (ogni utente usa la propria chiave localmente). Esclude un servizio ospitato multi-utente basato su chiavi Personal. |
| R6 | Starter "push only" (webhook) | [DOC] | Un backend Starter per le **letture** richiede un endpoint HTTPS pubblico che riceve i dati: incompatibile con un server stdio locale. Per le **scritture** (Training API) serve il flusso OAuth dell'utente e un'app business approvata. |

## 6. Stato reale del repository (rilevato in G0/H)

- Il piano di hardening P1–P8 (`SPEC.md`, `PROGRESS.md`) era **solo pianificato**: nessun
  `src/`, `pyproject.toml`, test o CI esistevano. Il bug `only-include` **non si applica** (non
  c'era alcun `pyproject.toml`).
- Il server legacy **non partiva** con un'installazione pulita: `requirements.txt` (`mcp>=1.0.0`)
  risolve oggi a `mcp` 2.3.0, dove `mcp.server.fastmcp` non esiste più.
- `garminconnect` 0.3.20 (risolto da `>=0.3.0`): `TargetType.SPEED`/`HEART_RATE` rinominati
  (`push_workout` legacy sarebbe andato in errore), token in formato garth non più supportati
  (serve un nuovo login una tantum), retry interni anche sulle POST (disattivati, vedi H).
- **V-1 — Unità degli step (da verificare su dispositivo reale).** Il codice legacy inviava
  durate in millisecondi (×1000) e distanze in centimetri (×100). Gli helper ufficiali della
  libreria usano **secondi** e **metri**, e il nuovo codice segue la libreria. Prima di
  qualunque uso in produzione: creare un workout di prova (10 min + 1 km), verificarlo su
  Garmin Connect e cancellarlo (richiede la tua autorizzazione per la sessione di test).
- **V-2 — Target passo.** Il legacy usava `speed.zone`; la libreria per la corsa usa
  `pace.zone` (id 6, valori in m/s). Il nuovo codice usa `pace.zone`. Da verificare insieme a V-1.

## 7. Raccomandazione

**Strategia A — Ibrida "ufficiale prima, non ufficiale solo opt-in" (raccomandata).**

1. **Letture** → `OfficialPersonalBackend` per default quando è configurata
   `GARMIN_PERSONAL_API_KEY`: copre `get_activities`, `get_activity_details`,
   `get_training_status` (forma attuale) e quasi tutto `get_health_summary`, oltre ai dati nuovi
   per Race Coach (§3.1). Attribuzione Garmin in ogni risposta.
2. **Training Readiness** → non disponibile ufficialmente: il campo resta `unavailable` con
   motivo esplicito; fallback al backend non ufficiale **solo** se abilitato esplicitamente
   (es. `GARMIN_ALLOW_UNOFFICIAL_READS=true`). Race Coach lavora senza (usa HRV, RHR, sonno,
   Body Battery che sono ufficiali).
3. **Scritture** (`push_*`, `delete_*`, `get_workouts`) → `UnofficialBackend`, disattivate di
   default, attivabili con `GARMIN_ALLOW_UNOFFICIAL_WRITES=true` + conferma esplicita nel
   workflow; altrimenti errore strutturato con alternativa **export** (`.fit` workout,
   `.ics` + `.md`).
4. **Starter** → solo interfaccia/stub e documento requisiti; nessuna implementazione finché non
   esiste un account (richiede entità business e, per le letture, un endpoint webhook pubblico).

Alternative considerate:

- **B — Solo ufficiale:** massima conformità, ma niente invio workout al dispositivo (solo
  export manuale) e niente Training Readiness. Ottima come modalità `--official-only`.
- **C — Solo non ufficiale (stato attuale):** funzionalità piena ma rischio R4 e fragilità
  dimostrata; sconsigliata come default.

## 8. Decisioni richieste prima di A2/C3

1. Approvi la **Strategia A** (o preferisci B come default)?
2. Fallback non ufficiale per Training Readiness: **opt-in** come proposto, oppure eliminato?
3. Puoi eseguire `scripts/verify_personal_api.py` con la tua chiave sulla tua macchina e
   allegare il report (solo codici e presenza campi)? Serve a chiudere i punti [DEDOTTO].
4. Autorizzi **una sessione di test di scrittura** sul tuo account (1 workout di prova creato e
   poi cancellato) per chiudere V-1/V-2?
5. Puoi leggere/allegare i **termini del Personal API** (R1) e il PDF delle **API Brand
   Guidelines** (R3)?
