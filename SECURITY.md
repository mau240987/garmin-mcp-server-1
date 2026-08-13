# Security Policy

## Reporting a vulnerability

Report suspected vulnerabilities privately. Do not open a public issue.

1. Go to the [Security Advisories page](https://github.com/mrosano1987/garmin-mcp-server-1/security/advisories/new)
   and open a private advisory. This is the preferred route: it creates a private
   thread with the maintainers and gives you a CVE if one is warranted.
2. If you cannot use GitHub advisories, open a public issue that says only
   "security report, requesting a private channel" — with **no details** — and a
   maintainer will follow up.

Please include: what you found, the version or commit you tested, the steps to
reproduce it, and what an attacker gains. A proof of concept helps.

**What to expect.** This is a small volunteer-maintained project, not a funded
security program. Expect an acknowledgement within about a week and a status
update within about a month. There is no bug bounty. If a report goes
unanswered for a month, escalating in a public issue (still without details) is
reasonable.

### Supported versions

Only the latest released version receives fixes. There are no backports to
earlier versions.

## Threat model — read this before deploying

This section is deliberately blunt. Understanding it is more useful than any
checklist.

### The server holds full control of a Garmin Connect account

Authentication is account-level. There is no scope, no permission grant, no
read-only API key. Once the server has a valid session it can read every
activity and every health metric on the account — sleep, heart rate, HRV,
stress, SpO2 — and, unless started with `--read-only`, it can create, schedule
and permanently delete workouts.

Deleting a workout from Garmin Connect is not reversible from this server. There
is no undo and no trash.

An MCP server is driven by a language model. Treat every write tool as something
that may be invoked on the strength of an ambiguous instruction or of text the
model read somewhere else. If you do not need writes, run with `--read-only`.
That is not a soft guard: in read-only mode the write tools are never
registered, so they do not appear in the tool list and cannot be called.

### Credential handling

Credentials are used once. On first login the server exchanges your email and
password for OAuth tokens via the `garth` library, writes those tokens to the
token directory, and from then on resumes from the tokens. Your password is
never written to disk by this server.

The tokens are the sensitive artifact. They are bearer credentials for your
Garmin account, they are stored unencrypted, and they are valid for roughly a
year. Anyone who can read the token directory can act as you on Garmin Connect
without your password and without triggering MFA. Protect it accordingly:

```bash
chmod 700 ~/.garmin-coach-mcp
```

Neither credentials nor tokens are ever written to log output, and log output
goes to stderr only.

### Credentials in client configuration files

Putting `GARMIN_PASSWORD` into `claude_desktop_config.json` or an equivalent
client config works, and it is the wrong long-term arrangement. Those files are
plain text, are world-readable on many systems by default, are frequently
synced or backed up, and get pasted into bug reports.

The recommended sequence is: supply the credentials once, confirm that the token
directory has been populated, then delete the credentials from the config file.
The server does not need them again.

If you must keep credentials on disk, keep them in a `.env` file with mode
`600` that is excluded from version control, rather than in a config file you
are likely to share.

### HTTP transport is unauthenticated

The `--transport http` mode exposes an MCP endpoint with no authentication of
its own. Anyone who can reach the port controls the Garmin account behind it.
Bind it to `127.0.0.1` unless you have deliberately put an authenticating proxy
in front of it, and never expose it to a public network.

For the local-client use case — Claude Desktop, Claude Code, VS Code — use the
default stdio transport instead. The server runs as a subprocess of the client
and no network port is opened.

### Dependency on an unofficial API

This project is not affiliated with, endorsed by, or sponsored by Garmin Ltd.

It talks to Garmin Connect through
[`garminconnect`](https://github.com/cyberjunky/python-garminconnect) and
[`garth`](https://github.com/matin/garth), which use undocumented endpoints
reverse-engineered from Garmin's own web and mobile clients. That has
consequences you are accepting when you run this:

- **No stability contract.** Garmin can change or remove these endpoints at any
  time, without notice. Tools that work today can break tomorrow, and the fix is
  upstream in `garminconnect`, not here.
- **No terms-of-service guarantee.** Automated access of this kind is not
  something Garmin has sanctioned. Aggressive use may get an account
  rate-limited or locked. Keep request volume low and human-scale.
- **The trust boundary includes the upstream libraries.** Your credentials pass
  through `garminconnect` and `garth`. This project's security posture is
  bounded by theirs.
- **Personal use only.** Do not run this against accounts you do not own.

### Out of scope

The following are not treated as vulnerabilities in this project:

- Upstream breakage caused by Garmin changing its endpoints.
- Vulnerabilities in `garminconnect`, `garth` or the MCP SDK — report those to
  their maintainers, though a heads-up here is welcome.
- The fact that an operator who configures the server can read the account's
  data. That is the purpose of the server.
- An exposed HTTP transport that the operator chose to bind to a public
  interface, given the warning above.
