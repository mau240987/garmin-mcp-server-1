"""C1: goal/plan model validation and local storage."""

from __future__ import annotations

import datetime as dt
import stat
from pathlib import Path

import pytest
from pydantic import ValidationError

from garmin_coach_mcp.coach.engine import build_plan
from garmin_coach_mcp.coach.models import AthleteBaseline, RaceGoal, RaceType, parse_hms
from garmin_coach_mcp.coach.render import plan_summary
from garmin_coach_mcp.coach.storage import CoachStore
from garmin_coach_mcp.config import Settings
from garmin_coach_mcp.errors import NotFoundError, UpstreamServiceError

RACE = dt.date(2027, 4, 18)


def goal(**kw: object) -> RaceGoal:
    base: dict[str, object] = {"race_type": "marathon", "race_date": RACE, "days_per_week": 4}
    return RaceGoal.model_validate({**base, **kw})


def test_goal_defaults_and_validation() -> None:
    g = goal(target_time_s=parse_hms("4:59:00"))
    assert g.distance_km == pytest.approx(42.195) and g.race_type is RaceType.MARATHON
    assert g.race_pace_s_per_km == pytest.approx(17940 / 42.195)
    assert goal(race_type="10k").distance_km == 10.0
    with pytest.raises(ValidationError, match="distance_km is required"):
        goal(race_type="ultra")  # ultra needs an explicit distance
    with pytest.raises(ValidationError, match="does not match"):
        goal(race_type="5k", distance_km=100)
    with pytest.raises(ValidationError, match="plausible"):
        goal(target_time_s=3600)  # 1:25/km marathon
    with pytest.raises(ValidationError, match="plausible"):
        goal(race_type="5k", target_time_s=72 * 3600)
    with pytest.raises(ValidationError, match="longer than a marathon"):
        goal(race_type="ultra", distance_km=30)
    assert goal(race_type="10k", distance_km=10.4).distance_km == 10.4  # course variation ok
    assert goal(race_type="other", distance_km=15).distance_km == 15
    with pytest.raises(ValidationError):
        goal(days_per_week=1)
    with pytest.raises(ValidationError):
        goal(constraints=["x" * 301])
    with pytest.raises(ValidationError):
        goal(unknown_field=1)


@pytest.mark.parametrize(("raw", "secs"), [("4:59:00", 17940), ("52:30", 3150), ("0:45:00", 2700)])
def test_parse_hms(raw: str, secs: int) -> None:
    assert parse_hms(raw) == secs


@pytest.mark.parametrize("raw", ["5h", "4:60:00", "1:2", "", "4:59:60"])
def test_parse_hms_rejects(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_hms(raw)


def test_store_roundtrip_and_permissions(tmp_path: Path) -> None:
    store = CoachStore(tmp_path / "data")
    g = goal(race_name="Milano", constraints=["calf strain in March"])
    path = store.save_goal(g)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert store.get_goal(g.id) == g
    plan = build_plan(g, AthleteBaseline(), dt.date(2026, 11, 2))
    store.save_plan(plan)
    assert store.get_plan(plan.id) == plan
    assert [p.id for p in store.list_plans(g.id)] == [plan.id]
    assert store.list_plans("goal_000000000000") == []
    assert [x.id for x in store.list_goals()] == [g.id]
    assert not list((tmp_path / "data" / "goals").glob(".tmp-*"))  # atomic write cleaned up


def test_store_rejects_bad_ids_and_reports_missing(tmp_path: Path) -> None:
    store = CoachStore(tmp_path)
    for bad in ("../../etc/passwd", "goal_xyz", "plan_0123456789ab"):
        with pytest.raises(NotFoundError):
            store.get_goal(bad)
    with pytest.raises(NotFoundError):
        store.get_goal("goal_0123456789ab")


def test_store_corrupted_file(tmp_path: Path) -> None:
    store = CoachStore(tmp_path)
    g = goal()
    path = store.save_goal(g)
    path.write_text("{not json")
    with pytest.raises(UpstreamServiceError):
        store.get_goal(g.id)
    assert store.list_goals() == []  # a damaged file does not break listing


def test_settings_data_dir(tmp_path: Path) -> None:
    s = Settings.from_env({"GARMIN_COACH_DATA_DIR": str(tmp_path / "coach")})
    assert s.coach_data_dir == tmp_path / "coach"
    assert str(Settings.from_env({}).coach_data_dir).endswith(".garmin-coach-mcp/data")


def test_plan_summary_readable() -> None:
    g = goal(target_time_s=5 * 3600, race_name="Milano Marathon")
    base = AthleteBaseline(
        weeks_analyzed=12,
        recent_weekly_km=30,
        longest_run_km=18,
        easy_pace_s_per_km=430,
        from_garmin=True,
    )
    plan = build_plan(g, base, dt.date(2026, 11, 2))
    text = plan_summary(plan, g, detailed=True)
    lines = text.splitlines()
    assert lines[0].startswith("# Training plan: Milano Marathon, 42.195 km on 2027-04-18")
    assert lines[1] == "Insights derived in part from Garmin device-sourced data."
    assert "| 1 | 2026-11-02 | base |" in text and "RACE: Milano Marathon" in text
    assert "not medical advice" in text


def test_store_undecodable_and_newer_files(tmp_path: Path) -> None:
    from garmin_coach_mcp.errors import ConfigurationError

    store = CoachStore(tmp_path)
    good, bad, newer = goal(), goal(), goal()
    for g in (good, bad, newer):
        store.save_goal(g)
    (tmp_path / "goals" / f"{bad.id}.json").write_bytes(b"\xff\xfe garbage")
    data = (
        (tmp_path / "goals" / f"{newer.id}.json")
        .read_text()
        .replace('"schema_version": 1', '"schema_version": 99')
    )
    (tmp_path / "goals" / f"{newer.id}.json").write_text(data)
    assert [g.id for g in store.list_goals()] == [good.id]
    with pytest.raises(ConfigurationError):
        store.get_goal(bad.id)
    with pytest.raises(ConfigurationError, match="newer version"):
        store.get_goal(newer.id)


def test_store_id_with_newline_rejected(tmp_path: Path) -> None:
    with pytest.raises(NotFoundError, match="not a valid"):
        CoachStore(tmp_path).get_goal("goal_0123456789ab\n")
