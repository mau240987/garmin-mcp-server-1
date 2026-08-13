# Production-Hardening Progress

Target: match or beat `@modelcontextprotocol/server-filesystem` and `github/github-mcp-server`.
Each piece got an independent **builder** with fresh context and disjoint file ownership.

**Status: HALTED BY USER after round 1. No agents running. No critic round ever ran.**

| # | Piece | Owner files | Build | Critique | Verdict |
|---|-------|-------------|-------|----------|---------|
| P1 | Package & install DX | `pyproject.toml`, `cli.py`, `__main__.py`, Docker | landed | not run | unreviewed |
| P2 | Tool surface & schemas | `models.py`, `workouts.py`, `formatting.py`, `server.py` | landed | not run | unreviewed |
| P3 | Runtime core & errors | `config.py`, `errors.py`, `retry.py`, `client.py`, `logging_utils.py` | landed | not run | unreviewed |
| P4 | Documentation | `README.md`, `CONTRIBUTING.md`, `SECURITY.md`, `CHANGELOG.md` | landed | not run | unreviewed |
| P5 | Tests & CI | `tests/`, `.github/workflows/` | **never started** | — | **missing** |

## What actually works, verified by running it

- `pip install -e .` succeeds; `garmin-coach-mcp --version` reports
  `0.1.0 (mcp 2.0.0, garminconnect 0.3.2, pydantic 2.13.4, python 3.11.15, linux)`.
- The server builds and registers **11 tools**: `list_activities`, `get_activity`,
  `get_health_summary`, `get_training_load`, `list_workouts`, `get_workout`, `create_workout`,
  `create_training_plan`, `schedule_workout`, `delete_workout`, `delete_workouts`.
- `GARMIN_READ_ONLY=true` correctly drops the surface to 6 read tools.

## Known defects in this snapshot — this is NOT production-ready

1. **`garmin-coach-mcp --check` crashes** with an `AttributeError` traceback. `cli.py` passes a
   log-level string where `logging_utils.configure_logging` expects a `Settings`. A seam bug
   between two pieces built in parallel — precisely what the critic round was meant to catch.
2. `ruff check src` — **37 errors**.
3. `mypy src/garmin_coach_mcp` — **19 errors**, all in `server.py`.
4. **Zero tests and no CI.** P5 was blocked on P2+P3 and never launched, so nothing here is
   covered by a single automated test.
5. Docs were written against SPEC.md rather than the delivered code, so tool names and flags in
   the README are unverified against the real surface.
6. `docs/` was created but left empty.

## Baseline defects in the legacy server (the reason for this work)

Verified against the installed `garminconnect` 0.3.2 and MCP SDK 2.0.

1. `get_daily_steps(d)` called with 1 of 2 required args — always raised, always swallowed.
2. Eight `except Exception: pass` blocks in `get_health_summary` hide every upstream failure.
3. `delete_plan_workouts()` with no argument deletes **every workout in the account**.
4. Retry decorator converts failures into the *string* `"Error after N attempts: ..."`, so the
   client sees a successful tool result containing an error message.
5. `push_workout` is retried on failure — a create that partially applied gets re-applied.
6. `push_training_plan` calls the decorated `push_workout`, nesting retry inside retry.
7. `pace_to_ms` raises an unhandled `ValueError` on malformed input.
8. `steps_json` / `workouts_json` are JSON-encoded **strings**, so the tool schema documents
   nothing and the client cannot validate input.
9. Targets MCP SDK 1.x (`mcp.server.fastmcp`), which does not exist in the current SDK — the
   legacy server cannot start at all against the current SDK.

The legacy `garmin_mcp_server.py` is still present and untouched; nothing has been removed.

## To resume

Run the critic round (P1–P4) plus the P5 builder. The `--check` crash, the lint/type errors and
the absent test suite are the first three things to fix.
