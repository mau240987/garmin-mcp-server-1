# Production-Hardening Progress

Target: match or beat `@modelcontextprotocol/server-filesystem` and `github/github-mcp-server`.
Session budget: 30 minutes. Each piece gets an independent **builder** and an independent
**critic** with fresh context. Critics inspect the real artifact — install from scratch, call the
tools, read the docs cold — and send back the single biggest gap.

**Status: round 1 — builders dispatched**

| # | Piece | Owner files | Build | Critique | Verdict |
|---|-------|-------------|-------|----------|---------|
| P1 | Package & install DX | `pyproject.toml`, `cli.py`, `__main__.py`, Docker | running | — | — |
| P2 | Tool surface & schemas | `models.py`, `workouts.py`, `formatting.py`, `server.py` | running | — | — |
| P3 | Runtime core & errors | `config.py`, `errors.py`, `retry.py`, `client.py`, `logging_utils.py` | running | — | — |
| P4 | Documentation | `README.md`, `docs/`, `CONTRIBUTING.md`, `SECURITY.md` | running | — | — |
| P5 | Tests & CI | `tests/`, `.github/workflows/` | blocked on P2+P3 | — | — |

## Baseline defects found in the legacy server

Recorded before any rewrite, verified against the installed `garminconnect` 0.3.2 and MCP SDK 2.0.

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
9. Targets MCP SDK 1.x (`mcp.server.fastmcp`), which does not exist in the current SDK.
