"""C2: deterministic plan engine. Named brief cases + rule invariants over a wide matrix."""

from __future__ import annotations

import datetime as dt
import itertools

import pytest

from garmin_coach_mcp.coach.engine import (
    LONG_CAP_KM,
    LONG_RUN_STEP_KM,
    MIN_EASY_KM,
    PROGRESSION,
    RECOVERY_EVERY,
    _distance_class,
    build_plan,
    infer_level,
)
from garmin_coach_mcp.coach.models import (
    AthleteBaseline,
    Level,
    Phase,
    RaceGoal,
    SessionKind,
    TrainingPlan,
)

MONDAY = dt.date(2026, 11, 2)
TOL = 0.51  # one rounding step (0.5 km)

BEGINNER = AthleteBaseline(
    weeks_analyzed=12,
    recent_weekly_km=10,
    max_weekly_km=14,
    longest_run_km=6,
    runs_per_week=2,
    from_garmin=True,
)
MARATHON_5H = AthleteBaseline(
    weeks_analyzed=12,
    recent_weekly_km=30,
    max_weekly_km=38,
    longest_run_km=18,
    runs_per_week=3.5,
    easy_pace_s_per_km=430,
    from_garmin=True,
)
ULTRA_RUNNER = AthleteBaseline(
    weeks_analyzed=12,
    recent_weekly_km=40,
    max_weekly_km=50,
    longest_run_km=30,
    runs_per_week=4,
    easy_pace_s_per_km=420,
    from_garmin=True,
)
ADVANCED = AthleteBaseline(
    weeks_analyzed=12,
    recent_weekly_km=65,
    max_weekly_km=75,
    longest_run_km=30,
    runs_per_week=6,
    easy_pace_s_per_km=300,
    from_garmin=True,
)
EMPTY = AthleteBaseline()


def check_invariants(plan: TrainingPlan, goal: RaceGoal, start: dt.date) -> None:
    weeks = plan.weeks
    sessions = [s for w in weeks for s in w.sessions]
    # R1: race is the last session, on the race date; nothing outside [start, race]
    assert sessions[-1].kind is SessionKind.RACE and sessions[-1].date == goal.race_date
    assert sum(s.kind is SessionKind.RACE for s in sessions) == 1
    assert all(start <= s.date <= goal.race_date for s in sessions)
    for w in weeks:
        assert w.start_date.weekday() == 0
        assert all(
            w.start_date <= s.date <= w.start_date + dt.timedelta(days=6) for s in w.sessions
        )
        dates = [s.date for s in w.sessions]
        assert len(dates) == len(set(dates)), "one session per day"
        assert len(w.sessions) <= goal.days_per_week + (1 if w is weeks[-1] else 0)
        assert not set(dates) & set(goal.unavailable_dates) - {goal.race_date}
        # weekly target is the sum of its sessions (race excluded)
        total = sum(s.distance_km for s in w.sessions if s.kind is not SessionKind.RACE)
        assert abs(total - w.target_km) < 0.11
    # R2: loading weeks progress at most 10% over the previous loading week (full weeks only)
    loading = [w for w in weeks if w.phase is not Phase.TAPER and not w.is_recovery]
    if not plan.baseline.is_empty and start.weekday() == 0 and loading:
        assert (
            loading[0].target_km
            <= max(
                plan.baseline.recent_weekly_km * PROGRESSION, goal.days_per_week * MIN_EASY_KM, 8
            )
            + TOL
        )
    full = loading if start.weekday() == 0 else loading[1:]
    # Weeks shortened by unavailable dates are not a planned load: the next week resumes the
    # planned progression, so they are left out of the comparison.
    blocked = set(goal.unavailable_dates)
    shortened = {
        w.index
        for w in weeks
        if any(w.start_date + dt.timedelta(days=d) in blocked for d in range(7))
    }
    for prev, cur in itertools.pairwise(full):
        if prev.index in shortened or cur.index in shortened:
            continue
        assert cur.target_km <= prev.target_km * PROGRESSION + TOL, (prev.index, cur.index)
    # R3: no more than RECOVERY_EVERY-1 loading weeks in a row before the taper
    pre = [w for w in weeks if w.phase is not Phase.TAPER]
    run = 0
    for w in pre[:-1]:  # the last pre-taper week may complete a block
        run = 0 if w.is_recovery else run + 1
        assert run <= RECOVERY_EVERY - 1
    for w in weeks:
        if w.is_recovery:
            assert not any(
                s.kind in (SessionKind.TEMPO, SessionKind.INTERVALS, SessionKind.RACE_PACE)
                for s in w.sessions
            )
    # R4: long run cap and growth
    cap = LONG_CAP_KM[_distance_class(goal.distance_km)][plan.level]
    longs = [w.long_run_km for w in weeks]
    assert max(longs) <= cap + TOL
    best = 0.0
    for w in weeks:
        if w.long_run_km and not w.is_recovery and w.phase is not Phase.TAPER and best:
            assert w.long_run_km <= best + LONG_RUN_STEP_KM + TOL
        best = max(best, w.long_run_km)
    # R5: taper volumes decrease and stay below the peak; no long run in race week
    taper = [w for w in weeks if w.phase is Phase.TAPER]
    assert taper and taper[-1] is weeks[-1]
    for prev, cur in itertools.pairwise(taper):
        assert cur.target_km <= prev.target_km + TOL
    assert all(w.target_km <= plan.peak_weekly_km + TOL for w in taper)
    assert weeks[-1].long_run_km == 0
    # Easy runs respect the minimum (race week excluded: short shake-outs)
    for w in weeks[:-1]:
        for s in w.sessions:
            if s.kind is SessionKind.EASY:
                assert s.distance_km >= MIN_EASY_KM - 0.01
    # R7: beginners
    if plan.level is Level.BEGINNER:
        for w in weeks:
            q = [
                s
                for s in w.sessions
                if s.kind
                in (
                    SessionKind.TEMPO,
                    SessionKind.INTERVALS,
                    SessionKind.RACE_PACE,
                    SessionKind.STRIDES,
                )
            ]
            if w.phase is Phase.BASE:
                assert not q
            assert len([s for s in q if s.kind is not SessionKind.STRIDES]) <= 1
    assert plan.notes and "not medical advice" in plan.notes[0]


# ----------------------------------------------------------------- named brief cases


def test_beginner_10k() -> None:
    goal = RaceGoal(
        race_type="10k", race_date=MONDAY + dt.timedelta(weeks=10, days=6), days_per_week=3
    )
    plan = build_plan(goal, BEGINNER, MONDAY)
    check_invariants(plan, goal, MONDAY)
    assert plan.level is Level.BEGINNER
    assert plan.peak_weekly_km <= 30 + TOL
    assert max(w.long_run_km for w in plan.weeks) <= 14
    assert plan.attribution and "Garmin" in plan.attribution


def test_marathon_5h() -> None:
    goal = RaceGoal(
        race_type="marathon",
        race_date=dt.date(2027, 4, 18),
        target_time_s=5 * 3600,
        days_per_week=4,
    )
    plan = build_plan(goal, MARATHON_5H, MONDAY)
    check_invariants(plan, goal, MONDAY)
    assert plan.level is Level.INTERMEDIATE
    longest = max(w.long_run_km for w in plan.weeks)
    assert 24 <= longest <= 210 * 60 / 430 + TOL  # meaningful, and <= 3h30 at easy pace
    assert [w.phase for w in plan.weeks[-3:]] == [Phase.TAPER] * 3
    race_pace = [s for w in plan.weeks for s in w.sessions if s.kind is SessionKind.RACE_PACE]
    assert race_pace and "7:07/km" in race_pace[0].description  # 5h / 42.195 km
    assert all(s.pace_hint for w in plan.weeks for s in w.sessions if s.kind is SessionKind.LONG)


def test_ultra_100km_passatore() -> None:
    goal = RaceGoal(
        race_type="ultra",
        distance_km=100,
        race_name="Passatore",
        race_date=dt.date(2027, 5, 29),
        days_per_week=5,
    )
    plan = build_plan(goal, ULTRA_RUNNER, MONDAY)
    check_invariants(plan, goal, MONDAY)
    specific = [w for w in plan.weeks if w.phase is Phase.SPECIFIC and not w.is_recovery]
    assert specific and all(
        any(s.kind is SessionKind.MEDIUM_LONG for s in w.sessions) for w in specific
    ), "back-to-back weekends in the specific phase"
    assert plan.weeks[-1].sessions[-1].description.startswith("RACE: Passatore")


def test_race_in_four_weeks() -> None:
    goal = RaceGoal(
        race_type="half_marathon", race_date=MONDAY + dt.timedelta(days=27), days_per_week=4
    )
    plan = build_plan(goal, MARATHON_5H, MONDAY)
    check_invariants(plan, goal, MONDAY)
    assert len(plan.weeks) == 4
    assert any("Short preparation" in w for w in plan.warnings)


def test_empty_history() -> None:
    goal = RaceGoal(
        race_type="5k", race_date=MONDAY + dt.timedelta(weeks=8, days=5), days_per_week=3
    )
    plan = build_plan(goal, EMPTY, MONDAY)
    check_invariants(plan, goal, MONDAY)
    assert plan.level is Level.BEGINNER
    assert plan.weeks[0].target_km <= 12 + TOL
    assert any("No recent running history" in w for w in plan.warnings)
    assert plan.attribution is None  # nothing came from Garmin


# ---------------------------------------------------------------------- the matrix

GOALS = [("5k", None), ("10k", None), ("half_marathon", None), ("marathon", None), ("ultra", 100.0)]
BASELINES = {"empty": EMPTY, "beginner": BEGINNER, "mid": MARATHON_5H, "adv": ADVANCED}


@pytest.mark.parametrize("race_type,distance", GOALS)
@pytest.mark.parametrize("base_name", list(BASELINES))
@pytest.mark.parametrize("days", [2, 3, 4, 5, 6, 7])
@pytest.mark.parametrize("weeks_out", [1, 3, 6, 12, 20, 30])
@pytest.mark.parametrize("race_weekday", [0, 3, 6])
def test_invariants_matrix(
    race_type: str,
    distance: float | None,
    base_name: str,
    days: int,
    weeks_out: int,
    race_weekday: int,
) -> None:
    race_date = MONDAY + dt.timedelta(weeks=weeks_out, days=race_weekday)
    goal = RaceGoal(
        race_type=race_type, distance_km=distance, race_date=race_date, days_per_week=days
    )
    plan = build_plan(goal, BASELINES[base_name], MONDAY)
    check_invariants(plan, goal, MONDAY)


@pytest.mark.parametrize("offset", [1, 3, 6])
def test_mid_week_start(offset: int) -> None:
    start = MONDAY + dt.timedelta(days=offset)
    goal = RaceGoal(
        race_type="half_marathon",
        race_date=MONDAY + dt.timedelta(weeks=12, days=6),
        days_per_week=5,
    )
    plan = build_plan(goal, MARATHON_5H, start)
    check_invariants(plan, goal, start)
    assert all(s.date >= start for s in plan.weeks[0].sessions)


def test_long_run_day_and_unavailable_dates() -> None:
    blocked = [MONDAY + dt.timedelta(days=d) for d in (7, 8, 9)]
    goal = RaceGoal(
        race_type="10k",
        race_date=MONDAY + dt.timedelta(weeks=9, days=5),
        days_per_week=4,
        long_run_day=5,
        unavailable_dates=blocked,
    )
    plan = build_plan(goal, MARATHON_5H, MONDAY)
    check_invariants(plan, goal, MONDAY)
    longs = [s for w in plan.weeks for s in w.sessions if s.kind is SessionKind.LONG]
    assert longs and all(s.date.weekday() == 5 for s in longs)
    assert any("unavailable dates" in w for w in plan.warnings)


def test_deterministic() -> None:
    goal = RaceGoal(race_type="marathon", race_date=dt.date(2027, 4, 18), days_per_week=5)
    a = build_plan(goal, MARATHON_5H, MONDAY).model_dump(exclude={"id", "created_at"})
    b = build_plan(goal, MARATHON_5H, MONDAY).model_dump(exclude={"id", "created_at"})
    assert a == b


def test_level_inference_and_override() -> None:
    assert infer_level(EMPTY) is Level.BEGINNER
    assert infer_level(BEGINNER) is Level.BEGINNER
    assert infer_level(MARATHON_5H) is Level.INTERMEDIATE
    assert infer_level(ADVANCED) is Level.ADVANCED
    goal = RaceGoal(
        race_type="10k", race_date=MONDAY + dt.timedelta(weeks=8), days_per_week=4, level="beginner"
    )
    assert build_plan(goal, ADVANCED, MONDAY).level is Level.BEGINNER


def test_beginner_marathon_warns() -> None:
    goal = RaceGoal(
        race_type="marathon", race_date=MONDAY + dt.timedelta(weeks=12, days=6), days_per_week=3
    )
    plan = build_plan(goal, BEGINNER, MONDAY)
    assert any("very demanding" in w for w in plan.warnings)
    assert any("not enough time" in w for w in plan.warnings)


def test_race_before_start_rejected() -> None:
    goal = RaceGoal(race_type="5k", race_date=MONDAY, days_per_week=3)
    with pytest.raises(ValueError):
        build_plan(goal, EMPTY, MONDAY)
