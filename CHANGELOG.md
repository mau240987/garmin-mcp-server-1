# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Nothing yet.

## [0.1.0] — Unreleased

First packaged release. The project was previously a single `garmin_mcp_server.py`
script installed by cloning the repository. This release turns it into an
installable Python package and fixes a set of correctness and safety defects in
the original script. Anyone upgrading should read "Changed" and "Fixed" below —
tool names, parameter shapes and the default token directory have all moved.

### Added

- Distributable package `garmin-coach-mcp` with the console script
  `garmin-coach-mcp` and module entry point `python -m garmin_coach_mcp`.
  `uvx garmin-coach-mcp` runs the server with no prior install step.
- `--read-only` flag and `GARMIN_READ_ONLY` environment variable. In read-only
  mode the tools that create, schedule or delete anything are not registered at
  all, so a client cannot call them even by accident.
- An explicit error taxonomy (`ConfigurationError`, `AuthenticationError`,
  `RateLimitError`, `UpstreamServiceError`, `NotFoundError`,
  `InvalidRequestError`, `ReadOnlyModeError`). Every error carries a remediation
  string saying what to do next, and every error surfaces as a protocol-level
  tool error rather than as text in a successful result.
- MCP tool annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`,
  `openWorldHint`) on every tool, so clients can warn before a destructive call.
- Structured tool output. Tools return typed models, so the output schema is
  published in the tool listing alongside a human-readable text rendering.
- Configuration via a documented set of `GARMIN_*` environment variables and
  matching CLI flags, with CLI flags taking precedence.
- Structured logging to stderr with a configurable level. Credentials and tokens
  are never logged.
- Documentation set: installation and configuration for Claude Desktop, Claude
  Code, VS Code and Docker; a full tool reference; a security and safety
  section; a configuration reference; and a troubleshooting guide.

### Changed

- **Breaking.** Import path is now `garmin_coach_mcp`. The top-level
  `garmin_mcp_server.py` script is superseded.
- **Breaking.** Workout steps are no longer passed as a JSON-encoded string.
  `steps_json` and `workouts_json` are replaced by real nested objects, so the
  tool schema documents the step format and the client validates it before the
  call is made.
- **Breaking.** The default token directory moved from `~/.garth` to
  `~/.garmin-coach-mcp/tokens`. `GARTH_TOKEN_DIR` is still honoured as a
  deprecated alias for `GARMIN_TOKEN_DIR`. To keep an existing session, copy the
  old directory to the new location instead of logging in again.
- Retries are applied only to idempotent reads and to writes that are safe to
  repeat. Creating a workout is no longer retried, because a create that
  partially applied would otherwise be applied twice.
- Bulk deletion requires an explicit, non-empty name filter.

### Fixed

- `get_daily_steps` was called with one argument when the library requires two.
  The call always raised, and the exception was swallowed, so the health summary
  silently reported no step data. It now passes both arguments.
- `get_health_summary` had eight `except Exception: pass` blocks. Any upstream
  failure — expired token, rate limit, Garmin outage — was reported as "no data
  available". Missing metrics and failed metrics are now distinguished, and
  failures are reported.
- `delete_plan_workouts()` called with no argument deleted **every workout in the
  account**. An empty filter is now rejected.
- The retry decorator returned the string `"Error after N attempts: ..."` on
  exhaustion. The MCP client saw a successful tool call whose result happened to
  contain an error message. Exhausted retries now raise.
- `push_training_plan` called the already-decorated `push_workout`, nesting one
  retry loop inside another and multiplying the worst-case wall-clock time.
- `time_condition` and `distance_condition` were annotated `-> dict` but returned
  a tuple.
- `pace_to_ms` raised an unhandled `ValueError` on any malformed pace string.
  Pace input is now validated and rejected with an actionable message.
- Token-saving failures were caught by a bare `except Exception`, and the login
  path interpolated the account email into log lines.

### Security

- Credentials are no longer written to log output.
- Token files are created in a directory owned by the invoking user; see
  [SECURITY.md](SECURITY.md) for the handling model and its limits.

[Unreleased]: https://github.com/mrosano1987/garmin-mcp-server-1/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/mrosano1987/garmin-mcp-server-1/releases/tag/v0.1.0
