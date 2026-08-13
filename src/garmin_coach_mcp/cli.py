"""Command-line interface for the garmin-coach-mcp server.

Responsibilities, in order:

1. Parse arguments and merge them with the environment (flag > env var > default).
2. Build :class:`~garmin_coach_mcp.config.Settings` and a
   :class:`~garmin_coach_mcp.client.GarminSession`.
3. Either run the doctor (``--check``) or start the MCP server on the chosen transport.
4. Turn expected failures into a short message plus a remediation on **stderr** —
   never a traceback, never anything on stdout while the stdio transport is live.

Rule that outranks every other consideration in this module: when the transport is
``stdio``, stdout belongs exclusively to the MCP protocol. Diagnostics, banners and
errors all go to stderr.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from garmin_coach_mcp import __version__

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Callable, Mapping

PROG = "garmin-coach-mcp"

DEFAULT_TRANSPORT = "stdio"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000

#: What the user may type, mapped to what the MCP SDK understands.
TRANSPORT_ALIASES: dict[str, str] = {
    "stdio": "stdio",
    "http": "streamable-http",
    "streamable-http": "streamable-http",
    "sse": "sse",
}

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

_OK = "[ ok ]"
_WARN = "[warn]"
_FAIL = "[fail]"


DESCRIPTION = """\
Garmin Connect as an MCP server: read activities, sleep, HRV, body battery and
training readiness, and create or schedule structured workouts — from Claude
Desktop, Claude Code, or any other Model Context Protocol client.

Credentials are only needed for the first login. After that, OAuth tokens are
cached in the token directory and reused, so you can drop the password.
"""

EPILOG = """\
configuration precedence
  command-line flag  >  environment variable  >  built-in default

  --transport      GARMIN_TRANSPORT         (default: stdio)
  --host           GARMIN_HOST              (default: 127.0.0.1)
  --port           GARMIN_PORT or PORT      (default: 8000)
  --token-dir      GARMIN_TOKEN_DIR         (default: ~/.garmin-coach-mcp/tokens)
  --read-only      GARMIN_READ_ONLY         (default: off)
  --log-level      GARMIN_LOG_LEVEL         (default: INFO)
                   GARMIN_EMAIL             Garmin Connect account e-mail
                   GARMIN_PASSWORD          Garmin Connect password (first login only)
                   GARMIN_REQUEST_TIMEOUT   per-request timeout, seconds
                   GARMIN_MAX_RETRIES       retries for idempotent reads

examples
  Verify the install and the Garmin login without starting the server:

      garmin-coach-mcp --check

  Run over stdio (what MCP clients launch for you):

      garmin-coach-mcp

  Run as an HTTP service, read-only, reachable from other containers:

      garmin-coach-mcp --transport http --host 0.0.0.0 --port 8000 --read-only

  Register with Claude Code:

      claude mcp add garmin \\
          --env GARMIN_EMAIL=you@example.com \\
          --env GARMIN_PASSWORD=your-password \\
          -- uvx garmin-coach-mcp

  Claude Desktop — claude_desktop_config.json:

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

  No install at all (uv fetches and runs it):

      uvx garmin-coach-mcp --check

  Docker, HTTP transport, tokens kept in a named volume:

      docker run --rm -p 8000:8000 \\
          -e GARMIN_EMAIL=you@example.com -e GARMIN_PASSWORD=your-password \\
          -v garmin-tokens:/data/tokens \\
          garmin-coach-mcp

home page: https://github.com/mrosano1987/garmin-mcp-server-1
"""


class _StartupError(Exception):
    """A failure that stops the CLI before the server is ever constructed."""

    def __init__(self, message: str, remediation: str, exit_code: int = 2) -> None:
        super().__init__(message)
        self.message = message
        self.remediation = remediation
        self.exit_code = exit_code


@dataclass(frozen=True)
class _Runtime:
    """The pieces of the package the CLI drives, imported on demand."""

    settings_cls: Any
    session_cls: Any
    build_server: Callable[[Any], Any]
    #: ``GarminMCPError``. Typed as ``Any`` because it is resolved at runtime;
    #: every instance carries ``.message`` and ``.remediation``.
    error_cls: Any
    configure_logging: Callable[[str], None] | None


class _HelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Keep description/epilog verbatim and give option help room to breathe."""

    def __init__(self, prog: str) -> None:
        width = min(shutil.get_terminal_size(fallback=(100, 24)).columns - 2, 100)
        super().__init__(prog, max_help_position=30, width=max(width, 60))


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser (also used to render ``--help`` in tests)."""
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=_HelpFormatter,
        add_help=True,
    )
    parser.add_argument(
        "-V",
        "--version",
        action="store_true",
        help="print version information and exit",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "run diagnostics — interpreter, dependencies, settings, token store, "
            "tool registration and a real Garmin sign-in — then exit without "
            "starting the server (exit 0 = healthy)"
        ),
    )

    transport = parser.add_argument_group("transport")
    transport.add_argument(
        "--transport",
        choices=sorted(TRANSPORT_ALIASES),
        default=None,
        metavar="{stdio,http,streamable-http,sse}",
        help="transport to serve on; 'http' is an alias for 'streamable-http' [env: GARMIN_TRANSPORT, default: stdio]",
    )
    transport.add_argument(
        "--host",
        default=None,
        metavar="ADDR",
        help="bind address for http/sse transports [env: GARMIN_HOST, default: 127.0.0.1]",
    )
    transport.add_argument(
        "--port",
        type=int,
        default=None,
        metavar="PORT",
        help="bind port for http/sse transports [env: GARMIN_PORT or PORT, default: 8000]",
    )

    garmin = parser.add_argument_group("garmin account")
    garmin.add_argument(
        "--token-dir",
        default=None,
        metavar="DIR",
        help=(
            "directory holding the cached Garmin OAuth tokens "
            "[env: GARMIN_TOKEN_DIR, default: ~/.garmin-coach-mcp/tokens]"
        ),
    )
    garmin.add_argument(
        "--read-only",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "register only the read tools; workout create/schedule/delete tools are "
            "left out entirely [env: GARMIN_READ_ONLY]"
        ),
    )

    diagnostics = parser.add_argument_group("diagnostics")
    diagnostics.add_argument(
        "--log-level",
        choices=LOG_LEVELS,
        default=None,
        type=str.upper,
        metavar="LEVEL",
        help="DEBUG, INFO, WARNING, ERROR or CRITICAL; always written to stderr [env: GARMIN_LOG_LEVEL, default: INFO]",
    )
    return parser


# ---------------------------------------------------------------------------
# option resolution: flag > env var > default
# ---------------------------------------------------------------------------


def _env_overlay(args: argparse.Namespace, env: Mapping[str, str]) -> dict[str, str]:
    """Return a copy of ``env`` with command-line overrides layered on top.

    ``Settings.from_env`` owns all parsing and normalisation of the ``GARMIN_*``
    variables, so the cleanest way to make a flag win over the environment is to
    hand ``from_env`` an environment in which the flag has already won.
    """
    overlay = dict(env)
    if args.token_dir is not None:
        overlay["GARMIN_TOKEN_DIR"] = args.token_dir
        overlay.pop("GARTH_TOKEN_DIR", None)  # deprecated alias must not shadow the flag
    if args.read_only is not None:
        overlay["GARMIN_READ_ONLY"] = "true" if args.read_only else "false"
    if args.log_level is not None:
        overlay["GARMIN_LOG_LEVEL"] = args.log_level
    return overlay


def _resolve_transport(args: argparse.Namespace, env: Mapping[str, str]) -> str:
    raw = args.transport or env.get("GARMIN_TRANSPORT") or DEFAULT_TRANSPORT
    key = raw.strip().lower()
    if key not in TRANSPORT_ALIASES:
        choices = ", ".join(sorted(TRANSPORT_ALIASES))
        raise _StartupError(
            f"Unknown transport {raw!r}.",
            f"Set GARMIN_TRANSPORT (or --transport) to one of: {choices}.",
        )
    return TRANSPORT_ALIASES[key]


def _resolve_host(args: argparse.Namespace, env: Mapping[str, str]) -> str:
    return args.host or env.get("GARMIN_HOST") or DEFAULT_HOST


def _resolve_port(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    if args.port is not None:
        port = int(args.port)
        source = "--port"
    else:
        raw = env.get("GARMIN_PORT") or env.get("PORT")
        if not raw:
            return DEFAULT_PORT
        source = "GARMIN_PORT" if env.get("GARMIN_PORT") else "PORT"
        try:
            port = int(raw)
        except ValueError:
            raise _StartupError(
                f"{source}={raw!r} is not a number.",
                "Set it to a TCP port between 1 and 65535, for example 8000.",
            ) from None
    if not 1 <= port <= 65535:
        raise _StartupError(
            f"{source}={port} is outside the valid TCP port range.",
            "Choose a port between 1 and 65535 (use 1024 or above as a non-root user).",
        )
    return port


# ---------------------------------------------------------------------------
# runtime wiring
# ---------------------------------------------------------------------------


def _load_runtime() -> _Runtime:
    """Import the parts of the package that actually talk to Garmin.

    Deferred so that ``--help`` and ``--version`` stay instant and keep working
    even if an optional dependency is missing or half-installed.
    """
    try:
        from garmin_coach_mcp.client import GarminSession
        from garmin_coach_mcp.config import Settings
        from garmin_coach_mcp.errors import GarminMCPError
        from garmin_coach_mcp.server import build_server
    except ImportError as exc:
        raise _StartupError(
            f"garmin-coach-mcp is not fully installed: {exc}.",
            "Reinstall it with 'uv pip install --force-reinstall garmin-coach-mcp' "
            "(or 'pip install --force-reinstall garmin-coach-mcp'), then run "
            "'garmin-coach-mcp --check' again.",
        ) from exc

    # logging_utils is owned by another module and is optional at import time;
    # fall back to a plain stderr handler if it is absent.
    configure: Callable[[str], None] | None = None
    try:
        from garmin_coach_mcp import logging_utils
    except ImportError:
        pass
    else:
        candidate = getattr(logging_utils, "configure_logging", None)
        if callable(candidate):
            configure = candidate

    return _Runtime(
        settings_cls=Settings,
        session_cls=GarminSession,
        build_server=build_server,
        error_cls=GarminMCPError,
        configure_logging=configure,
    )


def _configure_logging(runtime: _Runtime, level: str) -> None:
    """Send logs to stderr at the requested level. Never touches stdout."""
    if runtime.configure_logging is not None:
        runtime.configure_logging(level)
        return
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )


def _build_settings(runtime: _Runtime, overlay: Mapping[str, str]) -> Any:
    try:
        return runtime.settings_cls.from_env(overlay)
    except runtime.error_cls:
        raise
    except (ValueError, TypeError, OSError) as exc:
        raise _StartupError(
            f"Invalid configuration: {exc}",
            "Check the GARMIN_* environment variables and the flags you passed; "
            "'garmin-coach-mcp --help' lists every one of them.",
        ) from exc


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def _package_version(name: str) -> str | None:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version(name)
    except PackageNotFoundError:
        return None


def _mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    if not domain:
        return "***"
    head = local[:1] or "*"
    return f"{head}{'*' * max(len(local) - 1, 1)}@{domain}"


class _Report:
    """Accumulates doctor results and remembers whether anything failed."""

    def __init__(self) -> None:
        self.failed = False
        self._remediations: list[str] = []

    def section(self, title: str) -> None:
        print(f"\n{title}")

    def ok(self, label: str, detail: str = "") -> None:
        self._line(_OK, label, detail)

    def warn(self, label: str, detail: str = "") -> None:
        self._line(_WARN, label, detail)

    def fail(self, label: str, detail: str = "", remediation: str = "") -> None:
        self.failed = True
        self._line(_FAIL, label, detail)
        if remediation:
            self._remediations.append(remediation)

    def _line(self, marker: str, label: str, detail: str) -> None:
        print(f"  {marker}  {label:<22} {detail}".rstrip())

    def finish(self) -> int:
        if not self.failed:
            print("\nAll checks passed. The server is ready to start.")
            return 0
        print("\nSome checks failed:")
        for item in self._remediations:
            print(f"  - {item}")
        return 1


def _check_environment(report: _Report) -> None:
    report.section("environment")
    report.ok("python", f"{sys.version.split()[0]} ({sys.executable})")
    report.ok("garmin-coach-mcp", __version__)
    for dist, minimum in (("mcp", "2.0"), ("garminconnect", "0.3.2"), ("pydantic", "2.7")):
        found = _package_version(dist)
        if found is None:
            report.fail(
                dist,
                "not installed",
                f"Install the missing dependency: 'uv pip install \"{dist}>={minimum}\"'.",
            )
        else:
            report.ok(dist, found)


def _check_configuration(report: _Report, settings: Any, transport: str) -> None:
    report.section("configuration")
    report.ok("transport", transport)

    token_dir = settings.token_dir
    if token_dir.is_dir():
        tokens = [p for p in token_dir.iterdir() if p.is_file()]
        writable = os.access(token_dir, os.W_OK)
        if writable:
            report.ok("token directory", f"{token_dir} ({len(tokens)} cached token file(s))")
        else:
            report.fail(
                "token directory",
                f"{token_dir} is not writable",
                f"Grant write access to {token_dir}, or point --token-dir (GARMIN_TOKEN_DIR) somewhere writable.",
            )
        if tokens:
            report.ok("saved session", "found — credentials are optional from here on")
        else:
            report.warn("saved session", "none yet — the first login needs email + password")
    else:
        parent = next((p for p in token_dir.parents if p.exists()), token_dir)
        if os.access(parent, os.W_OK):
            report.ok("token directory", f"{token_dir} (will be created on first login)")
        else:
            report.fail(
                "token directory",
                f"{token_dir} cannot be created ({parent} is not writable)",
                f"Point --token-dir (GARMIN_TOKEN_DIR) at a writable location; {parent} is not writable.",
            )
        report.warn("saved session", "none yet — the first login needs email + password")

    if settings.email:
        report.ok("credentials", f"GARMIN_EMAIL={_mask_email(settings.email)}")
    else:
        report.warn("credentials", "GARMIN_EMAIL is not set")
    report.ok("password", "set (never logged)" if settings.password else "not set")
    report.ok(
        "read-only mode",
        "on — write tools are not registered" if settings.read_only else "off",
    )
    report.ok("log level", settings.log_level)
    report.ok("request timeout", f"{settings.request_timeout}s")
    report.ok("max retries", str(settings.max_retries))


def _check_server(report: _Report, runtime: _Runtime, session: Any) -> Any:
    report.section("server")
    try:
        server = runtime.build_server(session)
    except runtime.error_cls as exc:
        report.fail("tool registration", exc.message, exc.remediation or "")
        return None
    report.ok("tool registration", f"{_tool_count(server)} tools registered")
    return server


def _tool_count(server: Any) -> str:
    """Best-effort tool count; never let introspection break the doctor."""
    manager = getattr(server, "_tool_manager", None)
    lister = getattr(manager, "list_tools", None)
    if callable(lister):
        try:
            return str(len(lister()))
        except (TypeError, AttributeError):
            return "?"
    return "?"


def _check_authentication(report: _Report, runtime: _Runtime, session: Any) -> None:
    report.section("authentication")
    try:
        session.api()
    except runtime.error_cls as exc:
        report.fail("garmin connect", exc.message, exc.remediation or "")
        return
    report.ok("garmin connect", "signed in; tokens are valid")


def _doctor(runtime: _Runtime, settings: Any, transport: str) -> int:
    """Validate everything the server needs, without starting the server."""
    print(f"{PROG} {__version__} — diagnostics")
    report = _Report()
    _check_environment(report)
    _check_configuration(report, settings, transport)

    session = runtime.session_cls(settings)
    if _check_server(report, runtime, session) is not None:
        _check_authentication(report, runtime, session)
    else:
        report.section("authentication")
        report.warn("garmin connect", "skipped — the server could not be built")
    return report.finish()


# ---------------------------------------------------------------------------
# serving
# ---------------------------------------------------------------------------


def _banner(transport: str, settings: Any, host: str, port: int) -> None:
    """One line on stderr so operators can see what started. Never stdout."""
    mode = "read-only" if settings.read_only else "read-write"
    where = transport
    if transport != "stdio":
        where = f"{transport} on http://{host}:{port}{_http_path(transport)}"
    print(
        f"{PROG} {__version__} | {where} | {mode} | tokens={settings.token_dir}",
        file=sys.stderr,
    )


def _http_path(transport: str) -> str:
    return "/sse" if transport == "sse" else "/mcp"


def _serve(runtime: _Runtime, settings: Any, transport: str, host: str, port: int) -> int:
    session = runtime.session_cls(settings)
    server = runtime.build_server(session)
    _banner(transport, settings, host, port)
    if transport == "stdio":
        server.run(transport="stdio")
    else:
        server.run(transport=transport, host=host, port=port)
    return 0


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def _print_version() -> None:
    parts = [f"{PROG} {__version__}"]
    details = [
        f"mcp {_package_version('mcp') or 'not installed'}",
        f"garminconnect {_package_version('garminconnect') or 'not installed'}",
        f"pydantic {_package_version('pydantic') or 'not installed'}",
        f"python {sys.version.split()[0]}",
        sys.platform,
    ]
    parts.append("(" + ", ".join(details) + ")")
    print(" ".join(parts))


def _report_failure(message: str, remediation: str | None) -> None:
    print(f"{PROG}: error: {message}", file=sys.stderr)
    if remediation:
        print(f"{PROG}: try: {remediation}", file=sys.stderr)


def _execute(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    """Resolve configuration, then either run the doctor or serve.

    Raises:
        _StartupError: configuration could not be resolved at all.
    """
    transport = _resolve_transport(args, env)
    host = _resolve_host(args, env)
    port = _resolve_port(args, env)
    runtime = _load_runtime()
    settings = _build_settings(runtime, _env_overlay(args, env))
    _configure_logging(runtime, settings.log_level)
    try:
        if args.check:
            return _doctor(runtime, settings, transport)
        return _serve(runtime, settings, transport, host, port)
    except runtime.error_cls as exc:
        _report_failure(exc.message, exc.remediation)
        return 1


def main(argv: list[str] | None = None) -> int:
    """Run the command-line interface and return the process exit code.

    Args:
        argv: Argument list to parse. Defaults to ``sys.argv[1:]``.

    Returns:
        ``0`` on success, ``1`` for a runtime/auth failure, ``2`` for a startup or
        configuration failure, ``130`` if interrupted with Ctrl-C.
    """
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # --help, or an argparse usage error
        return int(exc.code or 0)

    if args.version:
        _print_version()
        return 0

    try:
        return _execute(args, os.environ)
    except _StartupError as exc:
        _report_failure(exc.message, exc.remediation)
        return exc.exit_code
    except KeyboardInterrupt:
        print(f"\n{PROG}: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
