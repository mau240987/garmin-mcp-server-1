"""Backend interface: one contract, interchangeable Garmin implementations.

Every backend returns payloads in the **same normalized shapes** (documented per method), so
tools never branch on which backend served them. Shapes follow the Garmin Connect DTOs, which
the official Personal API and the internal endpoints share for the covered data.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

#: Attribution attached to data served by an official backend. Placeholder wording until the
#: Garmin API Brand Guidelines PDF is reviewed (docs/api-coverage.md, risk R3).
GARMIN_ATTRIBUTION = "Data provided by Garmin Connect. Garmin is a trademark of Garmin Ltd."


class Capability(StrEnum):
    """Things a backend can do. Routing is per capability, never per tool."""

    ACTIVITIES_LIST = "activities.list"
    ACTIVITIES_BY_DATE = "activities.by_date"
    ACTIVITY_DETAILS = "activity.details"
    DAILY_SUMMARY = "health.daily_summary"
    SLEEP = "health.sleep"
    HRV = "health.hrv"
    TRAINING_READINESS = "health.training_readiness"
    WORKOUTS_LIST = "workouts.list"
    WORKOUT_CREATE = "workouts.create"
    WORKOUT_SCHEDULE = "workouts.schedule"
    WORKOUT_DELETE = "workouts.delete"


WRITE_CAPABILITIES = frozenset(
    {Capability.WORKOUT_CREATE, Capability.WORKOUT_SCHEDULE, Capability.WORKOUT_DELETE}
)


@runtime_checkable
class GarminBackend(Protocol):
    """Contract for a Garmin data source. Methods raise `GarminMCPError` subclasses only.

    A backend need not implement every method: `capabilities()` declares which ones are
    usable, and the router never calls a method whose capability is not declared.
    """

    name: str
    official: bool

    def capabilities(self) -> frozenset[Capability]: ...

    # Reads ---------------------------------------------------------------------------
    def list_activities(self, start: int, limit: int) -> list[dict[str, Any]]:
        """Newest first. Items: SummarizedActivityDTO-like (activityId, startTimeLocal,
        activityType.typeKey, distance [m], duration [s], averageHR, maxHR, calories, ...)."""
        ...

    def activities_between(self, start: dt.date, end: dt.date) -> list[dict[str, Any]]:
        """All activities whose local start date is in [start, end], same item shape."""
        ...

    def activity(self, activity_id: int) -> dict[str, Any]:
        """ActivitySummaryDTO-like: activityId, activityName, activityTypeDTO, summaryDTO, ..."""
        ...

    def daily_summary(self, day: dt.date) -> dict[str, Any]:
        """UserDailySummaryDto-like (totalSteps, restingHeartRate, bodyBattery*, ...)."""
        ...

    def sleep(self, day: dt.date) -> dict[str, Any]:
        """DailySleepDTO-like (sleepTimeSeconds, sleepScores, ...), **unwrapped**."""
        ...

    def hrv(self, day: dt.date) -> dict[str, Any] | None:
        """HrvChartDTO-like with `hrvSummary`; None when the day has no HRV data."""
        ...

    def training_readiness(self, day: dt.date) -> dict[str, Any] | None:
        """Single readiness record with `score`; None when absent."""
        ...

    def list_workouts(self, start: int, limit: int) -> list[dict[str, Any]]:
        """Workout library items: workoutId, workoutName, sportType.sportTypeKey."""
        ...

    # Writes (never retried) ----------------------------------------------------------
    def create_running_workout(self, workout: Any) -> dict[str, Any]:
        """Create a `garminconnect.workout.RunningWorkout`; returns a dict with workoutId."""
        ...

    def schedule_workout(self, workout_id: int, day: dt.date) -> None: ...

    def delete_workout(self, workout_id: int) -> None: ...


class BackendBase:
    """Default implementations: every capability a backend does not override is unavailable."""

    name = "base"
    official = False

    def capabilities(self) -> frozenset[Capability]:
        return frozenset()

    def _unsupported(self, cap: Capability) -> Any:
        from garmin_coach_mcp.errors import CapabilityUnavailableError

        raise CapabilityUnavailableError(
            f"The {self.name} backend does not provide '{cap.value}'.",
            remediation="This is a routing bug: report it with the server logs.",
        )

    def list_activities(self, start: int, limit: int) -> list[dict[str, Any]]:
        return self._unsupported(Capability.ACTIVITIES_LIST)  # type: ignore[no-any-return]

    def activities_between(self, start: dt.date, end: dt.date) -> list[dict[str, Any]]:
        return self._unsupported(Capability.ACTIVITIES_BY_DATE)  # type: ignore[no-any-return]

    def activity(self, activity_id: int) -> dict[str, Any]:
        return self._unsupported(Capability.ACTIVITY_DETAILS)  # type: ignore[no-any-return]

    def daily_summary(self, day: dt.date) -> dict[str, Any]:
        return self._unsupported(Capability.DAILY_SUMMARY)  # type: ignore[no-any-return]

    def sleep(self, day: dt.date) -> dict[str, Any]:
        return self._unsupported(Capability.SLEEP)  # type: ignore[no-any-return]

    def hrv(self, day: dt.date) -> dict[str, Any] | None:
        return self._unsupported(Capability.HRV)  # type: ignore[no-any-return]

    def training_readiness(self, day: dt.date) -> dict[str, Any] | None:
        return self._unsupported(Capability.TRAINING_READINESS)  # type: ignore[no-any-return]

    def list_workouts(self, start: int, limit: int) -> list[dict[str, Any]]:
        return self._unsupported(Capability.WORKOUTS_LIST)  # type: ignore[no-any-return]

    def create_running_workout(self, workout: Any) -> dict[str, Any]:
        return self._unsupported(Capability.WORKOUT_CREATE)  # type: ignore[no-any-return]

    def schedule_workout(self, workout_id: int, day: dt.date) -> None:
        self._unsupported(Capability.WORKOUT_SCHEDULE)

    def delete_workout(self, workout_id: int) -> None:
        self._unsupported(Capability.WORKOUT_DELETE)
