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
    RACE = "race"


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
        return data

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


class PlanWeek(_Model):
    index: int = Field(ge=1)
    start_date: dt.date
    phase: Phase
    is_recovery: bool = False
    target_km: float = Field(ge=0, description="Planned running volume of the week.")
    long_run_km: float = Field(ge=0)
    sessions: list[PlannedSession]


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
