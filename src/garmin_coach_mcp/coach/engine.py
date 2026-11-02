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
   Sessions on unavailable dates are dropped (with a warning), not moved: the following week
   resumes the planned load.
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

ENGINE_VERSION = "1.0.0"

PROGRESSION = 1.10
RECOVERY_EVERY = 4
RECOVERY_FACTOR = 0.75
RECOVERY_LONG_FACTOR = 0.70
LONG_RUN_STEP_KM = 2.0
MIN_EASY_KM = 3.0
ROUND_KM = 0.5

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
    """Move the rounding residual onto easy runs so the week sums to its target."""
    residual = round(target - sum(s.distance_km for s in sessions), 1)
    if abs(residual) < ROUND_KM:
        return
    ordered = sorted(sessions, key=lambda s: s.kind is not SessionKind.EASY)  # easy runs first
    for s in ordered:
        if s.kind in (SessionKind.EASY, SessionKind.LONG):
            new = _r(s.distance_km + residual)
            if new >= MIN_EASY_KM:
                if s.est_duration_min and s.distance_km:
                    s.est_duration_min = round(s.est_duration_min * new / s.distance_km)
                s.distance_km = new
                return


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
    warnings: list[str] = []
    notes = [SAFETY_NOTE]

    first_monday = _monday(start_date)
    n_weeks = (_monday(goal.race_date) - first_monday).days // 7 + 1
    taper = min(TAPER_WEEKS[dclass], max(1, n_weeks // 4)) if n_weeks > 1 else 1
    if goal.race_date.weekday() <= 2 and n_weeks > taper + 1:
        taper += 1  # race early in the week: the week before must already be lighter (R5)
    phases = _phase_layout(n_weeks, taper)

    # Volumes ------------------------------------------------------------------------------
    peak_target = PEAK_KM[dclass][level]
    days = goal.days_per_week
    floor_km = max(8.0, days * MIN_EASY_KM)
    if baseline.is_empty:
        start_km = min(max(days * 4.0, 8.0), 25.0)
        warnings.append(
            "No recent running history found: the plan starts conservatively. Review it after "
            "two weeks with review_week."
        )
    else:
        start_km = max(floor_km, min(baseline.recent_weekly_km, peak_target))
        if baseline.recent_weekly_km * PROGRESSION < floor_km:
            warnings.append(
                f"Recent volume (~{_r(baseline.recent_weekly_km)} km/week) is below the "
                f"{floor_km:g} km needed to run {days} days a week: week 1 is a step up. "
                "Consider fewer running days at first."
            )
    long_cap = LONG_CAP_KM[dclass][level]
    if baseline.easy_pace_s_per_km:
        long_cap = min(long_cap, LONG_RUN_MAX_MIN[dclass] * 60 / baseline.easy_pace_s_per_km)

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

    loading_peak = start_km
    weekly: list[float] = []
    longs: list[float] = []
    recovery_flags: list[bool] = []
    last_loading = start_km / PROGRESSION  # so week 1 is at most start_km
    last_long = baseline.longest_run_km if not baseline.is_empty else 0.0
    loading_count = 0
    pre_taper = phases.count(Phase.BASE) + phases.count(Phase.BUILD) + phases.count(Phase.SPECIFIC)
    for i, phase in enumerate(phases):
        if phase is Phase.TAPER:
            t_index = i - pre_taper
            factor = TAPER_FACTORS[taper][t_index]
            km = max(floor_km * 0.6, loading_peak * factor)
            long_km = min(longs[-1] if longs else long_cap, max(0.0, km * 0.35))
            weekly.append(km)
            longs.append(long_km)
            recovery_flags.append(False)
            continue
        is_recovery = (i + 1) % RECOVERY_EVERY == 0 and i + 1 < pre_taper
        if is_recovery:
            km = last_loading * RECOVERY_FACTOR
            long_km = (longs[-1] if longs else km * 0.35) * RECOVERY_LONG_FACTOR
        else:
            km = min(peak_target, last_loading * PROGRESSION) if loading_count else start_km
            share = max(LONG_SHARE[dclass], 0.40) if km < LOW_VOLUME_KM else LONG_SHARE[dclass]
            long_km = min(long_cap, km * share, max(last_long, km * 0.25) + LONG_RUN_STEP_KM)
            if longs and long_km > (max(longs) + LONG_RUN_STEP_KM):
                long_km = max(longs) + LONG_RUN_STEP_KM
            last_loading = km
            last_long = max(last_long, long_km)
            loading_peak = max(loading_peak, km)
            loading_count += 1
        weekly.append(km)
        longs.append(long_km)
        recovery_flags.append(is_recovery)

    if loading_peak < 0.6 * peak_target and dclass in ("half", "marathon", "ultra"):
        warnings.append(
            f"Peak volume reaches about {_r(loading_peak)} km/week, below the usual "
            f"{peak_target:.0f} km/week for this goal: there is not enough time to build up "
            "safely. Treat the goal as finishing comfortably."
        )

    # Sessions ---------------------------------------------------------------------------
    run_days = _running_days(days, goal.long_run_day)
    shift = goal.long_run_day - 6
    quality_days = [(d + shift) % 7 for d in QUALITY_DAYS]
    unavailable = set(goal.unavailable_dates)
    weeks: list[PlanWeek] = []
    best_long = 0.0  # longest long run actually planned so far (R4 growth is measured on it)
    for i, phase in enumerate(phases):
        monday = first_monday + dt.timedelta(weeks=i)
        is_race_week = i == n_weeks - 1
        km = weekly[i]
        long_km = 0.0 if is_race_week else longs[i]
        q = 0 if is_race_week else _quality_count(phase, level, days, recovery_flags[i])
        q_days = [d for d in quality_days if d in run_days and d != goal.long_run_day][:q]
        long_day = goal.long_run_day
        easy_days = [d for d in run_days if d not in q_days and d != long_day]
        sessions: list[PlannedSession] = []

        if is_race_week:
            race_wd = goal.race_date.weekday()
            pre_days = [d for d in run_days if d < race_wd][-3:]
            per = max(MIN_EASY_KM, min(8.0, km / max(1, len(pre_days))))
            for d in pre_days:
                date = monday + dt.timedelta(days=d)
                sessions.append(
                    _session(
                        date,
                        SessionKind.STRIDES if d == pre_days[-1] else SessionKind.EASY,
                        per * (0.6 if d == pre_days[-1] else 1.0),
                        "Z1-Z2",
                        "Short easy run" + (" with 4x15 s strides." if d == pre_days[-1] else "."),
                        baseline.easy_pace_s_per_km,
                    )
                )
            sessions.append(
                PlannedSession(
                    date=goal.race_date,
                    kind=SessionKind.RACE,
                    distance_km=round(goal.distance_km, 3),
                    zone="race",
                    description=f"RACE: {goal.race_name or goal.race_type.value} "
                    f"({goal.distance_km:g} km)"
                    + (
                        f", goal {format_pace(goal.race_pace_s_per_km)}"
                        if goal.race_pace_s_per_km
                        else ", goal: finish"
                    ),
                )
            )
        else:
            q_km = [min(16.0, max(5.0, km * 0.18)) for _ in q_days]
            # Low volume: quality sessions turn into easy runs when the minimums do not fit.
            while q_days and (sum(q_km) + MIN_EASY_KM * (len(run_days) - len(q_days)) > km):
                easy_days.append(q_days.pop())
                q_km.pop()
            easy_days.sort()
            # Ultra specific phase: Saturday medium-long instead of one easy run (back-to-back).
            medium_long_day = None
            if (
                dclass == "ultra"
                and phase is Phase.SPECIFIC
                and not recovery_flags[i]
                and km >= LOW_VOLUME_KM
            ):
                sat = (5 + shift) % 7
                if sat in easy_days:
                    medium_long_day = sat
            if best_long and not recovery_flags[i] and phase is not Phase.TAPER:
                long_km = min(long_km, best_long + LONG_RUN_STEP_KM)
            fixed = long_km + sum(q_km)
            ml_km = long_km * 0.6 if medium_long_day is not None else 0.0
            n_easy = len(easy_days) - (1 if medium_long_day is not None else 0)
            rest_km = km - fixed - ml_km
            easy_km = rest_km / n_easy if n_easy else 0.0
            if n_easy and easy_km < MIN_EASY_KM:
                # Low volume: shrink the long run first so easy runs stay >= MIN_EASY_KM.
                easy_km = MIN_EASY_KM
                long_km = max(MIN_EASY_KM, km - sum(q_km) - ml_km - n_easy * MIN_EASY_KM)
            for d in run_days:
                date = monday + dt.timedelta(days=d)
                if d == long_day:
                    desc = "Long run, easy and conversational."
                    if dclass == "marathon" and phase is Phase.SPECIFIC and not recovery_flags[i]:
                        desc = "Long run; last 20-30 min at goal marathon effort if feeling good."
                    if dclass == "ultra":
                        desc = "Long run on similar terrain; walk the climbs, practise fuelling."
                    sessions.append(
                        _session(
                            date, SessionKind.LONG, long_km, "Z2", desc, baseline.easy_pace_s_per_km
                        )
                    )
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
                            baseline.easy_pace_s_per_km,
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
                            baseline.easy_pace_s_per_km,
                        )
                    )

        if not is_race_week:
            _fix_rounding(sessions, _r(km))

        # Drop sessions before the start date, after the race, or on unavailable dates.
        dropped = [s for s in sessions if s.date in unavailable and s.kind is not SessionKind.RACE]
        if dropped:
            warnings.append(
                f"Week {i + 1}: {len(dropped)} session(s) skipped on unavailable dates "
                f"({', '.join(sorted({s.date.isoformat() for s in dropped}))})."
            )
        sessions = [
            s
            for s in sessions
            if start_date <= s.date <= goal.race_date
            and (s.kind is SessionKind.RACE or s.date not in unavailable)
        ]
        planned_km = round(
            sum(s.distance_km for s in sessions if s.kind is not SessionKind.RACE), 1
        )
        best_long = max(
            best_long,
            max((s.distance_km for s in sessions if s.kind is SessionKind.LONG), default=0.0),
        )
        long_planned = max(
            (s.distance_km for s in sessions if s.kind is SessionKind.LONG), default=0.0
        )
        weeks.append(
            PlanWeek(
                index=i + 1,
                start_date=monday,
                phase=phase,
                is_recovery=recovery_flags[i],
                target_km=planned_km,
                long_run_km=long_planned,
                sessions=sessions,
            )
        )

    return TrainingPlan(
        goal_id=goal.id,
        start_date=start_date,
        race_date=goal.race_date,
        level=level,
        baseline=baseline,
        peak_weekly_km=max(w.target_km for w in weeks),
        weeks=weeks,
        warnings=warnings,
        notes=notes,
        attribution=GARMIN_DERIVED_ATTRIBUTION if baseline.from_garmin else None,
        engine_version=ENGINE_VERSION,
    )
