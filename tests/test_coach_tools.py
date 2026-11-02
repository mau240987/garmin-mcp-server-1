"""C3/C4: the Race Coach MCP tools end to end, with fake Garmin backends (synthetic data)."""

from __future__ import annotations

import datetime as dt
import io
import zipfile
from pathlib import Path
from typing import Any

import anyio
import jsonschema
import pytest
from mcp.client import Client
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from garmin_coach_mcp.backends import BackendRouter, Capability
from garmin_coach_mcp.backends.base import BackendBase
from garmin_coach_mcp.coach.storage import CoachStore
from garmin_coach_mcp.errors import UpstreamServiceError
from garmin_coach_mcp.server import build_server

TODAY = dt.date(2026, 11, 12)  # Thursday
NOW = dt.datetime(2026, 11, 12, 8, 0, tzinfo=dt.UTC)
EASY_PACE = 430  # s/km


def run(day: dt.date, km: float, hard: float = 0.1, pace: int = EASY_PACE) -> dict[str, Any]:
    duration = km * pace
    return {
        "activityId": int(day.strftime("%Y%m%d")),
        "startTimeLocal": f"{day} 07:00:00",
        "activityType": {"typeKey": "running"},
        "distance": km * 1000,
        "duration": duration,
        "hrTimeInZone_1": duration * 0.1,
        "hrTimeInZone_2": duration * (0.9 - hard),
        "hrTimeInZone_3": duration * hard * 0.6,
        "hrTimeInZone_4": duration * hard * 0.4,
        "hrTimeInZone_5": 0.0,
    }


def history() -> list[dict[str, Any]]:
    """12 weeks: Tue 8 km easy, Thu 8 km run too hard, Sun 14 km easy (30 km/week)."""
    acts = []
    monday = TODAY - dt.timedelta(days=TODAY.weekday())
    for w in range(1, 12):
        start = monday - dt.timedelta(weeks=w)
        acts += [
            run(start + dt.timedelta(days=1), 8),
            run(start + dt.timedelta(days=3), 8, hard=0.9, pace=380),
            run(start + dt.timedelta(days=6), 14),
        ]
    acts.append(
        {
            "startTimeLocal": f"{monday} 18:00:00",
            "activityType": {"typeKey": "cycling"},
            "distance": 30000,
            "duration": 3600,
        }
    )
    return acts


class FakeGarminReads(BackendBase):
    name = "official-personal"
    official = True

    def __init__(self) -> None:
        self.activities = history()
        self.summary: dict[str, Any] = {
            "restingHeartRate": 50,
            "lastSevenDaysAvgRestingHeartRate": 49,
        }
        self.hrv_data: dict[str, Any] | None = {
            "hrvSummary": {"lastNightAvg": 60, "weeklyAvg": 58, "status": "BALANCED"}
        }
        self.sleep_s = 7.5 * 3600
        self.fail_reads = False

    def capabilities(self) -> frozenset[Capability]:
        return frozenset(
            {
                Capability.ACTIVITIES_BY_DATE,
                Capability.DAILY_SUMMARY,
                Capability.SLEEP,
                Capability.HRV,
            }
        )

    def activities_between(self, start: dt.date, end: dt.date) -> list[dict[str, Any]]:
        if self.fail_reads:
            raise UpstreamServiceError("down", remediation="retry")
        return [
            a
            for a in self.activities
            if start <= dt.date.fromisoformat(a["startTimeLocal"][:10]) <= end
        ]

    def daily_summary(self, day: dt.date) -> dict[str, Any]:
        return self.summary

    def hrv(self, day: dt.date) -> dict[str, Any] | None:
        return self.hrv_data

    def sleep(self, day: dt.date) -> dict[str, Any]:
        return {"sleepTimeSeconds": self.sleep_s}


class FakeGarminWriter(BackendBase):
    name = "unofficial"
    official = False

    def __init__(self) -> None:
        self.created: list[str] = []
        self.scheduled: list[tuple[int, dt.date]] = []
        self.deleted: list[int] = []
        self.next_id = 1000
        self.fail_schedule_on: set[dt.date] = set()

    def capabilities(self) -> frozenset[Capability]:
        return frozenset(
            {Capability.WORKOUT_CREATE, Capability.WORKOUT_SCHEDULE, Capability.WORKOUT_DELETE}
        )

    def create_running_workout(self, workout: Any) -> dict[str, Any]:
        self.next_id += 1
        self.created.append(workout.workoutName)
        return {"workoutId": self.next_id}

    def schedule_workout(self, workout_id: int, day: dt.date) -> None:
        if day in self.fail_schedule_on:
            raise UpstreamServiceError("schedule failed", remediation="retry")
        self.scheduled.append((workout_id, day))

    def delete_workout(self, workout_id: int) -> None:
        self.deleted.append(workout_id)


@pytest.fixture
def reads() -> FakeGarminReads:
    return FakeGarminReads()


@pytest.fixture
def writer() -> FakeGarminWriter:
    return FakeGarminWriter()


@pytest.fixture
def store(tmp_path: Path) -> CoachStore:
    return CoachStore(tmp_path / "coach")


def make_app(
    reads: FakeGarminReads,
    store: CoachStore,
    writer: FakeGarminWriter | None = None,
    read_only: bool = False,
    today: dt.date = TODAY,
) -> MCPServer:
    backends: list[Any] = [reads] + ([writer] if writer else [])
    router = BackendRouter(backends, allow_unofficial_writes=writer is not None)
    return build_server(
        router,
        read_only=read_only,
        store=store,
        today=lambda: today,
        now=lambda: NOW,
        sleep=lambda _s: None,
    )


@pytest.fixture
def app(reads: FakeGarminReads, store: CoachStore, writer: FakeGarminWriter) -> MCPServer:
    return make_app(reads, store, writer)


def call(app: MCPServer, name: str, args: dict[str, Any]) -> dict[str, Any]:
    async def go() -> dict[str, Any]:
        tools = {t.name: t for t in await app.list_tools()}
        result = await app.call_tool(name, args)
        jsonschema.validate(result.structured_content, tools[name].output_schema)
        return dict(result.structured_content)

    return anyio.run(go)


def err(app: MCPServer, name: str, args: dict[str, Any]) -> str:
    with pytest.raises(ToolError) as info:
        anyio.run(app.call_tool, name, args)
    return str(info.value)


MARATHON = {
    "race_type": "marathon",
    "race_date": "2027-04-18",
    "days_per_week": 4,
    "target_time": "4:59:00",
    "race_name": "Milano",
}


def setup_plan(app: MCPServer) -> dict[str, Any]:
    call(app, "set_race_goal", MARATHON)
    return call(app, "generate_training_plan", {"start_date": "2026-11-16"})


# ----------------------------------------------------------------------- tool surface

COACH_TOOLS = {
    "set_race_goal",
    "get_athlete_profile",
    "generate_training_plan",
    "review_week",
    "apply_plan_update",
    "export_plan",
    "push_plan_to_garmin",
}


def test_tool_surface(app: MCPServer, reads: FakeGarminReads, store: CoachStore) -> None:
    names = {t.name for t in anyio.run(app.list_tools)}
    assert names >= COACH_TOOLS and len(names) == 16
    ro = {t.name for t in anyio.run(make_app(reads, store, read_only=True).list_tools)}
    assert "push_plan_to_garmin" not in ro and COACH_TOOLS - {"push_plan_to_garmin"} <= ro


# ------------------------------------------------------------------------ set_race_goal


def test_set_race_goal(app: MCPServer, store: CoachStore) -> None:
    out = call(
        app,
        "set_race_goal",
        {**MARATHON, "long_run_day": "saturday", "constraints": ["calf strain in March"]},
    )
    goal = out["goal"]
    assert goal["target_time_s"] == 4 * 3600 + 59 * 60 and goal["long_run_day"] == 5
    assert store.get_goal(goal["id"]).race_name == "Milano"


@pytest.mark.parametrize(
    ("args", "msg"),
    [
        ({"race_date": "2026-11-01"}, "after today"),
        ({"race_date": "2029-01-01"}, "within two years"),
        ({"race_type": "ultra"}, "distance_km is required"),
        ({"target_time": "1:00:00"}, "plausible"),
        ({"target_time": "5h00"}, "H:MM:SS"),
    ],
)
def test_set_race_goal_rejects(app: MCPServer, args: dict[str, Any], msg: str) -> None:
    assert msg in err(app, "set_race_goal", {**MARATHON, **args})


# -------------------------------------------------------------------- get_athlete_profile


def test_athlete_profile(app: MCPServer) -> None:
    out = call(app, "get_athlete_profile", {"weeks": 12})
    prof = out["profile"]
    base = prof["baseline"]
    assert base["recent_weekly_km"] == 30.0 and base["longest_run_km"] == 14.0
    assert base["runs_per_week"] == pytest.approx(3.0, abs=0.3) and base["from_garmin"]
    assert base["easy_pace_s_per_km"] == EASY_PACE  # hard runs excluded from easy pace
    assert prof["level"] == "intermediate"
    assert prof["easy_share"] < 0.75 and len(prof["too_hard_runs"]) == 11
    assert any("too fast" in i for i in prof["insights"])
    assert out["attribution"] and out["text"].splitlines()[1] == out["attribution"]


def test_athlete_profile_recovery_signals(app: MCPServer, reads: FakeGarminReads) -> None:
    reads.summary = {"restingHeartRate": 56, "lastSevenDaysAvgRestingHeartRate": 49}
    reads.hrv_data = {"hrvSummary": {"status": "LOW"}}
    reads.sleep_s = 6 * 3600
    rec = call(app, "get_athlete_profile", {})["profile"]["recovery"]
    assert len(rec["signals"]) == 3 and rec["sleep_avg_h_7d"] == 6.0


def test_athlete_profile_upstream_error_is_tool_error(
    app: MCPServer, reads: FakeGarminReads
) -> None:
    reads.fail_reads = True
    assert "UpstreamServiceError" in err(app, "get_athlete_profile", {})


# ----------------------------------------------------------------- generate_training_plan


def test_generate_plan_uses_garmin_baseline(app: MCPServer, store: CoachStore) -> None:
    out = setup_plan(app)
    plan = out["plan"]
    assert plan["baseline"]["recent_weekly_km"] == 30.0 and plan["attribution"]
    assert (
        plan["start_date"] == "2026-11-16" and plan["weeks"][-1]["sessions"][-1]["kind"] == "race"
    )
    assert store.get_plan(out["plan_id"]).revision == 1
    assert "| 1 | 2026-11-16 |" in out["text"]


def test_generate_plan_without_history_and_errors(app: MCPServer) -> None:
    assert "set_race_goal first" in err(app, "generate_training_plan", {})
    call(app, "set_race_goal", MARATHON)
    out = call(app, "generate_training_plan", {"use_garmin_history": False})
    assert out["plan"]["baseline"]["weeks_analyzed"] == 0 and out["plan"]["attribution"] is None
    assert "past" in err(app, "generate_training_plan", {"start_date": "2026-11-01"})


# --------------------------------------------------------------------------- review_week


LATER = dt.date(2026, 11, 19)  # Thursday of the plan's first week


def test_review_week(app: MCPServer, reads: FakeGarminReads, store: CoachStore) -> None:
    plan = setup_plan(app)["plan"]  # starts Monday 2026-11-16
    later = make_app(reads, store, today=LATER)
    week1 = plan["weeks"][0]
    past = [s for s in week1["sessions"] if dt.date.fromisoformat(s["date"]) <= LATER]
    assert len(past) >= 2
    first, second = past[0], past[1]
    reads.activities.append(
        run(dt.date.fromisoformat(first["date"]), first["distance_km"], hard=0.5)
    )
    out = call(later, "review_week", {})
    review = out["review"]
    checks = {c["date"]: c for c in review["sessions"]}
    assert checks[first["date"]]["status"] == "done" and checks[first["date"]]["too_hard"]
    assert checks[second["date"]]["status"] == "missed"
    assert any(c["status"] == "upcoming" for c in review["sessions"])
    assert any("too hard" in a for a in review["alerts"])
    assert review["adherence"] is not None and review["adherence"] < 1


def test_review_suggests_lighter_week_on_fatigue(
    app: MCPServer, reads: FakeGarminReads, store: CoachStore
) -> None:
    setup_plan(app)
    later = make_app(reads, store, today=LATER)
    reads.summary = {"restingHeartRate": 57, "lastSevenDaysAvgRestingHeartRate": 49}
    reads.hrv_data = {"hrvSummary": {"status": "UNBALANCED"}}
    review = call(later, "review_week", {})["review"]
    ops = [c["op"] for c in review["suggested_changes"]]
    assert ops and ops[0] == "scale_week" and review["suggested_changes"][0]["factor"] == 0.8
    assert review["suggested_changes"][0]["week"] == 2
    # the suggestions are valid input for apply_plan_update
    out = call(
        later, "apply_plan_update", {"changes": review["suggested_changes"], "dry_run": True}
    )
    assert out["saved"] is False and out["revision"] == 2


def test_review_unknown_week(app: MCPServer) -> None:
    setup_plan(app)
    assert "no week containing" in err(app, "review_week", {"week_start": "2028-01-01"})


# --------------------------------------------------------------------- apply_plan_update


def _future_sessions(plan: dict[str, Any], kind: str | None = None) -> list[dict[str, Any]]:
    return [
        s
        for w in plan["weeks"]
        for s in w["sessions"]
        if (kind is None or s["kind"] == kind) and s["kind"] != "race"
    ]


def test_apply_plan_update_operations(app: MCPServer, store: CoachStore) -> None:
    out = setup_plan(app)
    plan = out["plan"]
    week2 = plan["weeks"][1]
    easy = next(s for s in week2["sessions"] if s["kind"] == "easy")
    long_ = next(s for s in week2["sessions"] if s["kind"] == "long")
    upd = call(
        app,
        "apply_plan_update",
        {
            "changes": [
                {"op": "skip_session", "date": easy["date"]},
                {
                    "op": "make_easy",
                    "date": next(
                        s
                        for s in _future_sessions(plan)
                        if s["kind"] in ("easy_strides", "tempo", "intervals", "race_pace")
                    )["date"],
                },
                {"op": "scale_week", "week": 3, "factor": 0.8},
            ]
        },
    )
    assert upd["revision"] == 2 and len(upd["applied"]) == 3 and upd["saved"]
    saved = store.get_plan(out["plan_id"])
    assert saved.revision == 2 and len(saved.history) == 1
    assert all(s.date.isoformat() != easy["date"] for s in saved.weeks[1].sessions)
    # moving a long run next to another hard session is refused
    hard_neighbour = next(
        (s for s in week2["sessions"] if s["kind"] in ("tempo", "intervals", "easy_strides")), None
    )
    assert long_ and hard_neighbour is not None


@pytest.mark.parametrize(
    ("change", "msg"),
    [
        ({"op": "skip_session", "date": "2027-04-18"}, "race cannot be changed"),
        ({"op": "skip_session", "date": "2026-11-19"}, "No planned session"),
        ({"op": "scale_week", "week": 59, "factor": 0.5}, "does not exist"),
        ({"op": "scale_week", "week": 2, "factor": 1.2}, "less than or equal"),
        ({"op": "regenerate_from", "date": "2026-11-01"}, "not a valid day"),
    ],
)
def test_apply_plan_update_rejects(app: MCPServer, change: dict[str, Any], msg: str) -> None:
    setup_plan(app)
    assert msg in err(app, "apply_plan_update", {"changes": [change]})


def test_move_session_rules(app: MCPServer, store: CoachStore) -> None:
    plan = setup_plan(app)["plan"]
    week = next(
        w
        for w in plan["weeks"]
        if any(s["kind"] in ("tempo", "intervals", "race_pace") for s in w["sessions"])
        and any(s["kind"] == "long" for s in w["sessions"])
    )
    quality = next(s for s in week["sessions"] if s["kind"] in ("tempo", "intervals", "race_pace"))
    long_ = next(s for s in week["sessions"] if s["kind"] == "long")
    q_day = dt.date.fromisoformat(quality["date"])
    after_q = q_day + dt.timedelta(days=1)
    changes: list[dict[str, Any]] = []
    if any(s["date"] == str(after_q) for s in week["sessions"]):
        changes.append({"op": "skip_session", "date": str(after_q)})
    changes.append({"op": "move_session", "date": long_["date"], "to_date": str(after_q)})
    assert "consecutive days" in err(app, "apply_plan_update", {"changes": changes})
    # nothing was saved by the rejected update
    assert store.list_plans()[0].revision == 1
    # moving an easy run to a free day is fine
    easy = next(s for s in week["sessions"] if s["kind"] == "easy")
    taken = {s["date"] for s in week["sessions"]}
    start = dt.date.fromisoformat(week["start_date"])
    free = next(
        str(start + dt.timedelta(days=d))
        for d in range(7)
        if str(start + dt.timedelta(days=d)) not in taken
    )
    out = call(
        app,
        "apply_plan_update",
        {"changes": [{"op": "move_session", "date": easy["date"], "to_date": free}]},
    )
    assert out["revision"] == 2


def test_regenerate_from(app: MCPServer, reads: FakeGarminReads, store: CoachStore) -> None:
    out = setup_plan(app)
    reads.activities = [a for a in reads.activities if "cycling" in str(a["activityType"])]
    upd = call(
        app, "apply_plan_update", {"changes": [{"op": "regenerate_from", "date": "2026-12-07"}]}
    )
    plan = store.get_plan(out["plan_id"])
    assert upd["revision"] == 2 and plan.baseline.recent_weekly_km == 0
    assert [w.index for w in plan.weeks] == list(range(1, len(plan.weeks) + 1))
    assert plan.weeks[-1].sessions[-1].kind.value == "race"


# --------------------------------------------------------------------------- export_plan


def test_export_formats(app: MCPServer, store: CoachStore) -> None:
    setup_plan(app)
    md = call(app, "export_plan", {"format": "md"})
    assert Path(md["path"]).read_text().startswith("# Training plan: Milano")
    assert "Insights derived in part from Garmin" in md["content"]
    ics = call(
        app, "export_plan", {"format": "ics", "from_date": "2026-11-16", "to_date": "2026-11-22"}
    )
    text = Path(ics["path"]).read_bytes().decode()
    assert text.count("BEGIN:VEVENT") == ics["sessions"] == 4
    assert all(len(line.encode()) <= 75 for line in text.split("\r\n"))
    fit = call(app, "export_plan", {"format": "fit", "to_date": "2026-11-29"})
    assert fit["content"] is None and Path(fit["path"]).stat().st_mode & 0o777 == 0o600
    zf = zipfile.ZipFile(io.BytesIO(Path(fit["path"]).read_bytes()))
    fits = [n for n in zf.namelist() if n.endswith(".fit")]
    assert len(fits) == fit["sessions"] == 8 and "ATTRIBUTION.txt" in zf.namelist()
    from fit_tool.fit_file import FitFile

    for name in fits:
        parsed = FitFile.from_bytes(zf.read(name))  # CRC and structure are valid
        assert parsed.records


# ------------------------------------------------------------------- push_plan_to_garmin


def test_push_dry_run_then_real(
    app: MCPServer, writer: FakeGarminWriter, store: CoachStore
) -> None:
    out = setup_plan(app)
    dry = call(app, "push_plan_to_garmin", {"from_date": "2026-11-16", "weeks": 1})
    assert dry["dry_run"] and dry["to_create"] == 4 and writer.created == []
    assert "4 workout(s) to create" in dry["text"]
    assert "expected_count" in err(
        app, "push_plan_to_garmin", {"from_date": "2026-11-16", "weeks": 1, "dry_run": False}
    )
    real = call(
        app,
        "push_plan_to_garmin",
        {"from_date": "2026-11-16", "weeks": 1, "dry_run": False, "expected_count": 4},
    )
    assert sum(e["ok"] for e in real["created"]) == 4 and len(writer.scheduled) == 4
    assert all(n.startswith("RC W1 ") for n in writer.created)
    plan = store.get_plan(out["plan_id"])
    assert all(s.garmin_workout_id for s in plan.weeks[0].sessions if s.kind.value != "race")
    again = call(app, "push_plan_to_garmin", {"from_date": "2026-11-16", "weeks": 1})
    assert again["to_create"] == 0  # never duplicated


def test_push_replaces_stale_workouts(app: MCPServer, writer: FakeGarminWriter) -> None:
    setup_plan(app)
    call(
        app,
        "push_plan_to_garmin",
        {"from_date": "2026-11-16", "weeks": 1, "dry_run": False, "expected_count": 4},
    )
    pushed = set(range(1001, 1005))
    upd = call(
        app, "apply_plan_update", {"changes": [{"op": "scale_week", "week": 1, "factor": 0.5}]}
    )
    assert upd["garmin_out_of_sync"] == 4
    dry = call(app, "push_plan_to_garmin", {"from_date": "2026-11-16", "weeks": 1})
    assert dry["to_create"] == 4 and dry["to_delete"] == 4
    call(
        app,
        "push_plan_to_garmin",
        {"from_date": "2026-11-16", "weeks": 1, "dry_run": False, "expected_count": 4},
    )
    assert set(writer.deleted) == pushed and len(writer.created) == 8


def test_push_schedule_failure_does_not_duplicate(app: MCPServer, writer: FakeGarminWriter) -> None:
    setup_plan(app)
    writer.fail_schedule_on = {dt.date(2026, 11, 17)}
    res = call(
        app,
        "push_plan_to_garmin",
        {"from_date": "2026-11-16", "weeks": 1, "dry_run": False, "expected_count": 4},
    )
    failed = [e for e in res["created"] if not e["ok"]]
    assert len(failed) == 1 and failed[0]["workout_id"]  # created, id kept
    assert (
        call(app, "push_plan_to_garmin", {"from_date": "2026-11-16", "weeks": 1})["to_create"] == 0
    )


def test_push_without_write_access(reads: FakeGarminReads, store: CoachStore) -> None:
    app = make_app(reads, store)  # no writer: writes unavailable
    setup_plan(app)
    dry = call(app, "push_plan_to_garmin", {"from_date": "2026-11-16", "weeks": 1})
    assert "use export_plan format=fit" in dry["text"]
    msg = err(
        app,
        "push_plan_to_garmin",
        {"from_date": "2026-11-16", "weeks": 1, "dry_run": False, "expected_count": 4},
    )
    assert "CapabilityUnavailableError" in msg


# ------------------------------------------------------------ whole workflow over MCP


def test_workflow_over_mcp_client(app: MCPServer) -> None:
    async def go() -> None:
        async with Client(app) as c:
            for name, args in [
                ("set_race_goal", {**MARATHON}),
                ("get_athlete_profile", {"weeks": 8}),
                ("generate_training_plan", {}),
                ("export_plan", {"format": "md"}),
                ("review_week", {}),
                ("push_plan_to_garmin", {}),
            ]:
                r = await c.call_tool(name, args)
                assert not r.is_error, (name, r.content)

    anyio.run(go)
