"""Coaching data model (C1): race goal, athlete baseline, training plan."""

from __future__ import annotations

import datetime as dt
import re
import uuid
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = 1


class RaceType(StrEnum):
    K5 = "5k"
    K10 = "10k"
    HALF = "half_marathon"
    MARATHON = "marathon"
    ULTRA = "ultra"
    OTHER = "other"


#: Plausible goal paces (s/km): faster than a world record or slower than walking 100 km in
#: ~33 h is almost certainly a typo in target_time.
MIN_GOAL_PACE_S = 150
MAX_GOAL_PACE_S = 1200
#: Standard distances accept small course variations (e.g. a 10.3 km "10k").
DISTANCE_TOLERANCE = 0.15

#: Default distances; OTHER and ULTRA require an explicit distance_km.
RACE_DISTANCE_KM: dict[RaceType, float] = {
    RaceType.K5: 5.0,
    RaceType.K10: 10.0,
    RaceType.HALF: 21.0975,
    RaceType.MARATHON: 42.195,
}


class Level(StrEnum):
    BEGINNER = "beginner"
    INTERMEDIATE = "intermediate"
    ADVANCED = "advanced"


class SessionKind(StrEnum):
    EASY = "easy"
    STRIDES = "easy_strides"
    LONG = "long"
    MEDIUM_LONG = "medium_long"
    TEMPO = "tempo"
    INTERVALS = "intervals"
    RACE_PACE = "race_pace"
    FARTLEK = "fartlek"
    RACE = "race"


class LongVariant(StrEnum):
    """Structured faster part inside a long run (R12)."""

    PROGRESSIVE = "progressive"  # the last fast_km at a steady effort (Z3), no pace target
    RACE_PACE = "race_pace"  # fast_km at goal race pace, in fast_blocks blocks


class Phase(StrEnum):
    BASE = "base"
    BUILD = "build"
    SPECIFIC = "specific"
    TAPER = "taper"


Weekday = Annotated[int, Field(ge=0, le=6, description="0 = Monday ... 6 = Sunday.")]
_HMS = re.compile(r"^(?:(\d{1,2}):)?(\d{1,2}):(\d{2})$")


def parse_hms(value: str) -> int:
    """'4:59:00' or '52:30' -> seconds."""
    m = _HMS.match(value.strip())
    if not m or int(m.group(3)) >= 60 or (m.group(1) is not None and int(m.group(2)) >= 60):
        raise ValueError(f"time must look like H:MM:SS or MM:SS, got {value!r}")
    return int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + int(m.group(3))


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RaceGoal(_Model):
    """What the athlete is training for (input of set_race_goal)."""

    id: str = Field(default_factory=lambda: new_id("goal"))
    schema_version: int = SCHEMA_VERSION
    created_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.UTC))
    race_type: RaceType
    race_name: str | None = Field(default=None, max_length=120)
    distance_km: float = Field(gt=0, le=400, description="Race distance in km.")
    race_date: dt.date
    target_time_s: int | None = Field(
        default=None, gt=0, le=72 * 3600, description="Target finish time in seconds."
    )
    days_per_week: int = Field(ge=2, le=7, description="Running days available per week.")
    long_run_day: Weekday = 6
    level: Level | None = Field(
        default=None, description="Override; otherwise derived from the Garmin baseline."
    )
    constraints: list[str] = Field(
        default_factory=list, max_length=20, description="Injuries, travel, notes (free text)."
    )
    unavailable_dates: list[dt.date] = Field(default_factory=list, max_length=366)

    @model_validator(mode="before")
    @classmethod
    def _default_distance(cls, data: object) -> object:
        if isinstance(data, dict) and data.get("distance_km") is None:
            race_type = data.get("race_type")
            try:
                rt = RaceType(race_type) if race_type is not None else None
            except ValueError:
                return data
            if rt in RACE_DISTANCE_KM:
                data = {**data, "distance_km": RACE_DISTANCE_KM[rt]}
            elif rt is not None:
                raise ValueError(
                    f"distance_km is required for race_type '{rt.value}' (e.g. 100 for a 100 km)"
                )
        return data

    @model_validator(mode="after")
    def _consistent(self) -> RaceGoal:
        standard = RACE_DISTANCE_KM.get(self.race_type)
        if standard and abs(self.distance_km - standard) > standard * DISTANCE_TOLERANCE:
            raise ValueError(
                f"distance_km {self.distance_km:g} does not match race_type "
                f"'{self.race_type.value}' ({standard:g} km); use race_type 'other' or 'ultra'"
            )
        if (
            self.race_type is RaceType.ULTRA
            and self.distance_km <= RACE_DISTANCE_KM[RaceType.MARATHON]
        ):
            raise ValueError("an ultra must be longer than a marathon (42.195 km)")
        if self.target_time_s is not None:
            pace = self.target_time_s / self.distance_km
            if not MIN_GOAL_PACE_S <= pace <= MAX_GOAL_PACE_S:
                raise ValueError(
                    f"target time gives {int(pace // 60)}:{int(pace % 60):02d}/km, outside the "
                    "plausible 2:30-20:00/km: check target_time (H:MM:SS)"
                )
        return self

    @field_validator("constraints")
    @classmethod
    def _short_constraints(cls, v: list[str]) -> list[str]:
        if any(len(c) > 300 for c in v):
            raise ValueError("each constraint must be at most 300 characters")
        return v

    @property
    def is_finish_goal(self) -> bool:
        return self.target_time_s is None

    @property
    def race_pace_s_per_km(self) -> float | None:
        return self.target_time_s / self.distance_km if self.target_time_s else None


class AthleteBaseline(_Model):
    """Deterministic summary of recent training, computed from Garmin data (C3) or given."""

    weeks_analyzed: int = Field(default=0, ge=0, le=104)
    recent_weekly_km: float = Field(default=0.0, ge=0, description="Average of last 4 weeks.")
    max_weekly_km: float = Field(default=0.0, ge=0)
    longest_run_km: float = Field(default=0.0, ge=0)
    runs_per_week: float = Field(default=0.0, ge=0)
    easy_pace_s_per_km: float | None = Field(default=None, gt=0, le=1200)
    from_garmin: bool = Field(default=False, description="True when derived from Garmin data.")

    @property
    def is_empty(self) -> bool:
        return self.weeks_analyzed == 0 or self.recent_weekly_km <= 0


class PlannedSession(_Model):
    date: dt.date
    kind: SessionKind
    distance_km: float = Field(ge=0)
    zone: str = Field(description="Target intensity, e.g. 'Z2', 'Z3-Z4'.")
    description: str
    pace_hint: str | None = None
    est_duration_min: int | None = None
    long_variant: LongVariant | None = Field(
        default=None, description="Long runs only: structured faster part (see fast_km)."
    )
    fast_km: float | None = Field(
        default=None, ge=0, description="Km of the long run at the faster effort."
    )
    fast_blocks: int | None = Field(
        default=None, ge=1, le=3, description="Race-pace blocks (1 = at the end), 1 km easy apart."
    )
    garmin_workout_id: int | None = Field(
        default=None, description="Set once the session was pushed to Garmin Connect."
    )
    garmin_scheduled: bool = Field(
        default=False, description="The pushed workout is on the Garmin calendar."
    )
    garmin_push_pending: bool = Field(
        default=False,
        description="A create was started but not confirmed: check Garmin before retrying.",
    )


class PlanWeek(_Model):
    index: int = Field(ge=1)
    start_date: dt.date
    phase: Phase
    is_recovery: bool = False
    target_km: float = Field(ge=0, description="Planned running volume of the week.")
    long_run_km: float = Field(ge=0)
    sessions: list[PlannedSession]


class PlanRevision(_Model):
    revision: int
    at: dt.datetime
    summary: str


class TrainingPlan(_Model):
    id: str = Field(default_factory=lambda: new_id("plan"))
    schema_version: int = SCHEMA_VERSION
    goal_id: str
    created_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.UTC))
    start_date: dt.date
    race_date: dt.date
    level: Level
    baseline: AthleteBaseline
    peak_weekly_km: float
    weeks: list[PlanWeek]
    warnings: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    attribution: str | None = None
    engine_version: str
    revision: int = 1
    history: list[PlanRevision] = Field(default_factory=list)
    stale_garmin_workout_ids: list[int] = Field(
        default_factory=list,
        description="Garmin workouts made obsolete by plan updates; removed on the next push.",
    )
    superseded_by: str | None = Field(
        default=None, description="Id of the plan that replaced this one for the same goal."
    )
    cancelled_dates: list[dt.date] = Field(
        default_factory=list,
        description="Dates whose session was removed or moved (cancelled in calendar exports).",
    )

    @property
    def easy_pace(self) -> float | None:
        return self.baseline.easy_pace_s_per_km

    def week_of(self, day: dt.date) -> PlanWeek | None:
        return next(
            (w for w in self.weeks if w.start_date <= day < w.start_date + dt.timedelta(days=7)),
            None,
        )
