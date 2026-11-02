# Motore del piano (C2) — regole

Codice: `src/garmin_coach_mcp/coach/engine.py`. Ogni numero qui sotto è una costante con nome
nel codice. Il motore è **deterministico**: stessi obiettivo, baseline e data di inizio
producono lo stesso piano, senza casualità, senza orologio e senza LLM. I test
(`tests/test_coach_engine.py`) verificano le regole sui 5 casi del brief, su una matrice di
circa 7.200 combinazioni (distanza, storico, giorni, anticipo, giorno di gara, giorno del lungo,
inizio a metà settimana, date indisponibili) e su un test di regressione per ogni difetto
trovato dalla revisione indipendente. Volumi, lungo e progressione si calcolano sempre su
quanto è **effettivamente pianificato** (versione 1.1).

| Regola | Contenuto |
|---|---|
| R1 Calendario | Settimane lunedì-domenica. L'ultima contiene la gara, che è l'ultima sessione. Nessuna sessione prima dell'inizio o dopo la gara. |
| R2 Progressione | Ogni settimana di carico ≤ +10% rispetto alla precedente di carico (su quanto pianificato). La prima non supera il volume recente (minimo 8 km). |
| R2b Giorni | Con poco volume si usano meno giorni di quelli richiesti (ognuno con almeno 3 km dopo il lungo), con avviso. |
| R3 Scarico | Una settimana ogni 4 (prima del taper) al 75% del volume e al 70% del lungo, senza qualità. |
| R4b Corse facili | Al massimo il 75% del lungo e al massimo 12-20 km per distanza. Il volume in eccesso va al lungo, entro i suoi limiti; se ancora non basta, avviso "aggiungi un giorno". La qualità non supera mai il lungo. |
| R4 Lungo | Tetto per distanza e livello (es. maratona 30/32/34 km, 100 km 40/45/50 km). Quota sul volume: 35% per 5K/10K, 38% mezza, 45% maratona/ultra, minimo 40% sotto i 30 km/settimana. Crescita ≤ +2 km. Durata massima al passo facile, se noto (maratona 3h30, ultra 5h30). |
| R5 Taper | 1 settimana per 5K/10K, 2 per la mezza, 3 per maratona/ultra (meno se la preparazione è corta, +1 se la gara è tra lunedì e mercoledì). Volume decrescente sul picco (es. 80%, 65%, 45%). |
| R6 Fasi | Base, costruzione e specifico (~40/35/25% delle settimane prima del taper). |
| R7 Principianti | Nessuna qualità in base, al massimo una seduta di qualità a settimana dopo. Livello dedotto dalla baseline (o impostato a mano). |
| R9 Vicino alla gara | Nessun lungo, medio-lungo o seduta di qualità negli ultimi 2/3/4/6/8 giorni (5K/10K/mezza/maratona/ultra): diventano corse facili brevi (≤ 8 km). |
| R10 Orizzonte | Al massimo 52 settimane di piano; per gare più lontane il piano parte 52 settimane prima, con avviso. |
| R11 Ripresa | Una settimana in cui le date indisponibili tolgono più di metà del volume è uno stop. La ripresa avviene al 75% (dopo 1 settimana) o al 60% (dopo 2 o più) del carico precedente, compreso il lungo, con avviso. |
| R8 Avvisi | Preparazione corta, storico vuoto, giorni ridotti, volume limitato dai giorni, maratona/ultra per principianti, picco irraggiungibile, gara troppo vicina per la distanza. Il motore non accelera mai per "recuperare". |
| Giorni | Schema per giorni/settimana con lungo la domenica (spostabile). Qualità martedì (e giovedì). Ultra in fase specifica: medio-lungo il sabato (back-to-back) da 30 km/settimana in su. |
| Date indisponibili | Le sessioni vengono saltate (con avviso), non spostate. Si riparte da quanto effettivamente pianificato (R11 per gli stop lunghi). |

Picchi di volume settimanale (km): 5K 25/35/50 · 10K 30/40/60 · mezza 35/50/70 · maratona
45/60/80 · ultra 60/80/100 (principiante/intermedio/avanzato).

Ogni piano porta la nota di sicurezza ("non è un parere medico…"). Se la baseline viene da
Garmin, porta anche l'attribuzione per dati derivati prevista dalle API Brand Guidelines.
