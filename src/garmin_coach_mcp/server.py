"""MCP server definition: the tool surface exposed to models and clients.

`build_server` assembles an :class:`mcp.server.mcpserver.MCPServer` around a
:class:`~garmin_coach_mcp.client.GarminSession`. Every tool follows the same
contract:

* All Garmin I/O goes through ``session.call(operation, fn)``, so retries,
  logging and exception mapping stay in one place. No tool touches the Garmin
  client directly.
* Failures propagate as :class:`~garmin_coach_mcp.errors.GarminMCPError`
  subclasses and are converted to protocol-level tool errors. No tool returns an
  error string dressed up as a successful result.
* Tools return a pydantic model, so the SDK derives an output schema and emits
  structured content, and each one attaches a compact human-readable text
  rendering for clients that display the unstructured content block.

Naming follows a single scheme, applied uniformly:
``list_*`` pages a collection, ``get_*`` fetches one thing, ``create_*`` makes
something new, ``schedule_*``/``delete_*`` act on something that already exists.
Read tools are always registered; write tools are only registered when the
session is not in read-only mode, so a read-only deployment advertises a
genuinely read-only surface instead of failing at call time.
"""

from __future__ import annotations

import time
from datetime import date, timedelta
from typing import Annotated, Any, TypeVar, cast

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, Field

from .client import GarminSession
from .errors import (
    AuthenticationError,
    ConfigurationError,
    GarminMCPError,
    InvalidRequestError,
    NotFoundError,
)
from .formatting import format_distance, format_duration, format_pace, pace_seconds_per_km
from .models import (
    ActivityDetail,
    ActivityList,
    ActivitySummary,
    ActivityType,
    BulkDeletionResult,
    DeletionCandidate,
    GarminId,
    HealthMetric,
    HealthSummary,
    IsoDate,
    LapSummary,
    MetricStatus,
    PlannedWorkout,
    PlanWorkoutOutcome,
    ScheduledWorkout,
    TrainingLoad,
    TrainingPlanResult,
    WorkoutCreated,
    WorkoutDefinition,
    WorkoutDeleted,
    WorkoutDetail,
    WorkoutList,
    WorkoutSummary,
)
from .workouts import (
    build_running_workout,
    count_steps,
    estimate_duration_seconds,
    project_workout,
    render_workout_definition,
    render_workout_detail,
)

__all__ = ["build_server"]

SERVER_NAME = "garmin-coach"
SERVER_TITLE = "Garmin Coach"

INSTRUCTIONS = """\
Read training and health data from a Garmin Connect account, and build \
structured running workouts and multi-week training plans that sync to the \
athlete's watch.

How the tools fit together:

* Completed training: `list_activities` pages the athlete's history and yields \
the `activity_id` that `get_activity` expands into splits and running \
dynamics. `get_training_load` aggregates the same history into weekly volume, \
which is what you want before deciding whether to add or cut load.
* Recovery: `get_health_summary` reports one day of sleep, HRV, stress, body \
battery, resting heart rate, SpO2, steps and training readiness. Each metric \
carries its own status, so "not recorded" and "the fetch failed" are always \
distinguishable — never treat a missing metric as a zero.
* Planned training: `list_workouts` and `get_workout` inspect the workout \
library and yield the `workout_id` used by `schedule_workout` and \
`delete_workout`. `create_workout` uploads one structured workout; \
`create_training_plan` uploads and schedules a whole block in one call. \
`delete_workouts` removes a plan again, and always requires an explicit filter.

Workouts are described with real objects, not JSON strings. A step is either an \
executable step (`type` of warmup/run/recover/cooldown/rest) or a repeat block \
(`type: "repeat"`) that loops nested steps. Distances are metres, durations are \
seconds, paces are minutes:seconds per kilometre such as "4:35", and heart-rate \
targets are bpm.

Dates are always ISO `YYYY-MM-DD` in the athlete's local calendar. Uploading a \
workout does not put it on the watch by itself — the athlete must sync the \
device afterwards, and it is worth saying so once per session.
"""

READ_ONLY_NOTICE = (
    "\nThis server is running in READ-ONLY mode: workout creation, scheduling "
    "and deletion tools are not available.\n"
)

_ModelT = TypeVar("_ModelT", bound=BaseModel)


def _result(model: _ModelT, text: str) -> _ModelT:
    """Return structured content plus a compact human-readable text rendering.

    The MCP SDK derives a tool's output schema from its return annotation, and
    validates the structured content of a returned ``CallToolResult`` against
    that schema. Returning the ``CallToolResult`` here lets each tool ship a
    readable text block instead of a JSON dump, while keeping the annotation —
    and therefore the published output schema — accurate. The cast is the price
    of that; the runtime shape is exactly what the SDK expects.
    """
    return cast(
        "_ModelT",
        CallToolResult(
            content=[TextContent(type="text", text=text)],
            structured_content=model.model_dump(mode="json"),
        ),
    )


def _as_str_id(value: Any) -> str:
    return "" if value is None else str(value)


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _summarize_activity(raw: dict[str, Any]) -> ActivitySummary:
    """Project one raw Garmin activity row onto :class:`ActivitySummary`."""
    activity_type = raw.get("activityType") or {}
    distance = _as_float(raw.get("distance"))
    duration = _as_float(raw.get("duration"))
    start_local = raw.get("startTimeLocal")
    return ActivitySummary(
        activity_id=_as_str_id(raw.get("activityId")),
        name=raw.get("activityName"),
        activity_type=(activity_type.get("typeKey") if isinstance(activity_type, dict) else None),
        start_time_local=start_local,
        date=start_local[:10] if isinstance(start_local, str) else None,
        distance_meters=distance,
        duration_seconds=duration,
        average_pace=format_pace(pace_seconds_per_km(distance, duration)),
        average_heart_rate=_as_int(raw.get("averageHR")),
        max_heart_rate=_as_int(raw.get("maxHR")),
        calories=_as_int(raw.get("calories")),
        elevation_gain_meters=_as_float(raw.get("elevationGain")),
    )


def _render_activity(item: ActivitySummary) -> str:
    bits = [
        f"[{item.activity_id}]",
        item.date or "????-??-??",
        item.activity_type or "unknown",
        format_distance(item.distance_meters) or "-",
        format_duration(item.duration_seconds) or "-",
        item.average_pace or "-",
        f"{item.average_heart_rate} bpm" if item.average_heart_rate else "-",
    ]
    return " | ".join(bits)


def build_server(session: GarminSession) -> MCPServer:
    """Construct the MCP server and register the tool surface.

    Args:
        session: An authenticated-on-demand Garmin session. Every tool routes its
            I/O through ``session.call`` so retry and error mapping stay
            centralised.

    Returns:
        A configured :class:`MCPServer`. Write tools are registered only when
        ``session.settings.read_only`` is false.
    """
    settings = session.settings
    read_only = bool(getattr(settings, "read_only", False))

    instructions = INSTRUCTIONS + (READ_ONLY_NOTICE if read_only else "")
    app: MCPServer = MCPServer(
        name=SERVER_NAME,
        title=SERVER_TITLE,
        instructions=instructions,
    )

    _register_read_tools(app, session)
    if not read_only:
        _register_write_tools(app, session)
    return app


# --------------------------------------------------------------------------- #
# Read tools
# --------------------------------------------------------------------------- #


def _register_read_tools(app: MCPServer, session: GarminSession) -> None:
    read_annotations = ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )

    @app.tool(
        name="list_activities",
        title="List completed activities",
        description=(
            "List the athlete's completed activities, most recent first, as a "
            "paginated summary: date, sport, distance, duration, average pace, "
            "heart rate and calories.\n\n"
            "Use this as the entry point for anything about training the athlete "
            "has already done, and to obtain the `activity_id` that "
            "`get_activity` expands into splits and running dynamics.\n\n"
            "Do NOT use this to compute weekly volume — `get_training_load` "
            "already aggregates a date window and is one call instead of many. "
            "Do not use it for planned or future workouts either; those live in "
            "the workout library behind `list_workouts`.\n\n"
            "`activity_type` is applied by Garmin server-side, so a filtered page "
            "still returns up to `limit` results."
        ),
        annotations=read_annotations,
        structured_output=True,
    )
    def list_activities(
        limit: Annotated[
            int,
            Field(
                default=10,
                ge=1,
                le=100,
                description=(
                    "Maximum number of activities to return. Keep it small; each "
                    "activity is a full summary row."
                ),
            ),
        ] = 10,
        start: Annotated[
            int,
            Field(
                default=0,
                ge=0,
                le=10_000,
                description=(
                    "Pagination offset, 0 being the most recent activity. To get "
                    "the next page, add the previous page's `count` to `start`."
                ),
            ),
        ] = 0,
        activity_type: Annotated[
            ActivityType | None,
            Field(
                default=None,
                description=(
                    "Restrict results to one sport, applied by Garmin server-side. "
                    "Omit for all sports."
                ),
            ),
        ] = None,
    ) -> ActivityList:
        """List completed Garmin activities.

        Args:
            limit: Page size, 1-100.
            start: Zero-based pagination offset from the most recent activity.
            activity_type: Optional server-side sport filter.

        Returns:
            An :class:`ActivityList` of summaries plus pagination state.

        Raises:
            GarminMCPError: If authentication fails or Garmin is unreachable.
        """
        type_key = activity_type.value if activity_type is not None else None
        raw = session.call(
            "activities.list",
            lambda api: api.get_activities(start, limit, type_key),
        )
        rows: list[dict[str, Any]] = [item for item in (raw or []) if isinstance(item, dict)]
        activities = [_summarize_activity(item) for item in rows]

        payload = ActivityList(
            activities=activities,
            count=len(activities),
            start=start,
            limit=limit,
            activity_type=type_key,
            has_more=len(activities) >= limit,
        )
        if activities:
            header = f"{len(activities)} activities" + (f" ({type_key})" if type_key else "")
            body = "\n".join(_render_activity(item) for item in activities)
            text = f"{header}:\n{body}"
            if payload.has_more:
                text += f"\n(more available — re-call with start={start + len(activities)})"
        else:
            text = "No activities found for this query."
        return _result(payload, text)

    @app.tool(
        name="get_activity",
        title="Get activity detail",
        description=(
            "Fetch one completed activity in depth: pace and heart-rate summary, "
            "running dynamics (cadence, stride length, ground contact time, "
            "vertical oscillation), elevation, training effect and per-lap "
            "splits when Garmin provides them.\n\n"
            "`activity_id` comes from `list_activities` or from "
            "`get_training_load`'s `recent_runs`; there is no way to guess it.\n\n"
            "The response is a deliberately bounded projection of Garmin's "
            "payload — hundreds of low-value fields are dropped and the count of "
            "dropped fields is reported. Call this for one or two activities you "
            "actually want to analyse, not in a loop over a whole month."
        ),
        annotations=read_annotations,
        structured_output=True,
    )
    def get_activity(
        activity_id: Annotated[
            GarminId,
            Field(
                description=(
                    "Garmin activity ID, exactly as returned by `list_activities` "
                    "in the `activity_id` field."
                )
            ),
        ],
    ) -> ActivityDetail:
        """Fetch a bounded detail projection for one activity.

        Args:
            activity_id: Garmin activity ID from `list_activities`.

        Returns:
            An :class:`ActivityDetail` projection.

        Raises:
            NotFoundError: If Garmin has no activity with that ID.
            GarminMCPError: On authentication or upstream failure.
        """
        raw = session.call(
            "activities.get",
            lambda api: api.get_activity(activity_id),
        )
        if not isinstance(raw, dict) or not raw:
            raise NotFoundError(
                f"Garmin returned no activity with ID {activity_id}.",
                remediation="Call list_activities and use an activity_id from its results.",
            )

        summary = raw.get("summaryDTO") or {}
        if not isinstance(summary, dict):
            summary = {}
        type_dto = raw.get("activityTypeDTO") or {}
        distance = _as_float(summary.get("distance"))
        duration = _as_float(summary.get("duration"))

        kept_keys = {
            "activityId",
            "activityName",
            "description",
            "activityTypeDTO",
            "summaryDTO",
            "locationName",
            "splitSummaries",
            "metadataDTO",
        }
        omitted = sum(1 for key in raw if key not in kept_keys)

        detail = ActivityDetail(
            activity_id=_as_str_id(raw.get("activityId") or activity_id),
            name=raw.get("activityName"),
            description=raw.get("description"),
            activity_type=(type_dto.get("typeKey") if isinstance(type_dto, dict) else None),
            start_time_local=summary.get("startTimeLocal"),
            location=raw.get("locationName"),
            distance_meters=distance,
            duration_seconds=duration,
            moving_duration_seconds=_as_float(summary.get("movingDuration")),
            elapsed_duration_seconds=_as_float(summary.get("elapsedDuration")),
            average_pace=format_pace(pace_seconds_per_km(distance, duration)),
            best_pace=_pace_from_speed(summary.get("maxSpeed")),
            elevation_gain_meters=_as_float(summary.get("elevationGain")),
            elevation_loss_meters=_as_float(summary.get("elevationLoss")),
            average_heart_rate=_as_int(summary.get("averageHR")),
            max_heart_rate=_as_int(summary.get("maxHR")),
            average_cadence_spm=_as_float(summary.get("averageRunCadence")),
            max_cadence_spm=_as_float(summary.get("maxRunCadence")),
            stride_length_cm=_as_float(summary.get("strideLength")),
            ground_contact_time_ms=_as_float(summary.get("groundContactTime")),
            vertical_oscillation_cm=_as_float(summary.get("verticalOscillation")),
            vertical_ratio=_as_float(summary.get("verticalRatio")),
            average_power_watts=_as_float(summary.get("avgPower")),
            calories=_as_int(summary.get("calories")),
            aerobic_training_effect=_as_float(summary.get("trainingEffect")),
            anaerobic_training_effect=_as_float(summary.get("anaerobicTrainingEffect")),
            vo2max=_as_float(summary.get("vO2MaxValue")),
            laps=_project_laps(raw.get("splitSummaries")),
            omitted_field_count=omitted,
        )

        lines = [
            f"[{detail.activity_id}] {detail.name or 'Activity'} "
            f"({detail.activity_type or 'unknown'}) {detail.start_time_local or ''}".strip(),
            f"Distance {format_distance(detail.distance_meters) or '-'} | "
            f"Duration {format_duration(detail.duration_seconds) or '-'} | "
            f"Pace {detail.average_pace or '-'}",
            f"HR avg {detail.average_heart_rate or '-'} / max {detail.max_heart_rate or '-'} bpm | "
            f"Cadence {detail.average_cadence_spm or '-'} spm | "
            f"Calories {detail.calories or '-'}",
        ]
        if detail.aerobic_training_effect or detail.anaerobic_training_effect:
            lines.append(
                f"Training effect aerobic {detail.aerobic_training_effect or '-'} / "
                f"anaerobic {detail.anaerobic_training_effect or '-'}"
            )
        if detail.laps:
            lines.append(f"Laps ({len(detail.laps)}):")
            lines.extend(
                f"  {lap.lap}: {format_distance(lap.distance_meters) or '-'} | "
                f"{format_duration(lap.duration_seconds) or '-'} | {lap.pace or '-'}"
                for lap in detail.laps
            )
        return _result(detail, "\n".join(lines))

    @app.tool(
        name="get_health_summary",
        title="Get daily health and recovery summary",
        description=(
            "Fetch one calendar day of recovery and wellness metrics: steps, "
            "resting heart rate, HRV, stress, sleep, body battery, SpO2 and "
            "training readiness.\n\n"
            "Use it before prescribing or adjusting training, and to explain a "
            "hard session that felt worse than expected.\n\n"
            "Each metric is queried independently and reports its own status: "
            "`ok` (a value came back), `not_recorded` (Garmin answered but has "
            "nothing for that day — the watch was off the wrist, or the device "
            "does not support the metric) or `fetch_failed` (the request failed "
            "and the value is unknown). Never read a missing metric as a zero: "
            "check `failed_count` before concluding anything about the day.\n\n"
            "One day per call. For trends, call it for a few representative days "
            "rather than every day of a month."
        ),
        annotations=read_annotations,
        structured_output=True,
    )
    def get_health_summary(
        summary_date: Annotated[
            IsoDate,
            Field(
                description=(
                    "Calendar day to summarise, ISO YYYY-MM-DD in the athlete's "
                    "local time. Sleep and HRV are attributed to the morning the "
                    "athlete woke up."
                ),
                alias="date",
            ),
        ],
    ) -> HealthSummary:
        """Fetch a day of health metrics with per-metric status.

        Args:
            summary_date: Calendar date, exposed to clients as ``date``.

        Returns:
            A :class:`HealthSummary` containing the full metric set, each entry
            annotated with whether it succeeded, was absent, or failed.

        Raises:
            AuthenticationError: Immediately, if the session cannot authenticate.
            GarminMCPError: If every metric fetch failed, since the result would
                otherwise be an empty summary masquerading as a successful call.
        """
        iso = summary_date.isoformat()
        metrics = _collect_health_metrics(session, iso)

        ok = sum(1 for m in metrics if m.status is MetricStatus.OK)
        absent = sum(1 for m in metrics if m.status is MetricStatus.NOT_RECORDED)
        failed = sum(1 for m in metrics if m.status is MetricStatus.FETCH_FAILED)

        payload = HealthSummary(
            date=iso,
            metrics=metrics,
            ok_count=ok,
            not_recorded_count=absent,
            failed_count=failed,
        )
        lines = [f"Health summary for {iso}:"]
        for metric in metrics:
            if metric.status is MetricStatus.OK:
                suffix = f" ({metric.detail})" if metric.detail else ""
                lines.append(f"  {metric.label:<20} {metric.display}{suffix}")
            elif metric.status is MetricStatus.NOT_RECORDED:
                lines.append(f"  {metric.label:<20} not recorded")
            else:
                lines.append(f"  {metric.label:<20} FETCH FAILED — {metric.detail}")
        lines.append(f"  ({ok} recorded, {absent} not recorded, {failed} failed)")
        return _result(payload, "\n".join(lines))

    @app.tool(
        name="get_training_load",
        title="Summarise recent training load",
        description=(
            "Aggregate the athlete's activities over a recent window into total "
            "and running-specific volume: session count by sport, running "
            "distance and duration, volume-weighted average pace, longest run, "
            "and the most recent runs.\n\n"
            "This is the tool to reach for before answering 'how much have I been "
            "running lately', 'should I add mileage', or before building a "
            "training block. It is one call where `list_activities` plus manual "
            "arithmetic would be several.\n\n"
            "Use `list_activities` instead when you need individual activities or "
            "pagination, and `get_activity` when you need one session in depth."
        ),
        annotations=read_annotations,
        structured_output=True,
    )
    def get_training_load(
        days: Annotated[
            int,
            Field(
                default=7,
                ge=1,
                le=90,
                description=(
                    "Length of the window in days, counting back from today "
                    "inclusive. 7 for the past week, 28 for a training block."
                ),
            ),
        ] = 7,
        end_date: Annotated[
            IsoDate | None,
            Field(
                default=None,
                description=(
                    "Last day of the window, ISO YYYY-MM-DD. Defaults to today. "
                    "Set it to look at a historical block."
                ),
            ),
        ] = None,
    ) -> TrainingLoad:
        """Aggregate recent training volume.

        Args:
            days: Window length in days, 1-90.
            end_date: Last day of the window; defaults to today.

        Returns:
            A :class:`TrainingLoad` aggregate plus up to ten recent runs.

        Raises:
            GarminMCPError: On authentication or upstream failure.
        """
        end = end_date or date.today()
        start = end - timedelta(days=days - 1)
        raw = session.call(
            "activities.by_date",
            lambda api: api.get_activities_by_date(start.isoformat(), end.isoformat()),
        )
        rows: list[dict[str, Any]] = [item for item in (raw or []) if isinstance(item, dict)]

        by_type: dict[str, int] = {}
        for item in rows:
            type_dto = item.get("activityType") or {}
            key = type_dto.get("typeKey", "unknown") if isinstance(type_dto, dict) else "unknown"
            by_type[key] = by_type.get(key, 0) + 1

        runs = [
            item
            for item in rows
            if "running" in str((item.get("activityType") or {}).get("typeKey", "")).lower()
        ]
        summaries = [_summarize_activity(item) for item in runs]
        total_distance = sum(s.distance_meters or 0.0 for s in summaries)
        total_duration = sum(s.duration_seconds or 0.0 for s in summaries)
        longest = max((s.distance_meters or 0.0 for s in summaries), default=0.0)

        payload = TrainingLoad(
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            days=days,
            total_activities=len(rows),
            activities_by_type=by_type,
            running_sessions=len(summaries),
            running_distance_meters=total_distance,
            running_duration_seconds=total_duration,
            average_running_pace=format_pace(pace_seconds_per_km(total_distance, total_duration)),
            longest_run_meters=longest or None,
            recent_runs=summaries[:10],
        )
        lines = [
            f"Training load {start.isoformat()} to {end.isoformat()} ({days} days):",
            f"  Activities        {len(rows)}"
            + (f" ({', '.join(f'{k} {v}' for k, v in sorted(by_type.items()))})" if by_type else ""),
            f"  Running sessions  {len(summaries)}",
            f"  Running distance  {format_distance(total_distance) or '0 m'}",
            f"  Running time      {format_duration(total_duration) or '0s'}",
            f"  Average pace      {payload.average_running_pace or '-'}",
            f"  Longest run       {format_distance(longest) or '-'}",
        ]
        if summaries:
            lines.append("  Recent runs:")
            lines.extend(f"    {_render_activity(item)}" for item in summaries[:10])
        return _result(payload, "\n".join(lines))

    @app.tool(
        name="list_workouts",
        title="List saved workouts",
        description=(
            "List structured workouts saved in the athlete's Garmin Connect "
            "workout library, newest first.\n\n"
            "Use it to find the `workout_id` needed by `get_workout`, "
            "`schedule_workout` and `delete_workout`, and to check what a "
            "previously pushed training plan left behind before creating more.\n\n"
            "This is the workout *library*, not the calendar and not completed "
            "training: entries here are templates that may or may not be "
            "scheduled. For sessions the athlete actually ran, use "
            "`list_activities`.\n\n"
            "`name_contains` filters client-side over the fetched page, so a "
            "filtered result may contain fewer than `limit` entries; raise "
            "`limit` if you expect more."
        ),
        annotations=read_annotations,
        structured_output=True,
    )
    def list_workouts(
        limit: Annotated[
            int,
            Field(
                default=25,
                ge=1,
                le=200,
                description="Maximum number of workouts to fetch from the library.",
            ),
        ] = 25,
        start: Annotated[
            int,
            Field(default=0, ge=0, le=10_000, description="Pagination offset into the library."),
        ] = 0,
        name_contains: Annotated[
            str | None,
            Field(
                default=None,
                min_length=1,
                max_length=80,
                description=(
                    "Case-insensitive substring filter on the workout name, "
                    "applied after fetching. Useful for isolating one training "
                    "plan by its name prefix."
                ),
            ),
        ] = None,
    ) -> WorkoutList:
        """List workouts in the Garmin Connect library.

        Args:
            limit: Page size, 1-200.
            start: Pagination offset.
            name_contains: Optional case-insensitive name substring filter.

        Returns:
            A :class:`WorkoutList` of summaries plus pagination state.

        Raises:
            GarminMCPError: On authentication or upstream failure.
        """
        raw = session.call("workouts.list", lambda api: api.get_workouts(start, limit))
        rows: list[dict[str, Any]] = [item for item in (raw or []) if isinstance(item, dict)]
        page_size = len(rows)
        if name_contains:
            needle = name_contains.lower()
            rows = [r for r in rows if needle in str(r.get("workoutName", "")).lower()]

        workouts = [_summarize_workout_row(r) for r in rows]
        payload = WorkoutList(
            workouts=workouts,
            count=len(workouts),
            start=start,
            limit=limit,
            name_contains=name_contains,
            has_more=page_size >= limit,
        )
        if workouts:
            body = "\n".join(
                f"[{w.workout_id}] {w.name} ({w.sport_type or '?'})"
                + (f" ~{format_duration(w.estimated_duration_seconds)}" if w.estimated_duration_seconds else "")
                for w in workouts
            )
            text = f"{len(workouts)} workouts:\n{body}"
        elif name_contains:
            text = f"No workouts in this page whose name contains {name_contains!r}."
        else:
            text = "The workout library is empty."
        return _result(payload, text)

    @app.tool(
        name="get_workout",
        title="Get saved workout structure",
        description=(
            "Fetch one saved workout and show its full step structure: each "
            "step's kind, end condition and pace or heart-rate target, with "
            "repeat blocks flattened into an indented, ordered list.\n\n"
            "`workout_id` comes from `list_workouts`. Use this before editing or "
            "recreating a workout, or to confirm that a workout you just created "
            "with `create_workout` came out as intended.\n\n"
            "This describes a planned template. It says nothing about whether the "
            "athlete completed it — that lives in `list_activities`."
        ),
        annotations=read_annotations,
        structured_output=True,
    )
    def get_workout(
        workout_id: Annotated[
            GarminId,
            Field(description="Garmin workout ID, as returned by `list_workouts`."),
        ],
    ) -> WorkoutDetail:
        """Fetch a saved workout and project its steps.

        Args:
            workout_id: Garmin workout ID from `list_workouts`.

        Returns:
            A :class:`WorkoutDetail` with a flattened step list.

        Raises:
            NotFoundError: If Garmin has no workout with that ID.
            GarminMCPError: On authentication or upstream failure.
        """
        raw = session.call("workouts.get", lambda api: api.get_workout_by_id(workout_id))
        if not isinstance(raw, dict) or not raw:
            raise NotFoundError(
                f"Garmin returned no workout with ID {workout_id}.",
                remediation="Call list_workouts and use a workout_id from its results.",
            )
        detail = project_workout(raw)
        return _result(detail, render_workout_detail(detail))


# --------------------------------------------------------------------------- #
# Write tools
# --------------------------------------------------------------------------- #


def _register_write_tools(app: MCPServer, session: GarminSession) -> None:
    create_annotations = ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    )
    schedule_annotations = ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
    delete_annotations = ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    )

    @app.tool(
        name="create_workout",
        title="Create a structured running workout",
        description=(
            "Create one structured running workout in the athlete's Garmin "
            "Connect library, optionally scheduling it on a date.\n\n"
            "The workout is described with real objects: an ordered list of steps "
            "where each step is either an executable step (warmup, run, recover, "
            "cooldown, rest) or a repeat block that loops nested steps — that is "
            "how intervals such as 6x800m are expressed. Durations are seconds, "
            "distances metres, paces 'min:sec' per kilometre (\"4:35\"), heart "
            "rates bpm. Supply a pace range, a heart-rate range, or both; with "
            "both, `target_priority` picks the primary gauge.\n\n"
            "This is NOT idempotent: calling it twice creates two workouts. If a "
            "call fails, check `list_workouts` before retrying. For a whole "
            "training block use `create_training_plan` instead of looping here.\n\n"
            "Creation alone does not put the workout on the watch — the athlete "
            "must sync the device."
        ),
        annotations=create_annotations,
        structured_output=True,
    )
    def create_workout(
        workout: Annotated[
            WorkoutDefinition,
            Field(description="The workout to build, including its ordered step list."),
        ],
        schedule_date: Annotated[
            IsoDate | None,
            Field(
                default=None,
                description=(
                    "Optional date to place the workout on the Garmin calendar, "
                    "ISO YYYY-MM-DD. Omit to only save it to the library; it can "
                    "be scheduled later with `schedule_workout`."
                ),
            ),
        ] = None,
    ) -> WorkoutCreated:
        """Upload a structured workout, optionally scheduling it.

        Args:
            workout: Validated workout definition.
            schedule_date: Optional calendar date for the workout.

        Returns:
            A :class:`WorkoutCreated` describing the new workout. If the workout
            was created but scheduling failed, ``scheduling_error`` is set and
            the workout still exists.

        Raises:
            GarminMCPError: If the upload itself fails. The upload is never
                retried, so a failure means nothing was created or the outcome is
                genuinely unknown; verify with `list_workouts`.
        """
        payload = build_running_workout(workout)
        response = session.call(
            "workouts.create",
            lambda api: api.upload_running_workout(payload),
        )
        workout_id = _as_str_id((response or {}).get("workoutId") if isinstance(response, dict) else None)
        if not workout_id:
            raise GarminMCPError(
                "Garmin accepted the upload but returned no workout ID.",
                remediation=(
                    "Call list_workouts to check whether the workout was created "
                    "before retrying, to avoid creating a duplicate."
                ),
            )

        duration = (
            workout.estimated_duration_minutes * 60
            if workout.estimated_duration_minutes is not None
            else estimate_duration_seconds(workout.steps)
        )
        scheduled: str | None = None
        scheduling_error: str | None = None
        if schedule_date is not None:
            iso = schedule_date.isoformat()
            try:
                session.call(
                    "workouts.schedule",
                    lambda api: api.schedule_workout(workout_id, iso),
                )
                scheduled = iso
            except GarminMCPError as exc:
                # The workout exists; degrading to "created but not scheduled" is
                # more useful than failing the whole call and losing its ID.
                scheduling_error = str(exc)

        result = WorkoutCreated(
            workout_id=workout_id,
            name=workout.name,
            step_count=count_steps(workout.steps),
            estimated_duration_seconds=duration,
            scheduled_date=scheduled,
            scheduling_error=scheduling_error,
        )
        lines = [
            f"Created workout [{workout_id}] {workout.name}",
            render_workout_definition(workout),
        ]
        if scheduled:
            lines.append(f"Scheduled for {scheduled}.")
        elif scheduling_error:
            lines.append(f"NOT scheduled — {scheduling_error}")
        lines.append("Sync the Garmin device to download it.")
        return _result(result, "\n".join(lines))

    @app.tool(
        name="create_training_plan",
        title="Create and schedule a multi-day training plan",
        description=(
            "Create several structured workouts at once and place each on the "
            "Garmin calendar, at a day offset from the plan's start date. Use it "
            "to push a training week or block in a single call.\n\n"
            "Each entry uses the same workout structure as `create_workout`. "
            "Prefix every workout name with a common tag (for example 'W1 - ') so "
            "the whole plan can be found later with `list_workouts` and removed "
            "with `delete_workouts`.\n\n"
            "This is NOT atomic and NOT idempotent: workouts are uploaded one by "
            "one and are never retried, so a partial success is possible and is "
            "reported per workout in `outcomes`. Do not blindly re-run the whole "
            "plan after a partial failure — that duplicates the workouts that did "
            "succeed. Re-create only the failed entries with `create_workout`.\n\n"
            "The athlete must sync their device for the plan to reach the watch."
        ),
        annotations=create_annotations,
        structured_output=True,
    )
    def create_training_plan(
        plan_name: Annotated[
            str,
            Field(
                min_length=1,
                max_length=80,
                description=(
                    "Label for this plan. Used only in the response; workout names "
                    "come from each entry."
                ),
                examples=["Marathon block - weeks 1-2"],
            ),
        ],
        start_date: Annotated[
            IsoDate,
            Field(
                description=(
                    "Date that `day_offset` 0 refers to, ISO YYYY-MM-DD. Every "
                    "workout is scheduled at start_date + its day_offset."
                )
            ),
        ],
        workouts: Annotated[
            list[PlannedWorkout],
            Field(
                min_length=1,
                max_length=30,
                description=(
                    "Workouts to create, each with the day offset it is scheduled "
                    "on. Order does not matter; offsets do."
                ),
            ),
        ],
    ) -> TrainingPlanResult:
        """Create and schedule a block of workouts.

        Args:
            plan_name: Label used in the response.
            start_date: Anchor date for the plan's day offsets.
            workouts: The plan entries.

        Returns:
            A :class:`TrainingPlanResult` with a per-workout outcome. Check
            ``failed`` before reporting success to the athlete.

        Raises:
            GarminMCPError: Only for failures that abort the whole plan, such as
                authentication. Per-workout failures are reported in
                ``outcomes``, not raised.
        """
        entries = sorted(workouts, key=lambda entry: entry.day_offset)
        outcomes: list[PlanWorkoutOutcome] = []

        for index, entry in enumerate(entries):
            target = start_date + timedelta(days=entry.day_offset)
            iso = target.isoformat()
            definition = entry.workout
            try:
                payload = build_running_workout(definition)
                response = session.call(
                    "plan.create_workout",
                    lambda api, payload=payload: api.upload_running_workout(payload),
                )
                workout_id = _as_str_id(
                    (response or {}).get("workoutId") if isinstance(response, dict) else None
                )
                if not workout_id:
                    raise GarminMCPError(
                        "Garmin accepted the upload but returned no workout ID.",
                        remediation="Check list_workouts before retrying this entry.",
                    )
                scheduled = False
                error: str | None = None
                try:
                    session.call(
                        "plan.schedule_workout",
                        lambda api, wid=workout_id, day=iso: api.schedule_workout(wid, day),
                    )
                    scheduled = True
                except GarminMCPError as exc:
                    error = f"created but not scheduled: {exc}"
                outcomes.append(
                    PlanWorkoutOutcome(
                        day_offset=entry.day_offset,
                        date=iso,
                        name=definition.name,
                        created=True,
                        workout_id=workout_id,
                        scheduled=scheduled,
                        error=error,
                    )
                )
            except (GarminMCPError, InvalidRequestError) as exc:
                outcomes.append(
                    PlanWorkoutOutcome(
                        day_offset=entry.day_offset,
                        date=iso,
                        name=definition.name,
                        created=False,
                        error=str(exc),
                    )
                )
            if index < len(entries) - 1:
                # Space out uploads; Garmin rate-limits bursts of writes.
                time.sleep(0.5)

        created = sum(1 for o in outcomes if o.created)
        end = start_date + timedelta(days=max(entry.day_offset for entry in entries))
        result = TrainingPlanResult(
            plan_name=plan_name,
            start_date=start_date.isoformat(),
            end_date=end.isoformat(),
            requested=len(entries),
            created=created,
            failed=len(entries) - created,
            outcomes=outcomes,
        )
        lines = [
            f"Plan '{plan_name}': {created}/{len(entries)} workouts created "
            f"({start_date.isoformat()} to {end.isoformat()})",
        ]
        for outcome in outcomes:
            mark = "ok " if outcome.created and outcome.scheduled else ("part" if outcome.created else "FAIL")
            lines.append(
                f"  {mark} {outcome.date} {outcome.name}"
                + (f" [{outcome.workout_id}]" if outcome.workout_id else "")
                + (f" — {outcome.error}" if outcome.error else "")
            )
        lines.append("Sync the Garmin device to download the plan.")
        return _result(result, "\n".join(lines))

    @app.tool(
        name="schedule_workout",
        title="Schedule an existing workout on a date",
        description=(
            "Place a workout that already exists in the library onto the Garmin "
            "calendar for a given date.\n\n"
            "`workout_id` comes from `list_workouts` or from the response of "
            "`create_workout`. Use this to reschedule a session the athlete "
            "missed, or to reuse a template on a new date, instead of creating a "
            "duplicate workout.\n\n"
            "Scheduling the same workout on the same date twice is harmless, but "
            "it does not remove any earlier scheduling of that workout on other "
            "dates."
        ),
        annotations=schedule_annotations,
        structured_output=True,
    )
    def schedule_workout(
        workout_id: Annotated[
            GarminId,
            Field(description="Garmin workout ID from `list_workouts` or `create_workout`."),
        ],
        scheduled_date: Annotated[
            IsoDate,
            Field(
                description="Calendar date to schedule the workout on, ISO YYYY-MM-DD.",
                alias="date",
            ),
        ],
    ) -> ScheduledWorkout:
        """Schedule an existing workout.

        Args:
            workout_id: Garmin workout ID.
            scheduled_date: Target date, exposed to clients as ``date``.

        Returns:
            A :class:`ScheduledWorkout` confirmation.

        Raises:
            NotFoundError: If no such workout exists.
            GarminMCPError: On authentication or upstream failure.
        """
        iso = scheduled_date.isoformat()
        session.call(
            "workouts.schedule",
            lambda api: api.schedule_workout(workout_id, iso),
        )
        result = ScheduledWorkout(workout_id=workout_id, scheduled_date=iso)
        return _result(result, f"Workout {workout_id} scheduled for {iso}. Sync the device.")

    @app.tool(
        name="delete_workout",
        title="Delete one saved workout",
        description=(
            "Permanently delete a single workout from the Garmin Connect library "
            "by its ID. Any calendar scheduling of that workout goes with it.\n\n"
            "`workout_id` comes from `list_workouts`. Prefer this over "
            "`delete_workouts` whenever the target is known: it removes exactly "
            "one thing and cannot over-match.\n\n"
            "This cannot be undone and Garmin has no trash. Deleting a workout "
            "does not delete any completed activity the athlete recorded from it."
        ),
        annotations=delete_annotations,
        structured_output=True,
    )
    def delete_workout(
        workout_id: Annotated[
            GarminId,
            Field(description="Garmin workout ID to delete, as returned by `list_workouts`."),
        ],
    ) -> WorkoutDeleted:
        """Delete one workout.

        Args:
            workout_id: Garmin workout ID.

        Returns:
            A :class:`WorkoutDeleted` confirmation.

        Raises:
            NotFoundError: If no such workout exists.
            GarminMCPError: On authentication or upstream failure.
        """
        session.call("workouts.delete", lambda api: api.delete_workout(workout_id))
        result = WorkoutDeleted(workout_id=workout_id, deleted=True)
        return _result(result, f"Workout {workout_id} deleted.")

    @app.tool(
        name="delete_workouts",
        title="Delete multiple workouts matching a filter (two-step)",
        description=(
            "Delete several workouts at once, selected either by an explicit list "
            "of IDs or by a name substring. Intended for removing a training plan "
            "before re-uploading it.\n\n"
            "This tool is deliberately hard to misuse:\n"
            "* A filter is REQUIRED. Exactly one of `name_contains` or "
            "`workout_ids` must be given, and `name_contains` must be at least "
            "two characters. There is no 'match everything' mode.\n"
            "* It is a two-step operation. The default call (`confirm=false`) "
            "deletes nothing and returns exactly which workouts WOULD be deleted. "
            "Show that list to the athlete, then re-call with identical arguments "
            "and `confirm=true` to apply it.\n"
            "* `max_deletions` caps the blast radius. If more workouts match than "
            "the cap allows, nothing is deleted and the tool tells you the count.\n\n"
            "Deletion is permanent and there is no trash. When you only need to "
            "remove one known workout, use `delete_workout` instead."
        ),
        annotations=delete_annotations,
        structured_output=True,
    )
    def delete_workouts(
        name_contains: Annotated[
            str | None,
            Field(
                default=None,
                min_length=2,
                max_length=80,
                description=(
                    "Case-insensitive substring that a workout's name must contain "
                    "to be deleted, e.g. a plan's name prefix. Must be at least "
                    "two characters. Mutually exclusive with `workout_ids`."
                ),
                examples=["W1 - "],
            ),
        ] = None,
        workout_ids: Annotated[
            list[GarminId] | None,
            Field(
                default=None,
                min_length=1,
                max_length=100,
                description=(
                    "Explicit workout IDs to delete, from `list_workouts`. The "
                    "safest option, because it cannot match anything you have not "
                    "seen. Mutually exclusive with `name_contains`."
                ),
            ),
        ] = None,
        confirm: Annotated[
            bool,
            Field(
                default=False,
                description=(
                    "Leave false for a dry run that reports what would be deleted "
                    "without deleting anything. Set true only after showing the "
                    "athlete that preview."
                ),
            ),
        ] = False,
        max_deletions: Annotated[
            int,
            Field(
                default=20,
                ge=1,
                le=100,
                description=(
                    "Refuse the operation if more than this many workouts match. "
                    "A safety cap on the blast radius; raise it deliberately."
                ),
            ),
        ] = 20,
        library_scan_limit: Annotated[
            int,
            Field(
                default=200,
                ge=1,
                le=500,
                description=(
                    "How many workouts to scan from the library when matching by "
                    "name. Workouts beyond this are never considered."
                ),
            ),
        ] = 200,
    ) -> BulkDeletionResult:
        """Delete workouts matching an explicit filter, with a dry-run first pass.

        Args:
            name_contains: Case-insensitive name substring, at least 2 characters.
            workout_ids: Explicit IDs to delete.
            confirm: False (default) previews; True applies.
            max_deletions: Refuse if more than this many workouts match.
            library_scan_limit: How much of the library to scan for name matches.

        Returns:
            A :class:`BulkDeletionResult`. In a dry run ``dry_run`` is true,
            ``deleted`` is 0 and ``candidates`` lists everything that would go.

        Raises:
            InvalidRequestError: If no filter, or both filters, are supplied, or
                if the match count exceeds ``max_deletions``.
            GarminMCPError: On authentication or upstream failure.
        """
        if (name_contains is None) == (workout_ids is None):
            raise InvalidRequestError(
                "delete_workouts requires exactly one filter: either name_contains "
                "or workout_ids.",
                remediation=(
                    "Call list_workouts first, then either pass the specific "
                    "workout_ids you want removed, or a name_contains substring of "
                    "at least two characters. This tool has no 'delete everything' "
                    "mode by design."
                ),
            )

        library = session.call(
            "workouts.list",
            lambda api: api.get_workouts(0, library_scan_limit),
        )
        rows: list[dict[str, Any]] = [item for item in (library or []) if isinstance(item, dict)]

        if workout_ids is not None:
            wanted = list(dict.fromkeys(workout_ids))
            known = {_as_str_id(r.get("workoutId")): r for r in rows}
            missing = [wid for wid in wanted if wid not in known]
            if missing:
                raise NotFoundError(
                    "These workout IDs are not in the library "
                    f"(scanned {len(rows)} workouts): {', '.join(missing)}.",
                    remediation=(
                        "Call list_workouts to get current IDs. If the library is "
                        "larger than the scan limit, raise library_scan_limit."
                    ),
                )
            matched = [known[wid] for wid in wanted]
            filter_description = f"{len(wanted)} explicit workout ID(s)"
        else:
            assert name_contains is not None
            needle = name_contains.lower()
            matched = [r for r in rows if needle in str(r.get("workoutName", "")).lower()]
            filter_description = f"name contains {name_contains!r}"

        if len(matched) > max_deletions:
            raise InvalidRequestError(
                f"{len(matched)} workouts match ({filter_description}), which "
                f"exceeds max_deletions={max_deletions}. Nothing was deleted.",
                remediation=(
                    "Narrow the filter, or raise max_deletions deliberately after "
                    "reviewing the list from list_workouts."
                ),
            )

        candidates: list[DeletionCandidate] = []
        deleted = 0
        failed = 0
        for row in matched:
            wid = _as_str_id(row.get("workoutId"))
            name = str(row.get("workoutName") or "Unnamed workout")
            if not confirm:
                candidates.append(DeletionCandidate(workout_id=wid, name=name, deleted=False))
                continue
            try:
                session.call("workouts.delete", lambda api, w=wid: api.delete_workout(w))
                deleted += 1
                candidates.append(DeletionCandidate(workout_id=wid, name=name, deleted=True))
            except GarminMCPError as exc:
                failed += 1
                candidates.append(
                    DeletionCandidate(workout_id=wid, name=name, deleted=False, error=str(exc))
                )
            time.sleep(0.3)

        result = BulkDeletionResult(
            dry_run=not confirm,
            filter_description=filter_description,
            matched=len(matched),
            deleted=deleted,
            failed=failed,
            library_size=len(rows),
            candidates=candidates,
        )
        if not matched:
            text = f"No workouts match {filter_description} (scanned {len(rows)})."
        elif not confirm:
            listing = "\n".join(f"  [{c.workout_id}] {c.name}" for c in candidates)
            text = (
                f"DRY RUN — nothing deleted. {len(matched)} workout(s) match "
                f"{filter_description}:\n{listing}\n"
                "Re-call with the same arguments and confirm=true to delete them."
            )
        else:
            listing = "\n".join(
                f"  [{c.workout_id}] {c.name}" + ("" if c.deleted else f" — FAILED: {c.error}")
                for c in candidates
            )
            text = f"Deleted {deleted}/{len(matched)} workout(s) ({failed} failed):\n{listing}"
        return _result(result, text)


# --------------------------------------------------------------------------- #
# Projection helpers
# --------------------------------------------------------------------------- #


def _pace_from_speed(speed_mps: Any) -> str | None:
    value = _as_float(speed_mps)
    if not value or value <= 0:
        return None
    return format_pace(1000.0 / value)


def _project_laps(raw: Any) -> list[LapSummary]:
    """Project Garmin's ``splitSummaries`` block, when present, onto lap rows."""
    if not isinstance(raw, list):
        return []
    laps: list[LapSummary] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            continue
        distance = _as_float(item.get("distance"))
        duration = _as_float(item.get("duration"))
        laps.append(
            LapSummary(
                lap=index,
                distance_meters=distance,
                duration_seconds=duration,
                pace=format_pace(pace_seconds_per_km(distance, duration)),
                average_heart_rate=_as_int(item.get("averageHR")),
            )
        )
    return laps


def _summarize_workout_row(raw: dict[str, Any]) -> WorkoutSummary:
    sport = raw.get("sportType") or {}
    return WorkoutSummary(
        workout_id=_as_str_id(raw.get("workoutId")),
        name=str(raw.get("workoutName") or "Unnamed workout"),
        sport_type=sport.get("sportTypeKey") if isinstance(sport, dict) else None,
        estimated_duration_seconds=_as_int(raw.get("estimatedDurationInSecs")),
        updated_date=raw.get("updateDate") or raw.get("updatedDate"),
        description=raw.get("description"),
    )


# --------------------------------------------------------------------------- #
# Health metric collection
# --------------------------------------------------------------------------- #


def _metric_ok(
    key: str, label: str, value: float | None, unit: str | None, display: str, detail: str | None = None
) -> HealthMetric:
    return HealthMetric(
        metric=key,
        label=label,
        status=MetricStatus.OK,
        value=value,
        unit=unit,
        display=display,
        detail=detail,
    )


def _metric_absent(key: str, label: str) -> HealthMetric:
    return HealthMetric(metric=key, label=label, status=MetricStatus.NOT_RECORDED)


def _collect_health_metrics(session: GarminSession, iso: str) -> list[HealthMetric]:
    """Query every daily metric independently, recording per-metric outcomes.

    The legacy server wrapped each metric in ``except Exception: pass``, so a
    broken call and a genuinely empty day were indistinguishable — and its steps
    call was broken on every invocation because ``get_daily_steps`` takes a start
    *and* an end date. Here each metric is fetched separately, a
    :class:`GarminMCPError` is recorded against that metric alone, and the
    distinction between "no data" and "fetch failed" survives into the output.

    Authentication and configuration errors are re-raised immediately: they will
    affect every metric, and reporting eight identical failures instead of one
    actionable error would be dishonest.
    """
    extractors: list[tuple[str, str, Any]] = [
        ("steps", "Steps", _steps_metric),
        ("resting_heart_rate", "Resting HR", _resting_hr_metric),
        ("hrv", "HRV", _hrv_metric),
        ("sleep", "Sleep", _sleep_metric),
        ("body_battery", "Body battery", _body_battery_metric),
        ("stress", "Stress", _stress_metric),
        ("spo2", "SpO2", _spo2_metric),
        ("training_readiness", "Training readiness", _training_readiness_metric),
    ]

    metrics: list[HealthMetric] = []
    first_failure: GarminMCPError | None = None
    for key, label, extractor in extractors:
        try:
            metrics.append(extractor(session, iso, key, label))
        except (AuthenticationError, ConfigurationError):
            raise
        except GarminMCPError as exc:
            if first_failure is None:
                first_failure = exc
            remediation = getattr(exc, "remediation", None)
            detail = f"{exc}" + (f" ({remediation})" if remediation else "")
            metrics.append(
                HealthMetric(
                    metric=key,
                    label=label,
                    status=MetricStatus.FETCH_FAILED,
                    detail=detail,
                )
            )

    if first_failure is not None and all(m.status is MetricStatus.FETCH_FAILED for m in metrics):
        # Every single metric failed: this is an outage or a bad date, not a
        # sparsely-recorded day. Surface it as a real error instead of returning
        # an empty summary that looks like a successful call.
        raise first_failure
    return metrics


def _steps_metric(session: GarminSession, iso: str, key: str, label: str) -> HealthMetric:
    # get_daily_steps takes a start AND an end date; the legacy server passed one
    # argument, so this metric raised TypeError on every single call.
    raw = session.call("health.steps", lambda api: api.get_daily_steps(iso, iso))
    if not isinstance(raw, list) or not raw:
        return _metric_absent(key, label)
    total = sum(int(item.get("totalSteps") or 0) for item in raw if isinstance(item, dict))
    if total <= 0:
        return _metric_absent(key, label)
    return _metric_ok(key, label, float(total), "steps", f"{total:,}")


def _resting_hr_metric(session: GarminSession, iso: str, key: str, label: str) -> HealthMetric:
    raw = session.call("health.heart_rate", lambda api: api.get_heart_rates(iso))
    value = raw.get("restingHeartRate") if isinstance(raw, dict) else None
    if value is None:
        return _metric_absent(key, label)
    return _metric_ok(key, label, float(value), "bpm", f"{int(value)} bpm")


def _hrv_metric(session: GarminSession, iso: str, key: str, label: str) -> HealthMetric:
    raw = session.call("health.hrv", lambda api: api.get_hrv_data(iso))
    summary = raw.get("hrvSummary") if isinstance(raw, dict) else None
    if not isinstance(summary, dict):
        return _metric_absent(key, label)
    value = summary.get("lastNightAvg") or summary.get("weeklyAvg")
    if value is None:
        return _metric_absent(key, label)
    status = summary.get("status")
    return _metric_ok(
        key,
        label,
        float(value),
        "ms",
        f"{int(value)} ms",
        detail=f"status {status}" if status else None,
    )


def _sleep_metric(session: GarminSession, iso: str, key: str, label: str) -> HealthMetric:
    raw = session.call("health.sleep", lambda api: api.get_sleep_data(iso))
    dto = raw.get("dailySleepDTO") if isinstance(raw, dict) else None
    if not isinstance(dto, dict):
        return _metric_absent(key, label)
    seconds = dto.get("sleepTimeSeconds")
    if not seconds:
        return _metric_absent(key, label)
    scores = dto.get("sleepScores")
    overall = None
    if isinstance(scores, dict) and isinstance(scores.get("overall"), dict):
        overall = scores["overall"].get("value")
    return _metric_ok(
        key,
        label,
        float(seconds),
        "seconds",
        format_duration(float(seconds)) or f"{seconds}s",
        detail=f"sleep score {int(overall)}" if overall else None,
    )


def _body_battery_metric(session: GarminSession, iso: str, key: str, label: str) -> HealthMetric:
    raw = session.call("health.body_battery", lambda api: api.get_body_battery(iso, iso))
    levels: list[float] = []
    if isinstance(raw, list):
        for day in raw:
            if not isinstance(day, dict):
                continue
            for reading in day.get("bodyBatteryValuesArray") or []:
                if isinstance(reading, list) and len(reading) >= 2 and reading[1] is not None:
                    levels.append(float(reading[1]))
            for field in ("charged", "drained"):
                if day.get(field) is not None:
                    levels.append(float(day[field]))
    if not levels:
        return _metric_absent(key, label)
    return _metric_ok(
        key,
        label,
        max(levels),
        "level",
        f"{int(min(levels))}-{int(max(levels))}",
        detail="range observed across the day",
    )


def _stress_metric(session: GarminSession, iso: str, key: str, label: str) -> HealthMetric:
    raw = session.call("health.stress", lambda api: api.get_stress_data(iso))
    value = raw.get("avgStressLevel") if isinstance(raw, dict) else None
    if value is None and isinstance(raw, dict):
        value = raw.get("overallStressLevel")
    if value is None or float(value) < 0:
        return _metric_absent(key, label)
    return _metric_ok(key, label, float(value), "0-100", f"{int(value)}/100")


def _spo2_metric(session: GarminSession, iso: str, key: str, label: str) -> HealthMetric:
    raw = session.call("health.spo2", lambda api: api.get_spo2_data(iso))
    value = raw.get("averageSpO2") if isinstance(raw, dict) else None
    if value is None:
        return _metric_absent(key, label)
    lowest = raw.get("lowestSpO2") if isinstance(raw, dict) else None
    return _metric_ok(
        key,
        label,
        float(value),
        "%",
        f"{int(value)}%",
        detail=f"lowest {int(lowest)}%" if lowest else None,
    )


def _training_readiness_metric(session: GarminSession, iso: str, key: str, label: str) -> HealthMetric:
    raw = session.call("health.training_readiness", lambda api: api.get_training_readiness(iso))
    entry: dict[str, Any] | None = None
    if isinstance(raw, list) and raw and isinstance(raw[0], dict):
        entry = raw[0]
    elif isinstance(raw, dict):
        entry = raw
    if entry is None or entry.get("score") is None:
        return _metric_absent(key, label)
    score = entry["score"]
    level = entry.get("level")
    return _metric_ok(
        key,
        label,
        float(score),
        "0-100",
        f"{int(score)}/100",
        detail=str(level) if level else None,
    )
