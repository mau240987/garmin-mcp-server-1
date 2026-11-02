"""Race Coach MCP tools (C3 + C4). Registered by `server.build_server`.

Workflow: set_race_goal -> get_athlete_profile -> generate_training_plan -> (export_plan |
push_plan_to_garmin) -> review_week -> apply_plan_update. Coaching logic is deterministic
(coach/*); this module only wires Garmin data, storage and MCP.
"""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel, BeforeValidator, Field

from garmin_coach_mcp.backends import GARMIN_DERIVED_ATTRIBUTION, BackendRouter, Capability
from garmin_coach_mcp.coach.engine import build_plan
from garmin_coach_mcp.coach.export import select_sessions, to_fit_zip, to_ics, to_markdown
from garmin_coach_mcp.coach.models import (
    AthleteBaseline,
    Level,
    RaceGoal,
    RaceType,
    SessionKind,
    TrainingPlan,
    parse_hms,
)
from garmin_coach_mcp.coach.profile import (
    AthleteProfile,
    RecoverySnapshot,
    compute_profile,
    recovery_snapshot,
)
from garmin_coach_mcp.coach.render import plan_summary
from garmin_coach_mcp.coach.review import WeekReview, review_week
from garmin_coach_mcp.coach.sessions import session_steps, workout_name
from garmin_coach_mcp.coach.storage import CoachStore
from garmin_coach_mcp.coach.updates import PlanChange, RegenerateFrom, apply_changes
from garmin_coach_mcp.errors import (
    ConfigurationError,
    GarminMCPError,
    InvalidRequestError,
    NotFoundError,
    UpstreamServiceError,
)
from garmin_coach_mcp.workouts import build_running_workout

MAX_GOAL_DAYS_AHEAD = 730
INLINE_EXPORT_LIMIT = 60_000
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

LOCAL_WRITE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
)
READ = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True
)
GARMIN_WRITE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
)

GoalId = Annotated[
    str | None,
    Field(description="Goal id (goal_...). Omit to use the most recent goal.", max_length=20),
]
PlanId = Annotated[
    str | None,
    Field(
        description="Plan id (plan_...). Omit to use the latest plan of the latest goal.",
        max_length=20,
    ),
]


def _weekday(value: Any) -> Any:
    if isinstance(value, str) and value.strip().lower() in WEEKDAYS:
        return WEEKDAYS.index(value.strip().lower())
    return value


# ------------------------------------------------------------------------------ outputs


class GoalResult(BaseModel):
    goal: RaceGoal
    text: str


class ProfileResult(BaseModel):
    profile: AthleteProfile
    attribution: str | None
    text: str


class PlanResult(BaseModel):
    plan_id: str
    goal_id: str
    revision: int
    plan: TrainingPlan
    text: str


class ReviewResult(BaseModel):
    review: WeekReview
    attribution: str | None
    text: str


class UpdateResult(BaseModel):
    plan_id: str
    revision: int
    applied: list[str]
    saved: bool
    garmin_out_of_sync: int = Field(
        description="Pushed Garmin workouts made obsolete; push_plan_to_garmin replaces them."
    )
    text: str


#: Last line of a dry run when no backend may write workouts. garmin-coach-push replaces it,
#: because its dry runs disable writes on purpose (they are enabled only with --apply).
WRITES_UNAVAILABLE_NOTE = (
    "Garmin writes are not available in this configuration; use export_plan "
    "format=fit instead (see the error of a real push for how to enable them)."
)


class PushEntry(BaseModel):
    date: dt.date
    name: str
    workout_id: int | None = None
    ok: bool
    error: str | None = None


class PushResult(BaseModel):
    plan_id: str
    dry_run: bool
    to_create: int
    to_schedule: int = Field(
        default=0, description="Already-created workouts whose scheduling failed earlier."
    )
    to_delete: int
    uncertain: list[dt.date] = Field(
        default_factory=list,
        description="Sessions whose create was interrupted: check Garmin Connect, then retry "
        "with retry_uncertain=true if the workout is not there.",
    )
    created: list[PushEntry] = Field(default_factory=list)
    deleted: list[int] = Field(default_factory=list)
    text: str


class ExportResult(BaseModel):
    plan_id: str
    format: Literal["md", "ics", "fit"]
    path: str
    bytes: int
    sessions: int
    content: str | None = Field(
        default=None, description="Inline text for md/ics when small enough."
    )
    text: str


# ---------------------------------------------------------------------------- register


def register_coach_tools(
    app: MCPServer,
    router: BackendRouter,
    store: CoachStore,
    *,
    read_only: bool,
    today: Callable[[], dt.date] = dt.date.today,
    now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    def fail(exc: GarminMCPError) -> Exception:
        return exc.to_tool_error()

    def load_goal(goal_id: str | None) -> RaceGoal:
        if goal_id:
            return store.get_goal(goal_id)
        goals = store.list_goals()
        if not goals:
            raise NotFoundError("No race goal saved yet.", remediation="Call set_race_goal first.")
        return goals[-1]

    def load_plan(plan_id: str | None) -> tuple[TrainingPlan, RaceGoal]:
        if plan_id:
            plan = store.get_plan(plan_id)
            return plan, store.get_goal(plan.goal_id)
        goal = load_goal(None)
        plans = store.list_plans(goal.id)
        if not plans:
            raise NotFoundError(
                f"No plan for the latest goal ({goal.id}).",
                remediation="Call generate_training_plan first.",
            )
        return plans[-1], goal

    def fetch_recovery(day: dt.date) -> tuple[RecoverySnapshot | None, list[str]]:
        notes: list[str] = []

        def safe(cap: Capability, fn: Callable[[Any], Any]) -> Any:
            try:
                return fn(router.select(cap))
            except GarminMCPError as exc:
                notes.append(f"{cap.value} unavailable: {exc.message}")
                return None

        summary = safe(Capability.DAILY_SUMMARY, lambda b: b.daily_summary(day))
        hrv = safe(Capability.HRV, lambda b: b.hrv(day))
        sleeps = []
        for offset in range(7):
            night = day - dt.timedelta(days=offset)

            def read_sleep(b: Any, night: dt.date = night) -> Any:
                return b.sleep(night)

            s = safe(Capability.SLEEP, read_sleep)
            if s:
                sleeps.append(s)
            if s is None and notes and notes[-1].startswith("health.sleep"):
                break  # do not repeat a failing call 7 times
        if summary is None and hrv is None and not sleeps:
            return None, notes
        return recovery_snapshot(summary, hrv, sleeps), notes

    def build_profile(weeks: int, with_recovery: bool) -> tuple[AthleteProfile, list[str]]:
        day = today()
        first = day - dt.timedelta(days=day.weekday()) - dt.timedelta(weeks=weeks - 1)
        acts = router.select(Capability.ACTIVITIES_BY_DATE).activities_between(first, day)
        recovery, notes = fetch_recovery(day) if with_recovery else (None, [])
        return compute_profile(acts, day, weeks, recovery), notes

    def supersede_older_plans(goal_id: str, plan: TrainingPlan, day: dt.date) -> list[TrainingPlan]:
        """The new plan replaces older ones of the same goal: their future Garmin workouts
        become stale in the new plan (removed on the next push), so nothing is duplicated."""
        changed: list[TrainingPlan] = []
        for old in store.list_plans(goal_id):
            if old.id == plan.id or old.superseded_by is not None:
                continue
            plan.stale_garmin_workout_ids.extend(old.stale_garmin_workout_ids)
            old.stale_garmin_workout_ids = []
            for w in old.weeks:
                for sess in w.sessions:
                    if sess.garmin_workout_id is not None and sess.date >= day:
                        plan.stale_garmin_workout_ids.append(sess.garmin_workout_id)
                        sess.garmin_workout_id = None
                        sess.garmin_scheduled = False
            old.superseded_by = plan.id
            changed.append(old)
        return changed

    # -- 1. set_race_goal -----------------------------------------------------------------
    @app.tool(
        name="set_race_goal",
        title="Set the race goal",
        description=(
            "Save the athlete's race goal (race type, date, optional target time, running days "
            "per week, long-run day, injuries/constraints, unavailable dates). Ask only what is "
            "missing; sensible defaults: finish goal, long run on Sunday. Stored locally."
        ),
        annotations=LOCAL_WRITE,
        structured_output=True,
    )
    def set_race_goal(
        race_type: RaceType,
        race_date: Annotated[dt.date, Field(description="Race day, YYYY-MM-DD.")],
        days_per_week: Annotated[int, Field(ge=2, le=7, description="Running days per week.")],
        distance_km: Annotated[
            float | None, Field(gt=0, le=400, description="Required for ultra/other.")
        ] = None,
        target_time: Annotated[
            str | None,
            Field(description="Goal finish time H:MM:SS; omit for 'just finish'.", max_length=9),
        ] = None,
        race_name: Annotated[str | None, Field(max_length=120)] = None,
        long_run_day: Annotated[
            int,
            BeforeValidator(_weekday),
            Field(ge=0, le=6, description="0=Monday..6=Sunday (or a weekday name)."),
        ] = 6,
        level: Level | None = None,
        constraints: Annotated[
            list[str] | None, Field(description="Injuries, travel, notes.", max_length=20)
        ] = None,
        unavailable_dates: Annotated[list[dt.date] | None, Field(max_length=366)] = None,
    ) -> GoalResult:
        day = today()
        if not day < race_date <= day + dt.timedelta(days=MAX_GOAL_DAYS_AHEAD):
            raise fail(
                InvalidRequestError(
                    f"race_date {race_date} must be after today ({day}) and within two years.",
                    remediation="Check the race date.",
                )
            )
        try:
            goal = RaceGoal(
                race_type=race_type,
                distance_km=distance_km,  # type: ignore[arg-type]  # defaulted by validator
                race_date=race_date,
                target_time_s=parse_hms(target_time) if target_time else None,
                days_per_week=days_per_week,
                race_name=race_name,
                long_run_day=long_run_day,
                level=level,
                constraints=constraints or [],
                unavailable_dates=unavailable_dates or [],
            )
        except ValueError as exc:
            raise fail(InvalidRequestError(str(exc), remediation="Fix the goal fields.")) from exc
        try:
            store.save_goal(goal)
        except GarminMCPError as exc:
            raise fail(exc) from exc
        text = (
            f"Goal saved ({goal.id}): {goal.race_name or goal.race_type.value} "
            f"{goal.distance_km:g} km on {goal.race_date}, {goal.days_per_week} days/week."
        )
        return GoalResult(goal=goal, text=text)

    # -- 2. get_athlete_profile -------------------------------------------------------------
    @app.tool(
        name="get_athlete_profile",
        title="Athlete profile from Garmin",
        description=(
            "Deterministic summary of the athlete's recent running from Garmin: weekly volume, "
            "longest run, runs per week, HR-zone distribution (flags easy runs run too hard), "
            "easy pace, and recovery signals (resting HR, HRV, sleep). Use it before planning."
        ),
        annotations=READ,
        structured_output=True,
    )
    def get_athlete_profile(
        weeks: Annotated[int, Field(ge=4, le=52, description="Weeks of history.")] = 12,
        include_recovery: bool = True,
    ) -> ProfileResult:
        try:
            profile, notes = build_profile(weeks, include_recovery)
        except GarminMCPError as exc:
            raise fail(exc) from exc
        attribution = GARMIN_DERIVED_ATTRIBUTION if profile.baseline.from_garmin else None
        b = profile.baseline
        lines = [f"Athlete profile ({weeks} weeks to {profile.as_of})"]
        if attribution:
            lines.append(attribution)
        lines += [
            f"Level: {profile.level.value} · {b.recent_weekly_km:g} km/week recent · max "
            f"{b.max_weekly_km:g} · longest run {b.longest_run_km:g} km · "
            f"{b.runs_per_week:g} runs/week",
            *[f"- {i}" for i in profile.insights],
            *[f"- note: {n}" for n in notes],
        ]
        return ProfileResult(profile=profile, attribution=attribution, text="\n".join(lines))

    # -- 3. generate_training_plan ----------------------------------------------------------
    @app.tool(
        name="generate_training_plan",
        title="Generate the training plan",
        description=(
            "Build a phased plan (base, build, specific, taper) to the race with the deterministic "
            "engine: <=10% weekly progression, recovery weeks, long-run caps, taper. Uses the "
            "Garmin history as baseline. Saves it locally; nothing is sent to Garmin."
        ),
        annotations=LOCAL_WRITE,
        structured_output=True,
    )
    def generate_training_plan(
        goal_id: GoalId = None,
        start_date: Annotated[
            dt.date | None, Field(description="First plan day; default today.")
        ] = None,
        use_garmin_history: Annotated[
            bool, Field(description="false = plan without history (conservative start).")
        ] = True,
    ) -> PlanResult:
        day = today()
        start = start_date or day
        try:
            goal = load_goal(goal_id)
            if start < day:
                raise InvalidRequestError("start_date is in the past.", remediation="Omit it.")
            baseline = (
                build_profile(12, with_recovery=False)[0].baseline
                if use_garmin_history
                else AthleteBaseline()
            )
            try:
                plan = build_plan(goal, baseline, start)
            except ValueError as exc:
                raise InvalidRequestError(str(exc), remediation="Check the goal dates.") from exc
            superseded = supersede_older_plans(goal.id, plan, day)
            store.save_plan(plan)
            for old in superseded:
                store.save_plan(old)
        except GarminMCPError as exc:
            raise fail(exc) from exc
        if plan.stale_garmin_workout_ids:
            plan.warnings.append(
                f"{len(plan.stale_garmin_workout_ids)} workout(s) pushed from the previous plan "
                "will be removed from Garmin by the next push_plan_to_garmin."
            )
        return PlanResult(
            plan_id=plan.id,
            goal_id=goal.id,
            revision=plan.revision,
            plan=plan,
            text=plan_summary(plan, goal),
        )

    # -- 4. review_week ---------------------------------------------------------------------
    @app.tool(
        name="review_week",
        title="Review a training week",
        description=(
            "Compare a plan week with what was actually run (Garmin activities): sessions done, "
            "partial or missed, easy runs run too hard (HR zones), recovery signals. Returns "
            "alerts and suggested_changes for apply_plan_update (apply only if the athlete agrees)."
        ),
        annotations=READ,
        structured_output=True,
    )
    def review_week_tool(
        week_start: Annotated[
            dt.date | None, Field(description="Any day of the week to review; default this week.")
        ] = None,
        plan_id: PlanId = None,
    ) -> ReviewResult:
        day = today()
        target = week_start or day
        try:
            plan, _goal = load_plan(plan_id)
            week = plan.week_of(target)
            if week is None:
                raise InvalidRequestError(
                    f"The plan has no week containing {target}.",
                    remediation=f"Use a date between {plan.weeks[0].start_date} and the race.",
                )
            end = min(day, week.start_date + dt.timedelta(days=6))
            acts = (
                router.select(Capability.ACTIVITIES_BY_DATE).activities_between(
                    week.start_date, end
                )
                if end >= week.start_date
                else []
            )
            recovery, notes = fetch_recovery(end) if end >= week.start_date else (None, [])
        except GarminMCPError as exc:
            raise fail(exc) from exc
        review = review_week(plan, target, acts, day, recovery)
        lines = [
            f"Week {review.week_index} ({review.week_start}, {review.phase.value}): "
            f"{review.actual_km_to_date:g}/{review.planned_km_to_date:g} km to date",
            GARMIN_DERIVED_ATTRIBUTION,
            *[
                f"- {c.date} {c.planned_kind.value} {c.planned_km:g} km: {c.status}"
                + (f", ran {c.actual_km:g} km" if c.actual_km else "")
                + (" (too hard)" if c.too_hard else "")
                for c in review.sessions
            ],
            *[f"! {a}" for a in review.alerts],
            *[f"> {s}" for s in review.suggestions],
            *[f"- note: {n}" for n in notes],
        ]
        return ReviewResult(
            review=review, attribution=GARMIN_DERIVED_ATTRIBUTION, text="\n".join(lines)
        )

    # -- 5. apply_plan_update ---------------------------------------------------------------
    @app.tool(
        name="apply_plan_update",
        title="Update the plan",
        description=(
            "Apply changes to the saved plan: scale_week (down only), skip_session, move_session "
            "(same week, hard sessions never adjacent or close to the race), make_easy, "
            "regenerate_from (rebuild from a date with the current Garmin baseline). Past "
            "sessions and the race cannot change. Confirm with the athlete first; dry_run=true "
            "previews. Pushed Garmin workouts are replaced by the next push_plan_to_garmin."
        ),
        annotations=LOCAL_WRITE,
        structured_output=True,
    )
    def apply_plan_update(
        changes: Annotated[list[PlanChange], Field(min_length=1, max_length=20)],
        plan_id: PlanId = None,
        dry_run: bool = False,
        expected_revision: Annotated[
            int | None,
            Field(
                ge=1,
                description="Plan revision the changes were based on (e.g. from "
                "review_week); the update is refused if the plan changed since.",
            ),
        ] = None,
    ) -> UpdateResult:
        try:
            plan, goal = load_plan(plan_id)
            if expected_revision is not None and expected_revision != plan.revision:
                raise InvalidRequestError(
                    f"The plan is at revision {plan.revision}, not {expected_revision}: it "
                    "changed since these changes were prepared. Nothing was applied.",
                    remediation="Review the current plan (or run review_week again) and retry.",
                )
            if plan.superseded_by:
                raise InvalidRequestError(
                    f"Plan {plan.id} was replaced by {plan.superseded_by}.",
                    remediation="Update the current plan instead (omit plan_id).",
                )
            baseline = None
            if any(isinstance(c, RegenerateFrom) for c in changes):
                try:
                    baseline = build_profile(12, with_recovery=False)[0].baseline
                except GarminMCPError:
                    baseline = None  # fall back to the plan's own volume (see updates.py)
            new, applied = apply_changes(
                plan, goal, list(changes), today=today(), now=now(), baseline=baseline
            )
            if not dry_run:
                store.save_plan(new)
        except GarminMCPError as exc:
            raise fail(exc) from exc
        stale = len(new.stale_garmin_workout_ids)
        lines = [
            f"{'Preview' if dry_run else 'Saved'}: plan {new.id} revision {new.revision}",
            *[f"- {a}" for a in applied],
        ]
        if baseline is None and any(isinstance(c, RegenerateFrom) for c in changes):
            lines.append(
                "- note: Garmin history unavailable; regenerated from the plan's own volume."
            )
        if stale:
            lines.append(
                f"- {stale} Garmin workout(s) are now out of date: run push_plan_to_garmin."
            )
        return UpdateResult(
            plan_id=new.id,
            revision=new.revision,
            applied=applied,
            saved=not dry_run,
            garmin_out_of_sync=stale,
            text="\n".join(lines),
        )

    # -- 6. export_plan ---------------------------------------------------------------------
    @app.tool(
        name="export_plan",
        title="Export the plan",
        description=(
            "Export the plan to a file: md (readable), ics (calendar), fit (zip of Garmin FIT "
            "workout files, importable without API access). Works without any Garmin write "
            "permission. Returns the file path (and inline text for md/ics)."
        ),
        annotations=LOCAL_WRITE,
        structured_output=True,
    )
    def export_plan(
        format: Literal["md", "ics", "fit"],
        plan_id: PlanId = None,
        from_date: dt.date | None = None,
        to_date: dt.date | None = None,
    ) -> ExportResult:
        try:
            plan, goal = load_plan(plan_id)
            if from_date and to_date and from_date > to_date:
                raise InvalidRequestError(
                    "from_date is after to_date.", remediation="Swap or fix the dates."
                )
        except GarminMCPError as exc:
            raise fail(exc) from exc
        sessions = select_sessions(plan, from_date, to_date)
        content: str | None = None
        if format == "md":
            payload = to_markdown(plan, goal).encode()
            content = payload.decode()
        elif format == "ics":
            payload = to_ics(plan, goal, from_date, to_date, stamp_at=now()).encode()
            content = payload.decode()
        else:
            payload, _count = to_fit_zip(plan, goal, from_date, to_date)
        ext = "zip" if format == "fit" else format
        out_dir = store.root / "exports"
        path: Path = out_dir / f"{plan.id}-r{plan.revision}.{ext}"
        try:
            out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.write_bytes(payload)
            path.chmod(0o600)
        except OSError as exc:
            raise fail(
                ConfigurationError(
                    f"Cannot write the export file {path}.",
                    remediation="Check free space and permissions of GARMIN_COACH_DATA_DIR.",
                    cause=exc,
                )
            ) from exc
        if content is not None and len(content) > INLINE_EXPORT_LIMIT:
            content = None
        return ExportResult(
            plan_id=plan.id,
            format=format,
            path=str(path),
            bytes=len(payload),
            sessions=len(sessions),
            content=content,
            text=f"Exported {len(sessions)} session(s) to {path}"
            + (f" — {plan.attribution}" if plan.attribution else ""),
        )

    if read_only:
        return

    # -- 7. push_plan_to_garmin -------------------------------------------------------------
    @app.tool(
        name="push_plan_to_garmin",
        title="Send plan workouts to Garmin",
        description=(
            "Create and schedule the plan's next sessions as structured Garmin workouts (pace "
            "targets when known). Defaults to dry_run=true: show the list to the athlete, then "
            "call again with dry_run=false, expected_count = workouts to send (create + "
            "re-schedule) and expected_delete_count = obsolete workouts to delete. Needs "
            "Garmin write access (off by default); otherwise use export_plan format=fit."
        ),
        annotations=GARMIN_WRITE,
        structured_output=True,
    )
    def push_plan_to_garmin(
        plan_id: PlanId = None,
        from_date: dt.date | None = None,
        weeks: Annotated[int, Field(ge=1, le=4, description="How many weeks to send.")] = 2,
        dry_run: bool = True,
        expected_count: Annotated[int | None, Field(ge=0, le=60)] = None,
        expected_delete_count: Annotated[int | None, Field(ge=0, le=500)] = None,
        retry_uncertain: Annotated[
            bool,
            Field(
                description="Re-create sessions whose earlier create was interrupted, after "
                "checking in Garmin Connect that the workout does not exist."
            ),
        ] = False,
    ) -> PushResult:
        day = today()
        start = max(from_date or day, day)
        end = start + dt.timedelta(days=7 * weeks - 1)
        try:
            plan, goal = load_plan(plan_id)
            if plan.superseded_by:
                raise InvalidRequestError(
                    f"Plan {plan.id} was replaced by {plan.superseded_by}.",
                    remediation="Push the current plan (omit plan_id).",
                )
        except GarminMCPError as exc:
            raise fail(exc) from exc
        window = [
            (w, s) for w, s in select_sessions(plan, start, end) if s.kind is not SessionKind.RACE
        ]
        uncertain = [s for _w, s in window if s.garmin_push_pending]
        create = [
            (w, s)
            for w, s in window
            if s.garmin_workout_id is None and (not s.garmin_push_pending or retry_uncertain)
        ]
        schedule = [
            (w, s) for w, s in window if s.garmin_workout_id is not None and not s.garmin_scheduled
        ]
        stale = list(plan.stale_garmin_workout_ids)
        to_send = len(create) + len(schedule)
        listing = [f"- create {s.date} {workout_name(s, w)}" for w, s in create]
        listing += [
            f"- schedule {s.date} {workout_name(s, w)} (already created)" for w, s in schedule
        ]
        listing += [
            f"- UNCERTAIN {s.date}: an earlier create was interrupted; check Garmin Connect "
            "for this workout before retrying with retry_uncertain=true"
            for s in uncertain
            if not retry_uncertain
        ]
        base = PushResult(
            plan_id=plan.id,
            dry_run=dry_run,
            to_create=len(create),
            to_schedule=len(schedule),
            to_delete=len(stale),
            uncertain=[s.date for s in uncertain],
            text="",
        )
        if dry_run:
            can_write = router.can(Capability.WORKOUT_CREATE)
            base.text = "\n".join(
                [
                    f"Dry run: {to_send} workout(s) to send ({len(create)} create, "
                    f"{len(schedule)} schedule), {len(stale)} obsolete to delete."
                ]
                + listing
                + ([] if can_write else [WRITES_UNAVAILABLE_NOTE])
            )
            return base
        if expected_count != to_send or (stale and expected_delete_count != len(stale)):
            raise fail(
                InvalidRequestError(
                    f"This push would send {to_send} workout(s) and delete {len(stale)}; got "
                    f"expected_count={expected_count}, expected_delete_count="
                    f"{expected_delete_count}. Nothing was sent.",
                    remediation="Run with dry_run=true, confirm with the athlete, then pass the "
                    "listed counts.",
                )
            )
        try:
            writer = router.select(Capability.WORKOUT_CREATE, Capability.WORKOUT_SCHEDULE)
            deleter = router.select(Capability.WORKOUT_DELETE) if stale else None
            result = base
            for wid in stale:
                try:
                    if deleter is not None:
                        deleter.delete_workout(wid)
                    result.deleted.append(wid)
                except NotFoundError:
                    result.deleted.append(wid)  # already gone
                except GarminMCPError:
                    continue
            plan.stale_garmin_workout_ids = [w for w in stale if w not in result.deleted]
            store.save_plan(plan)

            jobs = [(w, s, True) for w, s in create] + [(w, s, False) for w, s in schedule]
            for i, (week_index, s, needs_create) in enumerate(jobs):
                name = workout_name(s, week_index)
                try:
                    if needs_create:
                        steps = session_steps(s, plan.easy_pace, goal.race_pace_s_per_km)
                        minutes = s.est_duration_min or max(20, round(s.distance_km * 7))
                        # Mark before calling Garmin: a crash in between leaves an "uncertain"
                        # session instead of a silent duplicate on the next push.
                        s.garmin_push_pending = True
                        store.save_plan(plan)
                        created = writer.create_running_workout(
                            build_running_workout(name, steps, minutes * 60)
                        )
                        new_id = created.get("workoutId")
                        if not isinstance(new_id, int):
                            missing = UpstreamServiceError(
                                "Garmin returned no workoutId: the workout may exist without "
                                "being recorded.",
                                remediation="Check Garmin Connect before retrying.",
                            )
                            missing.retryable = False
                            raise missing
                        s.garmin_workout_id = new_id
                        s.garmin_push_pending = False
                        store.save_plan(plan)
                    assert s.garmin_workout_id is not None
                    writer.schedule_workout(s.garmin_workout_id, s.date)
                    s.garmin_scheduled = True
                    store.save_plan(plan)
                    result.created.append(
                        PushEntry(date=s.date, name=name, workout_id=s.garmin_workout_id, ok=True)
                    )
                except GarminMCPError as exc:
                    if (
                        needs_create
                        and s.garmin_workout_id is None
                        and not isinstance(exc, UpstreamServiceError)
                    ):
                        # Refused outright (4xx, auth, rate limit): nothing was created.
                        s.garmin_push_pending = False
                        store.save_plan(plan)
                    note = (
                        " (created but not scheduled: the next push schedules it)"
                        if s.garmin_workout_id is not None and not s.garmin_scheduled
                        else ""
                    )
                    result.created.append(
                        PushEntry(
                            date=s.date,
                            name=name,
                            workout_id=s.garmin_workout_id,
                            ok=False,
                            error=f"{exc}{note}",
                        )
                    )
                if i < len(jobs) - 1:
                    sleep(0.5)
        except GarminMCPError as exc:
            raise fail(exc) from exc
        ok = sum(e.ok for e in result.created)
        lines = [
            f"Sent {ok}/{len(jobs)} workout(s) to Garmin; deleted {len(result.deleted)} obsolete."
        ]
        lines += [f"  ✗ {e.date} {e.name}: {e.error}" for e in result.created if not e.ok]
        result.text = "\n".join(lines)
        return result
