from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from garmin_coach_mcp.config import Settings
from garmin_coach_mcp.errors import ConfigurationError, InvalidRequestError
from garmin_coach_mcp.formatting import (
    format_distance,
    format_duration,
    format_pace,
    meters_per_second_to_pace,
    pace_from,
    pace_to_meters_per_second,
    parse_pace,
)
from garmin_coach_mcp.models import RepeatStep, WorkoutStep, parse_steps_json
from garmin_coach_mcp.workouts import build_running_workout, build_running_workout_payload

# ---------------------------------------------------------------- formatting


@pytest.mark.parametrize(("raw", "secs"), [("5:30", 330), ("5:30/km", 330), (" 10:05 ", 605)])
def test_parse_pace(raw: str, secs: int) -> None:
    assert parse_pace(raw) == secs


@pytest.mark.parametrize("raw", ["", "5", "5:60", "abc", "0:00", "5:3"])
def test_parse_pace_rejects_malformed(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_pace(raw)


def test_pace_roundtrip() -> None:
    assert meters_per_second_to_pace(pace_to_meters_per_second("4:45")) == "4:45/km"
    assert format_pace(359.6) == "6:00/km"
    assert format_duration(3725) == "1h 02m"
    assert format_duration(600) == "10m"
    assert format_distance(12345) == "12.3 km"
    assert pace_from(0, 100) is None
    assert pace_from(10000, 3000) == "5:00/km"


# -------------------------------------------------------------------- config


def test_settings_from_env(tmp_path: Path) -> None:
    s = Settings.from_env(
        {
            "GARMIN_EMAIL": "a@b.invalid",
            "GARMIN_PASSWORD": "pw-SENTINEL",
            "GARTH_TOKEN_DIR": str(tmp_path),
            "GARMIN_READ_ONLY": "true",
            "GARMIN_MAX_RETRIES": "0",
            "GARMIN_LOG_LEVEL": "debug",
        }
    )
    assert s.read_only and s.max_retries == 0 and s.log_level == "DEBUG"
    assert s.token_dir == tmp_path
    assert "SENTINEL" not in repr(s) and "a@b" not in repr(s)


def test_settings_new_token_var_wins(tmp_path: Path) -> None:
    s = Settings.from_env({"GARMIN_TOKEN_DIR": str(tmp_path / "n"), "GARTH_TOKEN_DIR": "/old"})
    assert s.token_dir == tmp_path / "n"


@pytest.mark.parametrize(
    "env",
    [{"GARMIN_READ_ONLY": "maybe"}, {"GARMIN_MAX_RETRIES": "x"}, {"GARMIN_LOG_LEVEL": "LOUD"}],
)
def test_settings_invalid(env: dict[str, str]) -> None:
    with pytest.raises(ConfigurationError) as info:
        Settings.from_env(env)
    assert info.value.remediation


# -------------------------------------------------------------------- models


def test_step_validation() -> None:
    with pytest.raises(ValidationError):
        WorkoutStep(duration_type="time")  # missing duration_value
    with pytest.raises(ValidationError):
        WorkoutStep(duration_value=60, pace_low="5:00")  # half a range
    with pytest.raises(ValidationError):
        WorkoutStep(duration_value=60, pace_low="5:00", pace_high="5:30")  # inverted
    with pytest.raises(ValidationError):
        WorkoutStep(duration_value=60, hr_low=170, hr_high=150)
    with pytest.raises(ValidationError):
        WorkoutStep(duration_value=60, bogus=1)  # type: ignore[call-arg]
    assert WorkoutStep(duration_type="open").duration_value is None


def test_parse_steps_json_legacy_form() -> None:
    steps = parse_steps_json(
        '[{"type":"warmup","duration_value":600},'
        '{"type":"repeat","repeat_count":3,"steps":[{"duration_value":60}]}]'
    )
    assert isinstance(steps[0], WorkoutStep) and isinstance(steps[1], RepeatStep)
    with pytest.raises(ValueError):
        parse_steps_json("not json")
    with pytest.raises(ValueError):
        parse_steps_json('[{"type":"repeat","repeat_count":0,"steps":[]}]')


def test_errors_render_remediation() -> None:
    err = InvalidRequestError("bad", remediation="fix it")
    assert "bad" in str(err.to_tool_error()) and "fix it" in str(err.to_tool_error())


# ------------------------------------------------------------------ workouts


def _sample_steps() -> list[WorkoutStep | RepeatStep]:
    return [
        WorkoutStep(type="warmup", duration_value=600, hr_low=120, hr_high=140),
        RepeatStep(
            repeat_count=4,
            steps=[
                WorkoutStep(
                    duration_type="distance",
                    duration_value=1000,
                    pace_low="4:50",
                    pace_high="4:40",
                    hr_low=160,
                    hr_high=175,
                    primary="pace",
                ),
                WorkoutStep(type="recover", duration_value=90),
            ],
        ),
        WorkoutStep(type="cooldown", duration_type="open"),
    ]


def test_payload_structure_and_units() -> None:
    p = build_running_workout_payload("T", _sample_steps(), 3000)
    steps = p["workoutSegments"][0]["workoutSteps"]
    warm, rep, cool = steps
    # time in seconds, HR primary, values hoisted to top level
    assert warm["endCondition"]["conditionTypeKey"] == "time"
    assert warm["endConditionValue"] == 600.0
    assert warm["targetType"]["workoutTargetTypeKey"] == "heart.rate.zone"
    assert (warm["targetValueOne"], warm["targetValueTwo"]) == (120.0, 140.0)
    assert "targetValueOne" not in warm["targetType"]
    # repeat group precedes children in order
    assert rep["type"] == "RepeatGroupDTO" and rep["stepOrder"] == 2
    assert rep["numberOfIterations"] == 4
    interval, recover = rep["workoutSteps"]
    assert (interval["stepOrder"], recover["stepOrder"], cool["stepOrder"]) == (3, 4, 5)
    # distance in meters, pace primary in m/s (slower first), HR secondary
    assert interval["endConditionValue"] == 1000.0
    assert interval["targetType"]["workoutTargetTypeKey"] == "pace.zone"
    assert interval["targetValueOne"] == pytest.approx(1000 / 290)
    assert interval["targetValueTwo"] == pytest.approx(1000 / 280)
    assert interval["secondaryTargetType"]["workoutTargetTypeKey"] == "heart.rate.zone"
    assert (interval["secondaryTargetValueOne"], interval["secondaryTargetValueTwo"]) == (160, 175)
    assert recover["targetType"]["workoutTargetTypeKey"] == "no.target"
    assert cool["endCondition"]["conditionTypeKey"] == "lap.button"
    assert "endConditionValue" not in cool


def test_primary_falls_back_when_missing() -> None:
    p = build_running_workout_payload(
        "T", [WorkoutStep(duration_value=60, pace_low="6:00", pace_high="5:40")], 60
    )
    step = p["workoutSegments"][0]["workoutSteps"][0]
    assert step["targetType"]["workoutTargetTypeKey"] == "pace.zone"  # primary=hr but no HR
    assert "secondaryTargetType" not in step


def test_payload_is_valid_library_model() -> None:
    w = build_running_workout("T", _sample_steps(), 3000)
    group = w.workoutSegments[0].workoutSteps[1]
    assert type(group).__name__ == "RepeatGroup"
    assert type(group.workoutSteps[0]).__name__ == "ExecutableStep"
