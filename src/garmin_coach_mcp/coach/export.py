"""Plan export (C4): Markdown, iCalendar, FIT workouts. Pure functions returning bytes/str.

Every export carries the Garmin attribution next to the data when the plan was derived from
Garmin data (API Brand Guidelines: exports keep the attribution adjacent to the data).
"""

from __future__ import annotations

import datetime as dt
import io
import zipfile

from garmin_coach_mcp.coach.models import PlannedSession, RaceGoal, SessionKind, TrainingPlan
from garmin_coach_mcp.coach.render import plan_summary
from garmin_coach_mcp.coach.sessions import session_steps, workout_name
from garmin_coach_mcp.models import RepeatStep, WorkoutStep

PRODID = "-//garmin-coach-mcp//Race Coach//EN"


def select_sessions(
    plan: TrainingPlan, start: dt.date | None, end: dt.date | None
) -> list[tuple[int, PlannedSession]]:
    return [
        (w.index, s)
        for w in plan.weeks
        for s in w.sessions
        if (start is None or s.date >= start) and (end is None or s.date <= end)
    ]


# ---------------------------------------------------------------------------- Markdown


def to_markdown(plan: TrainingPlan, goal: RaceGoal) -> str:
    return plan_summary(plan, goal, detailed=True) + "\n"


# --------------------------------------------------------------------------- iCalendar


def _ics_escape(text: str) -> str:
    return (
        text.replace("\r", "")
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def _fold(line: str) -> str:
    """RFC 5545: lines of at most 75 octets, continuation lines start with a space."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    parts: list[str] = []
    while raw:
        limit = 75 if not parts else 74
        cut = min(limit, len(raw))
        while cut < len(raw) and (raw[cut] & 0xC0) == 0x80:  # never split a UTF-8 sequence
            cut -= 1
        parts.append(raw[:cut].decode("utf-8"))
        raw = raw[cut:]
    return "\r\n ".join(parts)


def to_ics(
    plan: TrainingPlan,
    goal: RaceGoal,
    start: dt.date | None = None,
    end: dt.date | None = None,
    stamp_at: dt.datetime | None = None,
) -> str:
    """One all-day event per session. UID = plan + date and SEQUENCE = plan revision, so
    re-importing a revised plan updates events; removed/moved sessions are CANCELLED."""
    stamp = (stamp_at or plan.created_at).astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    name = goal.race_name or goal.race_type.value.replace("_", " ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        f"X-WR-CALNAME:{_ics_escape('Race Coach - ' + name)}",
    ]
    for week, s in select_sessions(plan, start, end):
        title = (
            f"RACE: {name}"
            if s.kind is SessionKind.RACE
            else f"{s.kind.value.replace('_', ' ')} {s.distance_km:g} km ({s.zone})"
        )
        desc = [s.description]
        if s.pace_hint:
            desc.append(f"Pace: {s.pace_hint}")
        desc.append(f"Week {week}, plan {plan.id} rev {plan.revision}")
        if plan.attribution:
            desc.append(plan.attribution)
        lines += [
            "BEGIN:VEVENT",
            f"UID:{plan.id}-{s.date:%Y%m%d}@garmin-coach-mcp",
            f"SEQUENCE:{plan.revision}",
            f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{s.date:%Y%m%d}",
            f"DTEND;VALUE=DATE:{s.date + dt.timedelta(days=1):%Y%m%d}",
            f"SUMMARY:{_ics_escape(title)}",
            f"DESCRIPTION:{_ics_escape(chr(10).join(desc))}",
            "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ]
    current = {s.date for _w, s in select_sessions(plan, None, None)}
    for day in sorted(set(plan.cancelled_dates) - current):
        if (start is None or day >= start) and (end is None or day <= end):
            lines += [
                "BEGIN:VEVENT",
                f"UID:{plan.id}-{day:%Y%m%d}@garmin-coach-mcp",
                f"SEQUENCE:{plan.revision}",
                f"DTSTAMP:{stamp}",
                f"DTSTART;VALUE=DATE:{day:%Y%m%d}",
                f"DTEND;VALUE=DATE:{day + dt.timedelta(days=1):%Y%m%d}",
                "SUMMARY:Cancelled session",
                "STATUS:CANCELLED",
                "END:VEVENT",
            ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


# --------------------------------------------------------------------------------- FIT


def _fit_workout(
    name: str,
    steps: list[WorkoutStep | RepeatStep],
    created: dt.datetime,
    description: str | None = None,
) -> bytes:
    from fit_tool.fit_file_builder import FitFileBuilder
    from fit_tool.profile.messages.file_id_message import FileIdMessage
    from fit_tool.profile.messages.workout_message import WorkoutMessage
    from fit_tool.profile.messages.workout_step_message import WorkoutStepMessage
    from fit_tool.profile.profile_type import (
        FileType,
        Intensity,
        Manufacturer,
        Sport,
        WorkoutStepDuration,
        WorkoutStepTarget,
    )

    intensity = {
        "warmup": Intensity.WARMUP,
        "cooldown": Intensity.COOLDOWN,
        "recover": Intensity.RECOVERY,
        "rest": Intensity.REST,
        "run": Intensity.ACTIVE,
    }

    def step_msg(step: WorkoutStep, index: int) -> WorkoutStepMessage:
        m = WorkoutStepMessage()
        m.message_index = index
        m.intensity = intensity[step.type]
        if step.duration_type == "time":
            m.duration_type = WorkoutStepDuration.TIME
            m.duration_time = float(step.duration_value or 0)
        elif step.duration_type == "distance":
            m.duration_type = WorkoutStepDuration.DISTANCE
            m.duration_distance = float(step.duration_value or 0)
        else:
            m.duration_type = WorkoutStepDuration.OPEN
        if step.pace_low and step.pace_high and step.primary != "none":
            from garmin_coach_mcp.formatting import pace_to_meters_per_second

            m.target_type = WorkoutStepTarget.SPEED
            m.target_value = 0  # 0 = custom range
            m.custom_target_speed_low = pace_to_meters_per_second(step.pace_low)
            m.custom_target_speed_high = pace_to_meters_per_second(step.pace_high)
        else:
            m.target_type = WorkoutStepTarget.OPEN
        return m

    messages: list[WorkoutStepMessage] = []
    for item in steps:
        if isinstance(item, RepeatStep):
            first = len(messages)
            for child in item.steps:
                messages.append(step_msg(child, len(messages)))
            rep = WorkoutStepMessage()
            rep.message_index = len(messages)
            rep.duration_type = WorkoutStepDuration.REPEAT_UNTIL_STEPS_CMPLT
            rep.duration_step = first
            rep.target_repeat_steps = item.repeat_count
            messages.append(rep)
        else:
            messages.append(step_msg(item, len(messages)))

    file_id = FileIdMessage()
    file_id.type = FileType.WORKOUT
    file_id.manufacturer = Manufacturer.DEVELOPMENT.value
    file_id.product = 0
    file_id.serial_number = 0x12345678
    file_id.time_created = round(created.timestamp() * 1000)

    workout = WorkoutMessage()
    workout.sport = Sport.RUNNING
    workout.workout_name = name
    workout.num_valid_steps = len(messages)
    if description:
        workout.workout_description = description[:120]

    builder = FitFileBuilder(auto_define=True, min_string_size=50)
    builder.add(file_id)
    builder.add(workout)
    builder.add_all(messages)
    data: bytes = builder.build().to_bytes()
    return data


def to_fit_zip(
    plan: TrainingPlan,
    goal: RaceGoal,
    start: dt.date | None = None,
    end: dt.date | None = None,
) -> tuple[bytes, int]:
    """Zip of one FIT workout file per session (races excluded). Returns (zip bytes, count)."""
    buf = io.BytesIO()
    count = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if plan.attribution:
            zf.writestr("ATTRIBUTION.txt", plan.attribution + "\n")
        for week, s in select_sessions(plan, start, end):
            if s.kind is SessionKind.RACE:
                continue
            steps = session_steps(s, plan.easy_pace, goal.race_pace_s_per_km, plan.threshold_pace)
            name = workout_name(s, week)
            # Each FIT file travels alone once imported: attribution inside it (Brand Guidelines).
            data = _fit_workout(name, steps, plan.created_at, plan.attribution)
            info = zipfile.ZipInfo(f"{s.date:%Y-%m-%d}_{s.kind.value}.fit")
            info.date_time = (*plan.created_at.timetuple()[:6],)  # deterministic archive
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)
            count += 1
    return buf.getvalue(), count
