"""Runtime settings, read from environment variables."""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from garmin_coach_mcp.errors import ConfigurationError

DEFAULT_TOKEN_DIR = "~/.garmin-coach-mcp/tokens"  # noqa: S105 - a path, not a secret
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


def _parse_bool(name: str, raw: str | None, default: bool) -> bool:
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigurationError(
        f"{name} must be a boolean, got {raw!r}.",
        remediation=f"Set {name} to 'true' or 'false'.",
    )


def _parse_int(name: str, raw: str | None, default: int, *, lo: int, hi: int) -> int:
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(
            f"{name} must be an integer, got {raw!r}.",
            remediation=f"Set {name} to an integer between {lo} and {hi}.",
        ) from exc
    if not lo <= value <= hi:
        raise ConfigurationError(
            f"{name} must be between {lo} and {hi}, got {value}.",
            remediation=f"Set {name} to an integer between {lo} and {hi}.",
        )
    return value


@dataclass(frozen=True)
class Settings:
    """Server settings. Credentials are excluded from repr so they never reach logs."""

    email: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)
    token_dir: Path = field(default_factory=lambda: Path(DEFAULT_TOKEN_DIR).expanduser())
    read_only: bool = False
    max_retries: int = 2
    log_level: str = "INFO"
    #: Official Garmin Connect Personal API key (GARMIN_PERSONAL_API_KEY). Never logged.
    personal_api_key: str | None = field(default=None, repr=False)
    #: Expiry date chosen when the key was created (Garmin does not expose it via the API).
    personal_api_key_expires: dt.date | None = None
    personal_api_region: str = "global"
    #: Let reads the official API cannot serve (e.g. Training Readiness) fall back to the
    #: unofficial backend. Off by default (strategy A, docs/api-coverage.md §7).
    allow_unofficial_reads: bool = False
    #: Let workout create/schedule/delete use the unofficial backend. Off by default (A3):
    #: unofficial access may breach Garmin's terms and risks the account (docs, risk R4).
    allow_unofficial_writes: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **overrides: Any) -> Settings:
        """Build settings from `env` (defaults to `os.environ`), then apply overrides."""
        source: Mapping[str, str] = os.environ if env is None else env

        token_raw = source.get("GARMIN_TOKEN_DIR") or source.get("GARTH_TOKEN_DIR")
        token_dir = Path(token_raw or DEFAULT_TOKEN_DIR).expanduser().absolute()

        log_level = (source.get("GARMIN_LOG_LEVEL") or "INFO").strip().upper()
        if log_level not in LOG_LEVELS:
            raise ConfigurationError(
                f"GARMIN_LOG_LEVEL must be one of {', '.join(LOG_LEVELS)}, got {log_level!r}.",
                remediation="Fix GARMIN_LOG_LEVEL or remove it to use INFO.",
            )

        expires_raw = (source.get("GARMIN_PERSONAL_API_KEY_EXPIRES") or "").strip()
        try:
            expires = dt.date.fromisoformat(expires_raw) if expires_raw else None
        except ValueError as exc:
            raise ConfigurationError(
                f"GARMIN_PERSONAL_API_KEY_EXPIRES must be YYYY-MM-DD, got {expires_raw!r}.",
                remediation="Set it to the expiry date shown in Garmin Connect, or remove it.",
            ) from exc
        region = (source.get("GARMIN_PERSONAL_API_REGION") or "global").strip().lower()
        if region not in ("global", "cn"):
            raise ConfigurationError(
                f"GARMIN_PERSONAL_API_REGION must be 'global' or 'cn', got {region!r}.",
                remediation="Use 'cn' only for Garmin China accounts.",
            )

        settings = cls(
            email=(source.get("GARMIN_EMAIL") or None),
            password=(source.get("GARMIN_PASSWORD") or None),
            token_dir=token_dir,
            read_only=_parse_bool("GARMIN_READ_ONLY", source.get("GARMIN_READ_ONLY"), False),
            max_retries=_parse_int(
                "GARMIN_MAX_RETRIES", source.get("GARMIN_MAX_RETRIES"), 2, lo=0, hi=10
            ),
            log_level=log_level,
            personal_api_key=(source.get("GARMIN_PERSONAL_API_KEY") or "").strip() or None,
            personal_api_key_expires=expires,
            personal_api_region=region,
            allow_unofficial_reads=_parse_bool(
                "GARMIN_ALLOW_UNOFFICIAL_READS", source.get("GARMIN_ALLOW_UNOFFICIAL_READS"), False
            ),
            allow_unofficial_writes=_parse_bool(
                "GARMIN_ALLOW_UNOFFICIAL_WRITES",
                source.get("GARMIN_ALLOW_UNOFFICIAL_WRITES"),
                False,
            ),
        )
        clean = {k: v for k, v in overrides.items() if v is not None}
        return replace(settings, **clean) if clean else settings

    @property
    def has_credentials(self) -> bool:
        return bool(self.email and self.password)

    @property
    def has_unofficial_session(self) -> bool:
        """Credentials or stored tokens exist for the unofficial backend (no network check)."""
        if self.has_credentials:
            return True
        try:
            return self.token_dir.is_dir() and any(self.token_dir.iterdir())
        except OSError:  # unreadable token dir: not a usable session
            return False
