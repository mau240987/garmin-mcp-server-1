"""C2: deterministic plan engine. Named brief cases + rule invariants over a wide matrix."""

from __future__ import annotations

import datetime as dt
import itertools

import pytest

from garmin_coach_mcp.coach.engine import (
    EASY_MAX_KM,
    LONG_CAP_KM,
    LONG_RUN_STEP_KM,
    MAX_PLAN_WEEKS,
    MIN_EASY_KM,
    MIN_FAST_KM,
    MIN_WEEK_KM,
    PROGRESSION,
    RACE_PROTECT_DAYS,
    RECOVERY_EVERY,
    RECOVERY_FACTOR,
    STEADY_FINISH,
    _distance_class,
    build_plan,
    infer_level,
)
from garmin_coach_mcp.coach.models import (
    AthleteBaseline,
    Level,
    LongVariant,
    Phase,
    RaceGoal,
    SessionKind,
    TrainingPlan,
)
from garmin_coach_mcp.coach.sessions import session_steps
from garmin_coach_mcp.models import RepeatStep

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
    dclass = _distance_class(goal.distance_km)
    blocked = set(goal.unavailable_dates)
    eff_start = max(start, plan.start_date)
    # R1/R10: calendar, bounded horizon, race last
    assert len(weeks) <= MAX_PLAN_WEEKS
    assert sessions[-1].kind is SessionKind.RACE and sessions[-1].date == goal.race_date
    assert sum(s.kind is SessionKind.RACE for s in sessions) == 1
    assert all(eff_start <= s.date <= goal.race_date for s in sessions)
    for w in weeks:
        assert w.start_date.weekday() == 0
        assert all(
            w.start_date <= s.date <= w.start_date + dt.timedelta(days=6) for s in w.sessions
        )
        dates = [s.date for s in w.sessions]
        assert len(dates) == len(set(dates)), "one session per day"
        assert len(w.sessions) <= goal.days_per_week + (1 if w is weeks[-1] else 0)
        assert not set(dates) & blocked - {goal.race_date}
        assert all(s.distance_km > 0 for s in w.sessions)
        total = sum(s.distance_km for s in w.sessions if s.kind is not SessionKind.RACE)
        assert abs(total - w.target_km) < 0.11  # bookkeeping consistency
    # R9: nothing long or hard in the protected days before the race
    first_protected = goal.race_date - dt.timedelta(days=RACE_PROTECT_DAYS[dclass])
    for s in sessions:
        if first_protected <= s.date < goal.race_date:
            assert s.kind not in HARD, (s.date, s.kind)
    # R4b: easy runs are capped and never longer than the week's long run
    for w in weeks[:-1]:
        for s in w.sessions:
            if s.kind is SessionKind.EASY:
                assert s.distance_km >= MIN_EASY_KM - 0.01
                assert s.distance_km <= max(EASY_MAX_KM[dclass], MIN_EASY_KM) + TOL
                if w.long_run_km:
                    assert s.distance_km <= w.long_run_km + TOL, (w.index, s)
            if s.kind in (
                SessionKind.TEMPO,
                SessionKind.INTERVALS,
                SessionKind.RACE_PACE,
                SessionKind.FARTLEK,
            ):
                assert not w.long_run_km or s.distance_km <= w.long_run_km + TOL
    shortened = {
        w.index
        for w in weeks
        if any(w.start_date + dt.timedelta(days=d) in blocked for d in range(7))
    }
    # R2: first full week never above the recent volume (min 8 km); <=10% between loading weeks
    loading = [w for w in weeks if w.phase is not Phase.TAPER and not w.is_recovery]
    if loading and not plan.baseline.is_empty:
        cap1 = max(min(plan.baseline.recent_weekly_km, 1e9), MIN_WEEK_KM)
        assert loading[0].target_km <= cap1 + TOL
    full = loading if start.weekday() == 0 else loading[1:]
    for prev, cur in itertools.pairwise(full):
        if prev.index in shortened or cur.index in shortened or cur.index - 1 in shortened:
            continue
        assert cur.target_km <= prev.target_km * PROGRESSION + TOL, (prev.index, cur.index)
    # R3: recovery cadence and factor
    pre = [w for w in weeks if w.phase is not Phase.TAPER]
    run = 0
    for w in pre[:-1]:
        run = 0 if w.is_recovery else run + 1
        assert run <= RECOVERY_EVERY - 1
    prev_load = None
    for w in pre:
        if w.is_recovery and prev_load and prev_load.index not in shortened:
            assert w.target_km <= prev_load.target_km * RECOVERY_FACTOR + TOL + MIN_EASY_KM
            assert not any(s.kind in HARD[2:] for s in w.sessions)
        if not w.is_recovery:
            prev_load = w
    # R4: long-run cap and growth (measured on planned long runs)
    cap = LONG_CAP_KM[dclass][plan.level]
    assert max(w.long_run_km for w in weeks) <= cap + TOL
    best = 0.0
    for w in weeks:
        if w.long_run_km and not w.is_recovery and w.phase is not Phase.TAPER and best:
            assert w.long_run_km <= best + LONG_RUN_STEP_KM + TOL
        best = max(best, w.long_run_km)
    # R5: taper volumes decrease and stay well under the loading peak
    taper = [w for w in weeks if w.phase is Phase.TAPER]
    assert taper and taper[-1] is weeks[-1]
    for prev, cur in itertools.pairwise(taper):
        assert cur.target_km <= prev.target_km + TOL, (prev.index, cur.index)
    # Compare with complete loading weeks only (a partial first week is not a planned peak).
    peak_loading = max((w.target_km for w in full if w.index not in shortened), default=0.0)
    if peak_loading:
        assert all(w.target_km <= max(peak_loading * 0.85, MIN_WEEK_KM) + TOL for w in taper)
    assert weeks[-1].long_run_km == 0
    # R7: beginners
    if plan.level is Level.BEGINNER:
        for w in weeks:
            q = [s for s in w.sessions if s.kind in HARD[2:] or s.kind is SessionKind.STRIDES]
            if w.phase is Phase.BASE:
                assert not q
            assert len([s for s in q if s.kind is not SessionKind.STRIDES]) <= 1
    check_variety(plan, goal)
    assert plan.notes and "not medical advice" in plan.notes[0]


def _step_km(steps: list) -> float:
    km = 0.0
    for st in steps:
        if isinstance(st, RepeatStep):
            km += st.repeat_count * _step_km(st.steps)
        elif st.duration_type == "distance":
            km += st.duration_value / 1000
    return km


def check_variety(plan: TrainingPlan, goal: RaceGoal) -> None:
    """R12: fartlek and long-run variants only where allowed, and well-formed."""
    dclass = _distance_class(goal.distance_km)
    allowed = plan.level is not Level.BEGINNER and dclass != "ultra"
    for w in plan.weeks:
        hard_week = w.is_recovery or w.phase is Phase.TAPER or w.index == 1
        fartleks = [s for s in w.sessions if s.kind is SessionKind.FARTLEK]
        variants = [s for s in w.sessions if s.long_variant is not None]
        if not allowed or hard_week:
            assert not variants, (w.index, variants)
            if plan.level is not Level.BEGINNER or w.phase is Phase.BASE:
                assert not fartleks or not hard_week, (w.index, fartleks)
        assert not (fartleks and variants), f"week {w.index}: fartlek and long variant together"
        for s in variants:
            assert s.kind is SessionKind.LONG
            assert s.fast_km is not None and MIN_FAST_KM <= s.fast_km < s.distance_km
            if s.long_variant is LongVariant.PROGRESSIVE:
                share, cap = STEADY_FINISH[w.phase]
                assert s.fast_km <= min(cap, share * s.distance_km) + TOL
                assert s.fast_blocks == 1
            else:
                assert goal.race_pace_s_per_km is not None
                assert s.fast_km <= 0.30 * s.distance_km + TOL and s.fast_km <= 10.0
        for s in [*variants, *fartleks]:
            steps = session_steps(s, plan.easy_pace, goal.race_pace_s_per_km)
            if s.kind is SessionKind.LONG:  # same distance as planned, faster part included
                assert abs(_step_km(steps) - s.distance_km) < 0.11, (s, _step_km(steps))
            else:  # fartlek: distance steps + the timed block ~ the planned distance
                reps = next(st for st in steps if isinstance(st, RepeatStep))
                block_s = reps.repeat_count * sum(st.duration_value for st in reps.steps)
                est = _step_km(steps) + block_s / (plan.easy_pace or 390.0)
                assert abs(est - s.distance_km) <= 1.0 + TOL, (s, est)


HARD = (
    SessionKind.LONG,
    SessionKind.MEDIUM_LONG,
    SessionKind.TEMPO,
    SessionKind.INTERVALS,
    SessionKind.RACE_PACE,
    SessionKind.FARTLEK,
)


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


BLOCKS = {
    "none": [],
    "one-day": [MONDAY + dt.timedelta(days=9)],
    "two-weeks": [MONDAY + dt.timedelta(weeks=2, days=d) for d in range(14)],
}


@pytest.mark.parametrize("race_type,distance", GOALS)
@pytest.mark.parametrize("base_name", list(BASELINES))
@pytest.mark.parametrize("days", [2, 3, 5, 7])
@pytest.mark.parametrize("weeks_out", [1, 3, 8, 20, 45])
@pytest.mark.parametrize("race_weekday", [0, 5, 6])
@pytest.mark.parametrize("long_day", [6, 2, 0])
@pytest.mark.parametrize("start_offset", [0, 3])
def test_invariants_matrix(
    race_type: str,
    distance: float | None,
    base_name: str,
    days: int,
    weeks_out: int,
    race_weekday: int,
    long_day: int,
    start_offset: int,
) -> None:
    race_date = MONDAY + dt.timedelta(weeks=weeks_out, days=race_weekday)
    start = MONDAY + dt.timedelta(days=start_offset)
    goal = RaceGoal(
        race_type=race_type,
        distance_km=distance,
        race_date=race_date,
        days_per_week=days,
        long_run_day=long_day,
    )
    plan = build_plan(goal, BASELINES[base_name], start)
    check_invariants(plan, goal, start)


@pytest.mark.parametrize("block", list(BLOCKS))
@pytest.mark.parametrize("race_type,distance", GOALS)
@pytest.mark.parametrize("base_name", list(BASELINES))
def test_invariants_with_unavailable_dates(
    block: str, race_type: str, distance: float | None, base_name: str
) -> None:
    goal = RaceGoal(
        race_type=race_type,
        distance_km=distance,
        race_date=MONDAY + dt.timedelta(weeks=20, days=6),
        days_per_week=5,
        unavailable_dates=BLOCKS[block],
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


# ------------------------------------------------- regressions from the independent review


def test_no_long_run_the_day_before_a_monday_marathon() -> None:
    goal = RaceGoal(race_type="marathon", race_date=dt.date(2027, 4, 19), days_per_week=4)
    plan = build_plan(goal, MARATHON_5H, MONDAY)
    check_invariants(plan, goal, MONDAY)
    day_before = [s for w in plan.weeks for s in w.sessions if s.date == dt.date(2027, 4, 18)]
    assert all(s.kind is SessionKind.EASY and s.distance_km <= 8 for s in day_before)


def test_easy_runs_capped_with_few_days() -> None:
    goal = RaceGoal(
        race_type="5k", race_date=MONDAY + dt.timedelta(weeks=12, days=6), days_per_week=2
    )
    plan = build_plan(goal, ADVANCED, MONDAY)
    check_invariants(plan, goal, MONDAY)
    assert (
        max(s.distance_km for w in plan.weeks for s in w.sessions if s.kind is SessionKind.EASY)
        <= EASY_MAX_KM["5k"]
    )
    assert any("add a running day" in w for w in plan.warnings)


def test_resume_after_a_month_off() -> None:
    blocked = [MONDAY + dt.timedelta(weeks=8, days=d) for d in range(28)]
    goal = RaceGoal(
        race_type="ultra",
        distance_km=100,
        race_date=dt.date(2027, 5, 29),
        days_per_week=5,
        unavailable_dates=blocked,
    )
    plan = build_plan(goal, ULTRA_RUNNER, MONDAY)
    check_invariants(plan, goal, MONDAY)
    before = max(w.target_km for w in plan.weeks[:8])
    best_long_before = max(w.long_run_km for w in plan.weeks[:8])
    resume = plan.weeks[12]
    assert resume.target_km <= before * 0.6 + TOL
    assert resume.long_run_km <= best_long_before * 0.6 + TOL
    assert any("resumes at a reduced load" in w for w in plan.warnings)


def test_far_future_race_is_bounded_and_fast() -> None:
    import time

    goal = RaceGoal(race_type="marathon", race_date=dt.date(2100, 4, 18), days_per_week=4)
    t0 = time.perf_counter()
    plan = build_plan(goal, MARATHON_5H, MONDAY)
    assert time.perf_counter() - t0 < 1.0
    assert len(plan.weeks) == MAX_PLAN_WEEKS
    assert any("more than 52 weeks away" in w for w in plan.warnings)
    check_invariants(plan, goal, MONDAY)


def test_tiny_recent_volume_does_not_jump() -> None:
    tiny = AthleteBaseline(
        weeks_analyzed=8, recent_weekly_km=3, longest_run_km=2, runs_per_week=1, from_garmin=True
    )
    goal = RaceGoal(
        race_type="10k", race_date=MONDAY + dt.timedelta(weeks=14, days=6), days_per_week=7
    )
    plan = build_plan(goal, tiny, MONDAY)
    check_invariants(plan, goal, MONDAY)
    assert plan.weeks[0].target_km <= MIN_WEEK_KM + TOL
    assert len(plan.weeks[0].sessions) <= 2
    assert any("fewer running days" in w for w in plan.warnings)


def test_race_tomorrow_without_history_is_flagged() -> None:
    goal = RaceGoal(
        race_type="ultra", distance_km=100, race_date=MONDAY + dt.timedelta(days=1), days_per_week=4
    )
    plan = build_plan(goal, EMPTY, MONDAY)
    check_invariants(plan, goal, MONDAY)
    assert any("cannot prepare you" in w for w in plan.warnings)


# ----------------------------------------------------------------- R12 variety

PARIS_LIKE = AthleteBaseline(
    weeks_analyzed=12,
    recent_weekly_km=43.6,
    max_weekly_km=61.6,
    longest_run_km=42.6,
    runs_per_week=3.1,
    easy_pace_s_per_km=375,
    from_garmin=True,
)


def _paris_goal(**kw: object) -> RaceGoal:
    base: dict[str, object] = {
        "race_type": "marathon",
        "race_date": dt.date(2027, 4, 4),
        "days_per_week": 3,
        "target_time_s": 3 * 3600 + 59 * 60 + 59,
    }
    return RaceGoal(**{**base, **kw})


def test_variety_marathon_three_days() -> None:
    goal = _paris_goal()
    start = dt.date(2026, 11, 9)
    plan = build_plan(goal, PARIS_LIKE, start)
    check_invariants(plan, goal, start)
    sessions = [(w, s) for w in plan.weeks for s in w.sessions]
    fartlek_phases = {w.phase for w, s in sessions if s.kind is SessionKind.FARTLEK}
    assert fartlek_phases == {Phase.BASE, Phase.BUILD}
    variants = {(w.phase, s.long_variant, s.fast_blocks) for w, s in sessions if s.long_variant}
    assert (Phase.BASE, LongVariant.PROGRESSIVE, 1) in variants
    assert (Phase.BUILD, LongVariant.RACE_PACE, 1) in variants  # marathon-pace finish
    assert (Phase.SPECIFIC, LongVariant.RACE_PACE, 2) in variants
    # Easy long runs remain: variety alternates, it does not replace them.
    loading_longs = [
        s
        for w, s in sessions
        if s.kind is SessionKind.LONG and not w.is_recovery and w.phase is not Phase.TAPER
    ]
    assert sum(s.long_variant is None for s in loading_longs) >= len(loading_longs) // 2
    # Race-pace parts carry the goal pace (5:41/km, +10/-5 s) in the workout steps.
    rp_long = next(s for _w, s in sessions if s.long_variant is LongVariant.RACE_PACE)
    steps = session_steps(rp_long, plan.easy_pace, goal.race_pace_s_per_km)
    paced = [
        st
        for st in [*steps, *(x for st in steps if isinstance(st, RepeatStep) for x in st.steps)]
        if not isinstance(st, RepeatStep) and st.pace_low == "5:51"
    ]
    assert paced and all(st.pace_high == "5:36" for st in paced)


def test_variety_is_deterministic_and_keeps_volumes_of_v11_rules() -> None:
    goal = _paris_goal()
    a = build_plan(goal, PARIS_LIKE, dt.date(2026, 11, 9))
    b = build_plan(goal, PARIS_LIKE, dt.date(2026, 11, 9))
    assert a.model_dump(exclude={"id", "created_at"}) == b.model_dump(exclude={"id", "created_at"})
    # Same kilometres as a beginner-free plan of the same athlete: variety changes only how
    # part of the distance is run, not the weekly volume or the long run.
    plain = build_plan(goal.model_copy(update={"target_time_s": None}), PARIS_LIKE, a.start_date)
    assert [w.target_km for w in a.weeks] == [w.target_km for w in plain.weeks]
    assert [w.long_run_km for w in a.weeks] == [w.long_run_km for w in plain.weeks]


@pytest.mark.parametrize("base_name", ["beginner", "empty"])
def test_no_variety_for_beginners(base_name: str) -> None:
    goal = RaceGoal(
        race_type="half_marathon",
        race_date=MONDAY + dt.timedelta(weeks=20, days=6),
        days_per_week=3,
        target_time_s=9000,
    )
    plan = build_plan(goal, BASELINES[base_name], MONDAY)
    assert plan.level is Level.BEGINNER
    assert not any(s.long_variant for w in plan.weeks for s in w.sessions)
    assert not any(
        s.kind is SessionKind.FARTLEK
        for w in plan.weeks
        if w.phase is Phase.BASE
        for s in w.sessions
    )


def test_no_variety_for_ultras() -> None:
    goal = RaceGoal(
        race_type="ultra",
        distance_km=100,
        race_date=MONDAY + dt.timedelta(weeks=30),
        days_per_week=5,
    )
    plan = build_plan(goal, ULTRA_RUNNER, MONDAY)
    kinds = {s.kind for w in plan.weeks for s in w.sessions}
    assert SessionKind.FARTLEK not in kinds
    assert not any(s.long_variant for w in plan.weeks for s in w.sessions)


def test_no_race_pace_long_without_a_target_time() -> None:
    goal = _paris_goal(target_time_s=None)
    plan = build_plan(goal, PARIS_LIKE, dt.date(2026, 11, 9))
    variants = {s.long_variant for w in plan.weeks for s in w.sessions if s.long_variant}
    assert variants == {LongVariant.PROGRESSIVE}


@pytest.mark.parametrize(
    "race_type,target",
    [("marathon", 12600), ("half_marathon", 6300), ("10k", 2880), ("5k", 1380)],
)
@pytest.mark.parametrize("base_name", ["mid", "adv"])
@pytest.mark.parametrize("days", [3, 4, 6])
@pytest.mark.parametrize("weeks_out", [6, 18, 30])
def test_invariants_with_target_times(
    race_type: str, target: int, base_name: str, days: int, weeks_out: int
) -> None:
    goal = RaceGoal(
        race_type=race_type,
        race_date=MONDAY + dt.timedelta(weeks=weeks_out, days=6),
        days_per_week=days,
        target_time_s=target,
    )
    plan = build_plan(goal, BASELINES[base_name], MONDAY)
    check_invariants(plan, goal, MONDAY)
