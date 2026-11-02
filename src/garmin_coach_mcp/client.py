"""Authenticated Garmin Connect session (unofficial `garminconnect` library).

All library exceptions are mapped to `GarminMCPError` subclasses here, so tools never
see a raw third-party exception.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any, Protocol, TypeVar

from garmin_coach_mcp.config import Settings
from garmin_coach_mcp.errors import (
    AuthenticationError,
    ConfigurationError,
    GarminMCPError,
    InvalidRequestError,
    NotFoundError,
    RateLimitError,
    UpstreamServiceError,
)
from garmin_coach_mcp.logging_utils import get_logger
from garmin_coach_mcp.retry import call_with_retry

T = TypeVar("T")
log = get_logger("client")

ClientFactory = Callable[[Settings], Any]


class SupportsCall(Protocol):
    """What tools need from a session. Lets tests inject a fake."""

    @property
    def settings(self) -> Settings: ...

    def call(self, operation: str, fn: Callable[[Any], T], *, idempotent: bool = True) -> T: ...


def _default_factory(settings: Settings) -> Any:
    from garminconnect import Garmin

    # retry_attempts=0: the library would otherwise retry POSTs (workout creation) on 5xx.
    # Retries are owned by `GarminSession.call`, which only retries idempotent operations.
    return Garmin(settings.email, settings.password, retry_attempts=0)


def map_exception(exc: BaseException, operation: str) -> GarminMCPError:
    """Translate a garminconnect / transport exception into our taxonomy."""
    if isinstance(exc, GarminMCPError):
        return exc
    try:
        from garminconnect import (
            GarminConnectAuthenticationError,
            GarminConnectConnectionError,
            GarminConnectNotFoundError,
            GarminConnectTooManyRequestsError,
        )
    except ImportError:  # pragma: no cover - dependency is mandatory
        return UpstreamServiceError(f"{operation} failed.", cause=exc)

    if isinstance(exc, GarminConnectAuthenticationError):
        return AuthenticationError(
            f"Garmin Connect rejected the login during {operation}.",
            remediation=(
                "Check GARMIN_EMAIL/GARMIN_PASSWORD, or delete the token directory and log in "
                "again. If the account uses MFA, run `garmin-coach-mcp --login` in a terminal "
                "once to store tokens."
            ),
            cause=exc,
        )
    if isinstance(exc, GarminConnectTooManyRequestsError):
        return RateLimitError(
            f"Garmin Connect rate-limited {operation}.",
            remediation="Wait a few minutes before calling Garmin again; do not retry in a loop.",
            cause=exc,
        )
    if isinstance(exc, GarminConnectNotFoundError):
        return NotFoundError(
            f"{operation}: the requested item does not exist on Garmin Connect.",
            remediation="Check the id (use get_activities or get_workouts to list valid ids).",
            cause=exc,
        )
    if isinstance(exc, GarminConnectConnectionError):
        text = str(exc)
        if "client error (4" in text:
            return InvalidRequestError(
                f"Garmin Connect refused {operation} as invalid.",
                remediation="Check the arguments; the request will fail the same way if repeated.",
                cause=exc,
            )
        return UpstreamServiceError(
            f"Garmin Connect is unreachable or failing during {operation}.",
            remediation="Retry later; Garmin Connect may be temporarily unavailable.",
            cause=exc,
        )
    if isinstance(exc, ValueError):
        return InvalidRequestError(
            f"Invalid argument for {operation}: {exc}",
            remediation="Fix the argument and call the tool again.",
            cause=exc,
        )
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return UpstreamServiceError(
            f"Network error during {operation}.",
            remediation="Check the network connection and retry.",
            cause=exc,
        )
    return UpstreamServiceError(
        f"Unexpected failure during {operation} ({type(exc).__name__}).",
        remediation="Retry once; if it keeps failing, report it with the server logs.",
        cause=exc,
    )


class GarminSession:
    """Lazily authenticated, thread-safe Garmin client with retry and error mapping."""

    def __init__(self, settings: Settings, factory: ClientFactory | None = None) -> None:
        self._settings = settings
        self._factory = factory or _default_factory
        self._client: Any = None
        self._lock = threading.Lock()

    @property
    def settings(self) -> Settings:
        return self._settings

    def _has_tokens(self) -> bool:
        d = self._settings.token_dir
        return d.is_dir() and any(d.iterdir())

    def api(self) -> Any:
        """Authenticated client: resume from stored tokens, else log in with credentials."""
        with self._lock:
            if self._client is not None:
                return self._client
            if not self._has_tokens() and not self._settings.has_credentials:
                raise ConfigurationError(
                    "No saved Garmin session and no credentials configured.",
                    remediation=(
                        "Set GARMIN_EMAIL and GARMIN_PASSWORD for the first login (tokens are "
                        f"then stored in {self._settings.token_dir}), or point GARMIN_TOKEN_DIR "
                        "at an existing token directory."
                    ),
                )
            self._settings.token_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                client = self._factory(self._settings)
                client.login(str(self._settings.token_dir))
            except Exception as exc:
                raise map_exception(exc, "login") from exc
            log.info("Garmin Connect session ready")
            self._client = client
            return client

    def invalidate(self) -> None:
        with self._lock:
            self._client = None

    def call(self, operation: str, fn: Callable[[Any], T], *, idempotent: bool = True) -> T:
        """Run `fn(client)` with error mapping.

        Idempotent operations are retried on transient upstream errors, and re-authenticated
        once on an auth error. Non-idempotent ones (creates) run exactly once.
        """

        def attempt() -> T:
            client = self.api()
            try:
                return fn(client)
            except Exception as exc:
                raise map_exception(exc, operation) from exc

        def with_reauth() -> T:
            try:
                return attempt()
            except AuthenticationError:
                if not idempotent:
                    raise
                log.warning("%s: auth rejected, re-authenticating once", operation)
                self.invalidate()
                return attempt()

        if not idempotent:
            return attempt()
        return call_with_retry(with_reauth, operation=operation, retries=self._settings.max_retries)
