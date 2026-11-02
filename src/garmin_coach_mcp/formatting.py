"""Pure unit conversions and renderers. No I/O."""

from __future__ import annotations

import re

_PACE_RE = re.compile(r"^\s*(\d{1,2}):([0-5]\d)\s*(?:/\s*km)?\s*$")


def parse_pace(pace: str) -> int:
    """'5:30' or '5:30/km' -> seconds per km. Raises ValueError on malformed input."""
    match = _PACE_RE.match(pace)
    if not match:
        raise ValueError(f"pace must look like 'M:SS' (per km), got {pace!r}")
    seconds = int(match.group(1)) * 60 + int(match.group(2))
    if seconds <= 0:
        raise ValueError("pace must be greater than 0:00")
    return seconds


def pace_to_meters_per_second(pace: str) -> float:
    return 1000.0 / parse_pace(pace)


def meters_per_second_to_pace(speed: float) -> str:
    if speed <= 0:
        raise ValueError("speed must be positive")
    return format_pace(1000.0 / speed)


def format_pace(seconds_per_km: float) -> str:
    total = round(seconds_per_km)
    return f"{total // 60}:{total % 60:02d}/km"


def format_duration(seconds: float) -> str:
    total = round(seconds)
    hours, rem = divmod(total, 3600)
    minutes = rem // 60
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"


def format_distance(meters: float) -> str:
    return f"{meters / 1000:.1f} km"


def pace_from(distance_m: float | None, duration_s: float | None) -> str | None:
    if not distance_m or not duration_s or distance_m <= 0:
        return None
    return format_pace(duration_s / (distance_m / 1000.0))
