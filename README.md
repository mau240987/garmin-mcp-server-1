# garmin-mcp-server

MCP Server for Garmin Connect — read activities, health metrics, and push structured training plans from any AI assistant that supports the [Model Context Protocol](https://modelcontextprotocol.io).

Built on the battle-tested [garminconnect](https://github.com/cyberjunky/python-garminconnect) Python library.

---

## Features

| Tool | Type | Description |
|---|---|---|
| `get_activities` | Read | Recent activities with distance, pace, HR, calories |
| `get_activity_details` | Read | Full detail: splits, laps, running dynamics |
| `get_health_summary` | Read | Steps, resting HR, HRV, stress, sleep, body battery, SpO2, training readiness |
| `get_training_status` | Read | Weekly volume: total km, session count, avg pace |
| `get_workouts` | Read | List workouts saved on Garmin Connect |
| `push_workout` | Write | Create a structured workout with dual targets (pace + HR) |
| `push_training_plan` | Write | Schedule a multi-day plan on the Garmin calendar |
| `delete_workout` | Write | Delete a specific workout |
| `delete_plan_workouts` | Write | Bulk-delete workouts by name pattern |

### Race Coach tools

| Tool | What it does |
|---|---|
| `set_race_goal` | Save the race goal: type/distance, date, optional target time, days per week, long-run day, constraints, unavailable dates. |
| `get_athlete_profile` | Deterministic profile from Garmin: weekly volume, longest run, HR-zone distribution (flags easy runs run too hard), easy pace, recovery signals (resting HR, HRV, sleep). |
| `generate_training_plan` | Phased plan (base/build/specific/taper) from the deterministic engine, rules in [`docs/plan-engine.md`](docs/plan-engine.md). Saved locally. |
| `review_week` | Planned vs done for a week, alerts, and suggested changes. |
| `apply_plan_update` | Bounded edits with revision history: scale a week down, skip/move/ease a session, regenerate from a date. |
| `export_plan` | `md`, `ics` (calendar) or `fit` (zip of Garmin FIT workout files). Works without Garmin write access. |
| `push_plan_to_garmin` | Send the next 1-4 weeks as structured workouts (dry run by default, confirmed count; needs `GARMIN_ALLOW_UNOFFICIAL_WRITES=true`). |

**Use it from Claude, Gemini or ChatGPT** — see [`docs/clients.md`](docs/clients.md):

```bash
# Claude Code: plugin with the MCP server and the race-coach skill
claude plugin marketplace add mrosano1987/garmin-mcp-server-1
claude plugin install garmin-race-coach@garmin-coach
# Gemini CLI (from a clone of this repo)
gemini extensions install ./extensions/gemini/garmin-race-coach
```

The coaching workflow and rules live in one file, [`coach/race-coach.md`](coach/race-coach.md);
`scripts/build_coach_packages.py` generates the Claude skill, `GEMINI.md` and the ChatGPT
instructions from it.

Goals, plans and exports live in `GARMIN_COACH_DATA_DIR` (default `~/.garmin-coach-mcp/data`),
never in the repository. Plans and profiles carry the Garmin attribution for derived data.

### Behaviour

- **Structured output**: every tool has a typed input schema and returns structured content
  (plus a readable `text` field). Failures are MCP tool errors (`isError: true`) with a
  `Next step:` hint, never "successful" strings containing an error.
- **Retries only where safe**: reads retry transient Garmin/network failures with bounded
  exponential backoff (`GARMIN_MAX_RETRIES`, default 2) and re-authenticate once on an expired
  session. Writes (create/schedule/delete) run **exactly once**, so a half-applied create is
  never duplicated.
- **Safe bulk delete**: `delete_plan_workouts` requires a pattern of at least 3 characters,
  is a **dry run by default**, and deletes only with `dry_run=false` plus `expected_count` equal
  to the dry-run match count. (The old version deleted *every* workout when called without a
  pattern.)
- **Login protection**: a failed login is never retried within a call and further calls fail
  fast for 60 s, to avoid account lockout from repeated SSO attempts.
- **Read-only mode**: `GARMIN_READ_ONLY=true` or `--read-only` does not register write tools.
- `get_health_summary` lists every metric it could not read in `unavailable`, with the reason.

---

## Setup

### Prerequisites

- Python 3.12+ (required by `garminconnect` 0.3.20)
- A Garmin Connect account
- A Garmin device (for workout sync)

```bash
git clone https://github.com/mau240987/garmin-mcp-server.git
cd garmin-mcp-server
python3 -m venv .venv && .venv/bin/pip install .
# installs the `garmin-coach-mcp` command (python -m garmin_coach_mcp also works)
```

### Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `GARMIN_EMAIL`, `GARMIN_PASSWORD` | — | Needed only for the first login. |
| `GARMIN_TOKEN_DIR` | `~/.garmin-coach-mcp/tokens` | Where session tokens are stored (`GARTH_TOKEN_DIR` still accepted). |
| `GARMIN_READ_ONLY` | `false` | Hide write tools. |
| `GARMIN_MAX_RETRIES` | `2` | Retries for read operations on transient errors. |
| `GARMIN_LOG_LEVEL` | `INFO` | Logs go to stderr only. |
| `GARMIN_PERSONAL_API_KEY` | — | Official Garmin Connect **Personal API** key. When set, reads use the official API. |
| `GARMIN_PERSONAL_API_KEY_EXPIRES` | — | Expiry date you chose for the key (`YYYY-MM-DD`); tools warn 14 days ahead. |
| `GARMIN_PERSONAL_API_REGION` | `global` | `cn` for Garmin China accounts. |
| `GARMIN_ALLOW_UNOFFICIAL_READS` | `false` | With a Personal key: let reads the official API lacks (Training Readiness, workout library) use the unofficial backend. |
| `GARMIN_ALLOW_UNOFFICIAL_WRITES` | `false` | Enable workout create/schedule/delete through the unofficial backend. **Off by default.** |

> **Behaviour change (2.0):** workout writes (`push_workout`, `push_training_plan`,
> `delete_workout`, `delete_plan_workouts`) are now **disabled by default**, also for existing
> email/password setups. They go through Garmin Connect's internal endpoints with your
> credentials, which may breach Garmin's terms and put the account at risk (see
> [`docs/api-coverage.md`](docs/api-coverage.md), risk R4). To keep using them, set
> `GARMIN_ALLOW_UNOFFICIAL_WRITES=true`. Garmin's official Training API needs a business
> (Starter) account: see [`docs/starter-requirements.md`](docs/starter-requirements.md).
> The write tools stay listed while disabled, so the assistant can explain how to enable them;
> calling them then reaches nothing on Garmin. Use `GARMIN_READ_ONLY=true` to hide them.

### Backends: official first

| Configuration | Reads | Writes (`push_*`, `delete_*`) |
|---|---|---|
| Only `GARMIN_EMAIL`/`GARMIN_PASSWORD` (or saved tokens) | unofficial (as before) | unofficial **only with** `GARMIN_ALLOW_UNOFFICIAL_WRITES=true` |
| Only `GARMIN_PERSONAL_API_KEY` | **official**; Training Readiness and `get_workouts` unavailable (explained in the result) | unavailable, with guidance |
| Both | **official**; the missing ones only with `GARMIN_ALLOW_UNOFFICIAL_READS=true` | unofficial **only with** `GARMIN_ALLOW_UNOFFICIAL_WRITES=true` |

Data served by the official API carries a `sources` entry with Garmin attribution. The
Personal API is read-only and for your own data only; create the key in Garmin Connect Web
(Personal API Key page). Details and evidence: [`docs/api-coverage.md`](docs/api-coverage.md).

---

## Option 1 — Claude Desktop / Claude Code (recommended)

The simplest and most reliable setup. Claude Desktop launches the Python script directly as a subprocess via **stdio** — no Docker, no ports, no intermediaries.

### Claude Desktop

Edit `claude_desktop_config.json`:

- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "garmin": {
      "command": "/full/path/to/garmin-mcp-server/.venv/bin/garmin-coach-mcp",
      "env": {
        "GARMIN_EMAIL": "your@email.com",
        "GARMIN_PASSWORD": "yourpassword"
      }
    }
  }
}
```

Existing configs that run `python3 /full/path/to/garmin_mcp_server.py` keep working (that file
is now a thin wrapper), as long as that `python3` has the dependencies installed.

Fully restart Claude Desktop (`Cmd+Q` on macOS, not just close the window), then reopen it. The Garmin tools will appear under the 🔨 icon in the input bar.

> After the first successful login, tokens are saved to `~/.garmin-coach-mcp/tokens`.
> You can then remove the credentials from the config — the server resumes the session
> automatically. Tokens created by older versions (`~/.garth`, garth format) are no longer
> supported by `garminconnect` ≥ 0.3.20: log in once more.

### Claude Code

```bash
export GARMIN_EMAIL="your@email.com"
export GARMIN_PASSWORD="yourpassword"
claude mcp add garmin -- /full/path/to/garmin-mcp-server/.venv/bin/garmin-coach-mcp
```

---

## Option 2 — Docker (HTTP mode, remote clients)

Best for hosting the server as a persistent HTTP service — useful for remote AI clients or if you want to keep the server always running independently of Claude Desktop.

> **Note:** Do not use Docker + mcp-remote for Claude Desktop. The bridge
> outputs log lines to stdout that Claude Desktop tries to parse as JSON,
> causing connection errors. Use Option 1 for Claude Desktop.

### Build

```bash
docker build -t garmin-mcp-server .
# or with buildx:
docker buildx build -t garmin-mcp-server .
```

### First run (credentials required)

```bash
docker run -d \
  --name garmin-mcp \
  -p 8000:8000 \
  -e GARMIN_EMAIL=your@email.com \
  -e GARMIN_PASSWORD=yourpassword \
  -v garmin-tokens:/data/tokens \
  garmin-mcp-server
```

### Subsequent runs (tokens already saved)

```bash
docker run -d \
  --name garmin-mcp \
  -p 8000:8000 \
  -v garmin-tokens:/data/tokens \
  garmin-mcp-server
```

The server starts at `http://localhost:8000/mcp`.

### Or with Docker Compose

```bash
GARMIN_EMAIL=your@email.com GARMIN_PASSWORD=yourpassword docker compose up -d
```

---

## Authentication flow

```
First run                          Subsequent runs
─────────────────────              ────────────────────
GARMIN_EMAIL + PASSWORD            (not needed)
        │                                │
        ▼                                ▼
  garminconnect login            tokens on disk
        │                (~/.garmin-coach-mcp/tokens or volume)
        ▼                                │
  Save OAuth tokens ──────────────►  Resume session
        │                                │
        ▼                                ▼
   Ready to use                    Ready to use
```

Credentials are only needed **once**. After the first login, session tokens
are persisted by `garminconnect`. No passwords are ever stored on disk.

If your account has **MFA** enabled, run the interactive login once first:

```bash
garmin-coach-mcp --login     # prompts for email, password and MFA code; stores tokens
```

---

## Workout step format

The `push_workout` tool takes a typed `steps` array (the input schema documents every field;
the old JSON-string parameter `steps_json` is still accepted but deprecated). Units:
time in seconds, distance in meters, paces as `M:SS` per km (`pace_low` = slower).

```json
[
  {
    "type": "warmup",
    "duration_type": "time",
    "duration_value": 600,
    "pace_low": "7:05",
    "pace_high": "6:25",
    "hr_low": 120,
    "hr_high": 140,
    "primary": "hr"
  },
  {
    "type": "repeat",
    "repeat_count": 4,
    "steps": [
      {
        "type": "run",
        "duration_type": "distance",
        "duration_value": 1000,
        "pace_low": "5:10",
        "pace_high": "4:50",
        "primary": "pace"
      },
      {
        "type": "recover",
        "duration_type": "time",
        "duration_value": 120,
        "hr_low": 120,
        "hr_high": 145,
        "primary": "hr"
      }
    ]
  },
  {
    "type": "cooldown",
    "duration_type": "time",
    "duration_value": 600,
    "pace_low": "7:30",
    "pace_high": "6:30",
    "primary": "hr"
  }
]
```

### Step types

| Type | Description |
|---|---|
| `warmup` | Warm-up phase |
| `run` | Main interval / work phase |
| `recover` | Recovery between intervals |
| `cooldown` | Cool-down phase |
| `rest` | Full rest (standing) |
| `repeat` | Repeat group — wraps nested `steps` × `repeat_count` |

### Duration types

| Type | `duration_value` unit |
|---|---|
| `time` | seconds |
| `distance` | meters |
| `open` | lap button press |

### Target priority

| `primary` | Behavior on watch |
|---|---|
| `"hr"` | Heart rate is the main gauge, pace is secondary |
| `"pace"` | Pace is the main gauge, HR is secondary |
| `"none"` | No target (feel-based effort) |

Both targets are shown simultaneously on the watch. The `targetValueOne/Two`
fields are hoisted to the step root level — required for Garmin to correctly
parse dual targets.

---

## Example prompts

```
"Show me my last 10 runs with pace and heart rate"

"How has my sleep been this week?"

"Create a tempo run for tomorrow: 15 min warmup,
 4x1km at 5:00/km with 2min recovery, 10 min cooldown.
 HR primary on easy parts, pace primary on intervals."

"Push a 4-week training plan starting Monday: Tuesday easy 10km,
 Thursday progressive 12km, Sunday long run growing from 20 to 30km."

"What's my training load for the last 30 days?"

"Delete all workouts from the old plan and re-upload the new ones."
```

---

## Troubleshooting

**`ModuleNotFoundError`** (including `mcp.server.fastmcp`, removed in MCP SDK 2.x) — install
this package in the environment Claude launches:
```bash
pip install .
# if using a venv, use its full python path in the config:
# "command": "/Users/you/garmin-mcp-server/.venv/bin/python3"
```

**`ConfigurationError: No saved Garmin session and no credentials configured`** — no saved tokens and no env vars set.
Add `GARMIN_EMAIL` and `GARMIN_PASSWORD` to the config for the first login.

**`MFA / 2FA error`** — run the interactive login once to complete MFA:
```bash
garmin-coach-mcp --login     # prompts for email, password and MFA code; stores tokens
```

**Tools not appearing in Claude Desktop** — make sure you fully quit (`Cmd+Q`)
and restart Claude Desktop after editing the config.

---

## Garmin API coverage

An analysis of which tools can move to Garmin's official Connect Personal API, and what
remains unofficial, is in [`docs/api-coverage.md`](docs/api-coverage.md).

## Development

```bash
pip install -e ".[dev]"
ruff check src tests && ruff format --check src tests && mypy && pytest
```

Tests use a fake Garmin client: no network, no credentials, no personal data.

## Disclaimer

This project is not affiliated with, endorsed by, or sponsored by Garmin Ltd.
It uses `garminconnect`, which relies on reverse-engineered endpoints.
Use at your own risk, for personal use only.

## License

AGPL-3.0
