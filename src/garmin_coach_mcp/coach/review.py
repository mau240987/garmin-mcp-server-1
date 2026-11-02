"""Weekly review (C3, review_week): planned vs done, alerts, suggested changes. Deterministic."""

from __future__ import annotations

import datetime as dt
from typing import Any, Literal

from pydantic import BaseModel, Field

from garmin_coach_mcp.coach.models import Phase, SessionKind, TrainingPlan
from garmin_coach_mcp.coach.profile import RecoverySnapshot, activity_date, hard_share, is_run
from garmin_coach_mcp.coach.updates import MakeEasy, ScaleWeek

DONE_SHARE = 0.8  # >= 80% of the planned distance = done
PARTIAL_SHARE = 0.4
EASY_TOO_HARD = 0.30  # an easy session with >= 30% of time in Z3-Z5 was run too hard
LOW_ADHERENCE = 0.6
EASY_KINDS = frozenset(
    {SessionKind.EASY, SessionKind.STRIDES, SessionKind.LONG, SessionKind.MEDIUM_LONG}
)


class SessionCheck(BaseModel):
    date: dt.date
    planned_kind: SessionKind
    planned_km: float
    actual_km: float
    status: Literal["done", "partial", "missed", "upcoming", "race"]
    hard_share: float | None = Field(default=None, description="Share of time in HR Z3-Z5.")
    too_hard: bool = False


class ExtraRun(BaseModel):
    date: dt.date
    km: float


class WeekReview(BaseModel):
    plan_id: str
    plan_revision: int
    week_index: int
    week_start: dt.date
    phase: Phase
    planned_km_to_date: float
    actual_km_to_date: float
    adherence: float | None
    sessions: list[SessionCheck]
    extra_runs: list[ExtraRun]
    recovery: RecoverySnapshot | None = None
    alerts: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    suggested_changes: list[ScaleWeek | MakeEasy] = Field(
        default_factory=list,
        description="Changes for apply_plan_update; apply only after the athlete agrees.",
    )


def review_week(
    plan: TrainingPlan,
    week_start: dt.date,
    activities: list[dict[str, Any]],
    today: dt.date,
    recovery: RecoverySnapshot | None = None,
) -> WeekReview:
    week = plan.week_of(week_start)
    if week is None:
        raise ValueError(f"the plan has no week containing {week_start}")
    runs: dict[dt.date, list[dict[str, Any]]] = {}
    for a in activities:
        d = activity_date(a)
        if (
            is_run(a)
            and d is not None
            and week.start_date <= d < week.start_date + dt.timedelta(days=7)
        ):
            runs.setdefault(d, []).append(a)

    checks: list[SessionCheck] = []
    planned_to_date = actual_to_date = 0.0
    for s in week.sessions:
        day_runs = runs.get(s.date, [])
        km = round(sum(float(a.get("distance") or 0) for a in day_runs) / 1000, 1)
        shares = [h for a in day_runs if (h := hard_share(a)) is not None]
        share = round(max(shares), 2) if shares else None
        if s.kind is SessionKind.RACE:
            status: Literal["done", "partial", "missed", "upcoming", "race"] = "race"
        elif s.date > today:
            status = "upcoming"
        else:
            planned_to_date += s.distance_km
            actual_to_date += km
            ratio = km / s.distance_km if s.distance_km else 1.0
            status = (
                "done" if ratio >= DONE_SHARE else "partial" if ratio >= PARTIAL_SHARE else "missed"
            )
        too_hard = (
            s.kind in EASY_KINDS
            and s.long_variant is None  # a long run with a faster part is not "easy" (R12)
            and status in ("done", "partial")
            and share is not None
            and share >= EASY_TOO_HARD
        )
        checks.append(
            SessionCheck(
                date=s.date,
                planned_kind=s.kind,
                planned_km=s.distance_km,
                actual_km=km,
                status=status,
                hard_share=share,
                too_hard=too_hard,
            )
        )
    planned_days = {s.date for s in week.sessions}
    extra = [
        ExtraRun(date=d, km=round(sum(float(a.get("distance") or 0) for a in rs) / 1000, 1))
        for d, rs in sorted(runs.items())
        if d not in planned_days and d <= today
    ]
    actual_to_date += sum(e.km for e in extra)
    adherence = round(actual_to_date / planned_to_date, 2) if planned_to_date else None

    review = WeekReview(
        plan_id=plan.id,
        plan_revision=plan.revision,
        week_index=week.index,
        week_start=week.start_date,
        phase=week.phase,
        planned_km_to_date=round(planned_to_date, 1),
        actual_km_to_date=round(actual_to_date, 1),
        adherence=adherence,
        sessions=checks,
        extra_runs=extra,
        recovery=recovery,
    )

    hard_easy = [c for c in checks if c.too_hard]
    if hard_easy:
        review.alerts.append(
            f"{len(hard_easy)} easy session(s) were run too hard (>= 30% of the time in Z3-Z5): "
            + ", ".join(str(c.date) for c in hard_easy)
        )
        review.suggestions.append(
            "Keep easy and long runs in Z2: slow down, walk short climbs if needed."
        )
    if adherence is not None and adherence < LOW_ADHERENCE:
        review.alerts.append(f"Only {adherence:.0%} of the planned distance was run so far.")
        review.suggestions.append(
            "Do not make up missed kilometres; resume the plan and keep next week no harder."
        )
    if recovery and recovery.signals:
        review.alerts.extend(recovery.signals)

    # Volume-based suggestions only once the week is over: a few days are not a week.
    week_complete = today >= week.start_date + dt.timedelta(days=6)
    if not week_complete and adherence is not None and adherence < LOW_ADHERENCE:
        review.suggestions.append(
            "The week is not over yet: review it again after Sunday before changing next week."
        )
    nxt = next((w for w in plan.weeks if w.index == week.index + 1), None)
    if nxt is not None and nxt.phase is not Phase.TAPER:
        quality = [
            s
            for s in nxt.sessions
            if s.kind
            in (
                SessionKind.TEMPO,
                SessionKind.INTERVALS,
                SessionKind.RACE_PACE,
                SessionKind.FARTLEK,
            )
            or s.long_variant is not None
        ]
        fatigue = bool(recovery and len(recovery.signals) >= 2)
        if fatigue:
            review.suggested_changes.append(ScaleWeek(week=nxt.index, factor=0.8))
            # scale_week already turns variant long runs into easy long runs; make_easy on a
            # long run would also shorten it to an easy-run length.
            review.suggested_changes.extend(
                MakeEasy(date=s.date) for s in quality if s.kind is not SessionKind.LONG
            )
            review.suggestions.append(
                "Several recovery signals are off: reduce next week by 20% and make it all easy. "
                "If you feel ill or have pain, rest and consider seeing a doctor."
            )
        elif (
            week_complete and adherence is not None and adherence < LOW_ADHERENCE and nxt.target_km
        ):
            factor = max(0.5, min(1.0, round(actual_to_date * 1.1 / nxt.target_km, 2)))
            if factor < 1.0:
                review.suggested_changes.append(ScaleWeek(week=nxt.index, factor=factor))
    if not review.alerts:
        review.suggestions.append("On track: continue with the plan as written.")
    return review
