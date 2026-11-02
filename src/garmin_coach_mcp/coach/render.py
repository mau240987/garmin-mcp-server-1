"""Human-readable rendering of a plan (the 'versione leggibile' of C2; md export in C4)."""

from __future__ import annotations

from garmin_coach_mcp.coach.models import RaceGoal, SessionKind, TrainingPlan
from garmin_coach_mcp.formatting import format_pace

_WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _goal_line(goal: RaceGoal) -> str:
    name = goal.race_name or goal.race_type.value.replace("_", " ")
    target = (
        f"goal {goal.target_time_s // 3600}:{goal.target_time_s % 3600 // 60:02d}:"
        f"{goal.target_time_s % 60:02d} (~{format_pace(goal.race_pace_s_per_km or 0)})"
        if goal.target_time_s
        else "goal: finish"
    )
    return f"{name}, {goal.distance_km:g} km on {goal.race_date.isoformat()} — {target}"


def plan_summary(plan: TrainingPlan, goal: RaceGoal, *, detailed: bool = False) -> str:
    lines = [f"# Training plan: {_goal_line(goal)}"]
    if plan.attribution:
        lines.append(plan.attribution)  # API Brand Guidelines: next to the title
    lines += [
        "",
        f"Level: {plan.level.value} · {len(plan.weeks)} weeks from {plan.start_date} · "
        f"peak {plan.peak_weekly_km:g} km/week · {goal.days_per_week} running days/week",
    ]
    if plan.warnings:
        lines += ["", "## Warnings", *[f"- {w}" for w in plan.warnings]]
    lines += ["", "| Week | Start | Phase | km | Long run |", "|---|---|---|---|---|"]
    for w in plan.weeks:
        phase = w.phase.value + (" (recovery)" if w.is_recovery else "")
        lines.append(
            f"| {w.index} | {w.start_date} | {phase} | {w.target_km:g} | {w.long_run_km:g} |"
        )
    if detailed:
        for w in plan.weeks:
            lines += ["", f"## Week {w.index} — {w.phase.value}, {w.target_km:g} km"]
            for s in w.sessions:
                km = f"{s.distance_km:g} km" if s.kind is not SessionKind.RACE else "race"
                extra = f" · {s.pace_hint}" if s.pace_hint else ""
                lines.append(
                    f"- {_WEEKDAYS[s.date.weekday()]} {s.date}: {s.kind.value}, {km}, "
                    f"{s.zone} — {s.description}{extra}"
                )
    lines += ["", *[f"_{n}_" for n in plan.notes]]
    return "\n".join(lines)
