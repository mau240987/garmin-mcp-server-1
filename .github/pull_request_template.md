<!--
Thanks for the pull request. Please fill in the sections below.
See CONTRIBUTING.md for the house rules.
-->

## What this changes

<!-- One or two sentences. If it fixes an issue, write "Fixes #123". -->

## Why

<!-- The problem being solved. Skip if it is obvious from the above. -->

## How it was tested

<!--
Say what you ran. CI cannot reach Garmin, so note explicitly whether you
exercised this against a live account, and with which tools.
-->

- [ ] `uv run ruff check .`
- [ ] `uv run ruff format --check .`
- [ ] `uv run mypy src`
- [ ] `uv run pytest`
- [ ] Exercised against a live Garmin account (say which tools, or N/A)

## Checklist

- [ ] New behaviour has tests
- [ ] `CHANGELOG.md` updated under `Unreleased`, if user-visible
- [ ] `README.md` tool table updated, if a tool was added or changed
- [ ] No credentials, tokens, or personal Garmin data in the diff or in test
      fixtures

## Breaking changes

<!--
List anything that changes a tool name, a parameter shape, an environment
variable, a CLI flag, or a default. Write "None" if there are none.
-->

None
