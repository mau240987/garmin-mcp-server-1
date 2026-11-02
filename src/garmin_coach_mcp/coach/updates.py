"""Plan updates (C3, apply_plan_update): typed, bounded operations with revision history.

Safety rules: past sessions and the race are immutable; a week can only be scaled DOWN (load
increases go through regenerate_from, i.e. the engine's progression rules); hard sessions are
never moved next to another hard session or into the protected days before the race.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from garmin_coach_mcp.coach.engine import (
    EASY_MAX_KM,
    RACE_PROTECT_DAYS,
    _distance_class,
    _r,
    build_plan,
)
from garmin_coach_mcp.coach.models import (
    AthleteBaseline,
    Phase,
    PlannedSession,
    PlanRevision,
    PlanWeek,
    RaceGoal,
    SessionKind,
    TrainingPlan,
)
from garmin_coach_mcp.errors import InvalidRequestError
from garmin_coach_mcp.formatting import format_pace

HARD_KINDS = frozenset(
    {
        SessionKind.LONG,
        SessionKind.MEDIUM_LONG,
        SessionKind.TEMPO,
        SessionKind.INTERVALS,
        SessionKind.RACE_PACE,
        SessionKind.FARTLEK,
    }
)
MIN_SESSION_KM = 2.0


class ScaleWeek(BaseModel):
    op: Literal["scale_week"] = "scale_week"
    week: int = Field(ge=1, le=60, description="Plan week index.")
    factor: float = Field(ge=0.3, le=1.0, description="Scale remaining sessions (down only).")


class SkipSession(BaseModel):
    op: Literal["skip_session"] = "skip_session"
    date: dt.date


class MoveSession(BaseModel):
    op: Literal["move_session"] = "move_session"
    date: dt.date
    to_date: dt.date


class MakeEasy(BaseModel):
    op: Literal["make_easy"] = "make_easy"
    date: dt.date


class RegenerateFrom(BaseModel):
    op: Literal["regenerate_from"] = "regenerate_from"
    date: dt.date = Field(description="Rebuild the plan from this date with a fresh baseline.")


PlanChange = Annotated[
    ScaleWeek | SkipSession | MoveSession | MakeEasy | RegenerateFrom,
    Field(discriminator="op"),
]


def _bad(message: str, remediation: str) -> InvalidRequestError:
    return InvalidRequestError(message, remediation=remediation)


def _find(plan: TrainingPlan, day: dt.date) -> tuple[PlanWeek, int]:
    week = plan.week_of(day)
    if week is not None:
        for i, s in enumerate(week.sessions):
            if s.date == day:
                return week, i
    raise _bad(f"No planned session on {day}.", "Check the plan dates (export_plan or the plan).")


def _editable(session: PlannedSession, today: dt.date) -> None:
    if session.kind is SessionKind.RACE:
        raise _bad("The race cannot be changed.", "Create a new goal for a different race date.")
    if session.date < today:
        raise _bad(f"{session.date} is in the past.", "Only future sessions can be changed.")


def _refresh(week: PlanWeek) -> None:
    week.sessions.sort(key=lambda s: s.date)
    week.target_km = round(
        sum(s.distance_km for s in week.sessions if s.kind is not SessionKind.RACE), 1
    )
    week.long_run_km = max(
        (s.distance_km for s in week.sessions if s.kind is SessionKind.LONG), default=0.0
    )


def _retire(plan: TrainingPlan, session: PlannedSession) -> None:
    if session.garmin_workout_id is not None:
        plan.stale_garmin_workout_ids.append(session.garmin_workout_id)
        session.garmin_workout_id = None
    session.garmin_scheduled = False
    session.garmin_push_pending = False


def _cancel_date(plan: TrainingPlan, day: dt.date) -> None:
    """Remember a date that lost its session, so calendar exports can cancel the event."""
    if day not in plan.cancelled_dates:
        plan.cancelled_dates.append(day)


def baseline_from_plan(plan: TrainingPlan, before: dt.date) -> AthleteBaseline:
    """Baseline from the plan's own weeks before `before` (fallback when Garmin is down)."""
    kept = [w for w in plan.weeks if w.start_date < before and w.phase is not Phase.TAPER]
    loading = [w for w in kept if not w.is_recovery][-4:]
    if not loading:
        return plan.baseline
    return plan.baseline.model_copy(
        update={
            "recent_weekly_km": round(sum(w.target_km for w in loading) / len(loading), 1),
            "longest_run_km": max(w.long_run_km for w in kept),
            "weeks_analyzed": max(plan.baseline.weeks_analyzed, len(kept)),
        }
    )


def apply_changes(
    plan: TrainingPlan,
    goal: RaceGoal,
    changes: list[ScaleWeek | SkipSession | MoveSession | MakeEasy | RegenerateFrom],
    *,
    today: dt.date,
    now: dt.datetime,
    baseline: AthleteBaseline | None = None,
) -> tuple[TrainingPlan, list[str]]:
    """Return an updated copy of `plan` and a human summary of each applied change."""
    if not changes:
        raise _bad("No changes given.", "Pass at least one change.")
    new = plan.model_copy(deep=True)
    dclass = _distance_class(goal.distance_km)
    protect_from = goal.race_date - dt.timedelta(days=RACE_PROTECT_DAYS[dclass])
    done: list[str] = []

    for change in changes:
        if isinstance(change, ScaleWeek):
            week = next((w for w in new.weeks if w.index == change.week), None)
            if week is None:
                raise _bad(f"Week {change.week} does not exist.", f"Use 1-{len(new.weeks)}.")
            scaled = 0
            for s in week.sessions:
                if s.kind is SessionKind.RACE or s.date < today:
                    continue
                new_km = max(MIN_SESSION_KM, _r(s.distance_km * change.factor))
                if new_km != s.distance_km:
                    if s.est_duration_min and s.distance_km:
                        s.est_duration_min = round(s.est_duration_min * new_km / s.distance_km)
                    s.distance_km = new_km
                    _retire(new, s)
                    scaled += 1
            _refresh(week)
            done.append(f"Week {week.index}: {scaled} session(s) scaled by {change.factor:g}.")
        elif isinstance(change, SkipSession):
            week, i = _find(new, change.date)
            _editable(week.sessions[i], today)
            _retire(new, week.sessions[i])
            removed = week.sessions.pop(i)
            _cancel_date(new, removed.date)
            _refresh(week)
            done.append(f"{change.date}: {removed.kind.value} {removed.distance_km:g} km skipped.")
        elif isinstance(change, MoveSession):
            week, i = _find(new, change.date)
            s = week.sessions[i]
            _editable(s, today)
            target_week = new.week_of(change.to_date)
            if target_week is not week:
                raise _bad(
                    "Sessions can only move within the same week.", "Pick a day in that week."
                )
            if change.to_date < today or change.to_date >= goal.race_date:
                raise _bad(
                    f"{change.to_date} is not a valid day.", "Pick a future day before the race."
                )
            if change.to_date in goal.unavailable_dates:
                raise _bad(
                    f"{change.to_date} is marked unavailable in the goal.",
                    "Pick another day, or update the goal's unavailable dates.",
                )
            if any(o.date == change.to_date for o in week.sessions):
                raise _bad(
                    f"{change.to_date} already has a session.", "Skip or move that one first."
                )
            if s.kind in HARD_KINDS:
                if change.to_date >= protect_from:
                    raise _bad(
                        "A long or hard session cannot move that close to the race.",
                        "Make it easy (make_easy) or skip it.",
                    )
                neighbours = {
                    change.to_date - dt.timedelta(days=1),
                    change.to_date + dt.timedelta(days=1),
                }
                if any(
                    o.date in neighbours and o.kind in HARD_KINDS and o is not s
                    for w in new.weeks
                    for o in w.sessions
                ):
                    raise _bad(
                        "That would put two hard sessions on consecutive days.",
                        "Pick a day with an easy day (or rest) on both sides.",
                    )
            _retire(new, s)
            _cancel_date(new, s.date)
            s.date = change.to_date
            if change.to_date in new.cancelled_dates:
                new.cancelled_dates.remove(change.to_date)
            _refresh(week)
            done.append(f"{change.date} → {change.to_date}: {s.kind.value} moved.")
        elif isinstance(change, MakeEasy):
            week, i = _find(new, change.date)
            s = week.sessions[i]
            _editable(s, today)
            _retire(new, s)
            # A long run keeps its distance and loses only its faster part (R12).
            long_run = s.kind is SessionKind.LONG
            week.sessions[i] = PlannedSession(
                date=s.date,
                kind=SessionKind.LONG if long_run else SessionKind.EASY,
                distance_km=s.distance_km if long_run else min(s.distance_km, EASY_MAX_KM[dclass]),
                zone="Z2",
                description="Long run, easy and conversational."
                if long_run
                else "Easy run, conversational pace (Z2).",
                pace_hint=s.pace_hint
                or (
                    f"{format_pace(new.easy_pace)} - {format_pace(new.easy_pace + 30)} (easy)"
                    if new.easy_pace
                    else None
                ),
            )
            _refresh(week)
            done.append(f"{change.date}: {s.kind.value} turned into an easy run.")
        else:  # RegenerateFrom
            if change.date < today or change.date >= goal.race_date:
                raise _bad(
                    f"{change.date} is not a valid day.", "Pick a future day before the race."
                )
            # Regenerate from the next Monday: the current week stays as planned, so no week is
            # spliced from two plans (which could break the 10% rule).
            cut = change.date + dt.timedelta(days=(7 - change.date.weekday()) % 7)
            if cut >= goal.race_date:
                raise _bad(
                    "There is no full week left to regenerate before the race.",
                    "Adjust single sessions with make_easy or skip_session instead.",
                )
            seed = baseline or baseline_from_plan(new, cut)
            had_base = any(w.phase is Phase.BASE for w in new.weeks if w.start_date < cut)
            fresh = build_plan(goal, seed, cut, include_base=not had_base)
            kept = [w for w in new.weeks if w.start_date < cut]
            for w in new.weeks:
                if w.start_date >= cut:
                    for s in w.sessions:
                        _retire(new, s)
            new_dates = {s.date for w in fresh.weeks for s in w.sessions}
            for w in new.weeks:
                if w.start_date >= cut:
                    for s in w.sessions:
                        if s.date not in new_dates:
                            _cancel_date(new, s.date)
            for offset, w in enumerate(fresh.weeks):
                w.index = len(kept) + offset + 1
            new.weeks = kept + fresh.weeks
            new.baseline = seed
            new.warnings = fresh.warnings
            new.peak_weekly_km = max(w.target_km for w in new.weeks)
            new.engine_version = fresh.engine_version
            source = "Garmin history" if baseline is not None else "the plan's own volume"
            done.append(f"Plan regenerated from {cut} using {source}.")

    new.revision = plan.revision + 1
    new.history = [
        *plan.history,
        PlanRevision(revision=new.revision, at=now, summary="; ".join(done)),
    ]
    return new, done
