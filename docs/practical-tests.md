# Test pratici (da eseguire sul proprio computer)

L'ambiente di sviluppo non raggiunge Garmin: queste prove chiudono le verifiche aperte. Ogni test
dice cosa fare, cosa aspettarsi e cosa riportare. Nessun test scrive su Garmin tranne **T6**, che
è facoltativo e va autorizzato a parte.

Prerequisiti: `pip install "git+https://github.com/mrosano1987/garmin-mcp-server-1.git"` (o
`pip install .` da un clone) e la chiave Personal API in `GARMIN_PERSONAL_API_KEY`.

| Test | Cosa verifica | Scrive su Garmin? |
|---|---|---|
| T1 | Profilo, piano ed export sui tuoi dati reali (script) | No |
| T2 | Calendario ICS in un'app reale | No |
| T3 | Workout FIT sull'orologio | No (file locale) |
| T4 | Claude Desktop + skill | No |
| T5 | Claude Code + plugin | No |
| T6 | Invio di 1 settimana a Garmin e pulizia | **Sì, facoltativo** |
| T7 | Revisione settimanale dopo una settimana reale | No |

## T1 — Script sui dati reali (10 minuti)

```bash
python3 scripts/try_race_coach.py --race-type ultra --distance 100 --date 2027-05-29 \
    --name Passatore --days 5 > race-coach-trial.md
```

Atteso: backend `official-personal`; profilo con volume, lungo, distribuzione per zone FC e
segnali di recupero; piano con avvisi; file `.md`, `.ics`, `.zip` in `race-coach-trial/data/exports`.
Riportare: il report (contiene i tuoi volumi: rileggilo prima) **o** solo le risposte a
"il volume e il lungo recenti ti sembrano giusti? l'avviso sui lenti troppo veloci è corretto?"

## T2 — Calendario

Importare il `.ics` in Google Calendar / Apple Calendar / Outlook (meglio un calendario nuovo).
Atteso: un evento giornaliero per sessione con descrizione, passo e "Insights derived in part from
Garmin device-sourced data.". Riportare: app usata, eventuali errori o caratteri strani.

## T3 — Workout FIT sull'orologio

Dallo zip FIT prendere **un** file (es. un lento) e caricarlo sull'orologio. Il metodo dipende dal
modello: su molti Garmin collegati via USB si copia il file nella cartella `GARMIN/NewFiles`;
non tutti i modelli lo supportano. Atteso: workout in *Allenamenti* con riscaldamento/blocchi e
target di passo. Riportare: modello dell'orologio, se il file è stato accettato e se step e
target sono corretti.

## T4 — Claude Desktop

1. Configurare il server in `claude_desktop_config.json` (vedi `docs/clients.md`).
2. Caricare `race-coach.zip` in *Customize → Skills*.
3. Provare: «Voglio preparare il Passatore il 29 maggio, solo arrivare, 5 giorni a settimana.
   Guarda i miei dati Garmin e fammi il piano.»

Atteso: domande minime, profilo con i dati reali, avvisi prima del piano, nessun invio a Garmin
senza consenso, nota "non è un parere medico". Riportare: se la skill si attiva e cosa non torna.

## T5 — Claude Code

```bash
claude plugin marketplace add mrosano1987/garmin-mcp-server-1
claude plugin install garmin-race-coach@garmin-coach
```

Stesso prompt del T4, poi «com'è andata questa settimana?». Atteso: `review_week` con allarmi e
proposte, applicate solo dopo conferma.

## T6 — Invio a Garmin (facoltativo, scrive sul tuo account)

Richiede `GARMIN_ALLOW_UNOFFICIAL_WRITES=true` e un login (`garmin-coach-mcp --login`). Con
l'assistente: «manda a Garmin la prossima settimana» → controllare l'elenco della prova a vuoto →
confermare. Atteso: workout nel calendario di Garmin Connect e sull'orologio dopo la
sincronizzazione. Pulizia: «rigenera il piano» seguito da un nuovo invio rimuove gli obsoleti;
oppure cancellarli da Garmin Connect. Riportare: numero di workout creati/pianificati, errori.

## T7 — Revisione settimanale (dopo 7 giorni di allenamento reale)

Chiedere all'assistente il check-in settimanale. Atteso: sessioni fatte/saltate corrette, eventuali
lenti troppo intensi, segnali di recupero, proposte sensate solo a settimana conclusa.
