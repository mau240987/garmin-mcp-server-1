"""garmin-coach-weekly: review last week, adapt the plan, prepare (or push) the coming week."""

from __future__ import annotations

import datetime as dt
import functools
from pathlib import Path

import pytest

from garmin_coach_mcp import push_cli, weekly_cli
from garmin_coach_mcp.backends import BackendRouter
from garmin_coach_mcp.coach.storage import CoachStore
from garmin_coach_mcp.server import build_server
from tests.test_coach_tools import (
    MARATHON,
    FakeGarminReads,
    FakeGarminWriter,
    call,
    make_app,
    setup_plan,
)

MONDAY = "2026-11-23"  # plan starts 2026-11-16: last week is plan week 1


@pytest.fixture
def env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, FakeGarminReads, FakeGarminWriter]:
    data = tmp_path / "coach"
    reads, writer = FakeGarminReads(), FakeGarminWriter()
    setup_plan(make_app(reads, CoachStore(data)))
    monkeypatch.setattr(
        weekly_cli,
        "build_router",
        lambda s: BackendRouter([reads, writer], allow_unofficial_writes=s.allow_unofficial_writes),
    )
    monkeypatch.setattr(weekly_cli, "build_server", functools.partial(build_server))
    return data, reads, writer


def _plan(data: Path) -> object:
    store = CoachStore(data)
    return store.list_plans()[-1]


def test_reviews_adapts_and_prepares_without_writing(
    env: tuple[Path, FakeGarminReads, FakeGarminWriter], tmp_path: Path
) -> None:
    data, reads, writer = env
    _week1_runs(reads)
    # Several recovery signals: the review proposes a lighter, easy next week.
    reads.summary = {"restingHeartRate": 58, "lastSevenDaysAvgRestingHeartRate": 49}
    reads.hrv_data = {"hrvSummary": {"status": "LOW"}}
    reads.sleep_s = 5 * 3600
    before = CoachStore(data).list_plans()[-1]
    week2_km = before.weeks[1].target_km
    summary = tmp_path / "w.md"
    rc = weekly_cli.main(["--data-dir", str(data), "--today", MONDAY, "--summary", str(summary)])
    text = summary.read_text()
    assert rc == 0, text
    after = CoachStore(data).list_plans()[-1]
    assert after.revision == before.revision + 1
    assert after.weeks[1].target_km < week2_km  # lighter, never heavier
    assert not any(s.long_variant for s in after.weeks[1].sessions)
    longs = [s for s in after.weeks[1].sessions if s.kind.value == "long"]
    assert longs, "the long run stays a long run (scaled), not an easy run"
    assert "Plan adapted" in text and "Automatic push is off" in text
    assert writer.created == [] and writer.scheduled == []


def test_no_adapt_changes_nothing(
    env: tuple[Path, FakeGarminReads, FakeGarminWriter], tmp_path: Path
) -> None:
    data, reads, _writer = env
    reads.hrv_data = {"hrvSummary": {"status": "LOW"}}
    reads.sleep_s = 5 * 3600
    rev = CoachStore(data).list_plans()[-1].revision
    weekly_cli.main(["--data-dir", str(data), "--today", MONDAY, "--no-adapt"])
    assert CoachStore(data).list_plans()[-1].revision == rev


def test_auto_push_sends_exactly_the_dry_run(
    env: tuple[Path, FakeGarminReads, FakeGarminWriter], tmp_path: Path
) -> None:
    data, _reads, writer = env
    summary = tmp_path / "w.md"
    rc = weekly_cli.main(
        ["--data-dir", str(data), "--today", MONDAY, "--auto-push", "--summary", str(summary)]
    )
    assert rc == 0, summary.read_text()
    plan = CoachStore(data).list_plans()[-1]
    week = plan.week_of(dt.date.fromisoformat(MONDAY))
    assert week is not None
    sent = [s for s in week.sessions if s.garmin_workout_id is not None]
    assert len(writer.created) == len(sent) > 0
    # A second run has nothing left to send for that week.
    writer.created.clear()
    weekly_cli.main(["--data-dir", str(data), "--today", MONDAY, "--auto-push", "--no-adapt"])
    assert writer.created == []


def test_writes_stay_disabled_without_auto_push(
    env: tuple[Path, FakeGarminReads, FakeGarminWriter], monkeypatch: pytest.MonkeyPatch
) -> None:
    data, _reads, _writer = env
    seen: list[bool] = []
    real = weekly_cli.Settings.from_env

    def spy(**kw: object) -> object:
        seen.append(bool(kw["allow_unofficial_writes"]))
        return real(**kw)  # type: ignore[arg-type]

    monkeypatch.setattr(weekly_cli.Settings, "from_env", staticmethod(spy))
    weekly_cli.main(["--data-dir", str(data), "--today", MONDAY])
    assert seen == [False]


def test_active_plan_prefers_the_next_race(tmp_path: Path) -> None:
    store = CoachStore(tmp_path / "coach")
    reads = FakeGarminReads()
    app = make_app(reads, store)
    main = setup_plan(app)  # marathon 2027-04-18
    # A later-created tune-up race, already run by "today": it must not hide the marathon.
    call(
        app,
        "set_race_goal",
        {**MARATHON, "race_type": "half_marathon", "race_date": "2026-11-29", "target_time": None},
    )
    tune_up = call(app, "generate_training_plan", {"start_date": "2026-11-16"})
    assert push_cli.active_plan_id(store, dt.date(2026, 11, 20)) == tune_up["plan_id"]
    assert push_cli.active_plan_id(store, dt.date(2026, 12, 1)) == main["plan_id"]


# Regressions from the independent review of the weekly coach.


def _fatigue(reads: FakeGarminReads) -> None:
    reads.summary = {"restingHeartRate": 58, "lastSevenDaysAvgRestingHeartRate": 49}
    reads.hrv_data = {"hrvSummary": {"status": "LOW"}}
    reads.sleep_s = 5 * 3600


def _week1_runs(reads: FakeGarminReads) -> None:
    """Some running in plan week 1 (16-22 Nov), so the week is not 'nothing synced'."""
    from tests.test_coach_tools import run

    reads.activities += [run(dt.date(2026, 11, 17), 6), run(dt.date(2026, 11, 22), 10)]


def test_reruns_never_adapt_twice(
    env: tuple[Path, FakeGarminReads, FakeGarminWriter], tmp_path: Path
) -> None:
    data, reads, writer = env
    _fatigue(reads)
    _week1_runs(reads)
    args = ["--data-dir", str(data), "--today", MONDAY, "--auto-push"]
    assert weekly_cli.main(args) == 0
    first = CoachStore(data).list_plans()[-1]
    created = len(writer.created)
    summary = tmp_path / "again.md"
    assert weekly_cli.main([*args, "--summary", str(summary)]) == 0
    again = CoachStore(data).list_plans()[-1]
    assert again.revision == first.revision
    assert again.weeks[1].target_km == first.weeks[1].target_km
    assert len(writer.created) == created and writer.deleted == []  # no churn on Garmin
    assert "already reviewed" in summary.read_text()


def test_no_synced_activity_is_not_a_missed_week(
    env: tuple[Path, FakeGarminReads, FakeGarminWriter], tmp_path: Path
) -> None:
    data, _reads, _writer = env  # the fake history has nothing in plan week 1
    before = CoachStore(data).list_plans()[-1]
    summary = tmp_path / "w.md"
    weekly_cli.main(["--data-dir", str(data), "--today", MONDAY, "--summary", str(summary)])
    after = CoachStore(data).list_plans()[-1]
    assert after.revision == before.revision and after.auto_reviewed_weeks == []
    assert "not synced yet" in summary.read_text()


def test_mid_week_run_reviews_the_last_complete_week(
    env: tuple[Path, FakeGarminReads, FakeGarminWriter], tmp_path: Path
) -> None:
    data, reads, _writer = env
    _fatigue(reads)
    _week1_runs(reads)
    summary = tmp_path / "w.md"
    # Wednesday of week 2: reviews week 1, skips changes dated in the past, still applies.
    rc = weekly_cli.main(
        ["--data-dir", str(data), "--today", "2026-11-25", "--summary", str(summary)]
    )
    text = summary.read_text()
    assert rc == 0, text
    assert "Week 1 (2026-11-16" in text
    plan = CoachStore(data).list_plans()[-1]
    assert plan.auto_reviewed_weeks == [1] and plan.revision == 2


def test_active_plan_follows_a_goal_reset_for_the_same_race(tmp_path: Path) -> None:
    store = CoachStore(tmp_path / "coach")
    app = make_app(FakeGarminReads(), store)
    setup_plan(app)
    call(app, "set_race_goal", {**MARATHON, "days_per_week": 5})
    new = call(app, "generate_training_plan", {"start_date": "2026-11-16"})
    assert push_cli.active_plan_id(store, dt.date(2026, 11, 20)) == new["plan_id"]


# Preview: next Monday's check-in on a copy of the data.


def test_next_monday() -> None:
    assert weekly_cli.next_monday(dt.date(2026, 11, 22)) == dt.date(2026, 11, 23)  # Sunday
    assert weekly_cli.next_monday(dt.date(2026, 11, 23)) == dt.date(2026, 11, 30)  # Monday


def test_preview_shows_the_adaptation_but_changes_and_sends_nothing(
    env: tuple[Path, FakeGarminReads, FakeGarminWriter], tmp_path: Path
) -> None:
    data, reads, writer = env
    _fatigue(reads)
    _week1_runs(reads)
    saved = {p: p.read_bytes() for p in data.rglob("*") if p.is_file()}
    summary = tmp_path / "preview.md"
    # Sunday of plan week 1: previews Monday 2026-11-23, which reviews week 1.
    rc = weekly_cli.main(
        ["--data-dir", str(data), "--today", "2026-11-22", "--preview", "--summary", str(summary)]
    )
    text = summary.read_text()
    assert rc == 0, text
    assert "preview of the week of 2026-11-23 (data as of 2026-11-22)" in text
    assert "Week 1 (2026-11-16" in text and "Plan adapted" in text
    assert "Monday's check-in sends these" in text and "Automatic push is off" not in text
    assert {p: p.read_bytes() for p in data.rglob("*") if p.is_file()} == saved
    assert writer.created == [] and writer.scheduled == [] and writer.deleted == []


def test_preview_refuses_auto_push(env: tuple[Path, FakeGarminReads, FakeGarminWriter]) -> None:
    data, _reads, _writer = env
    with pytest.raises(SystemExit):
        weekly_cli.main(["--data-dir", str(data), "--preview", "--auto-push"])
