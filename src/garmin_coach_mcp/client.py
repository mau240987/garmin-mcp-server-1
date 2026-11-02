"""Authenticated Garmin Connect session (unofficial `garminconnect` library).

All library exceptions are mapped to `GarminMCPError` subclasses here, so tools never
see a raw third-party exception.
"""

from __future__ import annotations

import os
import re
import threading
import time
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


_STATUS_RE = re.compile(r"(?:API Error|client error \(|status[ =:]+)\s*(\d{3})", re.IGNORECASE)

#: After a failed login, further calls fail fast for this many seconds instead of hitting
#: Garmin's SSO again (repeated failed logins risk account lockout / bot blocking).
LOGIN_FAILURE_COOLDOWN_S = 60.0


def http_status(exc: BaseException) -> int | None:
    """HTTP status of a garminconnect/requests error, from `.response` or the message."""
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    if isinstance(code, int):
        return code
    match = _STATUS_RE.search(str(exc))
    return int(match.group(1)) if match else None


def error_for_status(status: int, exc: BaseException | None, operation: str) -> GarminMCPError:
    """Map an HTTP status to our taxonomy (5xx and unknown codes -> retryable upstream)."""
    if status in (401, 403):
        return AuthenticationError(
            f"Garmin Connect rejected the session during {operation} (HTTP {status}).",
            remediation=(
                "Check GARMIN_EMAIL/GARMIN_PASSWORD, or delete the token directory and log in "
                "again with `garmin-coach-mcp --login`."
            ),
            cause=exc,
        )
    if status == 429:
        return RateLimitError(
            f"Garmin Connect rate-limited {operation}.",
            remediation="Wait a few minutes before calling Garmin again; do not retry in a loop.",
            cause=exc,
        )
    if status == 404:
        return NotFoundError(
            f"{operation}: the requested item does not exist on Garmin Connect.",
            remediation="Check the id (use get_activities or get_workouts to list valid ids).",
            cause=exc,
        )
    if 400 <= status < 500:
        return InvalidRequestError(
            f"Garmin Connect refused {operation} as invalid (HTTP {status}).",
            remediation="Check the arguments; the request will fail the same way if repeated.",
            cause=exc,
        )
    return UpstreamServiceError(
        f"Garmin Connect failed during {operation} (HTTP {status}).",
        remediation="Retry later; Garmin Connect may be temporarily unavailable.",
        cause=exc,
    )


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

    status = http_status(exc)
    if (
        status is not None
        and status < 500
        and not isinstance(exc, GarminConnectAuthenticationError)
    ):
        return error_for_status(status, exc, operation)

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
        self._login_failure: tuple[float, GarminMCPError] | None = None

    @property
    def settings(self) -> Settings:
        return self._settings

    def _has_tokens(self) -> bool:
        d = self._settings.token_dir
        return d.is_dir() and any(d.iterdir())

    def api(self) -> Any:
        """Authenticated client: resume from stored tokens, else log in with credentials.

        Login errors are marked `during_login` and are never retried by `call`; a failed login
        is cached for LOGIN_FAILURE_COOLDOWN_S so one tool call cannot hammer Garmin's SSO.
        """
        with self._lock:
            if self._client is not None:
                return self._client
            if self._login_failure is not None:
                failed_at, error = self._login_failure
                if time.monotonic() - failed_at < LOGIN_FAILURE_COOLDOWN_S:
                    raise error
                self._login_failure = None
            try:
                client = self._login()
            except GarminMCPError as error:
                error.during_login = True
                self._login_failure = (time.monotonic(), error)
                raise
            log.info("Garmin Connect session ready")
            self._client = client
            return client

    def _login(self) -> Any:
        token_dir = self._settings.token_dir
        if not self._has_tokens() and not self._settings.has_credentials:
            raise ConfigurationError(
                "No saved Garmin session and no credentials configured.",
                remediation=(
                    "Set GARMIN_EMAIL and GARMIN_PASSWORD for the first login (tokens are "
                    f"then stored in {token_dir}), or point GARMIN_TOKEN_DIR "
                    "at an existing token directory."
                ),
            )
        try:
            token_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            raise ConfigurationError(
                f"Cannot create the token directory {token_dir}.",
                remediation="Set GARMIN_TOKEN_DIR to a directory this user can write.",
                cause=exc,
            ) from exc
        if not os.access(token_dir, os.W_OK | os.X_OK):
            # garminconnect silently ignores token-save failures; every restart would then
            # need a full (possibly MFA) login.
            raise ConfigurationError(
                f"The token directory {token_dir} is not writable.",
                remediation=(
                    "Fix its ownership/permissions (e.g. a Docker volume created by an older "
                    "root image: `chown -R 10001 /data/tokens`) or set GARMIN_TOKEN_DIR."
                ),
            )
        try:
            client = self._factory(self._settings)
            client.login(str(token_dir))
        except Exception as exc:
            raise map_exception(exc, "login") from exc
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
            except AuthenticationError as exc:
                # Re-login only when an established session was rejected, never after a
                # failed login (that would just repeat the failure against Garmin's SSO).
                if not idempotent or exc.during_login:
                    raise
                log.warning("%s: auth rejected, re-authenticating once", operation)
                self.invalidate()
                return attempt()

        if not idempotent:
            return attempt()
        return call_with_retry(with_reauth, operation=operation, retries=self._settings.max_retries)
