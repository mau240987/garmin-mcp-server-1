"""Settings for garmin-coach-mcp, parsed and validated from the environment.

Every value is validated at startup rather than at first use, so a typo in
`GARMIN_MAX_RETRIES` fails immediately with a message naming the variable and the bad
value — not with a `ValueError` from deep inside a retry loop an hour later.

Nothing in this module ever renders the password. `Settings.redacted()` and
`repr(settings)` are the only supported ways to print a configuration, and both mask the
password entirely and the email partially. The legacy server logged the full email on
every login; that is the mistake this class exists to prevent.
"""

from __future__ import annotations

import os
import warnings
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from garmin_coach_mcp.errors import (
    ENV_EMAIL,
    ENV_LOG_LEVEL,
    ENV_MAX_RETRIES,
    ENV_PASSWORD,
    ENV_READ_ONLY,
    ENV_REQUEST_TIMEOUT,
    ENV_TOKEN_DIR,
    ENV_TOKEN_DIR_DEPRECATED,
    TOKENS_FILENAME,
    ConfigurationError,
)

__all__ = [
    "DEFAULT_LOG_LEVEL",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_REQUEST_TIMEOUT",
    "DEFAULT_TOKEN_DIR",
    "ENV_VARS",
    "Settings",
]

DEFAULT_TOKEN_DIR = "~/.garmin-coach-mcp/tokens"
DEFAULT_READ_ONLY = False
DEFAULT_REQUEST_TIMEOUT = 20.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_LOG_LEVEL = "INFO"

#: Bounds that keep an obvious typo from hanging the server for an hour.
MIN_REQUEST_TIMEOUT = 0.1
MAX_REQUEST_TIMEOUT = 600.0
MAX_MAX_RETRIES = 10

#: Every environment variable this server reads, in documentation order.
ENV_VARS: tuple[str, ...] = (
    ENV_EMAIL,
    ENV_PASSWORD,
    ENV_TOKEN_DIR,
    ENV_READ_ONLY,
    ENV_REQUEST_TIMEOUT,
    ENV_MAX_RETRIES,
    ENV_LOG_LEVEL,
)

_TRUE_VALUES = frozenset({"1", "true", "t", "yes", "y", "on"})
_FALSE_VALUES = frozenset({"0", "false", "f", "no", "n", "off"})
_LOG_LEVELS = ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG")


def _default_token_dir() -> Path:
    """Return the default token directory, expanded and absolute."""
    return _resolve_dir(DEFAULT_TOKEN_DIR)


def _resolve_dir(value: str | Path) -> Path:
    """Expand `~`, make absolute, and normalise a directory path."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    # `resolve()` is safe on a path that does not exist yet (strict=False by default).
    return path.resolve()


def _parse_bool(raw: str, *, variable: str) -> bool:
    """Parse a boolean environment value, case-insensitively."""
    normalised = raw.strip().lower()
    if normalised in _TRUE_VALUES:
        return True
    if normalised in _FALSE_VALUES:
        return False
    raise ConfigurationError(
        f"{variable}={raw!r} is not a boolean.",
        remediation=(
            f"Set {variable} to one of {', '.join(sorted(_TRUE_VALUES))} to enable it, or "
            f"one of {', '.join(sorted(_FALSE_VALUES))} to disable it (case does not "
            f"matter). Unset the variable entirely to use the default "
            f"({str(DEFAULT_READ_ONLY).lower()}). Note that an empty value counts as "
            f"unset, but any other string is an error rather than a silent 'true'."
        ),
    )


def _parse_float(
    raw: str | float,
    *,
    variable: str,
    minimum: float,
    maximum: float,
) -> float:
    """Parse and range-check a float setting."""
    try:
        value = float(raw.strip() if isinstance(raw, str) else raw)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(
            f"{variable}={raw!r} is not a number.",
            remediation=(
                f"Set {variable} to a number of seconds between {minimum:g} and "
                f"{maximum:g} (for example {variable}=60), or unset it to use the default "
                f"of {DEFAULT_REQUEST_TIMEOUT:g}."
            ),
            cause=exc,
        ) from exc
    if value != value or not minimum <= value <= maximum:  # noqa: PLR0124 - NaN check
        raise ConfigurationError(
            f"{variable}={raw!r} is out of range.",
            remediation=(
                f"Set {variable} between {minimum:g} and {maximum:g} seconds. Values at or "
                f"below zero would make every request fail instantly; values above "
                f"{maximum:g} would let a single call hang the MCP client for longer than "
                f"it is willing to wait. The default is {DEFAULT_REQUEST_TIMEOUT:g}."
            ),
        )
    return value


def _parse_int(raw: str | int, *, variable: str, minimum: int, maximum: int) -> int:
    """Parse and range-check an integer setting."""
    try:
        value = int(raw.strip() if isinstance(raw, str) else raw)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(
            f"{variable}={raw!r} is not a whole number.",
            remediation=(
                f"Set {variable} to an integer between {minimum} and {maximum} (for "
                f"example {variable}=3), or unset it to use the default of "
                f"{DEFAULT_MAX_RETRIES}. Use 0 to disable retries entirely."
            ),
            cause=exc,
        ) from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(
            f"{variable}={raw!r} is out of range.",
            remediation=(
                f"Set {variable} between {minimum} and {maximum}. {minimum} disables "
                f"retries; more than {maximum} would keep a single tool call waiting for "
                f"many minutes, and Garmin rate limits long before that helps. The default "
                f"is {DEFAULT_MAX_RETRIES}."
            ),
        )
    return value


def _parse_log_level(raw: str, *, variable: str) -> str:
    """Parse and normalise a logging level name."""
    value = raw.strip().upper()
    if value not in _LOG_LEVELS:
        raise ConfigurationError(
            f"{variable}={raw!r} is not a logging level.",
            remediation=(
                f"Set {variable} to one of {', '.join(_LOG_LEVELS)} (case does not "
                f"matter), or unset it to use {DEFAULT_LOG_LEVEL}. Use DEBUG when "
                f"reporting a bug: it logs every Garmin call with its duration to stderr, "
                f"with credentials and tokens redacted."
            ),
        )
    return value


def _mask_email(email: str | None) -> str | None:
    """Mask the local part of an email so logs identify the account without leaking it."""
    if not email:
        return None
    local, separator, domain = email.partition("@")
    if not separator:
        return f"{local[:1]}***"
    return f"{local[:1]}***@{domain}"


@dataclass(frozen=True)
class Settings:
    """Validated runtime configuration.

    Attributes:
        email: Garmin Connect account email, or None when only saved tokens are used.
        password: Garmin Connect password. Never logged, never included in `repr`.
        token_dir: Directory holding the saved OAuth tokens. Expanded and absolute.
        read_only: When True, write tools are not registered at all.
        request_timeout: Per-request timeout in seconds.
        max_retries: Retries after the first attempt for retryable failures.
        log_level: Logging level name for the stderr handler.
    """

    email: str | None = None
    password: str | None = None
    token_dir: Path = field(default_factory=_default_token_dir)
    read_only: bool = DEFAULT_READ_ONLY
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT
    max_retries: int = DEFAULT_MAX_RETRIES
    log_level: str = DEFAULT_LOG_LEVEL

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        **overrides: Any,  # noqa: ANN401 - heterogeneous per-field overrides
    ) -> Settings:
        """Build settings from environment variables, with explicit overrides on top.

        Args:
            env: Mapping to read from. Defaults to `os.environ`.
            **overrides: Field values that win over the environment. A value of None
                means "not specified", so a CLI can pass every flag unconditionally.

        Returns:
            A validated `Settings`.

        Raises:
            ConfigurationError: If any value is missing a valid form. The message names
                the variable and the offending value.
        """
        source: Mapping[str, str] = os.environ if env is None else env

        unknown = set(overrides) - {f.name for f in fields(cls)}
        if unknown:
            raise ConfigurationError(
                f"Unknown setting(s): {', '.join(sorted(unknown))}.",
                remediation=(
                    "Settings.from_env only accepts the documented fields: "
                    f"{', '.join(f.name for f in fields(cls))}."
                ),
            )
        given = {key: value for key, value in overrides.items() if value is not None}

        values: dict[str, Any] = {
            "email": cls._env_email(source),
            "password": cls._env_password(source),
            "token_dir": cls._env_token_dir(source),
            "read_only": cls._env_flag(source, ENV_READ_ONLY, DEFAULT_READ_ONLY),
            "request_timeout": cls._env_timeout(source),
            "max_retries": cls._env_retries(source),
            "log_level": cls._env_log_level(source),
        }
        values.update(given)
        return cls(**cls._coerce(values))

    # -- individual readers -------------------------------------------------

    @staticmethod
    def _env_email(env: Mapping[str, str]) -> str | None:
        """Read and normalise the account email."""
        raw = env.get(ENV_EMAIL)
        return raw.strip() or None if raw else None

    @staticmethod
    def _env_password(env: Mapping[str, str]) -> str | None:
        """Read the password.

        Only a trailing newline is stripped (a common artefact of `read`, here-docs and
        secret files). Internal and leading whitespace is preserved: it may be part of
        the password, and silently changing a credential is worse than a login failure.
        """
        raw = env.get(ENV_PASSWORD)
        if raw is None:
            return None
        value = raw.rstrip("\r\n")
        return value or None

    @staticmethod
    def _env_token_dir(env: Mapping[str, str]) -> Path:
        """Read the token directory, honouring the deprecated alias."""
        primary = (env.get(ENV_TOKEN_DIR) or "").strip()
        legacy = (env.get(ENV_TOKEN_DIR_DEPRECATED) or "").strip()
        if primary:
            if legacy:
                warnings.warn(
                    f"Both {ENV_TOKEN_DIR} and the deprecated {ENV_TOKEN_DIR_DEPRECATED} "
                    f"are set; using {ENV_TOKEN_DIR}={primary!r} and ignoring "
                    f"{ENV_TOKEN_DIR_DEPRECATED}. Unset {ENV_TOKEN_DIR_DEPRECATED}.",
                    DeprecationWarning,
                    stacklevel=3,
                )
            return _resolve_dir(primary)
        if legacy:
            warnings.warn(
                f"{ENV_TOKEN_DIR_DEPRECATED} is deprecated and will be removed in a future "
                f"release; rename it to {ENV_TOKEN_DIR} (same value, {legacy!r}). Existing "
                f"tokens keep working — only the variable name changes.",
                DeprecationWarning,
                stacklevel=3,
            )
            return _resolve_dir(legacy)
        return _default_token_dir()

    @staticmethod
    def _env_flag(env: Mapping[str, str], variable: str, default: bool) -> bool:
        """Read a boolean variable."""
        raw = env.get(variable)
        if raw is None or not raw.strip():
            return default
        return _parse_bool(raw, variable=variable)

    @staticmethod
    def _env_timeout(env: Mapping[str, str]) -> float:
        """Read the per-request timeout."""
        raw = env.get(ENV_REQUEST_TIMEOUT)
        if raw is None or not raw.strip():
            return DEFAULT_REQUEST_TIMEOUT
        return _parse_float(
            raw,
            variable=ENV_REQUEST_TIMEOUT,
            minimum=MIN_REQUEST_TIMEOUT,
            maximum=MAX_REQUEST_TIMEOUT,
        )

    @staticmethod
    def _env_retries(env: Mapping[str, str]) -> int:
        """Read the retry budget."""
        raw = env.get(ENV_MAX_RETRIES)
        if raw is None or not raw.strip():
            return DEFAULT_MAX_RETRIES
        return _parse_int(raw, variable=ENV_MAX_RETRIES, minimum=0, maximum=MAX_MAX_RETRIES)

    @staticmethod
    def _env_log_level(env: Mapping[str, str]) -> str:
        """Read the log level."""
        raw = env.get(ENV_LOG_LEVEL)
        if raw is None or not raw.strip():
            return DEFAULT_LOG_LEVEL
        return _parse_log_level(raw, variable=ENV_LOG_LEVEL)

    @staticmethod
    def _coerce(values: dict[str, Any]) -> dict[str, Any]:
        """Re-validate the merged values, so overrides get the same checks as the env.

        A `--max-retries` flag or a programmatic override is not more trustworthy than an
        environment variable, and both must fail with the same actionable message.
        """
        coerced = dict(values)

        token_dir = coerced["token_dir"]
        if not isinstance(token_dir, Path) or not token_dir.is_absolute():
            coerced["token_dir"] = _resolve_dir(token_dir)

        read_only = coerced["read_only"]
        if not isinstance(read_only, bool):
            coerced["read_only"] = _parse_bool(str(read_only), variable=ENV_READ_ONLY)

        coerced["request_timeout"] = _parse_float(
            coerced["request_timeout"],
            variable=ENV_REQUEST_TIMEOUT,
            minimum=MIN_REQUEST_TIMEOUT,
            maximum=MAX_REQUEST_TIMEOUT,
        )
        coerced["max_retries"] = _parse_int(
            coerced["max_retries"],
            variable=ENV_MAX_RETRIES,
            minimum=0,
            maximum=MAX_MAX_RETRIES,
        )
        coerced["log_level"] = _parse_log_level(str(coerced["log_level"]), variable=ENV_LOG_LEVEL)

        for key in ("email", "password"):
            value = coerced[key]
            if value is not None and not isinstance(value, str):
                coerced[key] = str(value)
        return coerced

    # -- derived values -----------------------------------------------------

    @property
    def token_file(self) -> Path:
        """Path of the token file `garminconnect` writes inside `token_dir`."""
        return self.token_dir / TOKENS_FILENAME

    @property
    def has_credentials(self) -> bool:
        """Whether both an email and a password are available for a fresh login."""
        return bool(self.email and self.password)

    @property
    def has_saved_tokens(self) -> bool:
        """Whether a previously saved session exists on disk."""
        if self.token_file.is_file():
            return True
        return self.token_dir.is_dir() and any(self.token_dir.glob("*.json"))

    # -- safe rendering -----------------------------------------------------

    def redacted(self) -> dict[str, Any]:
        """Return a dict safe to log or print.

        The password is replaced by a marker; the email keeps only its first character
        and its domain, which is enough to tell two accounts apart without writing the
        address into a log file.
        """
        return {
            "email": _mask_email(self.email),
            "password": "<set>" if self.password else None,
            "token_dir": str(self.token_dir),
            "read_only": self.read_only,
            "request_timeout": self.request_timeout,
            "max_retries": self.max_retries,
            "log_level": self.log_level,
        }

    def __repr__(self) -> str:
        """Return a representation that cannot leak the password.

        The dataclass-generated `__repr__` would print the password in full, and a
        `Settings` object ends up inside tracebacks, `logger.debug("%r", settings)` calls
        and pytest assertion output. This override is the only `__repr__` there is.
        """
        inner = ", ".join(f"{key}={value!r}" for key, value in self.redacted().items())
        return f"Settings({inner})"

    def __str__(self) -> str:
        """Return the redacted representation."""
        return self.__repr__()
