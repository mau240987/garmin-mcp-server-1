"""Performance-based training paces (Daniels & Gilbert, "Oxygen Power", 1979).

VDOT is the VO2max equivalent of a race performance. From it come equivalent race times and
training paces as a share of VDOT. Deterministic, no data beyond distance and time.

- Oxygen cost of running at v m/min: VO2 = -4.60 + 0.182258 v + 0.000104 v^2
- Share of VO2max sustainable for t minutes:
  0.8 + 0.1894393 e^(-0.012778 t) + 0.2989558 e^(-0.1932605 t)
- VDOT = VO2(race velocity) / share(race time)

Training intensities (Daniels' Running Formula): easy runs 59-74% of VDOT, threshold ~88%.
"""

from __future__ import annotations

import math

#: Easy pace hint: from this share of VDOT (faster end) to EASY_RANGE_S slower.
EASY_SHARE = 0.70
THRESHOLD_SHARE = 0.88
#: Whole activities used to estimate VDOT: at least this long, plausible VDOT only.
MIN_EFFORT_M = 5000.0
VDOT_RANGE = (20.0, 85.0)


def _vo2(v_m_per_min: float) -> float:
    return -4.60 + 0.182258 * v_m_per_min + 0.000104 * v_m_per_min**2


def _sustainable_share(minutes: float) -> float:
    return (
        0.8 + 0.1894393 * math.exp(-0.012778 * minutes) + 0.2989558 * math.exp(-0.1932605 * minutes)
    )


def vdot_from_race(distance_m: float, time_s: float) -> float:
    minutes = time_s / 60
    return _vo2(distance_m / minutes) / _sustainable_share(minutes)


def _velocity_for_vo2(vo2: float) -> float:
    a, b, c = 0.000104, 0.182258, -4.60 - vo2
    return (-b + math.sqrt(b * b - 4 * a * c)) / (2 * a)


def pace_at_share(vdot: float, share: float) -> float:
    """Pace (s/km) at `share` of VDOT."""
    return 60_000 / _velocity_for_vo2(vdot * share)


def race_time(vdot: float, distance_m: float) -> float:
    """Equivalent race time (s) for `distance_m` at this VDOT (bisection, 1 s precision)."""
    lo, hi = 60.0, 60.0 * 60 * 12
    while hi - lo > 0.5:
        mid = (lo + hi) / 2
        if vdot_from_race(distance_m, mid) > vdot:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def easy_pace(vdot: float) -> float:
    return pace_at_share(vdot, EASY_SHARE)


def threshold_pace(vdot: float) -> float:
    return pace_at_share(vdot, THRESHOLD_SHARE)
