"""Planned session -> structured workout steps (shared by Garmin push and FIT export).

Paces: easy sessions get an easy pace range when the athlete's easy pace is known; race-pace
work gets the goal race pace (+10/-5 s/km). Everything else is effort-based (no target), so a
missing pace never produces a fake target.
"""

from __future__ import annotations

from garmin_coach_mcp.coach.models import PlannedSession, SessionKind
from garmin_coach_mcp.models import RepeatStep, WorkoutStep

WARMUP_KM = 2.0
COOLDOWN_KM = 2.0
EASY_RANGE_S = 30  # easy pace range: [easy pace + 30 s, easy pace]


def _pace(seconds_per_km: float) -> str:
    total = max(150, min(1200, round(seconds_per_km)))
    return f"{total // 60}:{total % 60:02d}"


def _run(
    km: float,
    step_type: str = "run",
    pace_range: tuple[float, float] | None = None,
) -> WorkoutStep:
    kwargs: dict[str, object] = {
        "type": step_type,
        "duration_type": "distance",
        "duration_value": max(100, round(km * 1000)),
        "primary": "pace" if pace_range else "none",
    }
    if pace_range:
        slow, fast = pace_range
        kwargs["pace_low"], kwargs["pace_high"] = _pace(slow), _pace(fast)
    return WorkoutStep.model_validate(kwargs)


def session_steps(
    session: PlannedSession,
    easy_pace: float | None,
    race_pace: float | None,
) -> list[WorkoutStep | RepeatStep]:
    """Steps for one session. Raises ValueError for a race (not a workout)."""
    km = session.distance_km
    easy = (easy_pace + EASY_RANGE_S, easy_pace) if easy_pace else None
    rp = (race_pace + 10, max(150.0, race_pace - 5)) if race_pace else None
    kind = session.kind
    if kind is SessionKind.RACE:
        raise ValueError("a race is not a workout")
    if kind in (SessionKind.EASY, SessionKind.LONG, SessionKind.MEDIUM_LONG):
        return [_run(km, "run", easy)]
    if kind is SessionKind.STRIDES:
        body = max(1.0, km - 1.0)
        return [
            _run(body, "run", easy),
            RepeatStep(
                repeat_count=6,
                steps=[
                    WorkoutStep(
                        type="run", duration_type="time", duration_value=20, primary="none"
                    ),
                    WorkoutStep(
                        type="recover", duration_type="time", duration_value=60, primary="none"
                    ),
                ],
            ),
        ]
    main = max(1.0, km - WARMUP_KM - COOLDOWN_KM)
    steps: list[WorkoutStep | RepeatStep] = [_run(WARMUP_KM, "warmup", easy)]
    if kind is SessionKind.INTERVALS:
        reps = max(3, min(8, int(main)))
        steps.append(
            RepeatStep(
                repeat_count=reps,
                steps=[
                    _run(1.0, "run", rp),
                    WorkoutStep(
                        type="recover", duration_type="time", duration_value=120, primary="none"
                    ),
                ],
            )
        )
    elif kind is SessionKind.RACE_PACE:
        steps.append(_run(main, "run", rp))
    else:  # TEMPO: effort-based (comfortably hard), no pace target without a threshold pace
        steps.append(_run(main, "run", None))
    steps.append(_run(COOLDOWN_KM, "cooldown", easy))
    return steps


def workout_name(session: PlannedSession, week_index: int) -> str:
    label = session.kind.value.replace("_", " ")
    return f"RC W{week_index} {session.date:%a %d/%m} {label} {session.distance_km:g}km"[:80]
