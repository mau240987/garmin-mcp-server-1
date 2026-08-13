# Contributing

Thanks for considering a contribution. This is a small project, so the process
is light. The one thing it is strict about is the checks in
[Before you open a pull request](#before-you-open-a-pull-request) — they run in
CI and a red build will not be merged.

## Getting set up

You need Python 3.10 or newer and [uv](https://docs.astral.sh/uv/). If you do
not have `uv`:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then:

```bash
git clone https://github.com/mrosano1987/garmin-mcp-server-1.git
cd garmin-mcp-server-1
uv sync --all-extras
```

`uv sync` creates `.venv/` and installs the project in editable mode together
with the development dependencies. You do not need to activate the virtualenv —
prefix commands with `uv run` and `uv` will use it.

Check that it worked:

```bash
uv run garmin-coach-mcp --help
```

If you would rather use plain `pip`:

```bash
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

## The checks

Run all four before pushing. They are the same four CI runs.

```bash
uv run ruff check .             # lint
uv run ruff format --check .    # formatting
uv run mypy src                 # type check
uv run pytest                   # tests
```

`ruff format .` (without `--check`) rewrites files in place and is the quickest
way to fix formatting complaints. `ruff check --fix .` auto-fixes the lint rules
that are safely auto-fixable.

For a coverage report:

```bash
uv run pytest --cov=garmin_coach_mcp --cov-report=term-missing
```

Tests marked `integration` talk to the live Garmin Connect API and are not run in
CI. To skip them locally:

```bash
uv run pytest -m "not integration"
```

Lint configuration lives in `pyproject.toml`. Two rule groups are load-bearing
rather than cosmetic: `BLE` (flake8-blind-except) enforces the no-bare-`except`
rule, and `T20` (flake8-print) enforces the no-stdout rule. `cli.py` is the one
module exempted from `T20`, because it is the only place allowed to write to the
terminal.

## Running the server while you work

Nothing in the test suite talks to Garmin. Tests use fakes, and they must
continue to: a test that needs live credentials cannot run in CI.

To exercise the server by hand against a real account you need your own Garmin
Connect credentials. Log in once so tokens are written, then drop the
credentials:

```bash
export GARMIN_EMAIL='you@example.com'
export GARMIN_PASSWORD='...'
uv run garmin-coach-mcp --log-level DEBUG
```

Use `--read-only` unless you are specifically testing a write path. It is easy
to leave stray workouts on a real Garmin calendar.

To drive it interactively, point a client at your checkout:

```bash
claude mcp add garmin-dev -- uv run --directory "$(pwd)" garmin-coach-mcp --read-only
```

The MCP Inspector is also useful for poking at schemas without an LLM in the
loop:

```bash
npx @modelcontextprotocol/inspector uv run --directory "$(pwd)" garmin-coach-mcp
```

## Project layout

```
src/garmin_coach_mcp/
    cli.py            argument parsing, transport selection, startup banner
    config.py         Settings, environment loading
    errors.py         exception taxonomy
    logging_utils.py  structured logging to stderr
    retry.py          backoff policy
    client.py         GarminSession — auth, token persistence, error mapping
    formatting.py     units, pace and duration rendering (pure functions)
    models.py         pydantic input and output models
    workouts.py       workout step models to Garmin payloads
    server.py         MCP server instance, tool registration
tests/
docs/
```

## House rules

These come from the defects the rewrite was built to remove. They are not
stylistic preferences.

- **No bare `except Exception: pass`.** Swallowed exceptions are how the original
  server reported a broken API call as "no data available" for months.
- **Errors are raised, never returned.** A tool that returns the string
  `"Error: ..."` looks like a successful call to the MCP client. Raise a
  `GarminMCPError` subclass; the server converts it to a protocol-level tool
  error.
- **Every error carries a remediation.** The caller is a language model. Tell it
  what to do next, in the error, not just what went wrong.
- **Never log credentials or tokens**, and never log a full auth payload.
- **All logging goes to stderr.** Under the stdio transport, stdout is the
  protocol channel. A stray `print()` corrupts the session.
- **Retry idempotent reads only.** Never blind-retry a create — a partially
  applied write gets applied twice.
- **No JSON-encoded-string parameters.** Nested input is modelled with pydantic
  so the schema documents itself and the client validates before the call.
- **Destructive tools stay narrow.** A bulk delete must not have a default that
  deletes everything.

## Adding a tool

1. Model the inputs and outputs in `models.py` with pydantic. Give every field a
   `Field(description=...)` and, where it applies, a constraint (`ge`, `le`,
   `min_length`, `pattern`). Use enums rather than free-form strings.
2. Register it in `server.py` with a `title`, a description that says when to
   use it, and complete `annotations` — `readOnlyHint`, `destructiveHint`,
   `idempotentHint`, `openWorldHint`.
3. If it writes, make sure it is registered only when
   `settings.read_only` is false.
4. Add tests: the happy path, at least one input-validation rejection, and the
   upstream-failure mapping.
5. Add a row to the tool table in `README.md`, and per-tool detail if the
   parameters are not self-evident.

## Before you open a pull request

- The four checks above pass.
- New behaviour has tests.
- User-visible changes are in `CHANGELOG.md` under `Unreleased`.
- Documentation is updated in the same pull request as the code.
- Commits are reasonably scoped. Squashing is fine; a 40-commit history of
  "fix" is not.

Open the PR against `main`. Describe what changed and why, and say how you
tested it — particularly whether you exercised it against a live Garmin account,
since CI cannot. If it is a breaking change, say so in the title.

For anything large, open an issue first. It is worth agreeing on the shape of a
change before it is written.

## Reporting bugs

Use the [issue templates](https://github.com/mrosano1987/garmin-mcp-server-1/issues/new/choose).
Include the version, the client you are using, and the stderr log with
`--log-level DEBUG`. Redact your email address and never paste the contents of
your token directory.

If a tool started failing without any change on your side, it is likely that
Garmin changed an endpoint. Check
[`garminconnect`](https://github.com/cyberjunky/python-garminconnect/issues)
first — the fix usually belongs there.

## Security issues

Do not open a public issue. Follow [SECURITY.md](SECURITY.md).

## License

Contributions are licensed under [AGPL-3.0](LICENSE), the same as the project.
