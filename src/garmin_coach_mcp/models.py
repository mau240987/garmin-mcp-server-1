"""Pydantic input and output models for the Garmin Coach MCP tool surface.

Everything a tool accepts or returns is declared here. Two design rules drive the
whole module:

1. **No JSON-encoded strings.** The legacy server took ``steps_json`` and
   ``workouts_json`` as opaque strings, so the generated tool schema documented
   nothing and the client could not validate anything before the call. Workouts
   are modelled here as a real recursive structure
   (:class:`WorkoutDefinition` -> :class:`WorkoutStep` | :class:`RepeatBlock`),
   which renders as a proper ``oneOf`` + ``discriminator`` JSON Schema.
2. **Closed sets are enums, bounded numbers carry bounds.** Every scalar
   parameter is an ``Annotated`` type with a description and, where meaningful,
   ``ge``/``le``/``pattern``, so a model reading the schema can get the call
   right on the first attempt.

Input models validate aggressively and raise on contradictions (a ``time`` step
with no ``duration_seconds``, a "slow" pace faster than the "fast" pace, and so
on) rather than silently guessing, which is what the legacy builder did.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Annotated, Any, Literal, Union

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    WithJsonSchema,
    model_validator,
)

from .formatting import PACE_EXAMPLE, PACE_PATTERN, pace_to_seconds_per_km

__all__ = [
    "ActivityDetail",
    "ActivityList",
    "ActivitySummary",
    "ActivityType",
    "BulkDeletionResult",
    "DeletionCandidate",
    "DurationType",
    "HealthMetric",
    "HealthSummary",
    "IsoDate",
    "MetricStatus",
    "PlanWorkoutOutcome",
    "PlannedWorkout",
    "RepeatBlock",
    "ScheduledWorkout",
    "StepKind",
    "TargetPriority",
    "TrainingLoad",
    "WorkoutCreated",
    "WorkoutDefinition",
    "WorkoutDeleted",
    "WorkoutDetail",
    "WorkoutList",
    "WorkoutStep",
    "WorkoutStepUnion",
    "WorkoutStepView",
    "WorkoutSummary",
]

# --------------------------------------------------------------------------- #
# Reusable annotated scalar types
# --------------------------------------------------------------------------- #

_ISO_DATE_SCHEMA: dict[str, Any] = {
    "type": "string",
    "format": "date",
    "pattern": r"^\d{4}-\d{2}-\d{2}$",
    "examples": ["2026-03-15"],
}


def _coerce_iso_date(value: Any) -> Any:
    """Accept ``date``/``datetime`` objects and ``YYYY-MM-DD`` strings alike.

    Pydantic's lax mode would otherwise also accept integers as Unix timestamps,
    which is never what a caller means by "date" here and produces baffling
    off-by-decades results.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return value.strip()
    raise ValueError(f"expected an ISO date string 'YYYY-MM-DD', got {type(value).__name__}")


#: Calendar date. Accepts an ISO ``YYYY-MM-DD`` string or a ``date`` object, and
#: always renders in the schema as ``{"type": "string", "format": "date"}``.
IsoDate = Annotated[date, BeforeValidator(_coerce_iso_date), WithJsonSchema(_ISO_DATE_SCHEMA)]

#: Running pace as minutes and seconds per kilometre, e.g. ``"4:35"``.
Pace = Annotated[str, Field(pattern=PACE_PATTERN, examples=[PACE_EXAMPLE, "5:20"])]

#: Heart rate in beats per minute, bounded to physiologically plausible values.
HeartRateBpm = Annotated[int, Field(ge=30, le=230)]

#: Garmin resource identifiers are numeric but are transported as strings so
#: large IDs survive JSON number handling intact.
GarminId = Annotated[str, Field(pattern=r"^\d{1,20}$", examples=["12345678901"])]


# --------------------------------------------------------------------------- #
# Enumerations (closed sets)
# --------------------------------------------------------------------------- #


class ActivityType(str, Enum):
    """Activity types Garmin Connect accepts as a server-side filter.

    These are the exact values the Garmin activity search endpoint understands.
    Filtering happens upstream, so a filtered page still returns up to ``limit``
    results — unlike the legacy server, which fetched a page and then discarded
    non-matching rows, silently returning fewer results than asked for.
    """

    RUNNING = "running"
    CYCLING = "cycling"
    SWIMMING = "swimming"
    WALKING = "walking"
    HIKING = "hiking"
    MULTI_SPORT = "multi_sport"
    FITNESS_EQUIPMENT = "fitness_equipment"
    OTHER = "other"


class StepKind(str, Enum):
    """The kind of effort an executable workout step represents."""

    WARMUP = "warmup"
    RUN = "run"
    RECOVER = "recover"
    COOLDOWN = "cooldown"
    REST = "rest"


class DurationType(str, Enum):
    """How a workout step decides it is finished."""

    TIME = "time"
    DISTANCE = "distance"
    LAP_BUTTON = "lap_button"


class TargetPriority(str, Enum):
    """Which target the watch shows as the primary gauge during a step."""

    PACE = "pace"
    HEART_RATE = "heart_rate"
    NONE = "none"


class MetricStatus(str, Enum):
    """Per-metric outcome in a health summary.

    The distinction matters: ``not_recorded`` means Garmin answered and had
    nothing for that day (the watch was off the wrist, the feature is not
    supported by the device), whereas ``fetch_failed`` means the request itself
    failed and the true value is unknown. The legacy server collapsed both into
    "line missing from the output".
    """

    OK = "ok"
    NOT_RECORDED = "not_recorded"
    FETCH_FAILED = "fetch_failed"


# --------------------------------------------------------------------------- #
# Workout definition (input)
# --------------------------------------------------------------------------- #


class WorkoutStep(BaseModel):
    """One executable step of a structured running workout.

    A step ends when its duration condition is met (``duration_type``), and may
    carry a pace range, a heart-rate range, or both. When both are given, the
    watch shows both gauges and ``target_priority`` decides which one is
    primary.

    Pace bounds are named by *effort*, not by numeric value: ``pace_slow`` is the
    slower (numerically larger) end of the range and ``pace_fast`` is the faster
    (numerically smaller) end. Getting them backwards is a validation error, not
    a silently inverted target.
    """

    model_config = ConfigDict(extra="forbid")

    type: Annotated[
        Literal[
            StepKind.WARMUP,
            StepKind.RUN,
            StepKind.RECOVER,
            StepKind.COOLDOWN,
            StepKind.REST,
        ],
        Field(
            description=(
                "Effort kind for this step. 'warmup' and 'cooldown' bracket the "
                "session, 'run' is a work interval or a steady effort, 'recover' "
                "is easy jogging between hard efforts, 'rest' is standing still. "
                "Use 'repeat' (a different step object) to loop a group of steps."
            )
        ),
    ]
    duration_type: Annotated[
        DurationType,
        Field(
            description=(
                "How the step ends. 'time' requires duration_seconds, 'distance' "
                "requires distance_meters, 'lap_button' ends only when the athlete "
                "presses the lap button and takes neither."
            )
        ),
    ]
    duration_seconds: Annotated[
        int | None,
        Field(
            default=None,
            ge=5,
            le=86_400,
            description=(
                "Step length in seconds. Required when duration_type='time', and "
                "must be omitted otherwise."
            ),
            examples=[600, 90],
        ),
    ]
    distance_meters: Annotated[
        int | None,
        Field(
            default=None,
            ge=10,
            le=200_000,
            description=(
                "Step length in metres. Required when duration_type='distance', "
                "and must be omitted otherwise."
            ),
            examples=[1000, 400],
        ),
    ]
    pace_slow: Annotated[
        Pace | None,
        Field(
            default=None,
            description=(
                "Slower end of the pace range, min:sec per kilometre. Must be "
                "given together with pace_fast and must be numerically larger "
                "than it (5:30 is slower than 5:00)."
            ),
        ),
    ]
    pace_fast: Annotated[
        Pace | None,
        Field(
            default=None,
            description=(
                "Faster end of the pace range, min:sec per kilometre. Must be "
                "given together with pace_slow."
            ),
        ),
    ]
    heart_rate_low: Annotated[
        HeartRateBpm | None,
        Field(
            default=None,
            description=(
                "Lower bound of the heart-rate target in bpm. Must be given "
                "together with heart_rate_high."
            ),
            examples=[135],
        ),
    ]
    heart_rate_high: Annotated[
        HeartRateBpm | None,
        Field(
            default=None,
            description=(
                "Upper bound of the heart-rate target in bpm. Must be given "
                "together with heart_rate_low and must exceed it."
            ),
            examples=[150],
        ),
    ]
    target_priority: Annotated[
        TargetPriority | None,
        Field(
            default=None,
            description=(
                "Which gauge the watch treats as primary. Omit to infer: heart "
                "rate wins when an HR range is present, otherwise pace, otherwise "
                "no target. Setting this to a target you did not supply is an "
                "error."
            ),
        ),
    ]
    description: Annotated[
        str | None,
        Field(
            default=None,
            max_length=512,
            description="Optional coaching cue shown on the watch for this step.",
            examples=["Relaxed shoulders, quick cadence"],
        ),
    ]

    @model_validator(mode="after")
    def _check_coherent(self) -> WorkoutStep:
        if self.duration_type is DurationType.TIME:
            if self.duration_seconds is None:
                raise ValueError("duration_type='time' requires duration_seconds")
            if self.distance_meters is not None:
                raise ValueError("duration_type='time' must not set distance_meters")
        elif self.duration_type is DurationType.DISTANCE:
            if self.distance_meters is None:
                raise ValueError("duration_type='distance' requires distance_meters")
            if self.duration_seconds is not None:
                raise ValueError("duration_type='distance' must not set duration_seconds")
        else:  # LAP_BUTTON
            if self.duration_seconds is not None or self.distance_meters is not None:
                raise ValueError(
                    "duration_type='lap_button' must not set duration_seconds or distance_meters"
                )

        if (self.pace_slow is None) != (self.pace_fast is None):
            raise ValueError("pace_slow and pace_fast must be supplied together")
        if self.pace_slow is not None and self.pace_fast is not None:
            slow = pace_to_seconds_per_km(self.pace_slow)
            fast = pace_to_seconds_per_km(self.pace_fast)
            if slow <= fast:
                raise ValueError(
                    f"pace_slow ({self.pace_slow}) must be slower than pace_fast "
                    f"({self.pace_fast}); a slower pace has a larger min:sec value"
                )

        if (self.heart_rate_low is None) != (self.heart_rate_high is None):
            raise ValueError("heart_rate_low and heart_rate_high must be supplied together")
        if (
            self.heart_rate_low is not None
            and self.heart_rate_high is not None
            and self.heart_rate_low >= self.heart_rate_high
        ):
            raise ValueError(
                f"heart_rate_low ({self.heart_rate_low}) must be below "
                f"heart_rate_high ({self.heart_rate_high})"
            )

        if self.target_priority is TargetPriority.PACE and self.pace_slow is None:
            raise ValueError("target_priority='pace' requires pace_slow and pace_fast")
        if self.target_priority is TargetPriority.HEART_RATE and self.heart_rate_low is None:
            raise ValueError(
                "target_priority='heart_rate' requires heart_rate_low and heart_rate_high"
            )
        return self

    @property
    def effective_priority(self) -> TargetPriority:
        """Resolve ``target_priority``, applying the documented inference rules."""
        if self.target_priority is not None:
            return self.target_priority
        if self.heart_rate_low is not None:
            return TargetPriority.HEART_RATE
        if self.pace_slow is not None:
            return TargetPriority.PACE
        return TargetPriority.NONE


class RepeatBlock(BaseModel):
    """A group of steps executed ``repeat_count`` times in a row.

    This is how interval sessions are expressed: a repeat block containing a
    work step and a recovery step, repeated N times. Repeat blocks may nest, up
    to :data:`MAX_STEP_NESTING_DEPTH` levels.
    """

    model_config = ConfigDict(extra="forbid")

    type: Annotated[
        Literal["repeat"],
        Field(description="Marks this object as a repeat group rather than an executable step."),
    ]
    repeat_count: Annotated[
        int,
        Field(
            ge=1,
            le=99,
            description="How many times the contained steps are executed, e.g. 6 for 6x800m.",
            examples=[6],
        ),
    ]
    steps: Annotated[
        list["WorkoutStepUnion"],
        Field(
            min_length=1,
            max_length=20,
            description=(
                "Steps executed on each repetition, in order. Typically a work "
                "step followed by a recovery step."
            ),
        ),
    ]


#: A workout step is either an executable step or a repeat group; the ``type``
#: field discriminates between them, which renders as a JSON-Schema
#: ``oneOf`` + ``discriminator`` block rather than an ambiguous union.
WorkoutStepUnion = Annotated[Union[WorkoutStep, RepeatBlock], Field(discriminator="type")]

RepeatBlock.model_rebuild()

#: Repeat blocks may nest this many levels deep (1 = no nesting).
MAX_STEP_NESTING_DEPTH = 3

#: Upper bound on executable steps once repeat groups are expanded structurally.
MAX_TOTAL_STEPS = 100


def _count_and_check_depth(steps: list[Any], depth: int = 1) -> int:
    total = 0
    for step in steps:
        if isinstance(step, RepeatBlock):
            if depth >= MAX_STEP_NESTING_DEPTH:
                raise ValueError(
                    f"repeat blocks may not nest more than {MAX_STEP_NESTING_DEPTH} levels deep"
                )
            total += 1 + _count_and_check_depth(step.steps, depth + 1)
        else:
            total += 1
    return total


class WorkoutDefinition(BaseModel):
    """A complete structured running workout, ready to upload to Garmin Connect.

    The step list is ordered and may contain repeat blocks. Estimated duration is
    optional: when omitted it is derived from the steps (timed steps contribute
    their duration, distance steps are estimated from their pace target or a
    default easy pace, lap-button steps contribute a nominal five minutes).
    """

    model_config = ConfigDict(extra="forbid")

    name: Annotated[
        str,
        Field(
            min_length=1,
            max_length=80,
            description=(
                "Name shown in the Garmin workout library and on the watch. Keep "
                "it short and identifiable; prefixing every workout of a plan with "
                "a common tag (e.g. 'W1 - ') makes them easy to find and to remove "
                "later with delete_workouts."
            ),
            examples=["W1 Tue - 6x800m @ 3:55"],
        ),
    ]
    steps: Annotated[
        list[WorkoutStepUnion],
        Field(
            min_length=1,
            max_length=50,
            description="Ordered steps of the workout. Repeat blocks may be used for intervals.",
        ),
    ]
    estimated_duration_minutes: Annotated[
        int | None,
        Field(
            default=None,
            ge=1,
            le=1440,
            description=(
                "Total expected duration in minutes. Omit to have it computed "
                "from the steps."
            ),
        ),
    ]
    description: Annotated[
        str | None,
        Field(
            default=None,
            max_length=1024,
            description="Optional notes attached to the workout in Garmin Connect.",
        ),
    ]

    @model_validator(mode="after")
    def _check_size(self) -> WorkoutDefinition:
        total = _count_and_check_depth(self.steps)
        if total > MAX_TOTAL_STEPS:
            raise ValueError(
                f"workout has {total} steps, which exceeds the supported maximum "
                f"of {MAX_TOTAL_STEPS}"
            )
        return self


class PlannedWorkout(BaseModel):
    """One dated entry of a multi-day training plan."""

    model_config = ConfigDict(extra="forbid")

    day_offset: Annotated[
        int,
        Field(
            ge=0,
            le=364,
            description=(
                "Days after the plan's start_date on which this workout is "
                "scheduled. 0 is the start date itself, 1 is the next day."
            ),
            examples=[0, 2, 5],
        ),
    ]
    workout: Annotated[
        WorkoutDefinition,
        Field(description="The workout to create and schedule on that day."),
    ]


# --------------------------------------------------------------------------- #
# Output models
# --------------------------------------------------------------------------- #


class ActivitySummary(BaseModel):
    """A single completed activity, as shown in a list."""

    activity_id: str = Field(description="Garmin activity ID. Pass this to get_activity.")
    name: str | None = Field(default=None, description="User-visible activity name.")
    activity_type: str | None = Field(
        default=None, description="Garmin type key, e.g. 'running', 'trail_running'."
    )
    start_time_local: str | None = Field(
        default=None, description="Local start timestamp reported by Garmin."
    )
    date: str | None = Field(default=None, description="Local calendar date, YYYY-MM-DD.")
    distance_meters: float | None = Field(default=None, description="Distance covered, in metres.")
    duration_seconds: float | None = Field(default=None, description="Total timer duration, in seconds.")
    average_pace: str | None = Field(
        default=None, description="Average pace as 'm:ss/km'; null when the activity has no distance."
    )
    average_heart_rate: int | None = Field(default=None, description="Average heart rate, bpm.")
    max_heart_rate: int | None = Field(default=None, description="Maximum heart rate, bpm.")
    calories: int | None = Field(default=None, description="Active calories, kcal.")
    elevation_gain_meters: float | None = Field(default=None, description="Total ascent, in metres.")


class ActivityList(BaseModel):
    """A page of completed activities."""

    activities: list[ActivitySummary] = Field(description="Activities, most recent first.")
    count: int = Field(description="Number of activities in this page.")
    start: int = Field(description="Pagination offset this page was fetched from.")
    limit: int = Field(description="Page size that was requested.")
    activity_type: str | None = Field(
        default=None, description="Server-side type filter that was applied, if any."
    )
    has_more: bool = Field(
        description=(
            "True when the page came back full, meaning more activities probably "
            "exist; re-call with start = start + count."
        )
    )


class ActivityDetail(BaseModel):
    """A bounded projection of one activity's detail payload.

    Garmin returns several hundred fields per activity. Dumping all of them (as
    the legacy server did) floods the context window without adding usable
    signal, so this model keeps the coaching-relevant subset and reports how many
    fields were dropped.
    """

    activity_id: str = Field(description="Garmin activity ID.")
    name: str | None = Field(default=None, description="User-visible activity name.")
    description: str | None = Field(default=None, description="Athlete's note on the activity.")
    activity_type: str | None = Field(default=None, description="Garmin type key.")
    start_time_local: str | None = Field(default=None, description="Local start timestamp.")
    location: str | None = Field(default=None, description="Location name reported by Garmin.")

    distance_meters: float | None = Field(default=None, description="Distance, in metres.")
    duration_seconds: float | None = Field(default=None, description="Timer duration, in seconds.")
    moving_duration_seconds: float | None = Field(
        default=None, description="Duration excluding pauses, in seconds."
    )
    elapsed_duration_seconds: float | None = Field(
        default=None, description="Wall-clock duration including pauses, in seconds."
    )
    average_pace: str | None = Field(default=None, description="Average pace, 'm:ss/km'.")
    best_pace: str | None = Field(default=None, description="Fastest instantaneous pace, 'm:ss/km'.")
    elevation_gain_meters: float | None = Field(default=None, description="Total ascent, in metres.")
    elevation_loss_meters: float | None = Field(default=None, description="Total descent, in metres.")

    average_heart_rate: int | None = Field(default=None, description="Average heart rate, bpm.")
    max_heart_rate: int | None = Field(default=None, description="Maximum heart rate, bpm.")
    average_cadence_spm: float | None = Field(
        default=None, description="Average running cadence, steps per minute."
    )
    max_cadence_spm: float | None = Field(
        default=None, description="Maximum running cadence, steps per minute."
    )
    stride_length_cm: float | None = Field(default=None, description="Average stride length, cm.")
    ground_contact_time_ms: float | None = Field(
        default=None, description="Average ground contact time, milliseconds."
    )
    vertical_oscillation_cm: float | None = Field(
        default=None, description="Average vertical oscillation, cm."
    )
    vertical_ratio: float | None = Field(
        default=None, description="Vertical oscillation as a percentage of stride length."
    )
    average_power_watts: float | None = Field(default=None, description="Average running power, W.")

    calories: int | None = Field(default=None, description="Active calories, kcal.")
    aerobic_training_effect: float | None = Field(
        default=None, description="Garmin aerobic training effect, 0.0-5.0."
    )
    anaerobic_training_effect: float | None = Field(
        default=None, description="Garmin anaerobic training effect, 0.0-5.0."
    )
    vo2max: float | None = Field(default=None, description="VO2max estimate from this activity.")

    laps: list["LapSummary"] = Field(
        default_factory=list,
        description=(
            "Per-lap splits when Garmin includes them in the activity summary. "
            "Empty when the activity has no lap data."
        ),
    )
    omitted_field_count: int = Field(
        default=0,
        description=(
            "How many top-level fields of the raw Garmin payload were dropped by "
            "this projection. Purely informational."
        ),
    )


class LapSummary(BaseModel):
    """One lap or split of an activity."""

    lap: int = Field(description="1-based lap index.")
    distance_meters: float | None = Field(default=None, description="Lap distance, in metres.")
    duration_seconds: float | None = Field(default=None, description="Lap duration, in seconds.")
    pace: str | None = Field(default=None, description="Lap pace, 'm:ss/km'.")
    average_heart_rate: int | None = Field(default=None, description="Average heart rate for the lap, bpm.")


class HealthMetric(BaseModel):
    """One daily health metric, with an explicit per-metric outcome."""

    metric: str = Field(description="Stable machine key, e.g. 'resting_heart_rate'.")
    label: str = Field(description="Human-readable metric name.")
    status: MetricStatus = Field(
        description=(
            "'ok' = a value was returned; 'not_recorded' = Garmin answered but has "
            "no data for that day; 'fetch_failed' = the request failed and the "
            "true value is unknown."
        )
    )
    value: float | None = Field(default=None, description="Numeric value when status is 'ok'.")
    unit: str | None = Field(default=None, description="Unit of `value`, e.g. 'bpm', 'steps'.")
    display: str | None = Field(default=None, description="Preformatted value for display.")
    detail: str | None = Field(
        default=None,
        description=(
            "Extra context when status is 'ok' (e.g. a sleep score), or the error "
            "message and remediation when status is 'fetch_failed'."
        ),
    )


class HealthSummary(BaseModel):
    """Daily health and recovery metrics for one calendar date."""

    date: str = Field(description="The calendar date these metrics describe, YYYY-MM-DD.")
    metrics: list[HealthMetric] = Field(description="One entry per metric, always the full set.")
    ok_count: int = Field(description="Metrics that returned a value.")
    not_recorded_count: int = Field(description="Metrics Garmin has no data for on this date.")
    failed_count: int = Field(
        description="Metrics whose fetch failed. Non-zero means this summary is incomplete."
    )


class TrainingLoad(BaseModel):
    """Aggregated training volume over a recent window."""

    start_date: str = Field(description="First day of the window, YYYY-MM-DD, inclusive.")
    end_date: str = Field(description="Last day of the window, YYYY-MM-DD, inclusive.")
    days: int = Field(description="Length of the window in days.")
    total_activities: int = Field(description="All activities in the window, any type.")
    activities_by_type: dict[str, int] = Field(
        default_factory=dict, description="Activity count keyed by Garmin type key."
    )
    running_sessions: int = Field(description="Activities whose type key contains 'running'.")
    running_distance_meters: float = Field(description="Total running distance, in metres.")
    running_duration_seconds: float = Field(description="Total running duration, in seconds.")
    average_running_pace: str | None = Field(
        default=None, description="Volume-weighted average running pace, 'm:ss/km'."
    )
    longest_run_meters: float | None = Field(
        default=None, description="Distance of the longest single run, in metres."
    )
    recent_runs: list[ActivitySummary] = Field(
        default_factory=list, description="Up to 10 most recent runs in the window."
    )


class WorkoutSummary(BaseModel):
    """A workout in the Garmin Connect workout library."""

    workout_id: str = Field(description="Garmin workout ID. Pass this to get_workout or delete_workout.")
    name: str = Field(description="Workout name.")
    sport_type: str | None = Field(default=None, description="Garmin sport type key, e.g. 'running'.")
    estimated_duration_seconds: int | None = Field(
        default=None, description="Duration estimate stored with the workout."
    )
    updated_date: str | None = Field(default=None, description="Last modification timestamp.")
    description: str | None = Field(default=None, description="Workout description, if any.")


class WorkoutList(BaseModel):
    """A page of saved workouts."""

    workouts: list[WorkoutSummary] = Field(description="Matching workouts.")
    count: int = Field(description="Number of workouts returned.")
    start: int = Field(description="Pagination offset this page was fetched from.")
    limit: int = Field(description="Page size that was requested.")
    name_contains: str | None = Field(
        default=None, description="Client-side name filter that was applied, if any."
    )
    has_more: bool = Field(
        description="True when the underlying page came back full; re-call with a larger start."
    )


class WorkoutStepView(BaseModel):
    """One step of a saved workout, flattened for reading."""

    order: int = Field(description="1-based position within the workout.")
    depth: int = Field(description="Nesting level; 0 is top level, 1 is inside a repeat block.")
    kind: str = Field(description="Garmin step type key, e.g. 'warmup', 'interval', 'repeat'.")
    duration: str | None = Field(
        default=None, description="Human-readable end condition, e.g. '10m 00s', '800 m', 'lap button'."
    )
    target: str | None = Field(
        default=None, description="Human-readable target, e.g. '4:30/km - 4:10/km' or '140-155 bpm'."
    )
    repeat_count: int | None = Field(default=None, description="Iterations, for repeat steps only.")


class WorkoutDetail(BaseModel):
    """A saved workout including its step structure."""

    workout: WorkoutSummary = Field(description="Library metadata for the workout.")
    steps: list[WorkoutStepView] = Field(description="Steps in execution order, repeats expanded inline.")
    step_count: int = Field(description="Number of step entries listed.")


class WorkoutCreated(BaseModel):
    """Result of creating a structured workout."""

    workout_id: str = Field(description="ID of the newly created workout.")
    name: str = Field(description="Name the workout was saved under.")
    step_count: int = Field(description="Number of steps uploaded, repeat groups included.")
    estimated_duration_seconds: int = Field(description="Duration estimate stored with the workout.")
    scheduled_date: str | None = Field(
        default=None, description="Date the workout was scheduled for, or null if not scheduled."
    )
    scheduling_error: str | None = Field(
        default=None,
        description=(
            "Set when the workout was created but scheduling failed. The workout "
            "exists; retry with schedule_workout."
        ),
    )


class ScheduledWorkout(BaseModel):
    """Result of scheduling an existing workout on a date."""

    workout_id: str = Field(description="The workout that was scheduled.")
    scheduled_date: str = Field(description="Calendar date it was scheduled on, YYYY-MM-DD.")


class PlanWorkoutOutcome(BaseModel):
    """Per-workout outcome inside a training-plan push."""

    day_offset: int = Field(description="Offset from the plan start date.")
    date: str = Field(description="Resolved calendar date, YYYY-MM-DD.")
    name: str = Field(description="Workout name.")
    created: bool = Field(description="Whether the workout was created on Garmin Connect.")
    workout_id: str | None = Field(default=None, description="ID of the created workout, if any.")
    scheduled: bool = Field(default=False, description="Whether it was placed on the calendar.")
    error: str | None = Field(default=None, description="Failure reason when created is false.")


class TrainingPlanResult(BaseModel):
    """Result of pushing a multi-day training plan.

    Creation is not atomic: each workout is uploaded independently and is not
    retried, so a partial success is possible and is reported honestly here.
    """

    plan_name: str = Field(description="Name given to the plan.")
    start_date: str = Field(description="Plan start date, YYYY-MM-DD.")
    end_date: str = Field(description="Date of the last scheduled workout, YYYY-MM-DD.")
    requested: int = Field(description="Workouts in the submitted plan.")
    created: int = Field(description="Workouts successfully created.")
    failed: int = Field(description="Workouts that could not be created.")
    outcomes: list[PlanWorkoutOutcome] = Field(description="One entry per requested workout, in order.")


class WorkoutDeleted(BaseModel):
    """Result of deleting a single workout."""

    workout_id: str = Field(description="The workout that was deleted.")
    deleted: bool = Field(description="Always true; a failure raises instead.")


class DeletionCandidate(BaseModel):
    """A workout matched by a bulk-delete filter."""

    workout_id: str = Field(description="Garmin workout ID.")
    name: str = Field(description="Workout name.")
    deleted: bool = Field(description="False in a dry run, or when this particular delete failed.")
    error: str | None = Field(default=None, description="Failure reason, when the delete failed.")


class BulkDeletionResult(BaseModel):
    """Result of a filtered bulk delete, in either dry-run or committed form."""

    dry_run: bool = Field(
        description=(
            "True when nothing was deleted and this is a preview. Re-call with "
            "confirm=true to apply."
        )
    )
    filter_description: str = Field(description="Human-readable description of the filter applied.")
    matched: int = Field(description="Workouts matching the filter.")
    deleted: int = Field(description="Workouts actually deleted (0 in a dry run).")
    failed: int = Field(description="Deletions that were attempted and failed.")
    library_size: int = Field(description="Total workouts inspected in the library.")
    candidates: list[DeletionCandidate] = Field(description="Every matched workout, with its outcome.")


ActivityDetail.model_rebuild()
