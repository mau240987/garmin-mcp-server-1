"""Translation between the workout DSL in :mod:`.models` and Garmin's payloads.

This module is the only place that knows Garmin's wire representation of a
workout step. It goes both ways:

* :func:`build_running_workout` turns a validated :class:`~.models.WorkoutDefinition`
  into a ``garminconnect.workout.RunningWorkout`` ready for upload.
* :func:`project_workout` turns a workout DTO fetched back from Garmin into the
  compact, readable :class:`~.models.WorkoutDetail`.

No network access happens here; everything is pure data transformation, which
keeps it unit-testable without an account.
"""

from __future__ import annotations

from typing import Any

from garminconnect.workout import (
    ConditionType,
    ExecutableStep,
    RepeatGroup,
    RunningWorkout,
    SportType,
    StepType,
    TargetType,
    WorkoutSegment,
)

from .formatting import (
    format_distance,
    format_duration,
    format_pace,
    meters_per_second_to_pace,
    pace_to_meters_per_second,
    pace_to_seconds_per_km,
)
from .models import (
    DurationType,
    RepeatBlock,
    StepKind,
    TargetPriority,
    WorkoutDefinition,
    WorkoutDetail,
    WorkoutStep,
    WorkoutStepView,
    WorkoutSummary,
)

__all__ = [
    "build_running_workout",
    "count_steps",
    "estimate_duration_seconds",
    "project_workout",
    "render_workout_definition",
    "render_workout_detail",
    "summarize_workout",
]

# --------------------------------------------------------------------------- #
# Wire units
#
# Garmin's workout service does not use SI units for `endConditionValue`. The
# scaling factors below are inherited verbatim from the legacy uploader in this
# repository, which was tuned against a live Garmin Connect account; they are
# isolated here so the tool surface can speak plain seconds and metres while the
# wire format stays byte-identical to the version that was verified to work.
# --------------------------------------------------------------------------- #

#: Multiplier applied to a step duration in seconds before it is sent.
TIME_CONDITION_SCALE = 1000

#: Multiplier applied to a step distance in metres before it is sent.
DISTANCE_CONDITION_SCALE = 100

#: Fallback pace, in seconds per kilometre, used to estimate how long a
#: distance-based step with no pace target will take.
DEFAULT_ESTIMATE_PACE_S_PER_KM = 360.0

#: Nominal duration attributed to an open (lap-button) step when estimating.
LAP_BUTTON_ESTIMATE_SECONDS = 300

_STEP_TYPE_WIRE: dict[StepKind, tuple[int, str, int]] = {
    StepKind.WARMUP: (StepType.WARMUP, "warmup", 1),
    StepKind.COOLDOWN: (StepType.COOLDOWN, "cooldown", 2),
    StepKind.RUN: (StepType.INTERVAL, "interval", 3),
    StepKind.RECOVER: (StepType.RECOVERY, "recovery", 4),
    StepKind.REST: (StepType.REST, "rest", 5),
}

_NO_TARGET: dict[str, Any] = {
    "workoutTargetTypeId": TargetType.NO_TARGET,
    "workoutTargetTypeKey": "no.target",
    "displayOrder": 1,
}


# --------------------------------------------------------------------------- #
# DSL -> Garmin
# --------------------------------------------------------------------------- #


def _speed_target(pace_slow: str, pace_fast: str) -> dict[str, Any]:
    """Build a Garmin speed-zone target from a slow/fast pace pair."""
    return {
        "workoutTargetTypeId": TargetType.SPEED,
        "workoutTargetTypeKey": "speed.zone",
        "displayOrder": 1,
        "targetValueOne": pace_to_meters_per_second(pace_slow),
        "targetValueTwo": pace_to_meters_per_second(pace_fast),
    }


def _heart_rate_target(low_bpm: int, high_bpm: int) -> dict[str, Any]:
    """Build a Garmin heart-rate-zone target."""
    return {
        "workoutTargetTypeId": TargetType.HEART_RATE,
        "workoutTargetTypeKey": "heart.rate.zone",
        "displayOrder": 1,
        "targetValueOne": low_bpm,
        "targetValueTwo": high_bpm,
    }


def _end_condition(step: WorkoutStep) -> tuple[dict[str, Any], float | None]:
    """Build the ``endCondition`` block and its scaled value for a step."""
    if step.duration_type is DurationType.DISTANCE:
        assert step.distance_meters is not None
        return (
            {
                "conditionTypeId": ConditionType.DISTANCE,
                "conditionTypeKey": "distance",
                "displayOrder": 1,
                "displayable": True,
            },
            float(step.distance_meters * DISTANCE_CONDITION_SCALE),
        )
    if step.duration_type is DurationType.TIME:
        assert step.duration_seconds is not None
        return (
            {
                "conditionTypeId": ConditionType.TIME,
                "conditionTypeKey": "time",
                "displayOrder": 1,
                "displayable": True,
            },
            float(step.duration_seconds * TIME_CONDITION_SCALE),
        )
    return (
        {
            "conditionTypeId": ConditionType.LAP_BUTTON,
            "conditionTypeKey": "lap.button",
            "displayOrder": 1,
            "displayable": True,
        },
        None,
    )


def _targets(step: WorkoutStep) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Resolve a step's primary and secondary targets from its declared priority."""
    pace_target = (
        _speed_target(step.pace_slow, step.pace_fast)
        if step.pace_slow is not None and step.pace_fast is not None
        else None
    )
    hr_target = (
        _heart_rate_target(step.heart_rate_low, step.heart_rate_high)
        if step.heart_rate_low is not None and step.heart_rate_high is not None
        else None
    )

    priority = step.effective_priority
    if priority is TargetPriority.HEART_RATE and hr_target is not None:
        return hr_target, pace_target
    if priority is TargetPriority.PACE and pace_target is not None:
        return pace_target, hr_target
    return _NO_TARGET, None


def _executable_step(step: WorkoutStep, order: int) -> ExecutableStep:
    """Build one ``ExecutableStep``, hoisting target values to the step root.

    Garmin only honours dual (primary + secondary) targets when
    ``targetValueOne``/``targetValueTwo`` sit at the root of the step object
    rather than nested inside ``targetType``. That hoisting is preserved here
    from the verified legacy uploader.
    """
    step_type_id, step_type_key, display_order = _STEP_TYPE_WIRE[StepKind(step.type)]
    condition, condition_value = _end_condition(step)
    primary, secondary = _targets(step)

    payload: dict[str, Any] = {
        "stepOrder": order,
        "stepType": {
            "stepTypeId": step_type_id,
            "stepTypeKey": step_type_key,
            "displayOrder": display_order,
        },
        "endCondition": condition,
        "targetType": {
            "workoutTargetTypeId": primary["workoutTargetTypeId"],
            "workoutTargetTypeKey": primary["workoutTargetTypeKey"],
            "displayOrder": primary.get("displayOrder", 1),
        },
        "targetValueOne": primary.get("targetValueOne"),
        "targetValueTwo": primary.get("targetValueTwo"),
    }
    if condition_value is not None:
        payload["endConditionValue"] = condition_value
    if step.description:
        payload["description"] = step.description
    if secondary is not None and secondary["workoutTargetTypeId"] != TargetType.NO_TARGET:
        payload["secondaryTargetType"] = {
            "workoutTargetTypeId": secondary["workoutTargetTypeId"],
            "workoutTargetTypeKey": secondary["workoutTargetTypeKey"],
            "displayOrder": secondary.get("displayOrder", 1),
        }
        payload["secondaryTargetValueOne"] = secondary.get("targetValueOne")
        payload["secondaryTargetValueTwo"] = secondary.get("targetValueTwo")

    return ExecutableStep(**payload)


def _repeat_group(block: RepeatBlock, order: int, counter: list[int]) -> RepeatGroup:
    """Build a ``RepeatGroup``, recursing into nested steps."""
    children = [_build_step(child, counter) for child in block.steps]
    return RepeatGroup(
        stepOrder=order,
        stepType={"stepTypeId": StepType.REPEAT, "stepTypeKey": "repeat", "displayOrder": 6},
        numberOfIterations=block.repeat_count,
        workoutSteps=children,
        endCondition={
            "conditionTypeId": ConditionType.ITERATIONS,
            "conditionTypeKey": "iterations",
            "displayOrder": 7,
            "displayable": False,
        },
        endConditionValue=float(block.repeat_count),
    )


def _build_step(step: WorkoutStep | RepeatBlock, counter: list[int]) -> ExecutableStep | RepeatGroup:
    counter[0] += 1
    order = counter[0]
    if isinstance(step, RepeatBlock):
        return _repeat_group(step, order, counter)
    return _executable_step(step, order)


def count_steps(steps: list[WorkoutStep | RepeatBlock]) -> int:
    """Count every step object in a workout, repeat groups included."""
    total = 0
    for step in steps:
        total += 1
        if isinstance(step, RepeatBlock):
            total += count_steps(step.steps)
    return total


def _step_seconds(step: WorkoutStep) -> float:
    if step.duration_type is DurationType.TIME:
        assert step.duration_seconds is not None
        return float(step.duration_seconds)
    if step.duration_type is DurationType.DISTANCE:
        assert step.distance_meters is not None
        if step.pace_slow is not None and step.pace_fast is not None:
            pace = (
                pace_to_seconds_per_km(step.pace_slow) + pace_to_seconds_per_km(step.pace_fast)
            ) / 2.0
        else:
            pace = DEFAULT_ESTIMATE_PACE_S_PER_KM
        return step.distance_meters / 1000.0 * pace
    return float(LAP_BUTTON_ESTIMATE_SECONDS)


def estimate_duration_seconds(steps: list[WorkoutStep | RepeatBlock]) -> int:
    """Estimate how long a step list takes to execute, in seconds.

    Timed steps contribute their duration exactly. Distance steps are estimated
    from the midpoint of their pace target, falling back to a 6:00/km easy pace
    when no pace target is set. Lap-button steps contribute a nominal five
    minutes because their real duration is unknowable.
    """
    total = 0.0
    for step in steps:
        if isinstance(step, RepeatBlock):
            total += step.repeat_count * estimate_duration_seconds(step.steps)
        else:
            total += _step_seconds(step)
    return int(round(total))


def build_running_workout(definition: WorkoutDefinition) -> RunningWorkout:
    """Convert a validated :class:`~.models.WorkoutDefinition` into a Garmin payload.

    Args:
        definition: The workout to upload. Already validated by pydantic, so this
            function performs no further semantic checks.

    Returns:
        A ``RunningWorkout`` suitable for ``Garmin.upload_running_workout``.
    """
    counter = [0]
    steps = [_build_step(step, counter) for step in definition.steps]
    duration = (
        definition.estimated_duration_minutes * 60
        if definition.estimated_duration_minutes is not None
        else estimate_duration_seconds(definition.steps)
    )
    workout = RunningWorkout(
        workoutName=definition.name,
        estimatedDurationInSecs=duration,
        workoutSegments=[
            WorkoutSegment(
                segmentOrder=1,
                sportType={
                    "sportTypeId": SportType.RUNNING,
                    "sportTypeKey": "running",
                    "displayOrder": 1,
                },
                workoutSteps=steps,
            )
        ],
    )
    if definition.description:
        workout.description = definition.description
    return workout


# --------------------------------------------------------------------------- #
# Human-readable rendering of the DSL
# --------------------------------------------------------------------------- #


def _describe_step_target(step: WorkoutStep) -> str:
    parts = []
    if step.pace_slow and step.pace_fast:
        parts.append(f"{step.pace_fast}-{step.pace_slow}/km")
    if step.heart_rate_low and step.heart_rate_high:
        parts.append(f"{step.heart_rate_low}-{step.heart_rate_high} bpm")
    if not parts:
        return "no target"
    priority = step.effective_priority.value
    return " + ".join(parts) + (f" (primary: {priority})" if len(parts) > 1 else "")


def _describe_step_duration(step: WorkoutStep) -> str:
    if step.duration_type is DurationType.TIME:
        return format_duration(step.duration_seconds) or "?"
    if step.duration_type is DurationType.DISTANCE:
        return format_distance(step.distance_meters) or "?"
    return "until lap button"


def render_workout_definition(definition: WorkoutDefinition) -> str:
    """Render a workout definition as compact indented text for human review."""
    lines: list[str] = []

    def walk(steps: list[WorkoutStep | RepeatBlock], indent: int) -> None:
        pad = "  " * indent
        for step in steps:
            if isinstance(step, RepeatBlock):
                lines.append(f"{pad}{step.repeat_count}x:")
                walk(step.steps, indent + 1)
            else:
                kind = StepKind(step.type).value
                lines.append(
                    f"{pad}{kind:<8} {_describe_step_duration(step):<12} "
                    f"{_describe_step_target(step)}"
                )

    walk(definition.steps, 1)
    duration = (
        definition.estimated_duration_minutes * 60
        if definition.estimated_duration_minutes is not None
        else estimate_duration_seconds(definition.steps)
    )
    header = f"{definition.name} (~{format_duration(duration)})"
    return "\n".join([header, *lines])


# --------------------------------------------------------------------------- #
# Garmin -> readable projection
# --------------------------------------------------------------------------- #


def summarize_workout(payload: dict[str, Any]) -> WorkoutSummary:
    """Project a Garmin workout DTO onto :class:`~.models.WorkoutSummary`."""
    sport = payload.get("sportType") or {}
    return WorkoutSummary(
        workout_id=str(payload.get("workoutId", "")),
        name=str(payload.get("workoutName") or "Unnamed workout"),
        sport_type=sport.get("sportTypeKey") if isinstance(sport, dict) else None,
        estimated_duration_seconds=payload.get("estimatedDurationInSecs"),
        updated_date=payload.get("updateDate") or payload.get("updatedDate"),
        description=payload.get("description"),
    )


def _describe_wire_condition(step: dict[str, Any]) -> str | None:
    condition = step.get("endCondition") or {}
    key = condition.get("conditionTypeKey") if isinstance(condition, dict) else None
    value = step.get("endConditionValue")
    if key == "time" and value:
        return format_duration(float(value) / TIME_CONDITION_SCALE)
    if key == "distance" and value:
        return format_distance(float(value) / DISTANCE_CONDITION_SCALE)
    if key == "lap.button":
        return "until lap button"
    if key == "iterations" and value:
        return f"{int(value)} iterations"
    return None


def _describe_wire_target(step: dict[str, Any]) -> str | None:
    target = step.get("targetType") or {}
    key = target.get("workoutTargetTypeKey") if isinstance(target, dict) else None
    one, two = step.get("targetValueOne"), step.get("targetValueTwo")
    if key == "speed.zone" and one and two:
        # targetValueOne is the slower bound, i.e. the lower speed.
        return f"{meters_per_second_to_pace(float(two))} - {meters_per_second_to_pace(float(one))}"
    if key == "heart.rate.zone" and one and two:
        return f"{int(one)}-{int(two)} bpm"
    if key in (None, "no.target"):
        return None
    return str(key)


def _project_steps(
    raw_steps: list[Any], views: list[WorkoutStepView], depth: int, counter: list[int]
) -> None:
    for raw in raw_steps:
        if not isinstance(raw, dict):
            continue
        counter[0] += 1
        step_type = raw.get("stepType") or {}
        kind = step_type.get("stepTypeKey", "unknown") if isinstance(step_type, dict) else "unknown"
        iterations = raw.get("numberOfIterations")
        views.append(
            WorkoutStepView(
                order=counter[0],
                depth=depth,
                kind=str(kind),
                duration=_describe_wire_condition(raw),
                target=_describe_wire_target(raw),
                repeat_count=int(iterations) if iterations else None,
            )
        )
        children = raw.get("workoutSteps")
        if isinstance(children, list):
            _project_steps(children, views, depth + 1, counter)


def project_workout(payload: dict[str, Any]) -> WorkoutDetail:
    """Project a full Garmin workout DTO onto :class:`~.models.WorkoutDetail`.

    Repeat groups are flattened into an ordered list with a ``depth`` marker,
    which reads far better in a transcript than nested JSON and stays bounded in
    size regardless of how deeply the workout nests.
    """
    views: list[WorkoutStepView] = []
    counter = [0]
    segments = payload.get("workoutSegments")
    if isinstance(segments, list):
        for segment in segments:
            if isinstance(segment, dict) and isinstance(segment.get("workoutSteps"), list):
                _project_steps(segment["workoutSteps"], views, 0, counter)
    return WorkoutDetail(
        workout=summarize_workout(payload),
        steps=views,
        step_count=len(views),
    )


def render_workout_detail(detail: WorkoutDetail) -> str:
    """Render a :class:`~.models.WorkoutDetail` as compact indented text."""
    summary = detail.workout
    header = f"[{summary.workout_id}] {summary.name} ({summary.sport_type or 'unknown sport'})"
    if summary.estimated_duration_seconds:
        header += f" ~{format_duration(summary.estimated_duration_seconds)}"
    lines = [header]
    for view in detail.steps:
        pad = "  " * (view.depth + 1)
        bits = [view.kind]
        if view.repeat_count:
            bits.append(f"x{view.repeat_count}")
        if view.duration:
            bits.append(view.duration)
        if view.target:
            bits.append(view.target)
        lines.append(pad + " | ".join(bits))
    if not detail.steps:
        lines.append("  (no steps returned)")
    return "\n".join(lines)
