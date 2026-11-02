"""Athlete profile from Garmin data (C3, get_athlete_profile). Deterministic computations.

Inputs are the normalized activity dicts of the backends (SummarizedActivityDTO-like: distance
in m, duration in s, hrTimeInZone_1..5 in s) and optional recovery dicts. No LLM estimation.
"""

from __future__ import annotations

import datetime as dt
import statistics
from typing import Any

from pydantic import BaseModel, Field

from garmin_coach_mcp.coach.models import AthleteBaseline, Level
from garmin_coach_mcp.coach.vdot import MIN_EFFORT_M, VDOT_RANGE, vdot_from_race
from garmin_coach_mcp.coach.vdot import easy_pace as vdot_easy_pace
from garmin_coach_mcp.formatting import format_pace

EASY_ZONE_SHARE = 0.70  # a run is "easy" when >= 70% of its time is in HR zones 1-2
TOO_HARD_SHARE = 0.40  # a non-long run with >= 40% of its time in Z3+ is flagged
POLARIZED_TARGET = 0.75  # ~75-80% of running time should be easy (Z1-Z2)


def _num(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def is_run(activity: dict[str, Any]) -> bool:
    type_key = activity.get("activityType") or activity.get("activityTypeDTO") or {}
    key = str(type_key.get("typeKey") or "") if isinstance(type_key, dict) else ""
    # running, trail_running, treadmill_running, ..., but also ultra_run, virtual_run,
    # obstacle_run (Garmin activity type keys).
    return "run" in key.lower()


def activity_date(activity: dict[str, Any]) -> dt.date | None:
    raw = str(activity.get("startTimeLocal") or "")[:10]
    try:
        return dt.date.fromisoformat(raw)
    except ValueError:
        return None


def zone_seconds(activity: dict[str, Any]) -> list[float]:
    return [_num(activity.get(f"hrTimeInZone_{i}")) for i in range(1, 6)]


def hard_share(activity: dict[str, Any]) -> float | None:
    zones = zone_seconds(activity)
    total = sum(zones)
    return (zones[2] + zones[3] + zones[4]) / total if total > 0 else None


class WeekVolume(BaseModel):
    week_start: dt.date
    km: float
    runs: int
    longest_km: float
    complete: bool


class RecoverySnapshot(BaseModel):
    resting_hr: int | None = None
    resting_hr_7d_avg: int | None = None
    hrv_last_night: float | None = None
    hrv_weekly_avg: float | None = None
    hrv_status: str | None = None
    sleep_avg_h_7d: float | None = None
    nights_with_sleep_data: int = 0
    signals: list[str] = Field(default_factory=list)


class AthleteProfile(BaseModel):
    as_of: dt.date
    weeks: int
    baseline: AthleteBaseline
    level: Level
    weekly: list[WeekVolume]
    hr_zone_share: list[float] | None = Field(
        default=None, description="Share of running time in HR zones 1-5 (Garmin zones)."
    )
    easy_share: float | None = None
    too_hard_runs: list[dt.date] = Field(
        default_factory=list, description="Non-long runs with >= 40% of time in Z3-Z5."
    )
    recovery: RecoverySnapshot | None = None
    insights: list[str] = Field(default_factory=list)


def best_effort(runs: list[dict[str, Any]]) -> tuple[float, str] | None:
    """Highest VDOT among whole runs of at least MIN_EFFORT_M, with a description."""
    best: tuple[float, str] | None = None
    for a in runs:
        meters, seconds = _num(a.get("distance")), _num(a.get("duration"))
        if meters < MIN_EFFORT_M or seconds <= 0:
            continue
        v = vdot_from_race(meters, seconds)
        if not VDOT_RANGE[0] <= v <= VDOT_RANGE[1]:
            continue  # implausible: GPS error, wrong activity type
        if best is None or v > best[0]:
            s = round(seconds)
            when = activity_date(a)
            best = (
                v,
                f"{meters / 1000:.2f} km in {s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"
                + (f" on {when}" if when else ""),
            )
    return best


def compute_profile(
    activities: list[dict[str, Any]],
    today: dt.date,
    weeks: int,
    recovery: RecoverySnapshot | None = None,
) -> AthleteProfile:
    from garmin_coach_mcp.coach.engine import infer_level

    monday = today - dt.timedelta(days=today.weekday())
    first = monday - dt.timedelta(weeks=weeks - 1)
    runs = [
        a
        for a in activities
        if is_run(a) and (d := activity_date(a)) is not None and first <= d <= today
    ]

    weekly: list[WeekVolume] = []
    for i in range(weeks):
        start = first + dt.timedelta(weeks=i)
        week_runs = [
            a for a in runs if start <= (activity_date(a) or start) < start + dt.timedelta(days=7)
        ]
        kms = [_num(a.get("distance")) / 1000 for a in week_runs]
        weekly.append(
            WeekVolume(
                week_start=start,
                km=round(sum(kms), 1),
                runs=len(week_runs),
                longest_km=round(max(kms, default=0.0), 1),
                complete=start + dt.timedelta(days=6) < today,
            )
        )
    complete = [w for w in weekly if w.complete]
    recent = complete[-4:]
    recent_km = round(sum(w.km for w in recent) / len(recent), 1) if recent else 0.0

    easy_paces = [
        (_num(a.get("movingDuration")) or _num(a.get("duration")))
        / (_num(a.get("distance")) / 1000)
        for a in runs
        if _num(a.get("distance")) >= 2000
        and (h := hard_share(a)) is not None
        and h <= 1 - EASY_ZONE_SHARE
    ]
    hr_easy_pace = round(statistics.median(easy_paces)) if easy_paces else None
    best = best_effort(runs)
    # Performance first: the HR-zone easy pace depends on how the athlete's Garmin zones are set
    # (a low Z2 ceiling gives an unrealistically slow pace).
    easy_pace = round(vdot_easy_pace(best[0])) if best else hr_easy_pace

    longest = max((_num(a.get("distance")) / 1000 for a in runs), default=0.0)
    baseline = AthleteBaseline(
        weeks_analyzed=len(complete) if runs else 0,
        recent_weekly_km=recent_km,
        max_weekly_km=max((w.km for w in complete), default=0.0),
        longest_run_km=round(longest, 1),
        runs_per_week=round(sum(w.runs for w in complete) / len(complete), 1) if complete else 0.0,
        easy_pace_s_per_km=easy_pace,
        vdot=round(best[0], 1) if best else None,
        vdot_source=best[1] if best else None,
        from_garmin=bool(runs),
    )

    zone_totals = [sum(zone_seconds(a)[i] for a in runs) for i in range(5)]
    total = sum(zone_totals)
    zone_share = [round(z / total, 3) for z in zone_totals] if total > 0 else None
    easy_share = round(zone_share[0] + zone_share[1], 3) if zone_share else None
    too_hard = sorted(
        d
        for a in runs
        if (h := hard_share(a)) is not None
        and h >= TOO_HARD_SHARE
        and _num(a.get("distance")) / 1000 < 0.8 * max(longest, 1)
        and (d := activity_date(a)) is not None
    )

    insights: list[str] = []
    if not runs:
        insights.append(f"No runs found in the last {weeks} weeks.")
    else:
        insights.append(
            f"Average {recent_km:g} km/week over the last {len(recent)} complete week(s); "
            f"longest run {baseline.longest_run_km:g} km."
        )
    if easy_share is not None and easy_share < POLARIZED_TARGET:
        insights.append(
            f"Only {easy_share:.0%} of running time is in HR zones 1-2 (target ~75-80%): easy "
            "runs are probably too fast. Slow them down to Z2."
        )
    if len(too_hard) >= 2:
        insights.append(
            f"{len(too_hard)} shorter runs spent >= 40% of their time in Z3-Z5 "
            "(easy runs run too hard)."
        )
    if best and easy_pace is not None:
        insights.append(
            f"Performance level VDOT {best[0]:.1f} from {best[1]}: easy pace "
            f"{format_pace(easy_pace)}-{format_pace(easy_pace + 30)}."
        )
        if hr_easy_pace is not None and hr_easy_pace - easy_pace > 45:
            insights.append(
                f"Runs kept in Garmin HR zones 1-2 average {format_pace(hr_easy_pace)}, much "
                "slower than the performance-based easy pace: the Z2 ceiling of the Garmin HR "
                "zones may be set too low. The plan uses the performance-based pace."
            )
    if easy_pace is None and runs:
        insights.append("Easy pace unknown (no runs with HR zone data mostly in Z1-Z2).")
    if recovery:
        insights.extend(recovery.signals)

    return AthleteProfile(
        as_of=today,
        weeks=weeks,
        baseline=baseline,
        level=infer_level(baseline),
        weekly=weekly,
        hr_zone_share=zone_share,
        easy_share=easy_share,
        too_hard_runs=too_hard,
        recovery=recovery,
        insights=insights,
    )


def recovery_snapshot(
    summary: dict[str, Any] | None,
    hrv: dict[str, Any] | None,
    sleeps: list[dict[str, Any]],
) -> RecoverySnapshot:
    """Deterministic recovery signals from the latest day's data and the last nights' sleep."""
    snap = RecoverySnapshot()
    if summary:
        rhr, avg = summary.get("restingHeartRate"), summary.get("lastSevenDaysAvgRestingHeartRate")
        snap.resting_hr = int(rhr) if isinstance(rhr, (int, float)) else None
        snap.resting_hr_7d_avg = int(avg) if isinstance(avg, (int, float)) else None
        if (
            snap.resting_hr
            and snap.resting_hr_7d_avg
            and snap.resting_hr >= snap.resting_hr_7d_avg + 5
        ):
            snap.signals.append(
                f"Resting HR {snap.resting_hr} bpm is {snap.resting_hr - snap.resting_hr_7d_avg} "
                "bpm above its 7-day average: possible fatigue or illness."
            )
    hrv_summary = (hrv or {}).get("hrvSummary") if isinstance(hrv, dict) else None
    if isinstance(hrv_summary, dict):
        snap.hrv_last_night = _num(hrv_summary.get("lastNightAvg")) or None
        snap.hrv_weekly_avg = _num(hrv_summary.get("weeklyAvg")) or None
        status = hrv_summary.get("status")
        snap.hrv_status = status if isinstance(status, str) else None
        if snap.hrv_status and snap.hrv_status.upper() in ("LOW", "UNBALANCED", "POOR"):
            snap.signals.append(f"HRV status is {snap.hrv_status}: recovery is reduced.")
    hours = [
        _num(s.get("sleepTimeSeconds")) / 3600 for s in sleeps if _num(s.get("sleepTimeSeconds"))
    ]
    snap.nights_with_sleep_data = len(hours)
    if hours:
        snap.sleep_avg_h_7d = round(sum(hours) / len(hours), 1)
        if snap.sleep_avg_h_7d < 6.5:
            snap.signals.append(
                f"Average sleep {snap.sleep_avg_h_7d:g} h over the last {len(hours)} night(s): "
                "below 6.5 h."
            )
    return snap
