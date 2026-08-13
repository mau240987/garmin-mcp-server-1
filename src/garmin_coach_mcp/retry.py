"""Retry policy for Garmin Connect calls.

What the legacy decorator did wrong, and what this module does instead:

| Legacy                                                    | Here                                            |
| --------------------------------------------------------- | ----------------------------------------------- |
| Claimed jitter in the docstring, computed none             | Equal jitter on every delay                     |
| Decided retryability from `str(exc).lower()` substrings    | Decides from the mapped error class             |
| Retried `push_workout`, duplicating uploads                | The caller declares idempotency; writes are not retried |
| Returned `"Error after N attempts: ..."` as a tool result  | Raises the mapped `GarminMCPError`              |
| Re-authenticated on every attempt containing "token"       | Re-authenticates once, then gives up            |
| Slept with `time.sleep`, so tests slept too                | `sleep` and `rng` are injectable                |
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from garmin_coach_mcp.errors import (
    AuthenticationError,
    GarminMCPError,
    RateLimitError,
    map_exception,
)

__all__ = ["RetryPolicy", "call_with_retry", "compute_delay"]

T = TypeVar("T")

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetryPolicy:
    """How hard to try again after a retryable failure.

    Attributes:
        max_retries: Extra attempts after the first one. 0 disables retrying.
        base_delay: Delay before the first retry, in seconds, before jitter.
        max_delay: Ceiling for any single sleep, in seconds.
        rate_limit_multiplier: Extra backoff applied to HTTP 429, which needs a longer
            pause than a transient 503.
        jitter: Fraction of the computed delay that is randomised. 0.5 means the actual
            sleep is uniform in [delay/2, delay] — "equal jitter", which keeps several
            clients from retrying in lockstep without ever sleeping for ~0 seconds.
    """

    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 30.0
    rate_limit_multiplier: float = 3.0
    jitter: float = 0.5

    @classmethod
    def from_max_retries(cls, max_retries: int) -> RetryPolicy:
        """Build the default policy with a caller-supplied retry budget."""
        return cls(max_retries=max_retries)


def compute_delay(
    attempt: int,
    policy: RetryPolicy,
    *,
    rate_limited: bool = False,
    retry_after: float | None = None,
    rng: random.Random | None = None,
) -> float:
    """Compute how long to sleep before the next attempt.

    Args:
        attempt: Zero-based index of the attempt that just failed.
        policy: The policy in force.
        rate_limited: True for HTTP 429, which gets the longer backoff.
        retry_after: Seconds requested by the server, when it said so. Honoured, but
            still capped by `policy.max_delay` so a tool call cannot hang for minutes.
        rng: Random source. Injectable so tests are deterministic.

    Returns:
        Seconds to sleep, always within [0, policy.max_delay].
    """
    generator = rng if rng is not None else random
    delay = policy.base_delay * (2.0**attempt)
    if rate_limited:
        delay *= policy.rate_limit_multiplier
    if retry_after is not None and retry_after > delay:
        delay = retry_after
    delay = min(delay, policy.max_delay)
    low = delay * (1.0 - policy.jitter)
    return max(0.0, generator.uniform(low, delay))


def _is_retryable(error: GarminMCPError) -> bool:
    """Decide retryability from the mapped error, never from its text."""
    return error.retryable


def call_with_retry(  # noqa: PLR0912, PLR0913 - one explicit branch per failure mode
    fn: Callable[[], T],
    *,
    operation: str,
    policy: RetryPolicy,
    idempotent: bool,
    token_dir: str | None = None,
    timeout: float | None = None,
    on_auth_error: Callable[[], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
    logger: logging.Logger | None = None,
) -> T:
    """Run `fn`, retrying only failures that a retry can plausibly fix.

    The rules, in order:

    * A retryable failure (429, 5xx, timeout, connection error) is retried with
      exponential backoff plus jitter, up to `policy.max_retries` extra attempts —
      but only when `idempotent` is True. Repeating a create is worse than failing it.
    * An authentication failure invalidates the session via `on_auth_error` and is
      retried exactly once, even for non-idempotent operations: a rejected token means
      the request never reached Garmin's business logic, so nothing was applied.
    * Everything else (4xx, validation, not-found, configuration, MFA) is raised
      immediately. Retrying it would fail identically and waste the caller's time.

    Args:
        fn: Zero-argument callable performing one attempt.
        operation: Name used in log lines and error messages.
        policy: Backoff policy.
        idempotent: True only when running `fn` twice is indistinguishable from
            running it once.
        token_dir: Token directory, so auth errors can name the real path.
        timeout: Configured per-request timeout, quoted in timeout errors.
        on_auth_error: Called before the single auth retry, to drop the cached session.
        sleep: Sleep function. Injectable so tests do not actually sleep.
        rng: Random source for jitter. Injectable for determinism.
        logger: Logger for retry diagnostics.

    Returns:
        Whatever `fn` returns.

    Raises:
        GarminMCPError: Always, on failure. Never returns an error string, and never
            lets a raw upstream exception escape.
    """
    log = logger if logger is not None else _LOGGER
    attempts = 0
    auth_retry_used = False
    max_attempts = policy.max_retries + 1

    while True:
        attempts += 1
        try:
            return fn()
        except GarminMCPError as exc:
            error: GarminMCPError = exc
        except Exception as exc:  # noqa: BLE001 - mapped immediately; nothing is swallowed
            error = map_exception(
                exc,
                operation=operation,
                token_dir=token_dir,
                attempts=attempts,
                timeout=timeout,
            )

        # Authentication: drop the session and try once more, then stop. Bounded to one
        # attempt because a wrong password or an MFA prompt will never fix itself, and
        # hammering Garmin's SSO endpoint gets the whole account rate limited. Runs even
        # for non-idempotent operations: a rejected token means Garmin refused the
        # request before doing anything, so there is nothing to duplicate.
        if isinstance(error, AuthenticationError):
            if auth_retry_used or on_auth_error is None or not error.retryable:
                log.error(
                    "operation=%s authentication failed: %s", operation, type(error).__name__
                )
                raise error
            auth_retry_used = True
            log.warning(
                "operation=%s authentication rejected; invalidating session and retrying once",
                operation,
            )
            on_auth_error()
            continue

        if not _is_retryable(error):
            log.debug(
                "operation=%s failed with non-retryable %s", operation, type(error).__name__
            )
            raise error

        if not idempotent:
            log.error(
                "operation=%s failed and is not idempotent; not retrying to avoid a "
                "duplicate write",
                operation,
            )
            raise error.with_context(
                "The server did not retry, because repeating this operation could apply "
                "it twice. Check whether it took effect before calling it again."
            )

        if attempts >= max_attempts:
            log.error("operation=%s failed after %d attempts", operation, attempts)
            raise error.with_context(f"Gave up after {attempts} attempts.")

        delay = compute_delay(
            attempts - 1,
            policy,
            rate_limited=isinstance(error, RateLimitError),
            retry_after=error.retry_after,
            rng=rng,
        )
        log.warning(
            "operation=%s attempt %d/%d failed (%s); retrying in %.1fs",
            operation,
            attempts,
            max_attempts,
            type(error).__name__,
            delay,
        )
        sleep(delay)
