# Motore del piano (C2) — regole

Codice: `src/garmin_coach_mcp/coach/engine.py`. Ogni numero qui sotto è una costante con nome
nel codice. Il motore è **deterministico**: stessi obiettivo, baseline e data di inizio
producono lo stesso piano, senza casualità, senza orologio e senza LLM. I test
(`tests/test_coach_engine.py`) verificano le regole su 5 casi del brief e su una matrice di
2.160 combinazioni.

| Regola | Contenuto |
|---|---|
| R1 Calendario | Settimane lunedì-domenica. L'ultima contiene la gara, che è l'ultima sessione. Nessuna sessione prima dell'inizio o dopo la gara. |
| R2 Progressione | Ogni settimana di carico ≤ +10% rispetto alla precedente di carico. La prima non supera il volume recente Garmin +10% (o il minimo per i giorni scelti, con avviso). |
| R3 Scarico | Una settimana ogni 4 (prima del taper) al 75% del volume e al 70% del lungo, senza qualità. |
| R4 Lungo | Tetto per distanza e livello (es. maratona 30/32/34 km, 100 km 40/45/50 km). Quota sul volume: 35% per 5K/10K, 38% mezza, 45% maratona/ultra, minimo 40% sotto i 30 km/settimana. Crescita ≤ +2 km. Durata massima al passo facile, se noto (maratona 3h30, ultra 5h30). |
| R5 Taper | 1 settimana per 5K/10K, 2 per la mezza, 3 per maratona/ultra (meno se la preparazione è corta, +1 se la gara è tra lunedì e mercoledì). Volume decrescente sul picco (es. 80%, 65%, 45%). |
| R6 Fasi | Base, costruzione e specifico (~40/35/25% delle settimane prima del taper). |
| R7 Principianti | Nessuna qualità in base, al massimo una seduta di qualità a settimana dopo. Livello dedotto dalla baseline (o impostato a mano). |
| R8 Avvisi | Preparazione corta, storico vuoto, volume recente troppo basso, maratona/ultra per principianti, picco irraggiungibile. Il motore non accelera mai per "recuperare". |
| Giorni | Schema per giorni/settimana con lungo la domenica (spostabile). Qualità martedì (e giovedì). Ultra in fase specifica: medio-lungo il sabato (back-to-back) da 30 km/settimana in su. |
| Date indisponibili | Le sessioni vengono saltate (con avviso), non spostate. La settimana dopo riprende il carico previsto. |

Picchi di volume settimanale (km): 5K 25/35/50 · 10K 30/40/60 · mezza 35/50/70 · maratona
45/60/80 · ultra 60/80/100 (principiante/intermedio/avanzato).

Ogni piano porta la nota di sicurezza ("non è un parere medico…"). Se la baseline viene da
Garmin, porta anche l'attribuzione per dati derivati prevista dalle API Brand Guidelines.
