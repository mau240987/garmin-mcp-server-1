"""Deterministic training-plan engine (C2).

Same inputs (goal, baseline, start date) -> same plan. No randomness, no clock, no LLM.
Every coaching rule is a named constant below so it can be reviewed and tested.

Rules (all checked by tests/test_coach_engine.py):
R1 Weeks run Monday-Sunday; the last week contains the race, which is the last session.
R2 Loading weeks grow at most PROGRESSION (10%) over the previous loading week, and never
   start above the recent Garmin volume x PROGRESSION.
R3 Every RECOVERY_EVERY-th week (before the taper) is a recovery week at RECOVERY_FACTOR of
   the previous loading week, with no quality session.
R4 The long run is capped per distance and level, by a share of the weekly volume, and grows
   at most LONG_RUN_STEP_KM per loading week.
R5 Taper: TAPER_WEEKS by distance, volume a decreasing share of the peak (TAPER_FACTORS).
R6 Phases before the taper: base / build / specific (~40/35/25%).
R7 Beginners get no quality session in the base phase and at most one later.
   Sessions on unavailable dates are dropped (with a warning), not moved.
R9 No long or hard session within RACE_PROTECT_DAYS of the race.
R10 At most MAX_PLAN_WEEKS weeks are planned.
R11 After a gap (unavailable dates), training resumes at RESUME_FACTOR of the previous load.
Volumes, long runs and progression are always computed from what is actually planned.
See docs/plan-engine.md for the full list (R2b, R4b).
R8 Short preparations, empty history and big volume gaps produce explicit warnings; the
   engine never raises volume faster than R2 to "catch up".
"""

from __future__ import annotations

import datetime as dt
import math

from garmin_coach_mcp.backends.base import GARMIN_DERIVED_ATTRIBUTION
from garmin_coach_mcp.coach.models import (
    AthleteBaseline,
    Level,
    Phase,
    PlannedSession,
    PlanWeek,
    RaceGoal,
    SessionKind,
    TrainingPlan,
)
from garmin_coach_mcp.formatting import format_pace

ENGINE_VERSION = "1.1.0"

PROGRESSION = 1.10
RECOVERY_EVERY = 4
RECOVERY_FACTOR = 0.75
RECOVERY_LONG_FACTOR = 0.70
LONG_RUN_STEP_KM = 2.0
MIN_EASY_KM = 3.0
ROUND_KM = 0.5
MIN_WEEK_KM = 8.0
#: An easy run is at most this share of the week's long run, and at most EASY_MAX_KM (R4b).
EASY_LONG_RATIO = 0.75
EASY_MAX_KM = {"5k": 12.0, "10k": 13.0, "half": 15.0, "marathon": 16.0, "ultra": 20.0}
QUALITY_KM = (5.0, 16.0)
#: No long run or hard session within this many days before the race (R9).
RACE_PROTECT_DAYS = {"5k": 2, "10k": 3, "half": 4, "marathon": 6, "ultra": 8}
SHAKEOUT_MAX_KM = 8.0
#: Plans cover at most this many weeks; earlier weeks are left to free base running (R10).
MAX_PLAN_WEEKS = 52
#: A week where unavailable dates removed more than half the volume is a gap (R11).
GAP_THRESHOLD = 0.5
RESUME_FACTOR = {1: 0.75, 2: 0.60}  # after 1 gap week / after 2 or more

SAFETY_NOTE = (
    "This plan is general training guidance, not medical advice. Stop and see a doctor or "
    "physiotherapist if pain persists or worsens; skip sessions when ill."
)


def _distance_class(km: float) -> str:
    if km <= 6:
        return "5k"
    if km <= 12:
        return "10k"
    if km <= 25:
        return "half"
    if km <= 43:
        return "marathon"
    return "ultra"


#: Peak weekly volume (km) by distance class and level.
PEAK_KM: dict[str, dict[Level, float]] = {
    "5k": {Level.BEGINNER: 25, Level.INTERMEDIATE: 35, Level.ADVANCED: 50},
    "10k": {Level.BEGINNER: 30, Level.INTERMEDIATE: 40, Level.ADVANCED: 60},
    "half": {Level.BEGINNER: 35, Level.INTERMEDIATE: 50, Level.ADVANCED: 70},
    "marathon": {Level.BEGINNER: 45, Level.INTERMEDIATE: 60, Level.ADVANCED: 80},
    "ultra": {Level.BEGINNER: 60, Level.INTERMEDIATE: 80, Level.ADVANCED: 100},
}
#: Long-run cap (km) by distance class and level.
LONG_CAP_KM: dict[str, dict[Level, float]] = {
    "5k": {Level.BEGINNER: 10, Level.INTERMEDIATE: 12, Level.ADVANCED: 14},
    "10k": {Level.BEGINNER: 14, Level.INTERMEDIATE: 16, Level.ADVANCED: 18},
    "half": {Level.BEGINNER: 18, Level.INTERMEDIATE: 21, Level.ADVANCED: 24},
    "marathon": {Level.BEGINNER: 30, Level.INTERMEDIATE: 32, Level.ADVANCED: 34},
    "ultra": {Level.BEGINNER: 40, Level.INTERMEDIATE: 45, Level.ADVANCED: 50},
}
#: Long run as a share of the weekly volume (R4); 0.40 minimum below LOW_VOLUME_KM/week.
LONG_SHARE = {"5k": 0.35, "10k": 0.35, "half": 0.38, "marathon": 0.45, "ultra": 0.45}
LOW_VOLUME_KM = 30.0
#: Long run never exceeds this many minutes when an easy pace is known.
LONG_RUN_MAX_MIN: dict[str, int] = {
    "5k": 100,
    "10k": 110,
    "half": 150,
    "marathon": 210,
    "ultra": 330,
}
#: Recommended preparation length (weeks) and taper length by distance class.
RECOMMENDED_WEEKS = {"5k": 6, "10k": 8, "half": 10, "marathon": 16, "ultra": 20}
TAPER_WEEKS = {"5k": 1, "10k": 1, "half": 2, "marathon": 3, "ultra": 3}
TAPER_FACTORS = {1: [0.60], 2: [0.70, 0.50], 3: [0.80, 0.65, 0.45], 4: [0.85, 0.75, 0.60, 0.45]}

#: Running days (0 = Monday) when the long run is on Sunday; rotated for other long-run days.
DAY_PATTERNS: dict[int, list[int]] = {
    2: [2, 6],
    3: [1, 3, 6],
    4: [1, 2, 4, 6],
    5: [1, 2, 3, 5, 6],
    6: [0, 1, 2, 3, 5, 6],
    7: [0, 1, 2, 3, 4, 5, 6],
}
#: Preferred quality days (Sunday-long layout): Tuesday first, then Thursday.
QUALITY_DAYS = [1, 3]


def _r(km: float) -> float:
    return round(km / ROUND_KM) * ROUND_KM


def infer_level(baseline: AthleteBaseline) -> Level:
    if baseline.is_empty or baseline.recent_weekly_km < 15 or baseline.longest_run_km < 8:
        return Level.BEGINNER
    if baseline.recent_weekly_km >= 50 and baseline.longest_run_km >= 25:
        return Level.ADVANCED
    return Level.INTERMEDIATE


def _monday(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=d.weekday())


def _phase_layout(n_weeks: int, taper: int) -> list[Phase]:
    pre = n_weeks - taper
    if pre <= 0:
        return [Phase.TAPER] * n_weeks
    base = math.floor(pre * 0.40) if pre >= 4 else 0
    build = max(1, round(pre * 0.35)) if pre >= 2 else 0
    specific = pre - base - build
    if specific < 1:
        build -= 1
        specific += 1
    return (
        [Phase.BASE] * base
        + [Phase.BUILD] * build
        + [Phase.SPECIFIC] * specific
        + [Phase.TAPER] * taper
    )


def _running_days(days_per_week: int, long_day: int) -> list[int]:
    shift = long_day - 6
    return sorted((d + shift) % 7 for d in DAY_PATTERNS[days_per_week])


def _quality_count(phase: Phase, level: Level, days: int, recovery: bool) -> int:
    if recovery or (days <= 2 and phase is Phase.BASE):
        return 0
    if phase is Phase.BASE:
        return 0 if level is Level.BEGINNER else 1
    if phase is Phase.TAPER:
        return 1 if days >= 3 else 0
    if level is Level.ADVANCED and days >= 5:
        return 2
    return 1


def _quality_session(
    phase: Phase, dclass: str, goal: RaceGoal, level: Level, slot: int
) -> tuple[SessionKind, str, str]:
    """(kind, zone, description) for a quality session."""
    rp = goal.race_pace_s_per_km
    rp_txt = f" at goal race pace ~{format_pace(rp)}" if rp else " at goal race effort"
    if phase is Phase.BASE:
        return SessionKind.STRIDES, "Z2", "Easy run with 6x20 s relaxed strides at the end."
    if phase is Phase.TAPER:
        return SessionKind.RACE_PACE, "Z3", f"Easy run with 3x1 km{rp_txt}, full recovery."
    if phase is Phase.BUILD:
        if slot == 1:
            return SessionKind.INTERVALS, "Z4", "Hill repeats: 6-8x60 s uphill, jog down."
        if level is Level.BEGINNER:
            return SessionKind.TEMPO, "Z3", "Fartlek: 6x2 min steady (Z3), 2 min easy."
        return SessionKind.TEMPO, "Z3", "Tempo: 20-30 min continuous at comfortably hard effort."
    # specific
    if dclass in ("5k", "10k"):
        if slot == 1:
            return SessionKind.TEMPO, "Z3-Z4", "Threshold: 3x10 min, 2 min easy."
        return SessionKind.INTERVALS, "Z4-Z5", f"Intervals: 5-6x1 km{rp_txt}, 2-3 min easy."
    if dclass == "half":
        return SessionKind.RACE_PACE, "Z3-Z4", f"2x5-6 km{rp_txt}, 3 min easy."
    if dclass == "marathon":
        return SessionKind.RACE_PACE, "Z3", f"10-14 km continuous{rp_txt} inside an easy run."
    return SessionKind.TEMPO, "Z3", "Steady 60-90 min at ultra effort; practise fuelling."


def _session(
    date: dt.date,
    kind: SessionKind,
    km: float,
    zone: str,
    desc: str,
    easy_pace: float | None,
) -> PlannedSession:
    duration = round(km * easy_pace / 60) if easy_pace and km > 0 else None
    pace_hint = None
    if easy_pace and kind in (SessionKind.EASY, SessionKind.LONG, SessionKind.MEDIUM_LONG):
        pace_hint = f"{format_pace(easy_pace)} - {format_pace(easy_pace + 30)} (easy)"
    return PlannedSession(
        date=date,
        kind=kind,
        distance_km=_r(km),
        zone=zone,
        description=desc,
        pace_hint=pace_hint,
        est_duration_min=duration,
    )


def _fix_rounding(sessions: list[PlannedSession], target: float) -> None:
    """Move the rounding residual onto easy runs (long run only if there is none)."""
    residual = round(target - sum(s.distance_km for s in sessions), 1)
    if abs(residual) < ROUND_KM:
        return
    ordered = sorted(sessions, key=lambda s: s.kind is not SessionKind.EASY)
    for s in ordered:
        if s.kind in (SessionKind.EASY, SessionKind.LONG):
            new = _r(s.distance_km + residual)
            if new >= MIN_EASY_KM:
                if s.est_duration_min and s.distance_km:
                    s.est_duration_min = round(s.est_duration_min * new / s.distance_km)
                s.distance_km = new
                return


def _days_for_volume(requested: int, km: float, long_km: float) -> int:
    """R2b: fewer running days when the volume left after the long run cannot give every other
    day an easy run of at least MIN_EASY_KM."""
    others = int(max(0.0, km - max(long_km, MIN_EASY_KM)) // MIN_EASY_KM)
    return max(2, min(requested, 1 + others))


def _layout_week(
    *,
    monday: dt.date,
    km: float,
    long_km: float,
    long_limit: float,
    days: int,
    goal: RaceGoal,
    phase: Phase,
    level: Level,
    dclass: str,
    recovery: bool,
    easy_pace: float | None,
) -> tuple[list[PlannedSession], bool]:
    """Sessions of a normal (non-race) week. Returns (sessions, volume_was_capped)."""
    shift = goal.long_run_day - 6
    run_days = _running_days(days, goal.long_run_day)
    long_day = goal.long_run_day
    q = _quality_count(phase, level, days, recovery)
    quality_days = [(d + shift) % 7 for d in QUALITY_DAYS]
    q_days = [d for d in quality_days if d in run_days and d != long_day][:q]
    long_km = max(long_km, MIN_EASY_KM)
    q_km = [min(QUALITY_KM[1], max(QUALITY_KM[0], km * 0.18), long_km) for _ in q_days]
    if long_km < QUALITY_KM[0]:
        q_days, q_km = [], []  # a quality session never exceeds the long run
    medium_long_day = None
    if dclass == "ultra" and phase is Phase.SPECIFIC and not recovery and km >= LOW_VOLUME_KM:
        sat = (5 + shift) % 7
        if sat in run_days and sat not in q_days and sat != long_day:
            medium_long_day = sat
    ml_km = long_km * 0.6 if medium_long_day is not None else 0.0
    easy_days = [d for d in run_days if d not in q_days and d not in (long_day, medium_long_day)]
    # Quality sessions turn into easy runs when the minimums do not fit the volume.
    while q_days and long_km + sum(q_km) + ml_km + MIN_EASY_KM * len(easy_days) > km:
        easy_days.append(q_days.pop())
        q_km.pop()
    easy_days.sort()
    easy_cap = max(MIN_EASY_KM, min(EASY_MAX_KM[dclass], EASY_LONG_RATIO * long_km))
    n_easy = len(easy_days)
    free = km - long_km - sum(q_km) - ml_km
    capped = False
    if n_easy:
        easy_km = free / n_easy
        if easy_km > easy_cap:
            # Easy runs are full: the rest goes to the long run within its own limits (R4).
            extra = min(free - n_easy * easy_cap, max(0.0, long_limit - long_km))
            long_km += extra
            easy_cap = max(MIN_EASY_KM, min(EASY_MAX_KM[dclass], EASY_LONG_RATIO * long_km))
            easy_km = min(easy_cap, (free - extra) / n_easy)
            capped = (free - extra) / n_easy > easy_cap + 0.25
        if easy_km < MIN_EASY_KM:
            easy_km = MIN_EASY_KM
            long_km = max(MIN_EASY_KM, km - sum(q_km) - ml_km - n_easy * MIN_EASY_KM)
        if easy_km > long_km:  # tiny weeks: share evenly so no easy run beats the long run
            long_km = easy_km = (long_km + n_easy * easy_km) / (n_easy + 1)
    else:
        easy_km = 0.0
        capped = free > 0.5

    sessions: list[PlannedSession] = []
    for d in run_days:
        date = monday + dt.timedelta(days=d)
        if d == long_day:
            desc = "Long run, easy and conversational."
            if dclass == "marathon" and phase is Phase.SPECIFIC and not recovery:
                desc = "Long run; last 20-30 min at goal marathon effort if feeling good."
            if dclass == "ultra":
                desc = "Long run on similar terrain; walk the climbs, practise fuelling."
            sessions.append(_session(date, SessionKind.LONG, long_km, "Z2", desc, easy_pace))
        elif d in q_days:
            slot = q_days.index(d)
            kind, zone, desc = _quality_session(phase, dclass, goal, level, slot)
            sessions.append(_session(date, kind, q_km[slot], zone, desc, None))
        elif d == medium_long_day:
            sessions.append(
                _session(
                    date,
                    SessionKind.MEDIUM_LONG,
                    ml_km,
                    "Z2",
                    "Medium-long run the day before the long run (back-to-back).",
                    easy_pace,
                )
            )
        else:
            sessions.append(
                _session(
                    date,
                    SessionKind.EASY,
                    easy_km,
                    "Z2",
                    "Easy run, conversational pace (Z2).",
                    easy_pace,
                )
            )
    if not capped:
        _fix_rounding(sessions, _r(km))
    return sessions, capped


def _race_week(
    monday: dt.date, km: float, goal: RaceGoal, days: int, easy_pace: float | None
) -> list[PlannedSession]:
    race_wd = goal.race_date.weekday()
    run_days = _running_days(days, goal.long_run_day)
    # Only as many shake-outs as the (tapered) volume allows: never above the previous week.
    n_pre = min(3, max(1, int(km // MIN_EASY_KM)))
    pre_days = [d for d in run_days if d < race_wd][-n_pre:]
    sessions: list[PlannedSession] = []
    if pre_days:
        per = max(MIN_EASY_KM, min(SHAKEOUT_MAX_KM, km / len(pre_days)))
        for d in pre_days:
            last = d == pre_days[-1]
            sessions.append(
                _session(
                    monday + dt.timedelta(days=d),
                    SessionKind.STRIDES if last else SessionKind.EASY,
                    per * (0.6 if last else 1.0),
                    "Z1-Z2",
                    "Short easy run" + (" with 4x15 s strides." if last else "."),
                    easy_pace,
                )
            )
    goal_txt = (
        f", goal {format_pace(goal.race_pace_s_per_km)}"
        if goal.race_pace_s_per_km
        else ", goal: finish"
    )
    sessions.append(
        PlannedSession(
            date=goal.race_date,
            kind=SessionKind.RACE,
            distance_km=round(goal.distance_km, 3),
            zone="race",
            description=f"RACE: {goal.race_name or goal.race_type.value} ({goal.distance_km:g} km)"
            + goal_txt,
        )
    )
    return sessions


_HARD = (
    SessionKind.LONG,
    SessionKind.MEDIUM_LONG,
    SessionKind.TEMPO,
    SessionKind.INTERVALS,
    SessionKind.RACE_PACE,
)


def _protect_race(sessions: list[PlannedSession], goal: RaceGoal, dclass: str) -> int:
    """R9: no long or hard session in the last days before the race. Returns how many changed."""
    first_protected = goal.race_date - dt.timedelta(days=RACE_PROTECT_DAYS[dclass])
    changed = 0
    for i, s in enumerate(sessions):
        if s.kind in _HARD and first_protected <= s.date < goal.race_date:
            sessions[i] = PlannedSession(
                date=s.date,
                kind=SessionKind.EASY,
                distance_km=min(s.distance_km, SHAKEOUT_MAX_KM),
                zone="Z1-Z2",
                description="Easy run, keep it short: the race is close.",
                pace_hint=s.pace_hint,
            )
            changed += 1
    return changed


def build_plan(
    goal: RaceGoal,
    baseline: AthleteBaseline,
    start_date: dt.date,
) -> TrainingPlan:
    """Generate a plan from `start_date` (inclusive) to the race. Pure function."""
    if goal.race_date <= start_date:
        raise ValueError("race_date must be after start_date")
    dclass = _distance_class(goal.distance_km)
    level = goal.level or infer_level(baseline)
    easy_pace = baseline.easy_pace_s_per_km
    warnings: list[str] = []
    notes = [SAFETY_NOTE]

    first_monday = _monday(start_date)
    n_weeks = (_monday(goal.race_date) - first_monday).days // 7 + 1
    if n_weeks > MAX_PLAN_WEEKS:  # R10: bounded horizon
        first_monday = _monday(goal.race_date) - dt.timedelta(weeks=MAX_PLAN_WEEKS - 1)
        start_date = first_monday
        n_weeks = MAX_PLAN_WEEKS
        warnings.append(
            f"The race is more than {MAX_PLAN_WEEKS} weeks away: the plan starts on "
            f"{first_monday}. Until then keep running easily at your current volume."
        )
    taper = min(TAPER_WEEKS[dclass], max(1, n_weeks // 4)) if n_weeks > 1 else 1
    if goal.race_date.weekday() <= 2 and n_weeks > taper + 1:
        taper += 1  # race early in the week: the week before must already be lighter (R5)
    phases = _phase_layout(n_weeks, taper)
    pre_taper = n_weeks - taper

    peak_target = PEAK_KM[dclass][level]
    days_req = goal.days_per_week
    long_cap = LONG_CAP_KM[dclass][level]
    if easy_pace:
        long_cap = min(long_cap, LONG_RUN_MAX_MIN[dclass] * 60 / easy_pace)
    if baseline.is_empty:
        start_km = min(max(days_req * MIN_EASY_KM, MIN_WEEK_KM), 15.0)
        warnings.append(
            "No recent running history found: the plan starts conservatively. Review it after "
            "two weeks with review_week."
        )
    else:  # R2: never start above the recent volume (minimum MIN_WEEK_KM)
        start_km = max(MIN_WEEK_KM, min(baseline.recent_weekly_km, peak_target))

    if n_weeks < RECOMMENDED_WEEKS[dclass]:
        warnings.append(
            f"Short preparation: {n_weeks} week(s) for a {dclass} (recommended at least "
            f"{RECOMMENDED_WEEKS[dclass]}). Volume will not be raised faster than 10%/week to "
            "compensate; consider a later race or a more conservative goal."
        )
    if level is Level.BEGINNER and dclass in ("marathon", "ultra"):
        warnings.append(
            f"A {dclass} is very demanding for the current training level: aim to finish, keep "
            "every run easy unless stated, and reconsider the race if long runs feel too hard."
        )
    if (
        dclass in ("half", "marathon", "ultra")
        and n_weeks <= 2
        and baseline.longest_run_km < 0.5 * goal.distance_km
    ):
        warnings.append(
            f"The race is in {n_weeks} week(s) and the longest recent run is "
            f"{baseline.longest_run_km:g} km: this plan only keeps you fresh, it cannot prepare "
            f"you for {goal.distance_km:g} km. Consider a shorter race or a later date."
        )

    unavailable = set(goal.unavailable_dates)
    weeks: list[PlanWeek] = []
    last_loading = 0.0  # volume actually planned in the last loading week
    loading_peak = 0.0
    best_long = 0.0  # longest long run actually planned (R4 growth is measured on it)
    last_long = 0.0
    gap_weeks = 0
    pre_gap_loading = 0.0
    capped_weeks: list[int] = []
    reduced_days: list[int] = []
    resumed: list[int] = []
    protected = 0

    for i, phase in enumerate(phases):
        monday = first_monday + dt.timedelta(weeks=i)
        is_race_week = i == n_weeks - 1
        recovery = phase is not Phase.TAPER and (i + 1) % RECOVERY_EVERY == 0 and i + 1 < pre_taper
        if phase is Phase.TAPER:
            factor = TAPER_FACTORS[taper][i - pre_taper]
            # Relative to the most recent loading week (not an old peak before a gap).
            km = max(MIN_WEEK_KM * 0.6, (last_loading or start_km) * factor)
        elif recovery and last_loading:
            km = max(MIN_WEEK_KM, last_loading * RECOVERY_FACTOR)
        elif gap_weeks and pre_gap_loading:
            km = max(MIN_WEEK_KM, pre_gap_loading * RESUME_FACTOR[min(gap_weeks, 2)])
            resumed.append(i + 1)
        elif not last_loading:
            km = start_km
        else:
            km = min(peak_target, last_loading * PROGRESSION)
        km = _r(km)

        if is_race_week:
            sessions = _race_week(monday, km, goal, days_req, easy_pace)
        else:
            base_long = best_long or max(baseline.longest_run_km, km * 0.25)
            long_limit = min(long_cap, base_long + LONG_RUN_STEP_KM)
            if phase is Phase.TAPER:
                long_km = min(best_long or long_cap, km * 0.35)
                long_limit = long_km
            elif recovery:
                long_km = (last_long or km * 0.35) * RECOVERY_LONG_FACTOR
                long_limit = long_km
            else:
                share = max(LONG_SHARE[dclass], 0.40) if km < LOW_VOLUME_KM else LONG_SHARE[dclass]
                long_km = min(long_limit, km * share)
                # Do not shrink the long run below the best so far when the volume allows it.
                long_km = max(long_km, min(best_long, long_limit, km - MIN_EASY_KM))
                if resumed and resumed[-1] == i + 1:
                    long_limit = max(
                        best_long * RESUME_FACTOR[min(gap_weeks, 2)], MIN_EASY_KM * 1.5
                    )
                    long_km = min(long_km, long_limit)
            days = _days_for_volume(days_req, km, long_km)
            if days < days_req and phase is not Phase.TAPER:
                reduced_days.append(i + 1)
            sessions, capped = _layout_week(
                monday=monday,
                km=km,
                long_km=long_km,
                long_limit=long_limit,
                days=days,
                goal=goal,
                phase=phase,
                level=level,
                dclass=dclass,
                recovery=recovery,
                easy_pace=easy_pace,
            )
            if capped and phase is not Phase.TAPER:
                capped_weeks.append(i + 1)

        protected += _protect_race(sessions, goal, dclass)
        dropped = [s for s in sessions if s.date in unavailable and s.kind is not SessionKind.RACE]
        if dropped:
            warnings.append(
                f"Week {i + 1}: {len(dropped)} session(s) skipped on unavailable dates "
                f"({', '.join(sorted({s.date.isoformat() for s in dropped}))})."
            )
        intended = round(sum(s.distance_km for s in sessions if s.kind is not SessionKind.RACE), 1)
        sessions = [
            s
            for s in sessions
            if start_date <= s.date <= goal.race_date
            and (s.kind is SessionKind.RACE or s.date not in unavailable)
        ]
        planned = round(sum(s.distance_km for s in sessions if s.kind is not SessionKind.RACE), 1)
        long_planned = max(
            (s.distance_km for s in sessions if s.kind is SessionKind.LONG), default=0.0
        )

        # State for the next week (R2/R11: progress from what is actually planned).
        is_gap = bool(dropped) and intended > 0 and planned < GAP_THRESHOLD * intended
        if phase is not Phase.TAPER and not recovery:
            if is_gap:
                if not gap_weeks:
                    pre_gap_loading = last_loading or intended
                gap_weeks += 1
            else:
                gap_weeks = 0
                # A partial first week (start mid-week) progresses from its intended volume;
                # weeks with skipped days progress from what was actually planned.
                partial_first = i == 0 and start_date > monday
                last_loading = intended if partial_first else planned
                loading_peak = max(loading_peak, last_loading)
        if long_planned:
            best_long = max(best_long, long_planned)
            last_long = long_planned

        weeks.append(
            PlanWeek(
                index=i + 1,
                start_date=monday,
                phase=phase,
                is_recovery=recovery,
                target_km=planned,
                long_run_km=long_planned,
                sessions=sessions,
            )
        )

    if reduced_days:
        warnings.append(
            f"Weeks {_ranges(reduced_days)} use fewer running days than requested because the "
            "weekly volume is still low."
        )
    if capped_weeks:
        warnings.append(
            f"With {days_req} running day(s) a week the volume of weeks {_ranges(capped_weeks)} "
            "is limited by the easy-run cap; add a running day to approach the usual peak."
        )
    if resumed:
        warnings.append(
            f"Training resumes at a reduced load after unavailable weeks (week(s) "
            f"{_ranges(resumed)})."
        )
    if protected:
        notes.append(f"{protected} session(s) close to the race were turned into short easy runs.")
    peak = max((w.target_km for w in weeks), default=0.0)
    if peak < 0.6 * peak_target and dclass in ("half", "marathon", "ultra"):
        warnings.append(
            f"Peak volume reaches about {peak:g} km/week, below the usual {peak_target:.0f} "
            "km/week for this goal: there is not enough time to build up safely. Treat the goal "
            "as finishing comfortably."
        )

    return TrainingPlan(
        goal_id=goal.id,
        start_date=start_date,
        race_date=goal.race_date,
        level=level,
        baseline=baseline,
        peak_weekly_km=peak,
        weeks=weeks,
        warnings=warnings,
        notes=notes,
        attribution=GARMIN_DERIVED_ATTRIBUTION if baseline.from_garmin else None,
        engine_version=ENGINE_VERSION,
    )


def _ranges(nums: list[int]) -> str:
    """[1,2,3,7] -> '1-3, 7'."""
    out: list[str] = []
    start = prev = nums[0]
    for n in [*nums[1:], None]:
        if n is not None and n == prev + 1:
            prev = n
            continue
        out.append(f"{start}-{prev}" if start != prev else f"{start}")
        if n is not None:
            start = prev = n
    return ", ".join(out)
