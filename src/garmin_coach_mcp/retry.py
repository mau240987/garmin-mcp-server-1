"""Bounded exponential backoff for idempotent operations only."""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import TypeVar

from garmin_coach_mcp.errors import GarminMCPError
from garmin_coach_mcp.logging_utils import get_logger

T = TypeVar("T")
log = get_logger("retry")


def backoff_delay(attempt: int, *, base: float = 1.0, cap: float = 10.0) -> float:
    """Delay before retry number `attempt` (0-based): base * 2**attempt, capped, +-20% jitter."""
    delay = min(cap, base * (2**attempt))
    return float(delay * (0.8 + 0.4 * random.random()))  # noqa: S311 - jitter, not crypto


def call_with_retry(
    fn: Callable[[], T],
    *,
    operation: str,
    retries: int,
    sleep: Callable[[float], None] = time.sleep,
    base_delay: float = 1.0,
    max_delay: float = 10.0,
) -> T:
    """Run `fn`, retrying only `GarminMCPError`s marked `retryable`.

    Callers must only pass idempotent operations: a create that failed half-way must
    never be replayed blindly.
    """
    attempt = 0
    while True:
        try:
            return fn()
        except GarminMCPError as exc:
            if not exc.retryable or attempt >= retries:
                raise
            delay = backoff_delay(attempt, base=base_delay, cap=max_delay)
            log.warning(
                "%s failed (%s), retry %d/%d in %.1fs",
                operation,
                type(exc).__name__,
                attempt + 1,
                retries,
                delay,
            )
            sleep(delay)
            attempt += 1
