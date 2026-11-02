"""Shared fixtures: a fake Garmin client so no test ever touches the network.

Fixture payloads are synthetic: no personal data.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from garmin_coach_mcp.client import GarminSession
from garmin_coach_mcp.config import Settings

ACTIVITIES: list[dict[str, Any]] = [
    {
        "activityId": 1001,
        "activityName": "Easy run",
        "startTimeLocal": "2026-10-30 07:00:00",
        "activityType": {"typeKey": "running"},
        "distance": 10000.0,
        "duration": 3600.0,
        "averageHR": 142.0,
        "maxHR": 160.0,
        "calories": 700.0,
    },
    {
        "activityId": 1002,
        "activityName": "Trail",
        "startTimeLocal": "2026-10-28 07:00:00",
        "activityType": {"typeKey": "trail_running"},
        "distance": 5000.0,
        "duration": 1800.0,
        "averageHR": None,
    },
    {
        "activityId": 1003,
        "activityName": "Ride",
        "startTimeLocal": "2026-10-27 07:00:00",
        "activityType": {"typeKey": "cycling"},
        "distance": 30000.0,
        "duration": 3600.0,
    },
]


class FakeGarmin:
    """Records calls; behaviour can be overridden per test via `fail`."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fail: dict[str, BaseException] = {}
        self.workouts: list[dict[str, Any]] = [
            {
                "workoutId": 11,
                "workoutName": "PLAN-A W1 Easy",
                "sportType": {"sportTypeKey": "running"},
            },
            {
                "workoutId": 12,
                "workoutName": "PLAN-A W1 Long",
                "sportType": {"sportTypeKey": "running"},
            },
            {"workoutId": 13, "workoutName": "Other", "sportType": {"sportTypeKey": "running"}},
        ]
        self.next_id = 500
        self.uploaded: list[Any] = []

    def _record(self, name: str, *args: Any) -> None:
        self.calls.append((name, args))
        if name in self.fail:
            raise self.fail[name]

    def login(self, tokenstore: str | None = None) -> tuple[None, None]:
        self._record("login", tokenstore)
        return None, None

    def get_activities(self, start: int = 0, limit: int = 20) -> list[dict[str, Any]]:
        self._record("get_activities", start, limit)
        return ACTIVITIES[start : start + limit]

    def get_activities_by_date(self, start: str, end: str) -> list[dict[str, Any]]:
        self._record("get_activities_by_date", start, end)
        return ACTIVITIES

    def get_activity(self, activity_id: str) -> dict[str, Any]:
        self._record("get_activity", activity_id)
        return {
            "activityId": int(activity_id),
            "activityName": "Easy run",
            "activityTypeDTO": {"typeKey": "running"},
            "summaryDTO": {
                "startTimeLocal": "2026-10-30T07:00:00.0",
                "distance": 10000.0,
                "duration": 3000.0,
                "averageHR": 150.0,
            },
        }

    def get_user_summary(self, cdate: str) -> dict[str, Any]:
        self._record("get_user_summary", cdate)
        return {
            "totalSteps": 12345,
            "restingHeartRate": 48,
            "lastSevenDaysAvgRestingHeartRate": 49,
            "averageStressLevel": 27,
            "bodyBatteryHighestValue": 90,
            "bodyBatteryLowestValue": 20,
            "averageSpo2": 96.0,
        }

    def get_sleep_data(self, cdate: str) -> dict[str, Any]:
        self._record("get_sleep_data", cdate)
        return {
            "dailySleepDTO": {"sleepTimeSeconds": 27000, "sleepScores": {"overall": {"value": 81}}}
        }

    def get_hrv_data(self, cdate: str) -> dict[str, Any]:
        self._record("get_hrv_data", cdate)
        return {"hrvSummary": {"lastNightAvg": 55, "weeklyAvg": 58, "status": "BALANCED"}}

    def get_training_readiness(self, cdate: str) -> list[dict[str, Any]]:
        self._record("get_training_readiness", cdate)
        return [{"score": 72}]

    def get_workouts(self, start: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        self._record("get_workouts", start, limit)
        return self.workouts[start : start + limit]

    def upload_running_workout(self, workout: Any) -> dict[str, Any]:
        self._record("upload_running_workout", workout.workoutName)
        self.uploaded.append(workout)
        self.next_id += 1
        return {"workoutId": self.next_id}

    def schedule_workout(self, workout_id: int, date_str: str) -> dict[str, Any]:
        self._record("schedule_workout", workout_id, date_str)
        return {"workoutScheduleId": 1}

    def delete_workout(self, workout_id: int | str) -> None:
        self._record("delete_workout", int(workout_id))
        self.workouts = [w for w in self.workouts if w["workoutId"] != int(workout_id)]


@pytest.fixture
def fake() -> FakeGarmin:
    return FakeGarmin()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        email="athlete@example.invalid",
        password="not-a-real-password",
        token_dir=tmp_path / "tokens",
        max_retries=2,
    )


@pytest.fixture
def make_session(settings: Settings, fake: FakeGarmin) -> Callable[..., GarminSession]:
    def _make(**overrides: Any) -> GarminSession:
        s = Settings(**{**settings.__dict__, **overrides})
        return GarminSession(s, factory=lambda _s: fake)

    return _make


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("garmin_coach_mcp.retry.time.sleep", lambda _s: None)
