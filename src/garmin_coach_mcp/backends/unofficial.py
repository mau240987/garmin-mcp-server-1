"""Backend over the unofficial `garminconnect` library (Garmin SSO login, internal endpoints).

Today it is the only way to write workouts. See docs/api-coverage.md §5 (risk R4).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from garmin_coach_mcp.backends.base import BackendBase, Capability
from garmin_coach_mcp.client import GarminSession


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[dict[str, Any]]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


class UnofficialBackend(BackendBase):
    name = "unofficial"
    official = False

    _CAPABILITIES = frozenset(Capability)

    def __init__(self, session: GarminSession) -> None:
        self.session = session

    def capabilities(self) -> frozenset[Capability]:
        return self._CAPABILITIES

    # Reads (idempotent: retried by the session on transient errors)
    def list_activities(self, start: int, limit: int) -> list[dict[str, Any]]:
        return _as_list(
            self.session.call("get_activities", lambda c: c.get_activities(start, limit))
        )

    def activities_between(self, start: dt.date, end: dt.date) -> list[dict[str, Any]]:
        return _as_list(
            self.session.call(
                "get_activities_by_date",
                lambda c: c.get_activities_by_date(start.isoformat(), end.isoformat()),
            )
        )

    def activity(self, activity_id: int) -> dict[str, Any]:
        return _as_dict(
            self.session.call("get_activity", lambda c: c.get_activity(str(activity_id)))
        )

    def daily_summary(self, day: dt.date) -> dict[str, Any]:
        return _as_dict(
            self.session.call("get_user_summary", lambda c: c.get_user_summary(day.isoformat()))
        )

    def sleep(self, day: dt.date) -> dict[str, Any]:
        raw = self.session.call("get_sleep_data", lambda c: c.get_sleep_data(day.isoformat()))
        return _as_dict(_as_dict(raw).get("dailySleepDTO"))

    def hrv(self, day: dt.date) -> dict[str, Any] | None:
        raw = self.session.call("get_hrv_data", lambda c: c.get_hrv_data(day.isoformat()))
        return raw if isinstance(raw, dict) else None

    def training_readiness(self, day: dt.date) -> dict[str, Any] | None:
        raw = self.session.call(
            "get_training_readiness", lambda c: c.get_training_readiness(day.isoformat())
        )
        if isinstance(raw, list):
            raw = raw[0] if raw else None
        return raw if isinstance(raw, dict) else None

    def list_workouts(self, start: int, limit: int) -> list[dict[str, Any]]:
        return _as_list(self.session.call("get_workouts", lambda c: c.get_workouts(start, limit)))

    # Writes: run exactly once
    def create_running_workout(self, workout: Any) -> dict[str, Any]:
        return _as_dict(
            self.session.call(
                "create_workout", lambda c: c.upload_running_workout(workout), idempotent=False
            )
        )

    def schedule_workout(self, workout_id: int, day: dt.date) -> None:
        self.session.call(
            "schedule_workout",
            lambda c: c.schedule_workout(workout_id, day.isoformat()),
            idempotent=False,
        )

    def delete_workout(self, workout_id: int) -> None:
        self.session.call(
            "delete_workout", lambda c: c.delete_workout(workout_id), idempotent=False
        )
