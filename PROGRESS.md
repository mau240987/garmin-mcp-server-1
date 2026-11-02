# Progress — Garmin Race Coach

Plan of record: the "Garmin Race Coach" brief (pieces G0, H, A1–A4, C1–C4, S1–S2, V).
`SPEC.md` is the earlier hardening contract; its layout and rules were followed, but its API
notes predate MCP SDK 2.3 (snake_case `ToolAnnotations`, `mcp.server.mcpserver`).

| ID | Piece | Status | Notes |
|---|---|---|---|
| G0 | API coverage gate → `docs/api-coverage.md` | **Done — awaiting approval** | Official spec extracted and analyzed; real calls impossible from the dev sandbox (host blocked) → `scripts/verify_personal_api.py` for a local run. |
| H | Hardening P1–P8 | **Done** | See below. |
| A1 | `GarminBackend` protocol + capability router | Not started | Blocked on G0 approval (strategy choice drives the router). |
| A2 | `OfficialPersonalBackend` | Not started | Needs G0 approval. |
| A3 | `UnofficialBackend` + `GARMIN_ALLOW_UNOFFICIAL_WRITES` | Not started | `client.GarminSession` is the starting point. |
| A4 | `OfficialStarterBackend` stub + requirements | Not started | |
| C1–C4 | Goal/plan model, plan engine, new tools, export | Not started | |
| S1–S2 | Claude skill/plugin, ChatGPT/Gemini instructions | Not started | |
| V | Independent review + E2E | Partial | Review of H done by a separate agent; all findings fixed (see below). E2E pending later pieces. |

## H — what was done

Previous state: P1–P8 were only planned (no `src/`, `pyproject.toml`, tests or CI existed) and
the legacy server did not start on a clean install (MCP SDK 2.x removed `mcp.server.fastmcp`).

- P1 packaging: `pyproject.toml` (hatchling, `src/` layout, wheel verified non-empty, console
  script `garmin-coach-mcp`), `python -m garmin_coach_mcp`, Dockerfile installs the package and
  runs as non-root, legacy `garmin_mcp_server.py` kept as a compatibility wrapper.
- P2 tool surface: the 9 tools keep their names and parameters; typed pydantic inputs/outputs,
  titles, annotations, structured output. `steps_json`/`workouts_json` kept as deprecated aliases.
- P3 runtime: `config.Settings`, error taxonomy with remediation, retries only for idempotent
  reads, library-level retries disabled (they also retried POSTs), auth re-login once.
- Legacy defects fixed: silent `except: pass` (8×), `get_daily_steps` wrong arity, delete-all on
  empty pattern, errors returned as success strings, retried creates, nested retries, unhandled
  pace parse errors, credential in log line, removed `TargetType` names.
- P5 tests & CI: 83 tests (fake client, no network) incl. protocol-level tests over the MCP
  in-memory client and output-schema validation; GitHub Actions: ruff, mypy --strict, pytest on
  3.12/3.13, wheel build + clean-venv install smoke test.

## Open verification points

- V-1/V-2 (workout units and pace target) — see `docs/api-coverage.md` §6. Needs one authorized
  write test on a real account.
- CI on Python 3.12 has not been run locally (only 3.13 available in the dev sandbox).

## Independent review of H — findings and fixes

1. Python floor: `garminconnect` 0.3.20 requires Python ≥ 3.12 → `requires-python >=3.12`, CI 3.12/3.13.
2. Write errors (`API Error NNN` from POST/DELETE) were all mapped to retryable upstream errors →
   classification now by HTTP status (`.response.status_code`, else message).
3. A bad password caused up to 8–12 SSO logins per tool call → login errors never retried, no
   re-auth after a failed login, 60 s fail-fast cache.
4. Docker: non-root image vs. old root-owned volumes silently lost tokens → writability check
   (`ConfigurationError`), entrypoint honours `GARMIN_TOKEN_DIR`, healthcheck fixed (was always 400).
5. `delete_plan_workouts`: dry run by default + `expected_count` confirmation.
6–8. Legacy compatibility: `schedule_date=""`, case-insensitive `activity_type`, `duration_value`
   default 600 s for time steps.
Also: missing `workoutId` is an error; "created but not scheduled" plan entries name the workout
id (avoids duplicates); verification script does not follow redirects and has no tracebacks.
