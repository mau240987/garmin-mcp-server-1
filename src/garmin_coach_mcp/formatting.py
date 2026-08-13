"""Unit conversion and human-readable rendering helpers.

Every function here is pure: no I/O, no global state, no Garmin API access. That
makes the whole module trivially unit-testable and safe to call from validators.

Two families of function live here:

* **Strict parsers** (``pace_to_meters_per_second``) validate caller-supplied
  input and raise :class:`~garmin_coach_mcp.errors.InvalidRequestError` with a
  remediation hint when the input is malformed. The legacy server crashed with a
  bare ``ValueError`` here, which surfaced to the model as an opaque stack trace.
* **Lenient formatters** (``format_pace``, ``format_duration``,
  ``format_distance``) render values that came *back* from Garmin. Upstream data
  is routinely missing or zero, so these return ``None`` rather than raising —
  the caller maps ``None`` onto an omitted/optional output field.
"""

from __future__ import annotations

import re

from .errors import InvalidRequestError

__all__ = [
    "PACE_EXAMPLE",
    "PACE_PATTERN",
    "format_distance",
    "format_duration",
    "format_pace",
    "meters_per_second_to_pace",
    "pace_seconds_per_km",
    "pace_to_meters_per_second",
    "pace_to_seconds_per_km",
]

#: Regex used both for validation here and as the JSON-Schema ``pattern`` for
#: every pace-typed tool parameter, so the schema and the runtime agree exactly.
PACE_PATTERN = r"^(?:[0-9]|[1-5][0-9]):[0-5][0-9]$"

#: Rendered into error messages and field descriptions.
PACE_EXAMPLE = "4:35"

_PACE_RE = re.compile(r"^\s*(?P<minutes>\d{1,2}):(?P<seconds>[0-5]\d)\s*(?:/\s*km)?\s*$")

_PACE_HELP = (
    "Expected minutes and seconds per kilometre as 'm:ss' or 'mm:ss' "
    f"(for example '{PACE_EXAMPLE}' = 4 minutes 35 seconds per km). "
    "Seconds must be 00-59. Do not use decimals, and do not include a unit suffix."
)

_METRES_PER_KM = 1000.0


def pace_to_seconds_per_km(pace: str) -> float:
    """Parse a ``"m:ss"`` pace string into seconds per kilometre.

    Args:
        pace: Minutes and seconds per kilometre, e.g. ``"4:35"``. A trailing
            ``"/km"`` is tolerated, as is surrounding whitespace.

    Returns:
        Seconds per kilometre as a float, always strictly positive.

    Raises:
        InvalidRequestError: If ``pace`` is not a string, does not match
            ``m:ss``/``mm:ss``, or resolves to zero.
    """
    if not isinstance(pace, str):
        raise InvalidRequestError(
            f"Pace must be a string, got {type(pace).__name__}.",
            remediation=_PACE_HELP,
        )

    match = _PACE_RE.match(pace)
    if match is None:
        raise InvalidRequestError(
            f"Could not parse {pace!r} as a running pace.",
            remediation=_PACE_HELP,
        )

    total = int(match.group("minutes")) * 60 + int(match.group("seconds"))
    if total <= 0:
        raise InvalidRequestError(
            f"Pace {pace!r} resolves to zero seconds per kilometre.",
            remediation=_PACE_HELP,
        )
    return float(total)


def pace_to_meters_per_second(pace: str) -> float:
    """Convert a ``"m:ss"``-per-kilometre pace into metres per second.

    Garmin stores speed targets in metres per second, so this is the conversion
    applied when building a workout's speed target.

    Args:
        pace: Minutes and seconds per kilometre, e.g. ``"4:35"``.

    Returns:
        Speed in metres per second.

    Raises:
        InvalidRequestError: If ``pace`` is malformed. See
            :func:`pace_to_seconds_per_km`.
    """
    return _METRES_PER_KM / pace_to_seconds_per_km(pace)


def meters_per_second_to_pace(speed_mps: float) -> str:
    """Convert metres per second into a ``"m:ss/km"`` pace string.

    Args:
        speed_mps: Speed in metres per second. Must be strictly positive.

    Returns:
        Pace rendered as ``"4:35/km"``.

    Raises:
        InvalidRequestError: If ``speed_mps`` is not a positive number.
    """
    if not isinstance(speed_mps, int | float) or isinstance(speed_mps, bool):
        raise InvalidRequestError(
            f"Speed must be a number, got {type(speed_mps).__name__}.",
            remediation="Pass a positive speed in metres per second.",
        )
    if speed_mps <= 0:
        raise InvalidRequestError(
            f"Speed must be positive to express it as a pace, got {speed_mps}.",
            remediation="Pass a positive speed in metres per second.",
        )
    rendered = format_pace(_METRES_PER_KM / float(speed_mps))
    # format_pace only returns None for non-positive input, excluded above.
    assert rendered is not None
    return rendered


def pace_seconds_per_km(distance_meters: float | None, duration_seconds: float | None) -> float | None:
    """Derive average pace in seconds per kilometre from a distance and a duration.

    Args:
        distance_meters: Distance covered, in metres. May be ``None`` or zero.
        duration_seconds: Elapsed time, in seconds. May be ``None`` or zero.

    Returns:
        Seconds per kilometre, or ``None`` when either input is missing or
        non-positive (a Garmin activity with no distance, for example).
    """
    if not distance_meters or not duration_seconds:
        return None
    if distance_meters <= 0 or duration_seconds <= 0:
        return None
    return float(duration_seconds) / (float(distance_meters) / _METRES_PER_KM)


def format_pace(seconds_per_km: float | None) -> str | None:
    """Render seconds per kilometre as ``"m:ss/km"``.

    Args:
        seconds_per_km: Pace in seconds per kilometre.

    Returns:
        ``"4:35/km"``, or ``None`` when the input is missing or non-positive.
    """
    if seconds_per_km is None or seconds_per_km <= 0:
        return None
    total = int(round(seconds_per_km))
    return f"{total // 60}:{total % 60:02d}/km"


def format_duration(seconds: float | None) -> str | None:
    """Render a duration compactly: ``"1h 23m"``, ``"12m 30s"``, ``"45s"``.

    Args:
        seconds: Duration in seconds.

    Returns:
        A compact human-readable duration, or ``None`` when the input is missing
        or non-positive.
    """
    if seconds is None or seconds <= 0:
        return None
    total = int(round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s" if secs else f"{minutes}m"
    return f"{secs}s"


def format_distance(meters: float | None) -> str | None:
    """Render a distance as kilometres above 1 km, metres below.

    Args:
        meters: Distance in metres.

    Returns:
        ``"10.42 km"`` or ``"850 m"``, or ``None`` when the input is missing or
        non-positive.
    """
    if meters is None or meters <= 0:
        return None
    if meters >= _METRES_PER_KM:
        return f"{meters / _METRES_PER_KM:.2f} km"
    return f"{int(round(meters))} m"
