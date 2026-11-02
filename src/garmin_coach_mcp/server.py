"""MCP server: tool registration for the 9 Garmin Connect tools."""

from __future__ import annotations

import datetime as dt
import functools
import time
from collections.abc import Callable
from typing import Annotated, Any, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from garmin_coach_mcp import __version__
from garmin_coach_mcp.client import SupportsCall
from garmin_coach_mcp.errors import GarminMCPError, InvalidRequestError
from garmin_coach_mcp.formatting import format_duration, format_pace, pace_from
from garmin_coach_mcp.logging_utils import get_logger
from garmin_coach_mcp.models import (
    ActivityDetails,
    ActivityList,
    ActivitySummary,
    DeleteResult,
    HealthSummary,
    PlanEntryResult,
    PlanWorkout,
    PushPlanResult,
    PushWorkoutResult,
    RepeatStep,
    Step,
    TrainingVolume,
    WorkoutInfo,
    WorkoutList,
    WorkoutStep,
    parse_steps_json,
    parse_workouts_json,
)
from garmin_coach_mcp.workouts import build_running_workout

T = TypeVar("T")
log = get_logger("server")

INSTRUCTIONS = (
    "Garmin Connect tools for a running coach. Read tools return the athlete's activities, "
    "daily health metrics and recent training volume. Write tools create, schedule and delete "
    "structured running workouts on the athlete's Garmin calendar: always show the athlete "
    "what will be created or deleted and get explicit confirmation before calling them."
)

READ = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True
)
CREATE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
)
DELETE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True
)

ActivityId = Annotated[
    str, Field(pattern=r"^\d{1,20}$", description="Garmin activity id (from get_activities).")
]
WorkoutId = Annotated[
    str, Field(pattern=r"^\d{1,20}$", description="Garmin workout id (from get_workouts).")
]


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def summarize_activity(a: dict[str, Any]) -> ActivitySummary:
    distance = _as_float(a.get("distance")) or 0.0
    duration = _as_float(a.get("duration")) or 0.0
    return ActivitySummary(
        activity_id=_as_int(a.get("activityId")),
        date=(a.get("startTimeLocal") or "")[:10] or None,
        name=a.get("activityName"),
        type=str(_dict(a.get("activityType")).get("typeKey") or "unknown"),
        distance_km=round(distance / 1000, 2),
        duration_min=round(duration / 60, 1),
        pace=pace_from(distance, duration),
        avg_hr=_as_float(a.get("averageHR")),
        max_hr=_as_float(a.get("maxHR")),
        calories=_as_float(a.get("calories")),
    )


def _activity_line(s: ActivitySummary) -> str:
    hr = f"{s.avg_hr:.0f} bpm" if s.avg_hr else "N/A"
    return (
        f"• [{s.activity_id}] {s.date} — {s.type} | {s.distance_km:.1f} km | "
        f"{s.duration_min:.0f} min | {s.pace or 'N/A'} | HR: {hr}"
    )


def _delete_one(workout_id: int, client: Any) -> Any:
    return client.delete_workout(workout_id)


def _resolve_steps(
    steps: list[WorkoutStep | RepeatStep] | None, steps_json: str | list[Any] | None
) -> list[WorkoutStep | RepeatStep]:
    if steps and steps_json:
        raise InvalidRequestError(
            "Both steps and steps_json were given.",
            remediation="Pass only `steps` (steps_json is deprecated).",
        )
    if steps:
        return steps
    if steps_json:
        try:
            return parse_steps_json(steps_json)
        except ValueError as exc:
            raise InvalidRequestError(
                str(exc), remediation="Pass a valid `steps` array instead."
            ) from exc
    raise InvalidRequestError(
        "No workout steps given.", remediation="Pass a non-empty `steps` array."
    )


def build_server(
    session: SupportsCall, *, sleep: Callable[[float], None] = time.sleep
) -> MCPServer:
    """Construct the server and register tools. Honours `session.settings.read_only`."""
    app: MCPServer = MCPServer(
        name="garmin-coach-mcp",
        title="Garmin Coach",
        instructions=INSTRUCTIONS,
        version=__version__,
    )

    def run(operation: str, fn: Callable[[Any], T], *, idempotent: bool = True) -> T:
        try:
            return session.call(operation, fn, idempotent=idempotent)
        except GarminMCPError as exc:
            raise exc.to_tool_error() from exc

    # ------------------------------------------------------------------ read tools

    @app.tool(
        name="get_activities",
        title="List recent activities",
        description=(
            "List the athlete's most recent Garmin activities (newest first) with distance, "
            "duration, average pace and heart rate. Use the returned activity_id with "
            "get_activity_details."
        ),
        annotations=READ,
        structured_output=True,
    )
    def get_activities(
        limit: Annotated[int, Field(ge=1, le=100, description="Activities to fetch.")] = 20,
        start: Annotated[int, Field(ge=0, le=10_000, description="Pagination offset.")] = 0,
        activity_type: Annotated[
            str,
            Field(
                max_length=40,
                pattern=r"^[a-z_]*$",
                description=(
                    "Optional filter on Garmin's activity type key (substring match), e.g. "
                    "'running' (also matches trail_running), 'cycling', 'swimming', 'walking', "
                    "'hiking', 'strength'. Applied to the fetched page."
                ),
            ),
        ] = "",
    ) -> ActivityList:
        raw = run("get_activities", lambda c: c.get_activities(start, limit))
        items = [summarize_activity(a) for a in (raw or []) if isinstance(a, dict)]
        if activity_type:
            items = [s for s in items if activity_type in s.type.lower()]
        text = (
            "\n".join([f"Found {len(items)} activities:", *map(_activity_line, items)])
            if items
            else "No activities found."
        )
        return ActivityList(count=len(items), activities=items, text=text)

    @app.tool(
        name="get_activity_details",
        title="Activity details",
        description=(
            "Full Garmin payload for one activity (laps/splits summary, HR, running dynamics, "
            "training effect), plus a normalized summary."
        ),
        annotations=READ,
        structured_output=True,
    )
    def get_activity_details(activity_id: ActivityId) -> ActivityDetails:
        raw = run("get_activity_details", lambda c: c.get_activity(activity_id))
        data = _dict(raw)
        summary_src = {**_dict(data.get("summaryDTO")), **data}
        summary_src.setdefault("activityType", data.get("activityTypeDTO"))
        summary = summarize_activity(summary_src) if data else None
        return ActivityDetails(activity_id=int(activity_id), summary=summary, raw=data)

    @app.tool(
        name="get_health_summary",
        title="Daily health metrics",
        description=(
            "Recovery metrics for one day: steps, resting HR, stress, Body Battery, SpO2, sleep, "
            "HRV and training readiness. Metrics Garmin cannot provide are listed in "
            "`unavailable` with the reason, never silently omitted."
        ),
        annotations=READ,
        structured_output=True,
    )
    def get_health_summary(
        date_str: Annotated[dt.date, Field(description="Day to read, YYYY-MM-DD.")],
    ) -> HealthSummary:
        d = date_str.isoformat()
        out = HealthSummary(date=date_str)
        errors: list[GarminMCPError] = []

        def fetch(group: str, fn: Callable[[Any], Any]) -> Any:
            try:
                return session.call(f"get_health_summary.{group}", fn)
            except GarminMCPError as exc:
                errors.append(exc)
                out.unavailable[group] = exc.message
                return None

        summary = _dict(fetch("daily_summary", lambda c: c.get_user_summary(d)))
        if summary:
            out.steps = _as_int(summary.get("totalSteps"))
            out.resting_hr = _as_int(summary.get("restingHeartRate"))
            out.resting_hr_7d_avg = _as_int(summary.get("lastSevenDaysAvgRestingHeartRate"))
            stress = _as_int(summary.get("averageStressLevel"))
            out.stress_avg = stress if stress is not None and stress >= 0 else None
            out.body_battery_high = _as_int(summary.get("bodyBatteryHighestValue"))
            out.body_battery_low = _as_int(summary.get("bodyBatteryLowestValue"))
            out.spo2_avg = _as_float(summary.get("averageSpo2"))

        sleep = _dict(_dict(fetch("sleep", lambda c: c.get_sleep_data(d))).get("dailySleepDTO"))
        if sleep:
            secs = _as_float(sleep.get("sleepTimeSeconds"))
            out.sleep_hours = round(secs / 3600, 2) if secs else None
            out.sleep_score = _as_int(
                _dict(_dict(sleep.get("sleepScores")).get("overall")).get("value")
            )

        hrv = _dict(_dict(fetch("hrv", lambda c: c.get_hrv_data(d))).get("hrvSummary"))
        if hrv:
            out.hrv_last_night_avg = _as_float(hrv.get("lastNightAvg"))
            out.hrv_weekly_avg = _as_float(hrv.get("weeklyAvg"))
            out.hrv_status = hrv.get("status") if isinstance(hrv.get("status"), str) else None

        readiness = fetch("training_readiness", lambda c: c.get_training_readiness(d))
        if isinstance(readiness, list) and readiness:
            readiness = readiness[0]
        out.training_readiness = _as_int(_dict(readiness).get("score"))

        if len(errors) == 4:
            # Nothing at all could be read: that is a failure, not an empty day.
            raise errors[0].to_tool_error()

        rows = [
            ("Steps", out.steps),
            ("Resting HR (bpm)", out.resting_hr),
            ("Stress (avg)", out.stress_avg),
            (
                "Body Battery",
                f"{out.body_battery_low}-{out.body_battery_high}"
                if out.body_battery_high is not None
                else None,
            ),
            ("SpO2 avg (%)", out.spo2_avg),
            ("Sleep (h)", out.sleep_hours),
            ("Sleep score", out.sleep_score),
            ("HRV last night (ms)", out.hrv_last_night_avg),
            ("HRV status", out.hrv_status),
            ("Training readiness", out.training_readiness),
        ]
        lines = [f"Health summary for {d}:"]
        lines += [f"• {label}: {value}" for label, value in rows if value is not None]
        lines += [f"• {k}: unavailable ({v})" for k, v in out.unavailable.items()]
        if len(lines) == 1:
            lines.append("  No health data recorded for this date.")
        out.text = "\n".join(lines)
        return out

    @app.tool(
        name="get_training_status",
        title="Recent training volume",
        description=(
            "Training volume over the last N days computed from activities: activity count, "
            "running sessions, total running distance/duration, average running pace and the "
            "most recent runs."
        ),
        annotations=READ,
        structured_output=True,
    )
    def get_training_status(
        days: Annotated[int, Field(ge=1, le=90, description="Days to analyze.")] = 7,
    ) -> TrainingVolume:
        end = dt.date.today()
        start = end - dt.timedelta(days=days)
        raw = run(
            "get_training_status",
            lambda c: c.get_activities_by_date(start.isoformat(), end.isoformat()),
        )
        acts = [a for a in (raw or []) if isinstance(a, dict)]
        runs = [summarize_activity(a) for a in acts]
        runs = [r for r in runs if "running" in r.type.lower()]
        total_km = sum(r.distance_km for r in runs)
        total_s = sum(r.duration_min * 60 for r in runs)
        avg = format_pace(total_s / total_km) if total_km > 0 else None
        out = TrainingVolume(
            days=days,
            start_date=start,
            end_date=end,
            total_activities=len(acts),
            running_sessions=len(runs),
            total_distance_km=round(total_km, 2),
            total_duration=format_duration(total_s),
            avg_pace=avg,
            recent_runs=runs[:10],
        )
        out.text = "\n".join(
            [
                f"Training status — last {days} days:",
                f"• Total activities: {out.total_activities}",
                f"• Running sessions: {out.running_sessions}",
                f"• Total distance: {out.total_distance_km:.1f} km",
                f"• Total duration: {out.total_duration}",
                f"• Avg pace: {avg or 'N/A'}",
                *(_activity_line(r) for r in out.recent_runs),
            ]
        )
        return out

    @app.tool(
        name="get_workouts",
        title="List saved workouts",
        description="List structured workouts saved in the athlete's Garmin Connect library.",
        annotations=READ,
        structured_output=True,
    )
    def get_workouts(
        limit: Annotated[int, Field(ge=1, le=200, description="Workouts to fetch.")] = 20,
    ) -> WorkoutList:
        raw = run("get_workouts", lambda c: c.get_workouts(0, limit))
        items = [
            WorkoutInfo(
                workout_id=_as_int(w.get("workoutId")),
                name=str(w.get("workoutName") or "Unnamed"),
                sport=_dict(w.get("sportType")).get("sportTypeKey"),
            )
            for w in (raw or [])
            if isinstance(w, dict)
        ]
        text = (
            "\n".join(
                [f"Found {len(items)} workouts:"]
                + [f"• [{w.workout_id}] {w.name} ({w.sport})" for w in items]
            )
            if items
            else "No workouts found on Garmin Connect."
        )
        return WorkoutList(count=len(items), workouts=items, text=text)

    if session.settings.read_only:
        return app

    # ----------------------------------------------------------------- write tools

    def create_workout(
        name: str,
        steps: list[WorkoutStep | RepeatStep],
        minutes: int,
        when: dt.date | None,
    ) -> PushWorkoutResult:
        workout = build_running_workout(name, steps, minutes * 60)
        # Raises GarminMCPError (not ToolError) so plan pushes can report per entry.
        created = session.call(
            "push_workout.create", lambda c: c.upload_running_workout(workout), idempotent=False
        )
        workout_id = _as_int(_dict(created).get("workoutId"))
        result = PushWorkoutResult(workout_id=workout_id, name=name, scheduled_date=when)
        if when is not None and workout_id is not None:
            try:
                session.call(
                    "push_workout.schedule",
                    lambda c: c.schedule_workout(workout_id, when.isoformat()),
                    idempotent=False,
                )
                result.scheduled = True
            except GarminMCPError as exc:
                # The workout exists; report the partial success instead of hiding it.
                result.schedule_error = str(exc)
        return result

    @app.tool(
        name="push_workout",
        title="Create a structured workout",
        description=(
            "Create a structured running workout in Garmin Connect and optionally schedule it. "
            "Steps support warmup/run/recover/cooldown/rest with time, distance or open "
            "duration and pace and/or HR targets; use type='repeat' for intervals. "
            "Not retried on failure (it creates data). Ask the athlete to confirm first."
        ),
        annotations=CREATE,
        structured_output=True,
    )
    def push_workout(
        name: Annotated[str, Field(min_length=1, max_length=80, description="Workout name.")],
        steps: Annotated[
            list[Step] | None, Field(description="Ordered workout steps.", max_length=50)
        ] = None,
        estimated_duration_minutes: Annotated[int, Field(ge=1, le=1440)] = 60,
        schedule_date: Annotated[
            dt.date | None, Field(description="Optional calendar date YYYY-MM-DD.")
        ] = None,
        steps_json: Annotated[
            str | list[Any] | None,
            Field(description="Deprecated: JSON-encoded steps. Use `steps`."),
        ] = None,
    ) -> PushWorkoutResult:
        try:
            resolved = _resolve_steps(steps, steps_json)
            result = create_workout(name, resolved, estimated_duration_minutes, schedule_date)
        except GarminMCPError as exc:
            raise exc.to_tool_error() from exc
        lines = [f"Workout '{name}' created (ID: {result.workout_id})."]
        if result.scheduled:
            lines.append(f"Scheduled for {schedule_date}.")
        elif result.schedule_error:
            lines.append(f"Created but NOT scheduled: {result.schedule_error}")
        lines.append("Sync the Garmin device to receive it.")
        result.text = "\n".join(lines)
        return result

    @app.tool(
        name="push_training_plan",
        title="Create a multi-day plan",
        description=(
            "Create and schedule several running workouts starting at start_date. Each entry is "
            "created once (no automatic retry); per-entry results are reported so failed "
            "entries can be re-sent individually. Ask the athlete to confirm first."
        ),
        annotations=CREATE,
        structured_output=True,
    )
    def push_training_plan(
        name: Annotated[str, Field(min_length=1, max_length=80, description="Plan name.")],
        start_date: Annotated[dt.date, Field(description="Plan start date YYYY-MM-DD.")],
        workouts: Annotated[
            list[PlanWorkout] | None, Field(description="Plan entries.", max_length=200)
        ] = None,
        workouts_json: Annotated[
            str | list[Any] | None,
            Field(description="Deprecated: JSON-encoded entries. Use `workouts`."),
        ] = None,
    ) -> PushPlanResult:
        if workouts and workouts_json:
            raise InvalidRequestError(
                "Both workouts and workouts_json were given.",
                remediation="Pass only `workouts`.",
            ).to_tool_error()
        entries = workouts
        if not entries and workouts_json:
            try:
                entries = parse_workouts_json(workouts_json)
            except ValueError as exc:
                raise InvalidRequestError(
                    str(exc), remediation="Pass a valid `workouts` array."
                ).to_tool_error() from exc
        if not entries:
            raise InvalidRequestError(
                "No workouts given.", remediation="Pass a non-empty `workouts` array."
            ).to_tool_error()

        results: list[PlanEntryResult] = []
        for i, entry in enumerate(entries):
            when = start_date + dt.timedelta(days=entry.day_offset)
            try:
                r = create_workout(
                    entry.name, list(entry.steps), entry.estimated_duration_minutes, when
                )
                ok = r.scheduled
                results.append(
                    PlanEntryResult(
                        day_offset=entry.day_offset,
                        date=when,
                        name=entry.name,
                        workout_id=r.workout_id,
                        ok=ok,
                        error=None if ok else r.schedule_error,
                    )
                )
            except GarminMCPError as exc:
                results.append(
                    PlanEntryResult(
                        day_offset=entry.day_offset,
                        date=when,
                        name=entry.name,
                        ok=False,
                        error=str(exc),
                    )
                )
            if i < len(entries) - 1:
                sleep(0.5)  # be gentle with Garmin's rate limits

        ok_count = sum(r.ok for r in results)
        out = PushPlanResult(
            plan_name=name,
            total=len(results),
            succeeded=ok_count,
            failed=len(results) - ok_count,
            entries=results,
        )
        lines = [
            f"Training plan '{name}': {ok_count}/{len(results)} workouts created and scheduled."
        ]
        lines += [f"  ✗ {r.date} {r.name}: {r.error}" for r in results if not r.ok]
        out.text = "\n".join(lines)
        return out

    @app.tool(
        name="delete_workout",
        title="Delete a workout",
        description=(
            "Delete one workout from the Garmin Connect library. Irreversible; confirm first."
        ),
        annotations=DELETE,
        structured_output=True,
    )
    def delete_workout(workout_id: WorkoutId) -> DeleteResult:
        run("delete_workout", lambda c: c.delete_workout(workout_id), idempotent=False)
        return DeleteResult(deleted_ids=[int(workout_id)], text=f"Workout {workout_id} deleted.")

    @app.tool(
        name="delete_plan_workouts",
        title="Delete workouts by name",
        description=(
            "Delete every workout whose name contains `name_contains` (case-insensitive), e.g. "
            "to clear a plan before re-uploading it. The pattern is mandatory (min 3 chars). "
            "Use dry_run=true first and show the athlete the list. Irreversible."
        ),
        annotations=DELETE,
        structured_output=True,
    )
    def delete_plan_workouts(
        name_contains: Annotated[
            str,
            Field(min_length=3, max_length=80, description="Substring of the workout names."),
        ],
        dry_run: Annotated[bool, Field(description="Only list what would be deleted.")] = False,
    ) -> DeleteResult:
        needle = name_contains.strip().lower()
        if len(needle) < 3:
            raise InvalidRequestError(
                "name_contains must contain at least 3 non-space characters.",
                remediation="Use a pattern specific to the plan, e.g. its name.",
            ).to_tool_error()
        raw = run("delete_plan_workouts.list", lambda c: c.get_workouts(0, 500))
        matches = [
            w
            for w in (raw or [])
            if isinstance(w, dict)
            and needle in str(w.get("workoutName") or "").lower()
            and _as_int(w.get("workoutId")) is not None
        ]
        ids = [int(w["workoutId"]) for w in matches]
        if dry_run or not ids:
            names = ", ".join(f"{w.get('workoutName')} [{w.get('workoutId')}]" for w in matches)
            return DeleteResult(
                deleted_ids=[],
                text=(
                    f"Dry run: {len(ids)} workouts match '{name_contains}': {names}"
                    if ids
                    else f"No workouts match '{name_contains}'."
                ),
            )
        deleted: list[int] = []
        failed: dict[str, str] = {}
        for wid in ids:
            try:
                session.call(
                    "delete_plan_workouts.delete",
                    functools.partial(_delete_one, wid),
                    idempotent=False,
                )
                deleted.append(wid)
            except GarminMCPError as exc:
                failed[str(wid)] = str(exc)
            sleep(0.3)
        text = f"Deleted {len(deleted)}/{len(ids)} workouts matching '{name_contains}'."
        if failed:
            text += f" {len(failed)} failed."
        return DeleteResult(deleted_ids=deleted, failed=failed, text=text)

    return app
