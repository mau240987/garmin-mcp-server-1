"""Command-line entry point: `garmin-coach-mcp`."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from garmin_coach_mcp import __version__
from garmin_coach_mcp.client import GarminSession
from garmin_coach_mcp.config import LOG_LEVELS, Settings
from garmin_coach_mcp.errors import GarminMCPError
from garmin_coach_mcp.logging_utils import configure_logging
from garmin_coach_mcp.server import build_server


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="garmin-coach-mcp", description="Garmin Connect MCP server")
    p.add_argument(
        "--transport",
        default="stdio",
        choices=["stdio", "http", "streamable-http"],
        help="stdio for Claude Desktop/Code (default); http = streamable-http.",
    )
    p.add_argument("--host", default="127.0.0.1", help="HTTP bind address (default 127.0.0.1).")
    p.add_argument("--port", type=int, default=8000, help="HTTP port (default 8000).")
    p.add_argument("--read-only", action="store_true", help="Do not register write tools.")
    p.add_argument("--log-level", choices=LOG_LEVELS, default=None)
    p.add_argument(
        "--login",
        action="store_true",
        help="Log in interactively (supports MFA), store tokens, then exit.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p.parse_args(argv)


def _interactive_login(settings: Settings) -> int:
    import getpass

    from garminconnect import Garmin

    email = settings.email or input("Garmin email: ")
    password = settings.password or getpass.getpass("Garmin password: ")
    settings.token_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    client = Garmin(email, password, prompt_mfa=lambda: input("MFA code: "), retry_attempts=0)
    client.login(str(settings.token_dir))
    print(f"Tokens stored in {settings.token_dir}", file=sys.stderr)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        settings = Settings.from_env(
            read_only=True if args.read_only else None, log_level=args.log_level
        )
    except GarminMCPError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    log = configure_logging(settings.log_level)

    if args.login:
        return _interactive_login(settings)

    app = build_server(GarminSession(settings))
    log.info(
        "garmin-coach-mcp %s starting (transport=%s, read_only=%s)",
        __version__,
        args.transport,
        settings.read_only,
    )
    if args.transport == "stdio":
        app.run(transport="stdio")
    else:
        app.run(transport="streamable-http", host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
