"""Typed input and output models for every tool."""

from __future__ import annotations

import datetime as dt
import json
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    Tag,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from garmin_coach_mcp.formatting import parse_pace

PaceStr = Annotated[
    str,
    Field(
        pattern=r"^\s*\d{1,2}:[0-5]\d\s*(/\s*km)?\s*$",
        description="Pace per km as 'M:SS', e.g. '5:30'.",
        examples=["5:30"],
    ),
]
HeartRate = Annotated[int, Field(ge=30, le=230, description="Heart rate in bpm.")]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ----------------------------------------------------------------------------- inputs


class WorkoutStep(_Strict):
    """A single executable step."""

    type: Literal["warmup", "run", "recover", "cooldown", "rest"] = Field(
        default="run", description="Step kind."
    )
    duration_type: Literal["time", "distance", "open"] = Field(
        default="time",
        description="How the step ends: after a time, a distance, or on lap-button press.",
    )
    duration_value: int | None = Field(
        default=None,
        ge=1,
        le=360_000,
        description="Seconds when duration_type='time', meters when 'distance'. Omit for 'open'.",
    )
    pace_low: PaceStr | None = Field(default=None, description="Slower end of pace range.")
    pace_high: PaceStr | None = Field(default=None, description="Faster end of pace range.")
    hr_low: HeartRate | None = Field(default=None, description="Lower heart-rate bound.")
    hr_high: HeartRate | None = Field(default=None, description="Upper heart-rate bound.")
    primary: Literal["pace", "hr", "none"] = Field(
        default="hr",
        description="Which target the watch shows as primary when both are given.",
    )

    @model_validator(mode="after")
    def _check(self) -> WorkoutStep:
        if self.duration_type != "open" and self.duration_value is None:
            raise ValueError("duration_value is required unless duration_type is 'open'")
        if (self.pace_low is None) != (self.pace_high is None):
            raise ValueError("give both pace_low and pace_high, or neither")
        if (
            self.pace_low
            and self.pace_high
            and parse_pace(self.pace_low) < parse_pace(self.pace_high)
        ):
            raise ValueError("pace_low must be the slower pace (e.g. low='6:00', high='5:30')")
        if (self.hr_low is None) != (self.hr_high is None):
            raise ValueError("give both hr_low and hr_high, or neither")
        if self.hr_low is not None and self.hr_high is not None and self.hr_low > self.hr_high:
            raise ValueError("hr_low must be <= hr_high")
        return self


class RepeatStep(_Strict):
    """A block of steps repeated `repeat_count` times."""

    type: Literal["repeat"] = "repeat"
    repeat_count: int = Field(ge=1, le=99, description="Number of repetitions.")
    steps: list[WorkoutStep] = Field(min_length=1, max_length=20)


def _step_tag(value: Any) -> str:
    kind = value.get("type") if isinstance(value, dict) else getattr(value, "type", None)
    return "repeat" if kind == "repeat" else "step"


Step = Annotated[
    Annotated[WorkoutStep, Tag("step")] | Annotated[RepeatStep, Tag("repeat")],
    Discriminator(_step_tag),
]
_STEPS = TypeAdapter(list[Step])


def parse_steps_json(
    raw: str | list[Any], field_name: str = "steps_json"
) -> list[WorkoutStep | RepeatStep]:
    """Validate a legacy JSON-encoded step list (deprecated input form).

    MCP clients/SDKs may already have decoded the JSON string into a list.
    """
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
        return list(_STEPS.validate_python(data))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ValueError(f"{field_name} is not a valid step list: {exc}") from exc


class PlanWorkout(_Strict):
    """One workout of a multi-day plan."""

    day_offset: int = Field(ge=0, le=730, description="Days after start_date (0 = start_date).")
    name: str = Field(min_length=1, max_length=80)
    estimated_duration_minutes: int = Field(default=60, ge=1, le=1440)
    steps: list[Step] = Field(min_length=1, max_length=50)


_PLAN = TypeAdapter(list[PlanWorkout])


def parse_workouts_json(raw: str | list[Any]) -> list[PlanWorkout]:
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
        return list(_PLAN.validate_python(data))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ValueError(f"workouts_json is not a valid plan: {exc}") from exc


# ---------------------------------------------------------------------------- outputs


class ActivitySummary(BaseModel):
    activity_id: int | None
    date: str | None = Field(description="Local start date YYYY-MM-DD.")
    name: str | None = None
    type: str
    distance_km: float
    duration_min: float
    pace: str | None = Field(description="Average pace per km, if distance > 0.")
    avg_hr: float | None = None
    max_hr: float | None = None
    calories: float | None = None


class ActivityList(BaseModel):
    count: int
    activities: list[ActivitySummary]
    text: str = Field(description="Human-readable rendering.")


class ActivityDetails(BaseModel):
    activity_id: int
    summary: ActivitySummary | None = None
    raw: dict[str, Any] = Field(description="Full payload as returned by Garmin Connect.")


class HealthSummary(BaseModel):
    date: dt.date
    steps: int | None = None
    resting_hr: int | None = None
    resting_hr_7d_avg: int | None = None
    stress_avg: int | None = None
    body_battery_high: int | None = None
    body_battery_low: int | None = None
    spo2_avg: float | None = None
    sleep_hours: float | None = None
    sleep_score: int | None = None
    hrv_last_night_avg: float | None = None
    hrv_weekly_avg: float | None = None
    hrv_status: str | None = None
    training_readiness: int | None = None
    unavailable: dict[str, str] = Field(
        default_factory=dict,
        description="Metric group -> reason it could not be read. Never silently dropped.",
    )
    text: str = ""


class TrainingVolume(BaseModel):
    days: int
    start_date: dt.date
    end_date: dt.date
    total_activities: int
    running_sessions: int
    total_distance_km: float
    total_duration: str
    avg_pace: str | None
    recent_runs: list[ActivitySummary]
    text: str = ""


class WorkoutInfo(BaseModel):
    workout_id: int | None
    name: str
    sport: str | None = None


class WorkoutList(BaseModel):
    count: int
    workouts: list[WorkoutInfo]
    text: str = ""


class PushWorkoutResult(BaseModel):
    workout_id: int | None
    name: str
    scheduled_date: dt.date | None = None
    scheduled: bool = False
    schedule_error: str | None = None
    text: str = ""


class PlanEntryResult(BaseModel):
    day_offset: int
    date: dt.date
    name: str
    workout_id: int | None = None
    ok: bool
    error: str | None = None


class PushPlanResult(BaseModel):
    plan_name: str
    total: int
    succeeded: int
    failed: int
    entries: list[PlanEntryResult]
    text: str = ""


class DeleteResult(BaseModel):
    deleted_ids: list[int]
    failed: dict[str, str] = Field(default_factory=dict, description="workout id -> error.")
    text: str = ""
