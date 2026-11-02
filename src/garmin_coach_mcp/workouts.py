"""Step DSL -> Garmin Connect workout payloads.

Lessons kept from the legacy implementation:
- `targetValueOne/Two` and `secondaryTargetValueOne/Two` live at the top level of the
  ExecutableStep, not inside `targetType`.
- Workouts are created with `upload_running_workout()` + `schedule_workout()`; the FIT
  upload endpoint is for activities only.

Units follow `garminconnect.workout` (the library's own step builders): time end
conditions in **seconds**, distance end conditions in **meters**, pace/speed targets in
**m/s**. The legacy server sent milliseconds and centimeters; see docs/api-coverage.md
(open verification point V-1).
"""

from __future__ import annotations

from typing import Any

from garmin_coach_mcp.formatting import pace_to_meters_per_second
from garmin_coach_mcp.models import RepeatStep, WorkoutStep

# Garmin Connect workout-service ids (mirrors garminconnect.workout constants).
STEP_TYPES: dict[str, tuple[int, str]] = {
    "warmup": (1, "warmup"),
    "cooldown": (2, "cooldown"),
    "run": (3, "interval"),
    "recover": (4, "recovery"),
    "rest": (5, "rest"),
}
REPEAT_STEP_TYPE = (6, "repeat")
COND_LAP_BUTTON = (1, "lap.button")
COND_TIME = (2, "time")
COND_DISTANCE = (3, "distance")
COND_ITERATIONS = (7, "iterations")
TARGET_NONE = (1, "no.target")
TARGET_HR = (4, "heart.rate.zone")
TARGET_PACE = (6, "pace.zone")
RUNNING = {"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1}


def _ref(pair: tuple[int, str], id_key: str, key_key: str, order: int = 1) -> dict[str, Any]:
    return {id_key: pair[0], key_key: pair[1], "displayOrder": order}


def _condition(pair: tuple[int, str]) -> dict[str, Any]:
    return {
        "conditionTypeId": pair[0],
        "conditionTypeKey": pair[1],
        "displayOrder": pair[0],
        "displayable": True,
    }


def _targets(step: WorkoutStep) -> tuple[tuple[Any, ...] | None, tuple[Any, ...] | None]:
    pace = hr = None
    if step.pace_low and step.pace_high:
        # Slower pace = lower speed = targetValueOne.
        pace = (
            TARGET_PACE,
            pace_to_meters_per_second(step.pace_low),
            pace_to_meters_per_second(step.pace_high),
        )
    if step.hr_low is not None and step.hr_high is not None:
        hr = (TARGET_HR, float(step.hr_low), float(step.hr_high))
    if step.primary == "pace" and pace:
        return pace, hr
    if step.primary == "hr" and hr:
        return hr, pace
    if step.primary == "none":
        return None, None
    return (pace, hr) if pace else (hr, None)


def build_step(step: WorkoutStep, order: int) -> dict[str, Any]:
    step_type = STEP_TYPES[step.type]
    payload: dict[str, Any] = {
        "type": "ExecutableStepDTO",
        "stepOrder": order,
        "stepType": {"stepTypeId": step_type[0], "stepTypeKey": step_type[1], "displayOrder": 1},
    }
    if step.duration_type == "open":
        payload["endCondition"] = _condition(COND_LAP_BUTTON)
    elif step.duration_type == "distance":
        payload["endCondition"] = _condition(COND_DISTANCE)
        payload["endConditionValue"] = float(step.duration_value or 0)
    else:
        payload["endCondition"] = _condition(COND_TIME)
        payload["endConditionValue"] = float(step.duration_value or 0)

    primary, secondary = _targets(step)
    if primary is None:
        payload["targetType"] = _ref(TARGET_NONE, "workoutTargetTypeId", "workoutTargetTypeKey")
    else:
        payload["targetType"] = _ref(primary[0], "workoutTargetTypeId", "workoutTargetTypeKey")
        payload["targetValueOne"] = primary[1]
        payload["targetValueTwo"] = primary[2]
    if secondary is not None:
        payload["secondaryTargetType"] = _ref(
            secondary[0], "workoutTargetTypeId", "workoutTargetTypeKey", order=2
        )
        payload["secondaryTargetValueOne"] = secondary[1]
        payload["secondaryTargetValueTwo"] = secondary[2]
    return payload


def build_steps(steps: list[WorkoutStep | RepeatStep]) -> list[dict[str, Any]]:
    """Number steps in display order: a repeat group precedes its children."""
    counter = 0

    def nxt() -> int:
        nonlocal counter
        counter += 1
        return counter

    out: list[dict[str, Any]] = []
    for item in steps:
        if isinstance(item, RepeatStep):
            group_order = nxt()
            children = [build_step(child, nxt()) for child in item.steps]
            out.append(
                {
                    "type": "RepeatGroupDTO",
                    "stepOrder": group_order,
                    "stepType": {
                        "stepTypeId": REPEAT_STEP_TYPE[0],
                        "stepTypeKey": REPEAT_STEP_TYPE[1],
                        "displayOrder": 1,
                    },
                    "numberOfIterations": item.repeat_count,
                    "endCondition": _condition(COND_ITERATIONS),
                    "endConditionValue": float(item.repeat_count),
                    "smartRepeat": False,
                    "workoutSteps": children,
                }
            )
        else:
            out.append(build_step(item, nxt()))
    return out


def build_running_workout_payload(
    name: str, steps: list[WorkoutStep | RepeatStep], estimated_duration_s: int
) -> dict[str, Any]:
    return {
        "workoutName": name,
        "sportType": dict(RUNNING),
        "estimatedDurationInSecs": estimated_duration_s,
        "workoutSegments": [
            {
                "segmentOrder": 1,
                "sportType": dict(RUNNING),
                "workoutSteps": build_steps(steps),
            }
        ],
    }


def build_running_workout(
    name: str, steps: list[WorkoutStep | RepeatStep], estimated_duration_s: int
) -> Any:
    """Return a `garminconnect.workout.RunningWorkout` ready for `upload_running_workout`."""
    from garminconnect.workout import RunningWorkout

    return RunningWorkout.model_validate(
        build_running_workout_payload(name, steps, estimated_duration_s)
    )
