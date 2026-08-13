# garmin-coach-mcp

**Your Garmin Connect account as an MCP server: read your activities and health
metrics, and build structured training plans that sync to your watch.**

[![CI](https://github.com/mrosano1987/garmin-mcp-server-1/actions/workflows/ci.yml/badge.svg)](https://github.com/mrosano1987/garmin-mcp-server-1/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/garmin-coach-mcp.svg)](https://pypi.org/project/garmin-coach-mcp/)
[![Python versions](https://img.shields.io/pypi/pyversions/garmin-coach-mcp.svg)](https://pypi.org/project/garmin-coach-mcp/)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)](LICENSE)

[Model Context Protocol](https://modelcontextprotocol.io) is the standard that
lets an AI assistant call external tools. This server is one of those tools. Add
it to Claude Desktop, Claude Code, VS Code or any other MCP client, and the
assistant can look at your training history and write workouts to your Garmin
calendar.

---

## What it does

**Reads your data**

- Completed activities — distance, duration, pace, heart rate, calories, with
  server-side filtering by activity type.
- One activity in detail — laps and splits, running dynamics (cadence, stride
  length, ground contact time, vertical oscillation), training effect, VO2max.
- Daily health metrics for a date — steps, resting heart rate, HRV, stress,
  sleep, body battery, SpO2, training readiness. Each metric reports whether it
  was recorded, not recorded, or failed to fetch. It never reports a failed
  request as "no data".
- Training load over a window — running volume, session count, average pace,
  longest run, activity mix.
- Your saved workout library, and any single workout's step structure.

**Writes to your Garmin calendar**

- Create a structured running workout from typed, nested steps: warmup,
  intervals, recoveries, cooldowns, with repeat groups and simultaneous pace and
  heart-rate targets.
- Schedule a workout on a date.
- Push a multi-day training plan — many workouts, each on its own date.
- Delete a workout, or a filtered set of workouts with a mandatory dry run
  first.

**Built to be safe to hand to a language model**

- `--read-only` does not merely reject writes; it never registers the write
  tools, so they do not appear in the tool list at all.
- Every tool carries MCP annotations (`readOnlyHint`, `destructiveHint`,
  `idempotentHint`), so a client can warn you before a destructive call.
- Workout steps are typed, nested objects, not a JSON string. The client
  validates a workout against the published schema before the call is made.
- Failures raise protocol-level errors carrying a remediation. Nothing returns
  an error message dressed up as a successful result.
- Credentials and tokens are never logged, and all logging goes to stderr.

---

## Install and configure

**Prerequisites**

- A Garmin Connect account. (A Garmin watch too, if you want workouts to sync to
  your wrist.)
- Python 3.10 or newer — unless you use `uvx` or Docker, which bring their own.
- [uv](https://docs.astral.sh/uv/) for the zero-install path:
  `curl -LsSf https://astral.sh/uv/install.sh | sh`

`uvx garmin-coach-mcp` downloads and runs the server without installing
anything permanently. That is the path used in the configuration blocks below.
If you prefer a persistent install, `pipx install garmin-coach-mcp` or
`pip install garmin-coach-mcp` both give you a `garmin-coach-mcp` command, which
you can substitute for `uvx garmin-coach-mcp` anywhere below.

Before wiring up a client, check that it runs and that your account signs in:

```bash
GARMIN_EMAIL='you@example.com' GARMIN_PASSWORD='your-password' \
  uvx garmin-coach-mcp --check
```

`--check` inspects the interpreter, the dependencies, your settings, the token
store and tool registration, performs a real Garmin sign-in, then exits. Exit
code 0 means healthy. It writes only to stderr and never starts the server.

Once that passes, tokens are cached and you can drop the credentials — see
[Authentication](#authentication).

### Claude Desktop

Edit `claude_desktop_config.json`:

- **macOS** — `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows** — `%APPDATA%\Claude\claude_desktop_config.json`
- **Linux** — `~/.config/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "garmin": {
      "command": "uvx",
      "args": ["garmin-coach-mcp"],
      "env": {
        "GARMIN_EMAIL": "you@example.com",
        "GARMIN_PASSWORD": "your-password"
      }
    }
  }
}
```

Quit Claude Desktop completely and reopen it — on macOS that is `Cmd+Q`, not
closing the window. The Garmin tools then appear under the tools icon in the
message box.

After the first successful call, delete the `env` block. The server resumes from
the cached tokens:

```json
{
  "mcpServers": {
    "garmin": {
      "command": "uvx",
      "args": ["garmin-coach-mcp", "--read-only"]
    }
  }
}
```

`--read-only` is worth keeping unless you actually want the assistant creating
and deleting workouts. See [Security and safety](#security-and-safety).

> If `uvx` is not found, Claude Desktop is not seeing your shell's `PATH`. Use
> the absolute path instead — run `which uvx` (macOS/Linux) or `where uvx`
> (Windows) and put the result in `"command"`.

### Claude Code

```bash
claude mcp add garmin \
  --env GARMIN_EMAIL=you@example.com \
  --env GARMIN_PASSWORD=your-password \
  -- uvx garmin-coach-mcp
```

Everything after `--` is the command Claude Code launches. Verify with
`claude mcp list`, and remove with `claude mcp remove garmin`.

Once tokens are cached, re-add it without the credentials and read-only:

```bash
claude mcp remove garmin
claude mcp add garmin -- uvx garmin-coach-mcp --read-only
```

### VS Code

Create `.vscode/mcp.json` in your workspace, or run **MCP: Open User
Configuration** from the Command Palette to configure it for every workspace.

This block prompts for your credentials and stores them in VS Code's secret
storage rather than in a file:

```json
{
  "inputs": [
    {
      "type": "promptString",
      "id": "garmin_email",
      "description": "Garmin Connect email"
    },
    {
      "type": "promptString",
      "id": "garmin_password",
      "description": "Garmin Connect password",
      "password": true
    }
  ],
  "servers": {
    "garmin": {
      "type": "stdio",
      "command": "uvx",
      "args": ["garmin-coach-mcp"],
      "env": {
        "GARMIN_EMAIL": "${input:garmin_email}",
        "GARMIN_PASSWORD": "${input:garmin_password}"
      }
    }
  }
}
```

After the first login you can drop `inputs` and the `env` block entirely.

### Docker

No image is published to a registry yet, so build it from the repository:

```bash
git clone https://github.com/mrosano1987/garmin-mcp-server-1.git
cd garmin-mcp-server-1
docker build -t garmin-coach-mcp .
```

The image defaults to the HTTP transport on `0.0.0.0:8000`, and caches tokens in
`/data/tokens`. **Mount a volume there or your tokens are lost when the
container is removed, and you will be logging in with a password every time.**

First run, with credentials:

```bash
docker run -d --name garmin-mcp \
  -p 127.0.0.1:8000:8000 \
  -e GARMIN_EMAIL=you@example.com \
  -e GARMIN_PASSWORD=your-password \
  -v garmin-tokens:/data/tokens \
  garmin-coach-mcp
```

Later runs, no credentials:

```bash
docker run -d --name garmin-mcp \
  -p 127.0.0.1:8000:8000 \
  -v garmin-tokens:/data/tokens \
  -e GARMIN_READ_ONLY=true \
  garmin-coach-mcp
```

The endpoint is `http://127.0.0.1:8000/mcp`. The port is bound to loopback on
purpose — the MCP endpoint has **no authentication of its own**, so anyone who
can reach it controls your Garmin account.

Verify the setup without starting the server:

```bash
docker run --rm \
  -e GARMIN_EMAIL=you@example.com -e GARMIN_PASSWORD=your-password \
  -v garmin-tokens:/data/tokens \
  garmin-coach-mcp --check
```

Anything after the image name is passed straight to the CLI.

**Docker Compose** is set up in the repository:

```bash
cp .env.example .env          # fill in GARMIN_EMAIL / GARMIN_PASSWORD
docker compose run --rm garmin-coach-mcp --check
docker compose up -d
```

Compose already binds to `127.0.0.1:8000` and persists tokens in a
`garmin-tokens` volume. Once `--check` passes, clear the credentials from
`.env`.

**Connecting a stdio-only client to the HTTP server.** Claude Desktop speaks
stdio. To reach a containerised server it needs a bridge:

```json
{
  "mcpServers": {
    "garmin": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://127.0.0.1:8000/mcp"]
    }
  }
}
```

This adds a moving part for no benefit on a single machine. Prefer the direct
`uvx` configuration above for desktop clients, and keep Docker for when you
genuinely want a long-running service.

---

## Authentication

### How it works

Garmin has no API keys or OAuth apps for third parties. Signing in means using
your real account password — once.

1. **First run.** You supply `GARMIN_EMAIL` and `GARMIN_PASSWORD`. The server
   signs in and exchanges them for OAuth tokens.
2. **Tokens are written** to the token directory, default
   `~/.garmin-coach-mcp/tokens`.
3. **Every run after that** resumes from those tokens. No password is involved.
   They last roughly a year, and the server refreshes them as it goes.

Your password is never written to disk by this server, and neither password nor
tokens ever appear in log output.

### Remove your credentials once tokens exist

This is the single most important step in this section.

```bash
# 1. Log in once and confirm it worked
GARMIN_EMAIL='you@example.com' GARMIN_PASSWORD='your-password' \
  uvx garmin-coach-mcp --check

# 2. Confirm tokens landed
ls -la ~/.garmin-coach-mcp/tokens

# 3. Delete the credentials from your client config, then confirm it still works
uvx garmin-coach-mcp --check
```

Leaving `GARMIN_PASSWORD` in `claude_desktop_config.json` is not a good
end state. That file is plain text, is often world-readable, gets synced and
backed up, and is routinely pasted into bug reports. The server does not need it
after the first login.

The tokens themselves are bearer credentials — anyone who can read that
directory can act as you on Garmin Connect without your password. Lock it down:

```bash
chmod 700 ~/.garmin-coach-mcp
```

### Multi-factor authentication

**If your Garmin account has MFA enabled, the server cannot complete the first
login on its own.** It runs non-interactively, with no way to prompt you for a
code and no way to receive one.

Do the first login by hand, in a terminal, and let it write tokens to the same
directory the server will read:

```bash
uv run --with garminconnect python - <<'PY'
from garminconnect import Garmin
from pathlib import Path

tokens = Path("~/.garmin-coach-mcp/tokens").expanduser()
tokens.mkdir(parents=True, exist_ok=True)

g = Garmin("you@example.com", "your-password", prompt_mfa=lambda: input("MFA code: "))
g.login()
g.garth.dump(str(tokens))
print(f"Tokens written to {tokens}")
PY
```

Enter the code when prompted. From then on the server resumes from those tokens
like any other setup, and you never give it your credentials at all.

Redo this roughly once a year, when the tokens expire.

### Moving or sharing a token directory

The token directory is portable. To reuse a login somewhere else — a container,
another machine — copy the directory and point `GARMIN_TOKEN_DIR` at it. This is
also how you migrate from the pre-0.1.0 layout:

```bash
mkdir -p ~/.garmin-coach-mcp/tokens
cp ~/.garth/* ~/.garmin-coach-mcp/tokens/
```

The old `GARTH_TOKEN_DIR` variable is still honoured as a deprecated alias for
`GARMIN_TOKEN_DIR`, and warns when used.

Treat a copied token directory exactly like a copied password.

---

## Tool reference

`R` = read-only, safe to call freely. `W` = writes to your Garmin account.
`D` = destructive, deletes data permanently. Tools marked `W`/`D` are **not
registered at all** when the server runs with `--read-only`.

| Tool | Kind | What it does | Key parameters |
|---|---|---|---|
| `get_activities` | R | Recent completed activities with distance, pace, heart rate and calories. | `limit`, `start`, `activity_type` |
| `get_activity` | R | One activity in full: laps, splits, running dynamics, training effect. | `activity_id` |
| `get_health_summary` | R | Daily metrics for one date: steps, resting HR, HRV, stress, sleep, body battery, SpO2, training readiness. | `date` |
| `get_training_load` | R | Aggregated volume over a recent window: distance, sessions, average pace, longest run. | `days` |
| `list_workouts` | R | The saved workout library, optionally filtered by name. | `limit`, `start`, `name_contains` |
| `get_workout` | R | One saved workout with its step structure, repeats expanded. | `workout_id` |
| `create_workout` | W | Create a structured running workout, optionally scheduling it. | `workout`, `schedule_date` |
| `schedule_workout` | W | Put an existing workout on a calendar date. | `workout_id`, `date` |
| `create_training_plan` | W | Create and schedule many workouts across a date range. | `plan_name`, `start_date`, `workouts` |
| `delete_workout` | D | Permanently delete one workout by ID. | `workout_id` |
| `delete_workouts` | D | Delete every workout matching a name filter. Dry run unless confirmed. | `name_contains`, `confirm` |

Exact parameter names, constraints and output fields are published in the tool
schemas — your MCP client shows them, and `get_workout`-style tools describe
each field inline. What follows is detail for the tools whose shape is not
obvious.

### `create_workout`

Takes a whole workout as one nested object. There is no JSON-string parameter:
the steps are typed, so your client validates them before the call leaves.

A step is either an **executable step** (`type` is `warmup`, `run`, `recover`,
`cooldown` or `rest`) or a **repeat block** (`type` is `repeat`, wrapping nested
`steps`). Repeat blocks are how intervals are expressed.

Worked example — 15 min warmup, 6 × 800 m at pace with 2:30 jog recovery, 10 min
cooldown, scheduled for a date:

```json
{
  "workout": {
    "name": "W1 Tue - 6x800m @ 3:55",
    "description": "VO2max session. Full recovery between reps.",
    "steps": [
      {
        "type": "warmup",
        "duration_type": "time",
        "duration_seconds": 900,
        "heart_rate_low": 120,
        "heart_rate_high": 140,
        "target_priority": "heart_rate",
        "description": "Easy, build gradually"
      },
      {
        "type": "repeat",
        "repeat_count": 6,
        "steps": [
          {
            "type": "run",
            "duration_type": "distance",
            "distance_meters": 800,
            "pace_slow": "4:00",
            "pace_fast": "3:50",
            "heart_rate_low": 165,
            "heart_rate_high": 180,
            "target_priority": "pace"
          },
          {
            "type": "recover",
            "duration_type": "time",
            "duration_seconds": 150,
            "heart_rate_low": 110,
            "heart_rate_high": 140
          }
        ]
      },
      {
        "type": "cooldown",
        "duration_type": "time",
        "duration_seconds": 600,
        "pace_slow": "7:30",
        "pace_fast": "6:30"
      }
    ]
  },
  "schedule_date": "2026-03-10"
}
```

Reading that back:

- The **warmup** ends after 900 seconds. It targets 120–140 bpm, and heart rate
  is the primary gauge.
- The **repeat block** runs its two steps six times.
- Each **work interval** ends after 800 metres and carries both a pace range and
  a heart-rate range. `target_priority: "pace"` makes pace the main gauge on the
  watch; heart rate is shown as secondary.
- Each **recovery** is 150 seconds with a heart-rate range. `target_priority` is
  omitted, so it is inferred — heart rate wins when an HR range is present.
- The **cooldown** ends after 600 seconds, pace-targeted.
- `schedule_date` puts it on the calendar. Omit it to leave the workout in the
  library, unscheduled.

Three rules trip people up:

1. **`pace_slow` must be the numerically larger value.** The bounds are named by
   effort, not by number: `4:00` is slower than `3:50`. Reversing them is a
   validation error, not a silently inverted target.
2. **Duration fields must match `duration_type`.** `time` requires
   `duration_seconds` and forbids `distance_meters`; `distance` is the mirror
   image; `lap_button` takes neither and ends when the athlete presses the lap
   button.
3. **Pace and heart-rate bounds come in pairs.** `pace_slow` without `pace_fast`
   is rejected, as is `heart_rate_low` without `heart_rate_high`.

`estimated_duration_minutes` is optional. Omit it and it is computed from the
steps.

Full field reference, limits and more examples: [docs/workouts.md](docs/workouts.md).

### `create_training_plan`

Creates and schedules many workouts in one call. Each entry is a
`WorkoutDefinition` — the same object `create_workout` takes — plus a
`day_offset` counted from the plan's `start_date`. Offset `0` is the start date
itself.

```json
{
  "plan_name": "Marathon block - week 1",
  "start_date": "2026-03-09",
  "workouts": [
    {
      "day_offset": 1,
      "workout": {
        "name": "W1 Tue - 6x800m",
        "steps": [
          {
            "type": "warmup",
            "duration_type": "time",
            "duration_seconds": 900,
            "heart_rate_low": 120,
            "heart_rate_high": 140
          },
          {
            "type": "repeat",
            "repeat_count": 6,
            "steps": [
              {
                "type": "run",
                "duration_type": "distance",
                "distance_meters": 800,
                "pace_slow": "4:00",
                "pace_fast": "3:50"
              },
              {
                "type": "recover",
                "duration_type": "time",
                "duration_seconds": 150
              }
            ]
          },
          {
            "type": "cooldown",
            "duration_type": "time",
            "duration_seconds": 600
          }
        ]
      }
    },
    {
      "day_offset": 5,
      "workout": {
        "name": "W1 Sun - 24km long run",
        "steps": [
          {
            "type": "run",
            "duration_type": "distance",
            "distance_meters": 24000,
            "heart_rate_low": 130,
            "heart_rate_high": 148,
            "description": "Conversational. Do not race this."
          }
        ]
      }
    }
  ]
}
```

**This operation is not atomic.** Each workout is uploaded independently and is
not retried, because retrying a create that partially applied would create it
twice. If workout four of eight fails, the first three still exist on your
calendar. The result reports every workout's outcome individually — read
`created`, `failed`, and the per-workout `outcomes` list, and re-push only the
failures.

Give every workout in a plan a shared prefix (`W1 `, `Base block - `). It makes
them findable in the Garmin app, and it gives `delete_workouts` a safe filter if
you need to tear the plan down.

### `delete_workouts`

Deletes every workout whose name contains a substring. This is the sharpest tool
here, and it is deliberately awkward:

- `name_contains` cannot be empty. In the previous version of this server,
  calling it with no argument deleted **every workout in the account**. That is
  now rejected.
- It is a **dry run by default**. The first call returns every matching workout
  with `deleted: false` and `dry_run: true`, and deletes nothing.
- Only a second call with `confirm: true` actually deletes.

Read the dry-run list before confirming. A filter like `"run"` matches far more
than you expect. Deletion cannot be undone from this server.

### `get_health_summary`

Returns the full metric set every time, with a per-metric `status`:

- `ok` — Garmin returned a value.
- `not_recorded` — Garmin answered and had nothing for that date. The watch was
  off the wrist, or the device does not measure it.
- `fetch_failed` — the request itself failed, so the true value is unknown.

The distinction is the point. The previous version collapsed all three into a
missing line, so an expired token or a rate limit looked exactly like a rest
day.

---

## Security and safety

Read this before you connect the write tools to an assistant. There is a fuller
treatment in [SECURITY.md](SECURITY.md).

### What the server can reach

Garmin authentication is account-level. There is no scope, no read-only key, no
per-tool grant. Once the server has a session it can read everything on the
account — every activity, and health data including sleep, heart rate, HRV,
stress and SpO2 — and, unless read-only, create, schedule and permanently delete
workouts.

**Deletion is not reversible from this server.** There is no undo and no trash.

### Which tools are destructive

| Tool | Effect |
|---|---|
| `create_workout` | Adds a workout, optionally to your calendar. Reversible by deleting it. |
| `schedule_workout` | Adds a calendar entry. Reversible. |
| `create_training_plan` | Adds many workouts and calendar entries. Not atomic; partial success is possible. |
| `delete_workout` | **Permanently deletes one workout.** |
| `delete_workouts` | **Permanently deletes every workout matching a filter.** Dry run first; `confirm: true` required. |

Every tool declares MCP annotations, so a well-behaved client can prompt before
calling anything with `destructiveHint: true`. Do not rely on that alone —
clients vary.

### `--read-only`

```bash
garmin-coach-mcp --read-only
# or
GARMIN_READ_ONLY=true garmin-coach-mcp
```

In read-only mode the write tools are **never registered**. They are absent from
`tools/list`, so the model cannot see them, cannot be talked into calling them,
and gets a protocol-level "unknown tool" if something tries. This is a
structural guarantee, not a runtime check that could be argued around.

Use read-only for anything analytical — reviewing training, asking about sleep,
comparing weeks. Turn writes on deliberately, for the session where you actually
want workouts created.

`--no-read-only` overrides `GARMIN_READ_ONLY=true` from the environment, which
is useful for a one-off.

### Credentials and tokens

Covered in [Authentication](#authentication), and it comes down to one habit:
log in once, then delete the credentials from your config. The cached tokens are
what needs protecting after that — treat the token directory like a password
file and `chmod 700` it.

### The HTTP transport has no authentication

`--transport http` opens an unauthenticated MCP endpoint. Anyone who can reach
the port controls the Garmin account behind it. Bind it to `127.0.0.1` — that is
the default, and the Compose file keeps it that way — unless you have
deliberately put an authenticating proxy in front. Never expose it to the
internet.

For desktop clients, use the default stdio transport. The server runs as a
subprocess of the client and no port is opened at all.

### Unofficial API

This project is not affiliated with, endorsed by, or sponsored by Garmin Ltd. It
reaches Garmin Connect through
[`garminconnect`](https://github.com/cyberjunky/python-garminconnect), which uses
undocumented endpoints. Garmin can change them without notice, aggressive use
can get an account rate-limited, and your credentials pass through third-party
code. Personal use only, on accounts you own.

---

## Configuration reference

Precedence, highest first: **command-line flag → environment variable →
built-in default.**

### Command-line flags

| Flag | Environment variable | Default | Description |
|---|---|---|---|
| `--transport {stdio,http,streamable-http,sse}` | `GARMIN_TRANSPORT` | `stdio` | Transport to serve on. `http` is an alias for `streamable-http`. `sse` is the legacy transport, for older clients. |
| `--host ADDR` | `GARMIN_HOST` | `127.0.0.1` | Bind address, http/sse only. Use `0.0.0.0` inside a container. |
| `--port PORT` | `GARMIN_PORT`, then `PORT` | `8000` | Bind port, http/sse only. 1–65535. |
| `--token-dir DIR` | `GARMIN_TOKEN_DIR` | `~/.garmin-coach-mcp/tokens` | Where cached OAuth tokens live. Created on first login. |
| `--read-only` / `--no-read-only` | `GARMIN_READ_ONLY` | off | Register only the read tools. |
| `--log-level LEVEL` | `GARMIN_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`. Always to stderr. |
| `--check` | — | — | Run diagnostics and a real sign-in, then exit. `0` means healthy. |
| `-V`, `--version` | — | — | Print the version and exit. |
| `-h`, `--help` | — | — | Full help, with examples. |

### Environment-only variables

| Variable | Default | Description |
|---|---|---|
| `GARMIN_EMAIL` | — | Account email. First login only. |
| `GARMIN_PASSWORD` | — | Account password. First login only; never written to disk. |
| `GARMIN_REQUEST_TIMEOUT` | `20` | Per-request timeout in seconds against Garmin. Range 0.1–600. |
| `GARMIN_MAX_RETRIES` | `3` | Retry attempts for idempotent reads. Range 0–10; `0` disables retries. Writes are never blindly retried. |
| `GARTH_TOKEN_DIR` | — | **Deprecated** alias for `GARMIN_TOKEN_DIR`. Still read; warns. `GARMIN_TOKEN_DIR` wins if both are set. |

Booleans accept `1`, `true`, `t`, `yes`, `y`, `on` and their negatives `0`,
`false`, `f`, `no`, `n`, `off`. Case does not matter. An empty value means unset.

A bad value fails at startup with a message naming the variable and telling you
the valid range. It does not silently fall back to a default.

`.env.example` in the repository documents every variable inline; copy it to
`.env` for Docker Compose.

### Docker image defaults

The image overrides some defaults so it works as a service out of the box:
`GARMIN_TRANSPORT=streamable-http`, `GARMIN_HOST=0.0.0.0`, `GARMIN_PORT=8000`,
`GARMIN_TOKEN_DIR=/data/tokens`. Override any of them with `-e`, or append CLI
flags after the image name.

---

## Troubleshooting

Start here for anything: add `--log-level DEBUG` and read stderr. It logs every
Garmin call with its duration, credentials redacted.

### "No saved Garmin session and no credentials provided"

Neither a token directory nor `GARMIN_EMAIL`/`GARMIN_PASSWORD` was found. Either
the first login never happened, or the server is reading a different token
directory than the one you populated.

```bash
uvx garmin-coach-mcp --check          # says which token dir it is using
ls -la ~/.garmin-coach-mcp/tokens     # is anything there?
```

Common causes: you removed the credentials before the first successful call; the
client launches the server with a different `HOME` so `~` resolves elsewhere; or
you are in Docker without a volume mounted at `/data/tokens`. Pass `--token-dir`
explicitly to remove the ambiguity.

### Authentication fails, or worked yesterday and does not today

Tokens expire after roughly a year, and Garmin invalidates them if you change
your password. Supply the credentials once more:

```bash
GARMIN_EMAIL='you@example.com' GARMIN_PASSWORD='your-password' \
  uvx garmin-coach-mcp --check
```

If that fails too, delete the token directory and try again — a half-written
token file signs in about as well as no token file:

```bash
rm -rf ~/.garmin-coach-mcp/tokens
```

Check the account still signs in at [connect.garmin.com](https://connect.garmin.com).
Repeated failed logins can get an account temporarily locked.

### MFA / 2FA errors

The server runs non-interactively and cannot answer an MFA prompt. Do the first
login by hand using the script in [Multi-factor authentication](#multi-factor-authentication),
writing tokens to the directory the server reads. After that, MFA is not
involved.

### Rate limiting — 429, or "too many requests"

Garmin rate-limits aggressively, and there is no published budget. Reads are
retried with exponential backoff (`GARMIN_MAX_RETRIES`, default 3), but retries
cannot manufacture quota.

If you are hitting it: you are probably asking for large activity pages in a
loop, or pushing a big training plan. Lower `limit`, split the plan, and wait a
few minutes. Raising `GARMIN_MAX_RETRIES` mostly makes each tool call take
longer before failing.

### Tools do not appear in the client

- **Claude Desktop**: fully quit (`Cmd+Q` on macOS) and reopen. Reloading the
  window is not enough. Check the client's MCP log for a startup error.
- **`uvx` not found**: the client does not inherit your shell `PATH`. Put the
  absolute path from `which uvx` in `"command"`.
- **Only read tools appear**: the server is running with `--read-only` or
  `GARMIN_READ_ONLY=true`. That is what read-only means — the write tools are
  never registered.
- **Nothing at all**: run the exact command from your config in a terminal. A
  server that fails at startup shows the reason on stderr, and the client
  usually hides it.

### stdio vs HTTP confusion

Two transports, and mixing them up is the most common setup failure.

- **stdio** is the default and what desktop clients want. The client launches the
  server as a subprocess and talks over stdin/stdout. No port, no URL.
- **HTTP** (`--transport http`) is a long-running service on
  `http://HOST:PORT/mcp`. Use it for Docker or remote access.

Symptoms of a mismatch:

- A client configured with a URL, against a server started without
  `--transport http` — nothing is listening. Nothing will ever connect.
- A client configured with `"command"`, pointed at an already-running HTTP
  server — it launches a *second*, stdio server that ignores your port.
- Connection errors through `mcp-remote`, or a session that dies immediately.
  Usually the bridge is fine and the server is not running with `--transport
  http`. `curl -i http://127.0.0.1:8000/mcp` should get an HTTP response, not
  "connection refused".

If your client can launch a subprocess, use stdio. It is simpler and has no
unauthenticated port.

### Corrupted stdio session, or "unexpected token" from the client

Under stdio, stdout carries the protocol. Anything else written there corrupts
the session. This server sends every log line and diagnostic to stderr, so if
you see this, something else in the pipeline is printing to stdout — a wrapper
script, a shell profile that echoes a banner, or a bridge tool. Run the server
command directly, without the wrapper, to confirm.

### Tokens do not persist in Docker

The container writes tokens to `/data/tokens`. Without a volume there, they
vanish when the container is removed, and every start needs a password again.

```bash
docker run -d -v garmin-tokens:/data/tokens ...   # named volume — do this
docker volume inspect garmin-tokens               # confirm it exists
```

Also check:

- **The volume is mounted at exactly `/data/tokens`.** A mount at `/data` or
  `/tokens` does not count.
- **You did not override `GARMIN_TOKEN_DIR`** to a path outside the volume.
- **File ownership.** The image runs as a non-root user (`uid 10001`). A
  bind-mounted host directory owned by someone else is not writable:
  `chown -R 10001:10001 ./tokens` on the host, or use a named volume, which
  Docker sets up correctly on its own.
- **`docker compose down -v` deletes the volume.** Use `docker compose down`.

Confirm tokens landed:

```bash
docker run --rm -v garmin-tokens:/data/tokens --entrypoint ls \
  garmin-coach-mcp -la /data/tokens
```

### `ModuleNotFoundError`, or the command is not found

`uvx garmin-coach-mcp` sidesteps this by managing its own environment. If you
installed with `pip` into a virtualenv, the client must use that virtualenv's
interpreter — put the absolute path to `.venv/bin/garmin-coach-mcp` in
`"command"`, or use `pipx install garmin-coach-mcp`, which puts a working
executable on your `PATH`.

### A tool that used to work now fails

Garmin changes undocumented endpoints without notice. If a specific tool started
failing with no change on your side, check
[garminconnect issues](https://github.com/cyberjunky/python-garminconnect/issues)
— the fix usually belongs upstream. Upgrading helps:

```bash
uvx --refresh garmin-coach-mcp --check
```

### Still stuck

Open an issue with the version, your client, the transport, and the stderr log
at `--log-level DEBUG`. Redact your email. Never paste your token directory.

---

## Documentation

- [docs/workouts.md](docs/workouts.md) — the workout step format in full: every
  field, the validation rules, and worked examples.
- [docs/deployment.md](docs/deployment.md) — running the HTTP transport, Docker,
  Compose, and operational notes.
- [SECURITY.md](SECURITY.md) — threat model and how to report a vulnerability.
- [CONTRIBUTING.md](CONTRIBUTING.md) — dev setup, tests, house rules.
- [CHANGELOG.md](CHANGELOG.md) — what changed, including the breaking changes in
  0.1.0.

## Development

```bash
git clone https://github.com/mrosano1987/garmin-mcp-server-1.git
cd garmin-mcp-server-1
uv sync --all-extras
uv run pytest
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full workflow. Bug reports and
pull requests are welcome. Security issues go through
[SECURITY.md](SECURITY.md), not the issue tracker.

## License

[AGPL-3.0-only](LICENSE).
