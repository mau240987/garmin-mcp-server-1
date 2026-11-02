# Garmin Race Coach — private data

Goals (`goals/`), plans (`plans/`) and exports (`exports/`) saved by the Race Coach
([garmin-mcp-server-1](https://github.com/mrosano1987/garmin-mcp-server-1)). **Keep this
repository private**: it contains training and health-derived data.

## Push to Garmin

Actions → *Push to Garmin* → *Run workflow*:

1. Leave **apply** unchecked → dry run. The job summary checks the Garmin login and lists the
   workouts that would be created, scheduled or deleted, with the counts to use.
2. Run again with **apply** checked, `expected_count` and `expected_delete_count` as shown
   (same `plan_id`, `from_date`, `weeks`). The workout ids are committed back here.

Secrets (Settings → Secrets and variables → Actions): `GARMIN_EMAIL`, `GARMIN_PASSWORD`.
Optional variable `GARMIN_COACH_REF`: the server branch, tag or commit to install (default
`main`).

This uses the unofficial Garmin Connect access (see the server's `docs/api-coverage.md`, risk
R4): Garmin may block logins from GitHub's servers; the dry run shows it.
