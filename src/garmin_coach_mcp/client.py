"""Authenticated Garmin Connect session: lazy login, token persistence, error mapping.

`GarminSession.call` is the single funnel every Garmin request goes through. It applies
the retry policy, maps upstream exceptions onto the error taxonomy, and logs the
operation name and duration — never the arguments, which routinely carry ids and could
carry credentials.

Authentication is lazy and guarded by a lock. The legacy server kept an unsynchronised
module-level global, so two concurrent tool calls on a cold start each ran a full login;
Garmin rate limits logins aggressively, and the second one would often fail while the
first was still in flight.
"""

from __future__ import annotations

import contextlib
import functools
import random
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar

from garminconnect import Garmin

from garmin_coach_mcp.errors import (
    GarminMCPError,
    map_exception,
    missing_credentials_error,
    read_only_error,
    token_expired_error,
)
from garmin_coach_mcp.logging_utils import get_logger, register_secret
from garmin_coach_mcp.retry import RetryPolicy, call_with_retry

if TYPE_CHECKING:  # pragma: no cover - typing only
    from garmin_coach_mcp.config import Settings

__all__ = ["GarminSession", "is_write_operation"]

T = TypeVar("T")

_LOGGER = get_logger(__name__)

#: Operation-name prefixes that mutate the Garmin account. Used only as a safety net for
#: `call(...)` sites that forget to pass `idempotent=`; an explicit argument always wins.
_WRITE_PREFIXES: tuple[str, ...] = (
    "add",
    "create",
    "delete",
    "modify",
    "post",
    "push",
    "put",
    "remove",
    "rename",
    "reschedule",
    "schedule",
    "set",
    "unschedule",
    "update",
    "upload",
)

#: Directory / file modes for the token store. These are credentials: anyone who can
#: read them can act as the account holder without knowing the password.
_TOKEN_DIR_MODE = 0o700
_TOKEN_FILE_MODE = 0o600


def is_write_operation(operation: str) -> bool:
    """Report whether an operation name looks like a write.

    Args:
        operation: The operation name passed to `GarminSession.call`.

    Returns:
        True when the name starts with a mutating verb.
    """
    name = operation.strip().lower()
    return any(name.startswith(prefix) for prefix in _WRITE_PREFIXES)


class GarminSession:
    """A lazily authenticated, thread-safe Garmin Connect session.

    Args:
        settings: Validated configuration.
        client_factory: Builds the underlying `Garmin` client. Injectable for tests.
        policy: Retry policy. Defaults to one derived from `settings.max_retries`.
        sleep: Sleep function used for backoff. Injectable so tests do not sleep.
        rng: Random source for backoff jitter. Injectable for determinism.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        client_factory: Callable[..., Garmin] = Garmin,
        policy: RetryPolicy | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        """Store the configuration; no network access happens here."""
        self._settings = settings
        self._client_factory = client_factory
        self._policy = policy if policy is not None else RetryPolicy(max_retries=settings.max_retries)
        self._sleep = sleep
        self._rng = rng
        self._lock = threading.RLock()
        self._api: Garmin | None = None
        # So that a password echoed back inside an upstream error can never be logged.
        register_secret(settings.password)

    # -- configuration ------------------------------------------------------

    @property
    def settings(self) -> Settings:
        """The configuration this session was built with."""
        return self._settings

    @property
    def authenticated(self) -> bool:
        """Whether a client has already been authenticated and cached."""
        with self._lock:
            return self._api is not None

    # -- authentication -----------------------------------------------------

    def api(self) -> Garmin:
        """Return the authenticated client, logging in on first use.

        Resumes from saved tokens when there are any, and only falls back to the
        email/password pair when they are unusable.

        Returns:
            The authenticated `garminconnect.Garmin` client.

        Raises:
            GarminMCPError: Configuration or authentication failures, each carrying the
                exact remediation for the situation.
        """
        with self._lock:
            if self._api is not None:
                return self._api
            self._api = self._authenticate()
            return self._api

    def invalidate(self) -> None:
        """Drop the cached client so the next call authenticates again.

        The saved tokens on disk are left alone: they may still be valid, and the next
        login will try them first.
        """
        with self._lock:
            if self._api is not None:
                _LOGGER.info("Garmin session invalidated; the next call will re-authenticate")
            self._api = None

    def _authenticate(self) -> Garmin:
        """Perform the actual login. Always called with the lock held."""
        settings = self._settings
        resume_failure: BaseException | None = None

        if settings.has_saved_tokens:
            try:
                api = self._client_factory()
                self._apply_timeout(api)
                api.login(str(settings.token_dir))
            except Exception as exc:  # noqa: BLE001 - re-raised below, mapped, never swallowed
                resume_failure = exc
                _LOGGER.warning(
                    "Saved Garmin session in %s is unusable (%s)",
                    settings.token_dir,
                    type(exc).__name__,
                )
            else:
                _LOGGER.info("Resumed Garmin session from saved tokens in %s", settings.token_dir)
                return api

            if not settings.has_credentials:
                raise token_expired_error(
                    settings.token_dir, cause=resume_failure, have_credentials=False
                ) from resume_failure
            self._discard_stale_tokens()

        if not settings.has_credentials:
            raise missing_credentials_error(settings.token_dir)

        _LOGGER.info("Logging in to Garmin Connect with credentials")
        api = self._client_factory(email=settings.email, password=settings.password)
        self._apply_timeout(api)
        try:
            # prompt_mfa is deliberately left unset: garminconnect then raises a typed
            # authentication error for an MFA challenge, which maps to a message
            # explaining how to complete the login out of band. There is no interactive
            # terminal here to type a code into.
            api.login()
        except Exception as exc:  # noqa: BLE001 - mapped to the taxonomy immediately
            raise map_exception(
                exc,
                operation="login",
                token_dir=settings.token_dir,
                timeout=settings.request_timeout,
            ) from exc

        self._persist_tokens(api)
        _LOGGER.info(
            "Logged in to Garmin Connect; tokens cached in %s for future starts",
            settings.token_dir,
        )
        return api

    def _discard_stale_tokens(self) -> None:
        """Remove a token file that Garmin no longer accepts.

        Leaving it in place would make the next start resume the same dead session. It
        is a cache this server wrote itself, so removing it loses nothing.
        """
        token_file = self._settings.token_file
        try:
            token_file.unlink(missing_ok=True)
        except OSError as exc:
            _LOGGER.warning(
                "Could not remove the stale token file %s (%s); delete it by hand if "
                "logins keep failing",
                token_file,
                exc,
            )
        else:
            _LOGGER.info("Removed stale token file %s; logging in with credentials", token_file)

    def _persist_tokens(self, api: Garmin) -> None:
        """Write the OAuth tokens to disk with owner-only permissions.

        Failure to persist is a warning, not an error: the session in memory works, the
        user just has to log in again next start. The legacy server wrapped this in a
        bare `except Exception: pass`, so a wrong path silently forced a fresh login —
        and therefore a fresh rate-limited SSO round trip — on every single start.
        """
        settings = self._settings
        token_dir = settings.token_dir
        token_file = settings.token_file
        try:
            token_dir.mkdir(parents=True, exist_ok=True, mode=_TOKEN_DIR_MODE)
            token_dir.chmod(_TOKEN_DIR_MODE)
            # Create the file with restrictive permissions *before* the library writes
            # secrets into it, so the tokens are never briefly world-readable.
            token_file.touch(mode=_TOKEN_FILE_MODE, exist_ok=True)
            token_file.chmod(_TOKEN_FILE_MODE)
            api.client.dump(str(token_dir))
            token_file.chmod(_TOKEN_FILE_MODE)
        except (OSError, AttributeError) as exc:
            _LOGGER.warning(
                "Logged in, but could not save tokens to %s (%s). The server will have to "
                "log in again on the next start; check the directory exists and is "
                "writable, or point GARMIN_TOKEN_DIR somewhere else.",
                token_dir,
                exc,
            )

    def _apply_timeout(self, api: Garmin) -> None:
        """Force `settings.request_timeout` onto the underlying HTTP sessions.

        `garminconnect` builds a fresh `requests.Session` per call and hardcodes a 15s
        timeout, so the setting would otherwise be a lie. This reaches into a private
        factory on purpose; when the library changes shape the wrapper simply does not
        install and the library default applies.
        """
        timeout = self._settings.request_timeout
        client = getattr(api, "client", None)
        factory = getattr(client, "_fresh_api_session", None)
        if client is None or not callable(factory):
            _LOGGER.debug(
                "garminconnect exposes no session factory; GARMIN_REQUEST_TIMEOUT "
                "(%.1fs) cannot be applied and the library default is used",
                timeout,
            )
            return
        if getattr(factory, "_garmin_coach_mcp_timeout", None) is not None:
            return

        @functools.wraps(factory)
        def make_session() -> Any:  # noqa: ANN401 - whatever the library's session type is
            session = factory()
            original = session.request

            @functools.wraps(original)
            def request(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
                kwargs["timeout"] = timeout
                return original(*args, **kwargs)

            session.request = request
            return session

        make_session._garmin_coach_mcp_timeout = timeout  # type: ignore[attr-defined]  # noqa: SLF001
        with contextlib.suppress(AttributeError, TypeError):
            client._fresh_api_session = make_session  # noqa: SLF001

    # -- the single call funnel --------------------------------------------

    def call(
        self,
        operation: str,
        fn: Callable[[Garmin], T],
        *,
        idempotent: bool | None = None,
    ) -> T:
        """Run `fn` against the authenticated client, with retry and error mapping.

        Args:
            operation: Short name of what is being done, e.g. `get_activities`. Used in
                log lines and error messages, so make it match the Garmin call.
            fn: Receives the authenticated client and performs one attempt. It must be
                safe to run again when `idempotent` is True.
            idempotent: Whether repeating the call is harmless. When omitted, it is
                inferred from `operation`: names starting with a mutating verb
                (`create_`, `upload_`, `delete_`, `schedule_`, ...) are treated as
                non-idempotent and are never retried. Pass the flag explicitly when the
                name does not tell the truth.

        Returns:
            Whatever `fn` returns.

        Raises:
            GarminMCPError: On any failure. Subclasses only — no raw upstream exception
                escapes, and no error is ever returned as a successful value.
        """
        writes = is_write_operation(operation) if idempotent is None else not idempotent
        if writes and self._settings.read_only:
            raise read_only_error(operation)

        started = time.monotonic()
        _LOGGER.debug("operation=%s starting (idempotent=%s)", operation, not writes)
        try:
            result = call_with_retry(
                lambda: fn(self.api()),
                operation=operation,
                policy=self._policy,
                idempotent=not writes,
                token_dir=str(self._settings.token_dir),
                timeout=self._settings.request_timeout,
                on_auth_error=self.invalidate,
                sleep=self._sleep,
                rng=self._rng,
                logger=_LOGGER,
            )
        except GarminMCPError as error:
            _LOGGER.error(
                "operation=%s failed after %.3fs: %s: %s",
                operation,
                time.monotonic() - started,
                type(error).__name__,
                error.message,
            )
            raise
        _LOGGER.info("operation=%s succeeded in %.3fs", operation, time.monotonic() - started)
        return result
