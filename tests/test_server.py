"""Tool-level and protocol-level tests (non-regression for the 9 original tools)."""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable
from typing import Any

import anyio
import jsonschema
import pytest
from garminconnect import GarminConnectConnectionError
from mcp.client import Client
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from garmin_coach_mcp.client import GarminSession
from garmin_coach_mcp.server import build_server
from tests.conftest import FakeGarmin

ORIGINAL_TOOLS = {
    "get_activities",
    "get_activity_details",
    "get_health_summary",
    "get_training_status",
    "get_workouts",
    "push_workout",
    "push_training_plan",
    "delete_workout",
    "delete_plan_workouts",
}
WRITE_TOOLS = {"push_workout", "push_training_plan", "delete_workout", "delete_plan_workouts"}

STEPS = [
    {"type": "warmup", "duration_value": 600, "hr_low": 120, "hr_high": 140},
    {
        "type": "repeat",
        "repeat_count": 3,
        "steps": [
            {"duration_value": 180, "pace_low": "4:50", "pace_high": "4:40", "primary": "pace"},
            {"type": "recover", "duration_value": 90},
        ],
    },
    {"type": "cooldown", "duration_type": "open"},
]


@pytest.fixture
def app(make_session: Callable[..., GarminSession]) -> MCPServer:
    return build_server(make_session(), sleep=lambda _s: None)


def call(app: MCPServer, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Call a tool and validate its structured output against the advertised schema."""

    async def go() -> dict[str, Any]:
        tools = {t.name: t for t in await app.list_tools()}
        result = await app.call_tool(name, args)
        assert result.structured_content is not None
        jsonschema.validate(result.structured_content, tools[name].output_schema)
        return dict(result.structured_content)

    return anyio.run(go)


def tool_list(app: MCPServer) -> list[Any]:
    return anyio.run(app.list_tools)


# ---------------------------------------------------------------- surface


def test_all_original_tools_registered(app: MCPServer) -> None:
    tools = tool_list(app)
    assert {t.name for t in tools} == ORIGINAL_TOOLS
    for t in tools:
        assert t.title and t.description and t.annotations and t.output_schema
        assert t.annotations.read_only_hint is (t.name not in WRITE_TOOLS)
    destructive = {t.name for t in tools if t.annotations.destructive_hint}
    assert destructive == {"delete_workout", "delete_plan_workouts"}


def test_read_only_mode_hides_writes(make_session: Callable[..., GarminSession]) -> None:
    app = build_server(make_session(read_only=True))
    assert {t.name for t in tool_list(app)} == ORIGINAL_TOOLS - WRITE_TOOLS


def test_no_json_string_params_except_deprecated(app: MCPServer) -> None:
    for t in tool_list(app):
        steps = t.input_schema["properties"].get("steps")
        if steps:
            assert "array" in json.dumps(steps)


# ------------------------------------------------------------------- reads


def test_get_activities(app: MCPServer, fake: FakeGarmin) -> None:
    out = call(app, "get_activities", {"limit": 10})
    assert out["count"] == 3
    first = out["activities"][0]
    assert first["pace"] == "6:00/km" and first["distance_km"] == 10.0
    assert fake.calls[-1] == ("get_activities", (0, 10))


def test_get_activities_type_filter_substring(app: MCPServer) -> None:
    out = call(app, "get_activities", {"activity_type": "running"})
    assert [a["activity_id"] for a in out["activities"]] == [1001, 1002]


def test_get_activities_rejects_bad_limit(app: MCPServer) -> None:
    with pytest.raises(ToolError):
        anyio.run(app.call_tool, "get_activities", {"limit": 0})


def test_get_activity_details(app: MCPServer) -> None:
    out = call(app, "get_activity_details", {"activity_id": "1001"})
    assert out["summary"]["pace"] == "5:00/km"
    assert out["summary"]["type"] == "running"
    assert out["raw"]["activityId"] == 1001


def test_get_activity_details_rejects_non_numeric(app: MCPServer) -> None:
    with pytest.raises(ToolError):
        anyio.run(app.call_tool, "get_activity_details", {"activity_id": "../x"})


def test_get_health_summary(app: MCPServer) -> None:
    out = call(app, "get_health_summary", {"date_str": "2026-10-30"})
    assert out["steps"] == 12345 and out["resting_hr"] == 48
    assert out["body_battery_high"] == 90 and out["body_battery_low"] == 20
    assert out["sleep_hours"] == 7.5 and out["sleep_score"] == 81
    assert out["hrv_last_night_avg"] == 55 and out["training_readiness"] == 72
    assert out["unavailable"] == {}


def test_get_health_summary_reports_partial_failure(app: MCPServer, fake: FakeGarmin) -> None:
    fake.fail["get_training_readiness"] = GarminConnectConnectionError("client error (403)")
    out = call(app, "get_health_summary", {"date_str": "2026-10-30"})
    assert "training_readiness" in out["unavailable"]
    assert out["steps"] == 12345


def test_get_health_summary_total_failure_is_error(app: MCPServer, fake: FakeGarmin) -> None:
    for m in ("get_user_summary", "get_sleep_data", "get_hrv_data", "get_training_readiness"):
        fake.fail[m] = GarminConnectConnectionError("503")
    with pytest.raises(ToolError):
        anyio.run(app.call_tool, "get_health_summary", {"date_str": "2026-10-30"})


def test_get_health_summary_rejects_bad_date(app: MCPServer) -> None:
    with pytest.raises(ToolError):
        anyio.run(app.call_tool, "get_health_summary", {"date_str": "30/10/2026"})


def test_get_training_status(app: MCPServer) -> None:
    out = call(app, "get_training_status", {"days": 7})
    assert out["total_activities"] == 3 and out["running_sessions"] == 2
    assert out["total_distance_km"] == 15.0 and out["avg_pace"] == "6:00/km"
    assert dt.date.fromisoformat(out["end_date"]) - dt.date.fromisoformat(out["start_date"]) == (
        dt.timedelta(days=7)
    )


def test_get_workouts(app: MCPServer) -> None:
    out = call(app, "get_workouts", {})
    assert out["count"] == 3 and out["workouts"][0]["workout_id"] == 11


def test_upstream_error_is_tool_error_not_string(app: MCPServer, fake: FakeGarmin) -> None:
    fake.fail["get_workouts"] = GarminConnectConnectionError("503")
    with pytest.raises(ToolError) as info:
        anyio.run(app.call_tool, "get_workouts", {})
    assert "UpstreamServiceError" in str(info.value)


# ------------------------------------------------------------------ writes


def test_push_workout_creates_and_schedules(app: MCPServer, fake: FakeGarmin) -> None:
    out = call(
        app, "push_workout", {"name": "Intervals", "steps": STEPS, "schedule_date": "2026-11-05"}
    )
    assert out["workout_id"] == 501 and out["scheduled"] is True
    assert ("schedule_workout", (501, "2026-11-05")) in fake.calls


def test_push_workout_accepts_deprecated_steps_json(app: MCPServer, fake: FakeGarmin) -> None:
    out = call(app, "push_workout", {"name": "Legacy", "steps_json": json.dumps(STEPS)})
    assert out["workout_id"] == 501 and out["scheduled"] is False
    assert not any(c[0] == "schedule_workout" for c in fake.calls)


def test_push_workout_requires_steps(app: MCPServer) -> None:
    with pytest.raises(ToolError):
        anyio.run(app.call_tool, "push_workout", {"name": "x"})
    with pytest.raises(ToolError):
        anyio.run(app.call_tool, "push_workout", {"name": "x", "steps_json": "{bad"})


def test_push_workout_not_retried(app: MCPServer, fake: FakeGarmin) -> None:
    fake.fail["upload_running_workout"] = GarminConnectConnectionError("503")
    with pytest.raises(ToolError):
        anyio.run(app.call_tool, "push_workout", {"name": "x", "steps": STEPS})
    assert [c[0] for c in fake.calls].count("upload_running_workout") == 1


def test_push_workout_schedule_failure_is_partial(app: MCPServer, fake: FakeGarmin) -> None:
    fake.fail["schedule_workout"] = GarminConnectConnectionError("503")
    out = call(app, "push_workout", {"name": "x", "steps": STEPS, "schedule_date": "2026-11-05"})
    assert out["workout_id"] == 501 and out["scheduled"] is False
    assert out["schedule_error"]
    assert "NOT scheduled" in out["text"]


def test_push_training_plan(app: MCPServer, fake: FakeGarmin) -> None:
    plan = [
        {"day_offset": 0, "name": "W1 Easy", "steps": STEPS[:1]},
        {"day_offset": 2, "name": "W1 Intervals", "steps": STEPS},
    ]
    out = call(
        app, "push_training_plan", {"name": "P", "start_date": "2026-11-02", "workouts": plan}
    )
    assert out["succeeded"] == 2 and out["failed"] == 0
    dates = [c[1][1] for c in fake.calls if c[0] == "schedule_workout"]
    assert dates == ["2026-11-02", "2026-11-04"]


def test_push_training_plan_reports_per_entry_failure(app: MCPServer, fake: FakeGarmin) -> None:
    fake.fail["upload_running_workout"] = GarminConnectConnectionError("503")
    plan = [{"day_offset": 0, "name": "A", "steps": STEPS[:1]}]
    out = call(
        app,
        "push_training_plan",
        {"name": "P", "start_date": "2026-11-02", "workouts_json": json.dumps(plan)},
    )
    assert out["succeeded"] == 0 and out["entries"][0]["error"]
    assert [c[0] for c in fake.calls].count("upload_running_workout") == 1


def test_delete_workout(app: MCPServer, fake: FakeGarmin) -> None:
    out = call(app, "delete_workout", {"workout_id": "13"})
    assert out["deleted_ids"] == [13]


@pytest.mark.parametrize("pattern", ["", "  ", "ab", "   a  "])
def test_delete_plan_workouts_refuses_broad_pattern(
    app: MCPServer, fake: FakeGarmin, pattern: str
) -> None:
    with pytest.raises(ToolError):
        anyio.run(app.call_tool, "delete_plan_workouts", {"name_contains": pattern})
    assert not any(c[0] == "delete_workout" for c in fake.calls)


def test_delete_plan_workouts_dry_run_then_delete(app: MCPServer, fake: FakeGarmin) -> None:
    dry = call(app, "delete_plan_workouts", {"name_contains": "plan-a", "dry_run": True})
    assert dry["deleted_ids"] == [] and "2 workouts match" in dry["text"]
    assert not any(c[0] == "delete_workout" for c in fake.calls)
    out = call(app, "delete_plan_workouts", {"name_contains": "plan-a"})
    assert out["deleted_ids"] == [11, 12]
    assert [w["workoutId"] for w in fake.workouts] == [13]


# ---------------------------------------------------------- protocol level


def test_end_to_end_over_mcp_client(app: MCPServer) -> None:
    async def go() -> None:
        async with Client(app) as client:
            tools = await client.list_tools()
            assert {t.name for t in tools.tools} == ORIGINAL_TOOLS
            ok = await client.call_tool("get_workouts", {"limit": 2})
            assert not ok.is_error and ok.structured_content["count"] == 2
            bad = await client.call_tool("delete_plan_workouts", {"name_contains": ""})
            assert bad.is_error

    anyio.run(go)


def test_legacy_entrypoint_importable() -> None:
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "garmin_mcp_server.py"
    spec = importlib.util.spec_from_file_location("legacy_entry", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert callable(module.main)
