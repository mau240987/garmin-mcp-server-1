# Progress — Garmin Race Coach

Plan of record: the "Garmin Race Coach" brief (pieces G0, H, A1–A4, C1–C4, S1–S2, V).
`SPEC.md` is the earlier hardening contract; its layout and rules were followed, but its API
notes predate MCP SDK 2.3 (snake_case `ToolAnnotations`, `mcp.server.mcpserver`).

| ID | Piece | Status | Notes |
|---|---|---|---|
| G0 | API coverage gate → `docs/api-coverage.md` | **Done — strategy A approved** | Official spec extracted and analyzed; real calls impossible from the dev sandbox (host blocked) → `scripts/verify_personal_api.py` for a local run. |
| H | Hardening P1–P8 | **Done** | See below. |
| A1 | `GarminBackend` protocol + capability router | **Done** | `backends/base.py`, `backends/router.py`; 9 tools refactored; the 83 pre-existing tests pass unchanged through the router. |
| A2 | `OfficialPersonalBackend` | **Done — verified on the real API** (2026-11-02, owner's machine) | Token = plain-text JWT, `Bearer` confirmed, all used endpoints 200 with expected fields. See `docs/api-coverage.md` §1.2. |
| A3 | `UnofficialBackend` + `GARMIN_ALLOW_UNOFFICIAL_WRITES` | **Done** | Writes off by default in every configuration (behaviour change, documented in README); nothing reaches Garmin when disabled. |
| A4 | `OfficialStarterBackend` stub + requirements | **Done (stub)** | Declares no capabilities; router prefers it for writes once it does (tested). Requirements: `docs/starter-requirements.md`. |
| C1 | Goal/plan model + local storage | **Done** | `coach/models.py`, `coach/storage.py` (JSON, atomic, 0600, `GARMIN_COACH_DATA_DIR`). |
| C2 | Deterministic plan engine | **Done** (v1.1 after review) | `coach/engine.py`, rules R1-R11 in `docs/plan-engine.md`; 5 brief cases, ~7,200-case invariant matrix, one regression test per review finding. |
| C3–C4 | New MCP tools, export | Not started | |
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

- A2: token format and `Bearer` confirmed on the real API. Still unverified: pagination of
  `activities/search` beyond 100 results, `hrv/{date}` 204 on empty days, FIT download, rate limits.
- R3 closed: attribution follows the API Brand Guidelines PDF ("Garmin", shown under the heading).

- V-1/V-2 (workout units and pace target) — write test authorized; run
  `scripts/verify_workout_write.py` locally (sandbox cannot reach Garmin) and share the report.
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

## Independent review of A1+A2 — findings and fixes

Confirmed by the reviewer: every official call matches the spec (paths, query names, shapes);
router policy correct in all four configurations; unofficial-only behaviour unchanged.

1. 3xx / HTML token bodies were accepted as tokens → only 2xx accepted, redirects are explicit
   non-retryable errors, plain-text tokens must look like tokens, request-building errors mapped
   without echoing headers.
2. Persistent data-call 401 renewed the token on every call → no renewal of a token younger than
   60 s, concurrent renewals deduplicated, then a cached "token rejected" error.
3. Data-call 401/403 suggested GARMIN_EMAIL/PASSWORD → now points to the Personal API key.
4. Transient token-fetch failures were cached for 60 s → only a rejected key is cached;
   transient failures are retried.
5. Readiness/workouts hint now depends on whether an unofficial backend exists.
6. Short `expires_in` honoured (renewal at half-life when shorter than the 5-min margin).
7. Unreadable token directory no longer crashes startup.
Also found while fixing: retry backoff ignored the test patch (default arg bound at import), so
tests really slept; suite now 126 tests in ~3 s.

## Independent review of A3+A4 — findings and fixes

No path reaches a Garmin write (or even a login) when writes are disabled; router matrix
correct. Fixed: accurate messages when the write flag is on but no unofficial login exists;
no "writes ENABLED" startup warning in read-only mode or without a login; every remediation
says the athlete (not the assistant) changes settings and mentions the login; create and
schedule now always use the same backend (guards a future partial Starter implementation).

## Independent review of C1+C2 — findings and fixes

The review found serious defects the first test suite missed (it reproduced each one): a long
run the day before a Monday race (2,520 combinations), easy runs longer than the long run (up
to 36 km), a +42% jump back to full load after a month of unavailable dates, no horizon limit
(a far-future race never finished), week 1 at 7x the recent volume, implausible goals accepted,
undecodable files breaking listing, no schema-version check.
Fix: the engine was restructured to a single pass per week computing everything from the
volume actually planned, with new rules R2b, R4b, R9, R10, R11; goal consistency validation;
storage hardening (fsync, fullmatch ids, decode errors, schema version + migration hook).
The tests were strengthened (non-circular checks, wider matrix incl. long-run day, mid-week
start, unavailable dates); they caught three more defects during the fix (stagnation after a
gap, easy > long when days increase, race week above the taper week). The reviewer's own
scan now reports only the intentional 8 km minimum week.
