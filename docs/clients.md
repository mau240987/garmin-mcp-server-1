# Race Coach su Claude, Gemini e ChatGPT

Un solo server MCP (`garmin-coach-mcp`), una sola fonte di istruzioni (`coach/race-coach.md`),
tre confezioni generate da `scripts/build_coach_packages.py` (la CI fallisce se non sono
allineate alla fonte).

| Client | Pacchetto | Stato di verifica (2026-11-02) |
|---|---|---|
| **Claude Code** | Plugin `plugins/garmin-race-coach` (server MCP + skill `race-coach`), marketplace `.claude-plugin/marketplace.json` | **Verificato**: `claude plugin validate` OK; `claude plugin details` vede 1 skill + 1 server MCP; prova end-to-end reale con `claude -p` (obiettivo → piano → export ics) riuscita, con avvertenze e nota di sicurezza come da skill. |
| **Claude Desktop / claude.ai** | Server MCP in `claude_desktop_config.json` + skill caricata come zip | **Non verificato** (nessun Claude Desktop nell'ambiente di sviluppo). Formato dello zip conforme alla documentazione ufficiale. |
| **Gemini CLI** | Estensione `extensions/gemini/garmin-race-coach` (`gemini-extension.json` + `GEMINI.md`) | **Non verificato** (nessuna Gemini CLI nell'ambiente). Manifest conforme al reference ufficiale (`mcpServers`, `contextFileName`, `settings`). |
| **ChatGPT** | `instructions/chatgpt/system-prompt.md` + server in modalità HTTP | **Non verificato**: la documentazione OpenAI non era raggiungibile dall'ambiente; vedi i limiti di sicurezza sotto. |

## Prerequisito comune

```bash
python3 -m venv ~/.garmin-coach-venv
~/.garmin-coach-venv/bin/pip install /percorso/del/repo     # oppure il wheel
ln -s ~/.garmin-coach-venv/bin/garmin-coach-mcp ~/.local/bin/  # il comando deve essere nel PATH
```

Chiave Personal API (letture ufficiali): crearla in Garmin Connect Web e impostare
`GARMIN_PERSONAL_API_KEY` (mai in chat). Per le scritture non ufficiali (facoltative, disattivate
di default) eseguire una volta `garmin-coach-mcp --login` e impostare
`GARMIN_ALLOW_UNOFFICIAL_WRITES=true`.

## Claude Code (plugin)

```bash
# dalla cartella del repo clonato
claude plugin marketplace add ./
claude plugin install garmin-race-coach@garmin-coach --scope user
# oppure, per provarlo senza installarlo:
claude --plugin-dir ./plugins/garmin-race-coach
```

Il server riceve dalla shell le variabili `GARMIN_PERSONAL_API_KEY`,
`GARMIN_PERSONAL_API_KEY_EXPIRES`, `GARMIN_ALLOW_UNOFFICIAL_READS`,
`GARMIN_ALLOW_UNOFFICIAL_WRITES`, `GARMIN_TOKEN_DIR`, `GARMIN_COACH_DATA_DIR` (vuote = default).
La skill si attiva parlando di gara/piano/allenamenti, o con `/garmin-race-coach:race-coach`.

## Claude Desktop / claude.ai

1. Server: in `claude_desktop_config.json`

   ```json
   {"mcpServers": {"garmin-coach": {
       "command": "/Users/tu/.garmin-coach-venv/bin/garmin-coach-mcp",
       "env": {"GARMIN_PERSONAL_API_KEY": "…"}}}}
   ```

2. Skill: `python3 scripts/build_coach_packages.py --zip build/` crea `build/race-coach.zip`
   (cartella `race-coach/` con `SKILL.md`, solo `name`, `description`, `license` nel frontmatter);
   caricarlo da *Customize → Skills*. Lo zip non contiene il server: serve il punto 1.

## Gemini CLI

```bash
gemini extensions install ./extensions/gemini/garmin-race-coach
gemini extensions config garmin-race-coach   # chiede chiave API (salvata nel portachiavi) e opzioni
```

Secondo il reference ufficiale delle estensioni (letto il 2026-11-02, non provato), le
estensioni ricevono solo le variabili di ambiente standard più quelle dichiarate nei `settings`;
per questo chiave e opzioni sono dichiarate lì (la chiave come `sensitive`, salvata nel
portachiavi). `gemini extensions config` è il comando indicato dal reference (non provato).
`GEMINI.md` contiene il workflow.

## ChatGPT

ChatGPT non avvia server locali via stdio: serve il server in modalità HTTP
(`garmin-coach-mcp --transport http --port 8000`) raggiungibile da OpenAI via HTTPS, e il
contenuto di `instructions/chatgpt/system-prompt.md` come istruzioni del progetto/GPT.

**Limite di sicurezza (bloccante per l'uso reale):** il server HTTP **non ha autenticazione**.
Esporlo su Internet (tunnel, IP pubblico) darebbe a chiunque conosca l'URL accesso ai tuoi dati
Garmin e, se abilitate, alle scritture. Finché non c'è l'autenticazione OAuth della Fase 2
(`docs/roadmap-multiuser.md`), l'uso con ChatGPT è sconsigliato; per una prova personale breve:
`GARMIN_READ_ONLY=true`, tunnel temporaneo, chiudere subito dopo. Nota: con l'host di default
`127.0.0.1` l'SDK MCP attiva la protezione da DNS rebinding (host consentiti: solo localhost),
quindi una richiesta che arriva da un tunnel con un Host pubblico verrà probabilmente rifiutata
(HTTP 421); **non** usare `--host 0.0.0.0` per aggirarla. Le opzioni di autenticazione
dei connettori ChatGPT e i piani che li supportano **non sono stati verificati** (documentazione
non raggiungibile da qui).
