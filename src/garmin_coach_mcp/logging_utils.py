"""Logging configuration for garmin-coach-mcp.

Two hard constraints:

1. **stderr only.** Under the stdio transport, stdout *is* the MCP protocol channel. A
   single stray log line on stdout corrupts the JSON-RPC stream and the client drops the
   connection with a parse error that points nowhere near the real cause. So this module
   installs exactly one handler, on stderr, and actively removes any root handler that
   writes to stdout — including one a dependency installed via `logging.basicConfig()`.

2. **Nothing credential-shaped is ever written.** Redaction happens twice: a filter on
   the record (so other handlers, added by anyone, also see scrubbed text) and a
   formatter on our handler (which additionally covers formatted tracebacks). The
   configured password is registered as a literal secret, so even an upstream library
   echoing it back inside an error message cannot leak it.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import TYPE_CHECKING, Any, TextIO

if TYPE_CHECKING:  # pragma: no cover - typing only
    from garmin_coach_mcp.config import Settings

__all__ = [
    "REDACTED",
    "RedactingFilter",
    "RedactingFormatter",
    "configure_logging",
    "get_logger",
    "redact",
    "register_secret",
]

#: Replacement written in place of anything credential-shaped.
REDACTED = "***redacted***"

LOG_FORMAT = "%(asctime)s %(levelname)-8s [%(name)s] %(message)s"
DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"

_ROOT_LOGGER_NAME = "garmin_coach_mcp"

# Literal secrets registered at configuration time (the password, mainly). Compared
# verbatim, because an upstream error message may quote a credential in any form.
_SECRETS: set[str] = set()

# Patterns are anchored on the *key*, so the value shape does not matter.
_KEYED_SECRET = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|access_token|refresh_token|id_token|"
    r"api[_-]?key|authorization|auth|cookie|oauth[_-]?token|client[_-]?secret)"
    r"(\s*[=:]\s*|\"\s*:\s*\"?|'\s*:\s*'?)"
    r"([^\s,;)}\]'\"]+)"
)
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+([A-Za-z0-9._~+/=-]{8,})")
# A JWT, which is what Garmin's OAuth tokens look like on the wire.
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\b")
# `https://user:password@host` in a proxy URL.
_URL_CREDENTIALS = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^/\s:@]+):([^/\s@]+)@")


def register_secret(value: str | None) -> None:
    """Register a literal value that must never appear in a log line.

    Args:
        value: The secret. Short values (under four characters) are ignored, because
            redacting them would mangle ordinary text without protecting anything.
    """
    minimum_length = 4
    if value and len(value) >= minimum_length:
        _SECRETS.add(value)


def redact(text: str) -> str:
    """Return `text` with every credential-shaped substring replaced.

    Args:
        text: Any log text.

    Returns:
        The text with registered secrets, `key=value` credential pairs, bearer tokens,
        JWTs and URL userinfo replaced by `REDACTED`.
    """
    for secret in _SECRETS:
        if secret in text:
            text = text.replace(secret, REDACTED)
    text = _KEYED_SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    text = _BEARER.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    text = _JWT.sub(REDACTED, text)
    return _URL_CREDENTIALS.sub(lambda m: f"{m.group(1)}{m.group(2)}:{REDACTED}@", text)


class RedactingFilter(logging.Filter):
    """Scrub credential-shaped text out of a record before any handler sees it.

    Attached to the package logger and to the root logger, so a handler installed by
    another library still receives redacted records. Formatting is collapsed into
    `record.msg` when a redaction fires, since the arguments themselves may carry the
    secret.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Redact the record in place and always keep it."""
        try:
            rendered = record.getMessage()
        except (TypeError, ValueError):  # pragma: no cover - broken format string
            return True
        cleaned = redact(rendered)
        if cleaned != rendered:
            record.msg = cleaned
            record.args = ()
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


class RedactingFormatter(logging.Formatter):
    """Formatter that scrubs the fully rendered line, tracebacks included."""

    def format(self, record: logging.LogRecord) -> str:
        """Format the record and redact the result."""
        return redact(super().format(record))


def _is_stdout_handler(handler: logging.Handler) -> bool:
    """Report whether a handler writes to stdout."""
    stream = getattr(handler, "stream", None)
    return stream is not None and stream is sys.stdout


def configure_logging(
    settings: Settings | None = None,
    *,
    level: str | int | None = None,
    stream: TextIO | None = None,
    force: bool = True,
) -> logging.Logger:
    """Send this package's logs to stderr at the configured level, with redaction.

    Args:
        settings: Settings to take the level from, and whose password is registered as a
            literal secret. Optional so the CLI can log before settings are parsed.
        level: Explicit level name or number, overriding `settings.log_level`.
        stream: Destination stream. Defaults to `sys.stderr`; anything else is only for
            tests. Passing `sys.stdout` is refused, because it would corrupt the stdio
            MCP channel.
        force: Replace handlers this function installed earlier. Keeps repeated calls
            (CLI startup, then post-config re-configuration) from duplicating output.

    Returns:
        The package logger.

    Raises:
        ValueError: If `stream` is `sys.stdout`.
    """
    if stream is sys.stdout:
        msg = (
            "Refusing to log to stdout: stdout is the stdio MCP transport channel and "
            "any bytes written there corrupt the JSON-RPC stream. Logs go to stderr."
        )
        raise ValueError(msg)

    if settings is not None:
        register_secret(settings.password)

    resolved = level if level is not None else (settings.log_level if settings else "INFO")
    if isinstance(resolved, str):
        resolved = logging.getLevelName(resolved.strip().upper())
    if not isinstance(resolved, int):  # pragma: no cover - guarded by Settings validation
        resolved = logging.INFO

    logger = logging.getLogger(_ROOT_LOGGER_NAME)
    logger.setLevel(resolved)

    if force:
        for existing in list(logger.handlers):
            if getattr(existing, "_garmin_coach_mcp", False):
                logger.removeHandler(existing)
                existing.close()

    handler: logging.Handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setLevel(resolved)
    handler.setFormatter(RedactingFormatter(LOG_FORMAT, datefmt=DATE_FORMAT))
    handler.addFilter(RedactingFilter())
    handler._garmin_coach_mcp = True  # type: ignore[attr-defined]  # noqa: SLF001
    logger.addHandler(handler)
    logger.propagate = False

    # Defence in depth for third-party loggers: redact whatever they emit, and make sure
    # nothing in the process is writing log records to stdout.
    root = logging.getLogger()
    if not any(isinstance(existing, RedactingFilter) for existing in root.filters):
        root.addFilter(RedactingFilter())
    for existing in list(root.handlers):
        if _is_stdout_handler(existing):
            root.removeHandler(existing)
    if not root.handlers:
        fallback = logging.StreamHandler(sys.stderr)
        fallback.setFormatter(RedactingFormatter(LOG_FORMAT, datefmt=DATE_FORMAT))
        fallback.addFilter(RedactingFilter())
        root.addHandler(fallback)
    root.setLevel(max(resolved, logging.WARNING))

    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a logger under the package namespace.

    Args:
        name: Usually `__name__`. A name outside the package namespace is nested under
            it, so every log line this server emits shares one configurable prefix.

    Returns:
        The logger.
    """
    if not name or name == _ROOT_LOGGER_NAME:
        return logging.getLogger(_ROOT_LOGGER_NAME)
    if name.startswith(f"{_ROOT_LOGGER_NAME}."):
        return logging.getLogger(name)
    return logging.getLogger(f"{_ROOT_LOGGER_NAME}.{name}")


def log_settings(logger: logging.Logger, settings: Settings) -> None:
    """Log the effective configuration, redacted.

    Args:
        logger: Where to write.
        settings: The configuration. Only `settings.redacted()` is ever rendered — the
            password is replaced by a marker and the email keeps its first character and
            domain only.
    """
    safe: dict[str, Any] = settings.redacted()
    logger.info(
        "configuration: account=%s credentials=%s token_dir=%s read_only=%s "
        "timeout=%.1fs max_retries=%d log_level=%s",
        safe["email"] or "<none>",
        "provided" if safe["password"] else "from saved tokens",
        safe["token_dir"],
        safe["read_only"],
        safe["request_timeout"],
        safe["max_retries"],
        safe["log_level"],
    )
