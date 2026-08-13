"""Exception taxonomy for garmin-coach-mcp.

Two rules govern this module:

1. **Every error carries a remediation.** A message that only says what went wrong wastes
   the caller's turn. `remediation` says what to do next, naming the exact environment
   variable, path, or command involved.
2. **Classification is by exception type and HTTP status, never by substring.** The legacy
   server decided retryability with `str(exc).lower()` substring matching, so any message
   containing the word "invalid" became non-retryable and any message containing "token"
   triggered a re-login. Here, `classify_exception` walks the `__cause__` chain, inspects
   the real exception classes raised by `garminconnect`, `requests`, `httpx` and
   `curl_cffi`, and reads the status code off the attached response. Message parsing is a
   last-resort fallback, needed only because `garminconnect` formats the status into a
   plain string (`GarminConnectConnectionError("API Error 404 - ...")`) with no response
   object attached, and even then it matches only that library's known phrasings.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import ClassVar

from mcp.server.mcpserver.exceptions import ToolError

__all__ = [
    "AuthenticationError",
    "ConfigurationError",
    "GarminMCPError",
    "InvalidRequestError",
    "NotFoundError",
    "RateLimitError",
    "ReadOnlyModeError",
    "UpstreamServiceError",
    "classify_exception",
    "extract_status_code",
    "invalid_credentials_error",
    "map_exception",
    "mfa_required_error",
    "missing_credentials_error",
    "read_only_error",
    "token_expired_error",
]

# Environment variable names live here so that error text and `config.py` cannot drift
# apart. `config` imports these; this module imports nothing from `config`.
ENV_EMAIL = "GARMIN_EMAIL"
ENV_PASSWORD = "GARMIN_PASSWORD"  # noqa: S105 - a variable name, not a credential
ENV_TOKEN_DIR = "GARMIN_TOKEN_DIR"
ENV_TOKEN_DIR_DEPRECATED = "GARTH_TOKEN_DIR"
ENV_READ_ONLY = "GARMIN_READ_ONLY"
ENV_REQUEST_TIMEOUT = "GARMIN_REQUEST_TIMEOUT"
ENV_MAX_RETRIES = "GARMIN_MAX_RETRIES"
ENV_LOG_LEVEL = "GARMIN_LOG_LEVEL"

#: Filename `garminconnect` uses inside the token directory.
TOKENS_FILENAME = "garmin_tokens.json"


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------


class GarminMCPError(Exception):
    """Base class for every error this server raises.

    Args:
        message: What went wrong, in one or two sentences, in the user's terms.
        remediation: What the caller should do next. Falls back to the subclass
            `default_remediation` so an error is never left unactionable.
        cause: The upstream exception this was mapped from, if any. Also chained
            onto `__cause__` so tracebacks stay intact.
        retryable: Overrides the class default. `retry.call_with_retry` consults this.
        retry_after: Seconds the upstream asked us to wait, when it said so.
    """

    #: Stable, machine-readable discriminator included in the tool error text.
    code: ClassVar[str] = "garmin_error"

    #: Used when a caller constructs the error without spelling out a remediation.
    default_remediation: ClassVar[str | None] = None

    #: Whether retrying the same call unchanged could plausibly succeed.
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        remediation: str | None = None,
        cause: BaseException | None = None,
        retryable: bool | None = None,
        retry_after: float | None = None,
    ) -> None:
        """Create the error and attach its remediation."""
        super().__init__(message)
        self.message = message
        self.remediation = remediation if remediation is not None else self.default_remediation
        self.cause = cause
        self.retry_after = retry_after
        if retryable is not None:
            self.retryable = retryable
        if cause is not None and self.__cause__ is None:
            self.__cause__ = cause

    def __str__(self) -> str:
        """Return the plain message, without the remediation block."""
        return self.message

    def __repr__(self) -> str:
        """Return a debugging representation."""
        return f"{type(self).__name__}({self.message!r})"

    def format_for_llm(self) -> str:
        """Render message plus remediation as text an LLM can act on directly."""
        text = f"{self.message.strip()}"
        if self.remediation:
            text += f"\n\nHow to fix ({self.code}):\n{self.remediation.strip()}"
        return text

    def to_tool_error(self) -> ToolError:
        """Convert to the MCP protocol-level tool error.

        Returns:
            A `ToolError` whose text carries both the message and the remediation, so the
            model on the other end of the protocol can fix the problem without a round
            trip. Errors are raised, never returned as a successful tool result.
        """
        return ToolError(self.format_for_llm())

    def with_context(self, suffix: str) -> GarminMCPError:
        """Return a copy of this error with `suffix` appended to the message."""
        clone = type(self)(
            f"{self.message} {suffix}".strip(),
            remediation=self.remediation,
            cause=self.cause,
            retryable=self.retryable,
            retry_after=self.retry_after,
        )
        clone.__cause__ = self.__cause__
        return clone


# ---------------------------------------------------------------------------
# Taxonomy
# ---------------------------------------------------------------------------


class ConfigurationError(GarminMCPError):
    """The server is misconfigured: a missing credential or an unusable setting."""

    code: ClassVar[str] = "configuration_error"
    default_remediation: ClassVar[str | None] = (
        f"Check the server's environment variables ({ENV_EMAIL}, {ENV_PASSWORD}, "
        f"{ENV_TOKEN_DIR}, {ENV_READ_ONLY}, {ENV_REQUEST_TIMEOUT}, {ENV_MAX_RETRIES}, "
        f"{ENV_LOG_LEVEL}) and restart the server. This cannot be fixed by retrying."
    )


class AuthenticationError(GarminMCPError):
    """Garmin rejected the session: bad credentials, an expired token, or MFA.

    `retryable` has a narrower meaning here than on the other classes: it says whether
    dropping the cached session and logging in again could fix the failure. It is True
    for an expired token when credentials are available, and False for a wrong password
    or an MFA challenge, which no number of retries will resolve.
    """

    code: ClassVar[str] = "authentication_error"
    default_remediation: ClassVar[str | None] = (
        f"Re-authenticate: delete the saved tokens, set {ENV_EMAIL} and {ENV_PASSWORD} in "
        "the server's environment, and restart it."
    )


class RateLimitError(GarminMCPError):
    """Garmin returned HTTP 429."""

    code: ClassVar[str] = "rate_limited"
    retryable: bool = True
    default_remediation: ClassVar[str | None] = (
        "Wait a few minutes and try again. Garmin's rate limits are per account and reset "
        "on their side; there is nothing to change locally."
    )


class UpstreamServiceError(GarminMCPError):
    """Garmin is unreachable, timed out, or returned a 5xx."""

    code: ClassVar[str] = "upstream_service_error"
    retryable: bool = True
    default_remediation: ClassVar[str | None] = (
        "This is a Garmin-side or network problem, not a problem with your request. Try "
        "again in a minute. If it persists, check https://connect.garmin.com in a browser "
        "and https://status.garmin.com for an outage."
    )


class NotFoundError(GarminMCPError):
    """The requested activity, workout, or record does not exist."""

    code: ClassVar[str] = "not_found"
    default_remediation: ClassVar[str | None] = (
        "Confirm the id exists and belongs to this account: list activities with "
        "`list_activities` or workouts with `list_workouts` and use an id from the "
        "results. Ids are not shared between accounts."
    )


class InvalidRequestError(GarminMCPError):
    """The request was well-formed but semantically wrong for Garmin."""

    code: ClassVar[str] = "invalid_request"
    default_remediation: ClassVar[str | None] = (
        "Fix the arguments and call the tool again. Retrying the identical call will fail "
        "identically. Check the tool's input schema for the accepted ranges and formats."
    )


class ReadOnlyModeError(GarminMCPError):
    """A write was attempted while the server runs in read-only mode."""

    code: ClassVar[str] = "read_only_mode"
    default_remediation: ClassVar[str | None] = (
        f"Read-only mode is on because {ENV_READ_ONLY} is set to a true value, or because "
        "the server was started with --read-only. To allow writes, remove the flag or set "
        f"{ENV_READ_ONLY}=0, then restart the server. Read-only mode is a safety setting: "
        "leave it on if you did not intend to modify this Garmin account."
    )


# ---------------------------------------------------------------------------
# Purpose-built errors — the messages a stuck user actually reads
# ---------------------------------------------------------------------------


def _token_paths(token_dir: Path | str) -> tuple[str, str]:
    """Return the token directory and token file as display strings."""
    directory = Path(token_dir).expanduser()
    return str(directory), str(directory / TOKENS_FILENAME)


def missing_credentials_error(
    token_dir: Path | str,
    *,
    cause: BaseException | None = None,
) -> ConfigurationError:
    """Build the error for "no saved session and no credentials"."""
    directory, _ = _token_paths(token_dir)
    return ConfigurationError(
        "Not signed in to Garmin Connect: there is no saved session in "
        f"{directory} and neither {ENV_EMAIL} nor {ENV_PASSWORD} is set in the "
        "server's environment.",
        remediation=(
            f"Set both variables and restart the server:\n"
            f"\n"
            f"    export {ENV_EMAIL}='you@example.com'\n"
            f"    export {ENV_PASSWORD}='your-garmin-connect-password'\n"
            f"    garmin-coach-mcp\n"
            f"\n"
            f"If the server is launched by an MCP client (Claude Desktop, Claude Code, "
            f'VS Code), put them in the "env" block of that client\'s MCP config instead '
            f"of your shell — the server does not inherit your interactive shell.\n"
            f"\n"
            f"You only need the password once. After the first successful login the OAuth "
            f"tokens are written to {directory} (change the location with "
            f"{ENV_TOKEN_DIR}) and reused on every later start, so you can remove "
            f"{ENV_PASSWORD} from the config afterwards."
        ),
        cause=cause,
    )


def token_expired_error(
    token_dir: Path | str,
    *,
    cause: BaseException | None = None,
    have_credentials: bool = False,
) -> AuthenticationError:
    """Build the error for a saved session that no longer works."""
    directory, token_file = _token_paths(token_dir)
    if have_credentials:
        remediation = (
            f"Delete the stale token file and let the server log in again with "
            f"{ENV_EMAIL} / {ENV_PASSWORD}:\n"
            f"\n"
            f"    rm -f {token_file}\n"
            f"\n"
            f"Then call the tool again (the server re-authenticates on the next call; no "
            f"restart needed). If it still fails, the password itself is likely wrong or "
            f"the account now requires a multi-factor code."
        )
    else:
        remediation = (
            f"The saved tokens are the only credential this server has, and Garmin no "
            f"longer accepts them. Delete them and log in once more:\n"
            f"\n"
            f"    rm -f {token_file}\n"
            f"    {ENV_EMAIL}='you@example.com' {ENV_PASSWORD}='...' garmin-coach-mcp\n"
            f"\n"
            f"Garmin sessions expire after a period of inactivity and are invalidated when "
            f"you change your password or sign out of all devices. A fresh login writes new "
            f"tokens to {directory} and the password is no longer needed after that."
        )
    return AuthenticationError(
        f"The saved Garmin session in {directory} is expired or invalid.",
        remediation=remediation,
        cause=cause,
        # Worth one re-login only when there is a credential to log in with.
        retryable=have_credentials,
    )


def invalid_credentials_error(*, cause: BaseException | None = None) -> AuthenticationError:
    """Build the error for credentials Garmin rejected."""
    return AuthenticationError(
        "Garmin Connect rejected the email and password the server was started with.",
        remediation=(
            f"Sign in at https://connect.garmin.com in a browser with the same email and "
            f"password to confirm they work.\n"
            f"\n"
            f"  - If the browser login works, re-check {ENV_EMAIL} and {ENV_PASSWORD} for "
            f"a trailing space, a shell-mangled character (quote the value: "
            f"{ENV_PASSWORD}='p@ss$word'), or a stale value exported in an old shell.\n"
            f"  - If the browser asks for a verification code, see the MFA instructions "
            f"below — this server cannot answer an MFA prompt on its own.\n"
            f"  - If Garmin says the account is locked, too many failed logins have been "
            f"attempted; wait ~15 minutes before trying again and stop restarting the "
            f"server in the meantime, because every restart is another failed login."
        ),
        cause=cause,
    )


def mfa_required_error(
    token_dir: Path | str,
    *,
    cause: BaseException | None = None,
) -> AuthenticationError:
    """Build the error for a login that needs an interactive MFA code."""
    directory, _ = _token_paths(token_dir)
    return AuthenticationError(
        "Garmin Connect asked for a multi-factor authentication code. This server runs "
        "non-interactively over the MCP protocol and has no way to prompt you for one.",
        remediation=(
            f"Do the MFA login once by hand on this machine, then let the server reuse the "
            f"tokens it produces. Run this in a terminal:\n"
            f"\n"
            f"    python - <<'PY'\n"
            f"    from getpass import getpass\n"
            f"    from garminconnect import Garmin\n"
            f"    api = Garmin(input('Garmin email: '), getpass('Garmin password: '),\n"
            f"                 prompt_mfa=lambda: input('MFA code: '))\n"
            f"    api.login('{directory}')\n"
            f"    print('tokens written to {directory}')\n"
            f"    PY\n"
            f"\n"
            f"It prompts for the code Garmin sends to your email or authenticator app and "
            f"writes the resulting tokens to {directory}. Start the server with "
            f"{ENV_TOKEN_DIR}={directory} (the default) and it will resume that session "
            f"without any MFA prompt until the tokens expire.\n"
            f"\n"
            f"Alternatively, turn MFA off for this account under Garmin Connect > Account "
            f"Settings > Sign-in & Security, but re-authenticating out of band is the safer "
            f"option."
        ),
        cause=cause,
    )


def rate_limit_error(
    operation: str,
    *,
    attempts: int = 1,
    retry_after: float | None = None,
    cause: BaseException | None = None,
) -> RateLimitError:
    """Build the error for HTTP 429 after the retry budget is spent."""
    tried = (
        f"The server already retried {attempts} times with exponential backoff and Garmin "
        "is still refusing."
        if attempts > 1
        else "This request was refused before any work was done."
    )
    wait = (
        f"Garmin asked for a {retry_after:.0f}s pause."
        if retry_after
        else "Garmin does not say how long the block lasts; a few minutes is typical."
    )
    return RateLimitError(
        f"Garmin Connect is rate limiting this account (HTTP 429) while running "
        f"`{operation}`. {tried}",
        remediation=(
            f"Wait a few minutes, then call the tool again. {wait} The limit is applied by "
            f"Garmin per account and IP; nothing needs changing on this side.\n"
            f"\n"
            f"To avoid hitting it again: prefer one wide call over many narrow ones (a "
            f"single `list_activities` with limit=50 instead of 50 single-activity "
            f"lookups), and space bulk work out. Repeated *logins* are rate limited far "
            f"more aggressively than reads, so if you are also seeing authentication "
            f"errors, stop restarting the server for ~15 minutes."
        ),
        retry_after=retry_after,
        cause=cause,
    )


def read_only_error(operation: str) -> ReadOnlyModeError:
    """Build the error for a write attempted in read-only mode."""
    return ReadOnlyModeError(
        f"`{operation}` modifies the Garmin account, and this server is running in "
        f"read-only mode.",
        remediation=(
            f"Read-only mode is on because {ENV_READ_ONLY} is set to a true value in the "
            f"server's environment, or because it was started with --read-only.\n"
            f"\n"
            f"To allow writes, remove --read-only or set {ENV_READ_ONLY}=0 (in the MCP "
            f"client config if the client launches the server), then restart it. Write "
            f"tools are not even registered while read-only mode is on, so the tool list "
            f"will change after the restart.\n"
            f"\n"
            f"If you did not intend to modify this Garmin account, leave read-only on — "
            f"this is the setting doing its job."
        ),
    )


def timeout_error(
    operation: str,
    *,
    timeout: float | None = None,
    attempts: int = 1,
    cause: BaseException | None = None,
) -> UpstreamServiceError:
    """Build the error for a request that exceeded the configured timeout."""
    budget = f" after {timeout:g}s" if timeout else ""
    return UpstreamServiceError(
        f"Garmin Connect did not respond{budget} while running `{operation}` "
        f"({attempts} attempt{'s' if attempts != 1 else ''}).",
        remediation=(
            f"Garmin's API is slow or unreachable from this machine. Try again in a minute; "
            f"the server retries transient failures automatically.\n"
            f"\n"
            f"If it keeps timing out: check that https://connect.garmin.com loads from this "
            f"host (a proxy or firewall blocking connectapi.garmin.com produces exactly this "
            f"error), and raise {ENV_REQUEST_TIMEOUT} (seconds, default 30) if the network "
            f"is simply slow. Large activity ranges take longer — narrow the date range."
        ),
        cause=cause,
    )


# ---------------------------------------------------------------------------
# Mapping upstream exceptions onto the taxonomy
# ---------------------------------------------------------------------------


def _exception_chain(exc: BaseException, *, limit: int = 10) -> list[BaseException]:
    """Return `exc` followed by its `__cause__` / `__context__` ancestors."""
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and len(chain) < limit and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


# `garminconnect` formats the status into the message and attaches no response object,
# so these patterns are the only way to recover it. They match that library's own
# phrasings and `requests`' HTTPError text — not arbitrary numbers in a message.
_STATUS_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bapi error\s+(\d{3})\b", re.IGNORECASE),
    re.compile(r"\berror\s*\((\d{3})\)", re.IGNORECASE),
    re.compile(r"\b(\d{3})\s+(?:client|server)\s+error\b", re.IGNORECASE),
    re.compile(r"\bhttp\s*(?:status\s*)?(?:error\s*)?[: ]\s*(\d{3})\b", re.IGNORECASE),
    re.compile(r"\bstatus[_ ]?code[=: ]\s*(\d{3})\b", re.IGNORECASE),
)


def _status_from_message(text: str) -> int | None:
    """Recover an HTTP status code from a formatted error message, or None."""
    for pattern in _STATUS_PATTERNS:
        match = pattern.search(text)
        if match:
            status = int(match.group(1))
            if 100 <= status < 600:
                return status
    return None


def extract_status_code(exc: BaseException) -> int | None:
    """Find the HTTP status code behind an exception.

    Checks, in order of trustworthiness: a `response.status_code` attribute (requests,
    httpx, curl_cffi), a `status_code`/`status` attribute (garth and friends), then the
    formatted message. Walks the `__cause__` chain, because `garminconnect` re-raises its
    own exception types over the transport library's.

    Args:
        exc: Any exception.

    Returns:
        The status code, or None when the failure never reached an HTTP response.
    """
    for link in _exception_chain(exc):
        response = getattr(link, "response", None)
        status = getattr(response, "status_code", None)
        if isinstance(status, int):
            return status
        for attribute in ("status_code", "status"):
            value = getattr(link, attribute, None)
            if isinstance(value, int) and 100 <= value < 600:
                return value
    for link in _exception_chain(exc):
        status = _status_from_message(str(link))
        if status is not None:
            return status
    return None


def _retry_after_seconds(exc: BaseException) -> float | None:
    """Read the Retry-After header off any response in the exception chain."""
    for link in _exception_chain(exc):
        headers = getattr(getattr(link, "response", None), "headers", None)
        if headers is None:
            continue
        raw = None
        try:
            raw = headers.get("Retry-After")
        except (AttributeError, TypeError):  # pragma: no cover - exotic header objects
            raw = None
        if raw is None:
            continue
        try:
            return float(str(raw).strip())
        except ValueError:  # A date-formatted Retry-After; fall back to our own backoff.
            return None
    return None


def _type_names(exc: BaseException) -> set[str]:
    """Return the qualified names of every class in the chain's MRO.

    Matching on names rather than imported classes keeps this working when a transport
    library (curl_cffi, httpx) is an optional dependency that is not installed.
    """
    names: set[str] = set()
    for link in _exception_chain(exc):
        for klass in type(link).__mro__:
            names.add(f"{klass.__module__}.{klass.__qualname__}")
            names.add(klass.__qualname__)
    return names


_TIMEOUT_TYPES = frozenset(
    {
        "Timeout",
        "ConnectTimeout",
        "ReadTimeout",
        "WriteTimeout",
        "PoolTimeout",
        "TimeoutException",
        "TimeoutError",
        "ConnectTimeoutError",
        "ReadTimeoutError",
        "socket.timeout",
    }
)

_CONNECTION_TYPES = frozenset(
    {
        "ConnectionError",
        "ConnectError",
        "ProxyError",
        "SSLError",
        "DNSError",
        "NewConnectionError",
        "NameResolutionError",
        "ProtocolError",
        "RemoteProtocolError",
        "ChunkedEncodingError",
        "ContentDecodingError",
        "IncompleteRead",
        "TooManyRedirects",
        "CurlError",
        "ConnectionResetError",
        "ConnectionAbortedError",
        "BrokenPipeError",
    }
)

_MALFORMED_INPUT_TYPES = frozenset({"ValueError", "TypeError", "KeyError", "ValidationError"})

# Exact markers `garminconnect` uses. These disambiguate *within* an already-typed
# authentication error; they never decide retryability on their own.
_MFA_MARKERS = ("mfa required", "needs_mfa", "multi-factor", "multifactor", "two-factor")
_MISSING_CREDENTIAL_MARKERS = ("username and password are required",)


def _is_mfa(exc: BaseException) -> bool:
    """Report whether the chain describes an unanswerable MFA challenge."""
    text = " ".join(str(link) for link in _exception_chain(exc)).lower()
    return any(marker in text for marker in _MFA_MARKERS)


def _is_missing_credentials(exc: BaseException) -> bool:
    """Report whether garminconnect refused to log in for lack of credentials."""
    text = " ".join(str(link) for link in _exception_chain(exc)).lower()
    return any(marker in text for marker in _MISSING_CREDENTIAL_MARKERS)


def classify_exception(exc: BaseException) -> type[GarminMCPError]:
    """Pick the taxonomy class for an upstream exception.

    Args:
        exc: The exception raised by `garminconnect` or its transport.

    Returns:
        The `GarminMCPError` subclass that describes the failure.
    """
    if isinstance(exc, GarminMCPError):
        return type(exc)

    names = _type_names(exc)
    status = extract_status_code(exc)

    if "GarminConnectTooManyRequestsError" in names or status == 429:
        return RateLimitError
    if status == 404:
        return NotFoundError
    if status in (401, 403):
        return AuthenticationError
    if "GarminConnectAuthenticationError" in names:
        return ConfigurationError if _is_missing_credentials(exc) else AuthenticationError
    if status is not None and 400 <= status < 500:
        return InvalidRequestError
    if status is not None and status >= 500:
        return UpstreamServiceError
    if names & _TIMEOUT_TYPES or names & _CONNECTION_TYPES:
        return UpstreamServiceError
    if "GarminConnectInvalidFileFormatError" in names:
        return InvalidRequestError
    if "GarminConnectConnectionError" in names:
        # No status and no transport exception underneath: a genuine network failure.
        return UpstreamServiceError
    if names & _MALFORMED_INPUT_TYPES:
        return InvalidRequestError
    return UpstreamServiceError


def map_exception(
    exc: BaseException,
    *,
    operation: str | None = None,
    token_dir: Path | str | None = None,
    attempts: int = 1,
    timeout: float | None = None,
) -> GarminMCPError:
    """Map any upstream exception onto the taxonomy, with a usable remediation.

    Args:
        exc: The exception raised by `garminconnect`, `requests`, `httpx` or `curl_cffi`.
        operation: Name of the operation being run, used in the message.
        token_dir: Token directory, so authentication errors can name the real path.
        attempts: How many attempts were made before giving up.
        timeout: The configured per-request timeout, quoted in timeout errors.

    Returns:
        A `GarminMCPError` subclass carrying a remediation. Never raises.
    """
    if isinstance(exc, GarminMCPError):
        return exc

    where = f" while running `{operation}`" if operation else ""
    tokens = token_dir if token_dir is not None else "~/.garmin-coach-mcp/tokens"
    kind = classify_exception(exc)
    status = extract_status_code(exc)
    names = _type_names(exc)
    detail = str(exc).strip() or type(exc).__name__

    if kind is RateLimitError:
        return rate_limit_error(
            operation or "the request",
            attempts=attempts,
            retry_after=_retry_after_seconds(exc),
            cause=exc,
        )

    if kind is ConfigurationError:
        return missing_credentials_error(tokens, cause=exc)

    if kind is AuthenticationError:
        if _is_mfa(exc):
            return mfa_required_error(tokens, cause=exc)
        if status in (401, 403) and operation not in (None, "login"):
            # Retryable in the specific sense retry.py cares about: dropping the cached
            # session and logging in again may fix it. Bad credentials and MFA cannot.
            return AuthenticationError(
                f"Garmin rejected the saved session (HTTP {status}){where}.",
                retryable=True,
                remediation=(
                    "The session expired mid-flight. The server already tried to "
                    "re-authenticate once and Garmin still refused.\n"
                    "\n"
                    f"Delete the saved tokens ({Path(str(tokens)).expanduser()}) and start "
                    f"the server with {ENV_EMAIL} and {ENV_PASSWORD} set so it can log in "
                    "again. If that also fails, sign in at https://connect.garmin.com to "
                    "check whether the account is locked or now requires a verification "
                    "code."
                ),
                cause=exc,
            )
        return invalid_credentials_error(cause=exc)

    if kind is NotFoundError:
        return NotFoundError(
            f"Garmin has no such record (HTTP 404){where}.",
            remediation=(
                "Check the id you passed. List the available items first "
                "(`list_activities` / `list_workouts`) and copy an id from the result; "
                "ids from another Garmin account or from a deleted item will always 404. "
                "Retrying this exact call will not help."
            ),
            cause=exc,
        )

    if kind is InvalidRequestError:
        if status is not None:
            return InvalidRequestError(
                f"Garmin rejected the request (HTTP {status}){where}: {detail}",
                remediation=(
                    "Garmin considered the request itself wrong, so retrying it unchanged "
                    "will fail the same way. Re-read the tool's parameter descriptions and "
                    "correct the arguments — the most common causes are a date outside the "
                    "allowed range, a limit above what the endpoint accepts, or an id of "
                    "the wrong kind (an activity id passed where a workout id is expected)."
                ),
                cause=exc,
            )
        return InvalidRequestError(
            f"The Garmin client rejected the arguments{where}: {detail}",
            remediation=(
                "This is an argument error raised before the request was sent. Fix the "
                "arguments and call the tool again; dates must be `YYYY-MM-DD`, counts "
                "must be positive integers, and ids must be the ids returned by the "
                "corresponding list tool."
            ),
            cause=exc,
        )

    if names & _TIMEOUT_TYPES:
        return timeout_error(operation or "the request", timeout=timeout, attempts=attempts, cause=exc)

    if status is not None and status >= 500:
        return UpstreamServiceError(
            f"Garmin Connect returned a server error (HTTP {status}){where}.",
            remediation=(
                f"Garmin's own service failed; the request was fine. The server retried "
                f"{attempts} time{'s' if attempts != 1 else ''} already. Wait a minute and "
                "try again, and check https://status.garmin.com if it keeps happening — "
                "Garmin's API has scheduled maintenance windows during which every call "
                "returns 5xx."
            ),
            cause=exc,
        )

    if names & _CONNECTION_TYPES or "GarminConnectConnectionError" in names:
        return UpstreamServiceError(
            f"Could not reach Garmin Connect{where}: {detail}",
            remediation=(
                "The request never got a response. Check that this machine has network "
                "access and that connectapi.garmin.com is not blocked by a proxy, VPN, or "
                "corporate firewall — try `curl -sS -o /dev/null -w '%{http_code}\\n' "
                "https://connect.garmin.com`. If the network is fine, Garmin is likely "
                "down; try again in a few minutes."
            ),
            cause=exc,
        )

    return UpstreamServiceError(
        f"Unexpected {type(exc).__name__}{where}: {detail}",
        remediation=(
            "This failure is not one the server recognises, so it was not retried. Try the "
            "call once more; if it repeats, run the server with GARMIN_LOG_LEVEL=DEBUG to "
            "get the full traceback on stderr and report it at "
            "https://github.com/mrosano1987/garmin-mcp-server-1/issues with that output "
            "(the logs redact credentials and tokens)."
        ),
        cause=exc,
        retryable=False,
    )
