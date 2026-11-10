"""`garmin-coach-weekly`: the weekly check-in, automated.

1. Reviews the week that just ended (plan vs Garmin activities, recovery signals) with the
   `review_week` tool.
2. Applies the review's suggested_changes to the coming weeks with `apply_plan_update`. These
   are only the engine's bounded changes: lighter next week after missed sessions or several
   recovery signals, hard sessions made easy. Never more volume.
3. Reports whether the performance level (VDOT) moved enough to regenerate the plan's paces.
4. Dry-runs `push_plan_to_garmin` for the coming week; with `--auto-push` it then sends exactly
   what the dry run listed (writes are enabled only then).

With `--preview` it runs the same steps as next Monday's check-in on a throwaway copy of the
data: the report shows how the plan would be adapted and what would be sent to Garmin, while
the saved plan and Garmin stay untouched.

Every step goes through the MCP tools, so their rules (expected_revision, dry-run counts) hold.
See docs/garmin-push-actions.md for the scheduled GitHub Actions workflow.
"""

from __future__ import annotations

import argparse
import datetime as dt
import shutil
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import anyio
from mcp.client import Client

from garmin_coach_mcp.cli import build_router
from garmin_coach_mcp.coach.models import TrainingPlan
from garmin_coach_mcp.coach.storage import CoachStore
from garmin_coach_mcp.config import Settings
from garmin_coach_mcp.errors import NotFoundError
from garmin_coach_mcp.push_cli import active_plan_id, dry_run_text, push_complete
from garmin_coach_mcp.server import build_server

#: A VDOT change at least this large is worth regenerating the plan's paces.
VDOT_DRIFT = 1.0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="garmin-coach-weekly", description=__doc__.splitlines()[0])
    p.add_argument("--data-dir", type=Path, required=True, help="Coach data (goals/, plans/).")
    p.add_argument("--plan-id", help="Plan (default: the active plan, next race first).")
    p.add_argument("--today", type=dt.date.fromisoformat, help=argparse.SUPPRESS)
    p.add_argument("--weeks", type=int, default=1, choices=range(1, 5), metavar="1-4")
    p.add_argument("--no-adapt", action="store_true", help="Review only, change nothing.")
    p.add_argument("--auto-push", action="store_true", help="Send the week's workouts.")
    p.add_argument("--summary", type=Path, help="Append the Markdown report to this file.")
    p.add_argument(
        "--preview",
        action="store_true",
        help="Next Monday's check-in on a copy of the data: changes and sends nothing.",
    )
    args = p.parse_args(argv)
    if args.preview and args.auto_push:
        p.error("--preview never sends to Garmin: drop --auto-push.")
    return args


def _text(result: Any) -> str:
    return getattr(result.content[0], "text", "") if result.content else "(no message)"


def next_monday(day: dt.date) -> dt.date:
    """The Monday after `day` (a Monday gives the following one)."""
    return day + dt.timedelta(days=7 - day.weekday())


async def run(args: argparse.Namespace) -> int:
    if not args.preview:
        return await _run(args)
    as_of = args.today or dt.date.today()
    monday = next_monday(as_of)
    with tempfile.TemporaryDirectory(prefix="coach-preview-") as tmp:
        copy = Path(tmp) / "data"
        shutil.copytree(args.data_dir, copy, ignore=shutil.ignore_patterns(".git"))
        header = [
            f"## Race Coach — preview of the week of {monday} (data as of {as_of})",
            "",
            "Preview: nothing is changed or sent now. Monday's check-in repeats these steps "
            "with the data synced by then.",
        ]
        return await _run(argparse.Namespace(**{**vars(args), "data_dir": copy}), monday, header)


async def _run(
    args: argparse.Namespace, today: dt.date | None = None, header: list[str] | None = None
) -> int:
    today = today or args.today or dt.date.today()
    settings = Settings.from_env(
        read_only=False,
        allow_unofficial_writes=args.auto_push,
        coach_data_dir=args.data_dir.absolute(),
    )
    store = CoachStore(settings.coach_data_dir)
    app = build_server(build_router(settings), store=store, today=lambda: today)
    out = [*(header or [f"## Race Coach — weekly check-in ({today})"]), ""]
    status = 0
    try:
        plan_id = args.plan_id or active_plan_id(store, today)
    except NotFoundError as exc:
        out.append(f"**Nothing to do:** {exc}")
        _report(out, args.summary)
        return 0
    plan = store.get_plan(plan_id)
    out.append(f"Plan `{plan_id}` (revision {plan.revision}).")

    async with Client(app) as client:
        # 1-2. Review the last complete week (Monday-Sunday) and adapt the coming weeks.
        last_week = today - dt.timedelta(days=today.weekday() + 7)
        reviewed = plan.week_of(last_week)
        if reviewed is None:
            out += ["", "No plan week last week: nothing to review."]
        else:
            r = await client.call_tool(
                "review_week", {"plan_id": plan_id, "week_start": last_week.isoformat()}
            )
            if r.is_error:
                out += ["", f"**Review failed:** {_text(r)}"]
                status = 1
            else:
                review = (r.structured_content or {}).get("review", {})
                out += ["", "### Last week", "", str((r.structured_content or {}).get("text"))]
                changes = _future_changes(review.get("suggested_changes") or [], today)
                note = _skip_reason(plan, reviewed.index, review, args.no_adapt)
                if changes and note:
                    out += ["", f"Suggested changes **not applied**: {note}", f"`{changes}`"]
                elif changes:
                    u = await client.call_tool(
                        "apply_plan_update",
                        {
                            "plan_id": plan_id,
                            "changes": changes,
                            "expected_revision": review.get("plan_revision"),
                        },
                    )
                    out += ["", "### Plan adapted", ""]
                    if u.is_error:
                        out.append(f"**Not applied:** {_text(u)}")
                        status = 1
                    else:
                        out += [f"- {a}" for a in (u.structured_content or {}).get("applied", [])]
                        _mark_reviewed(store, plan_id, reviewed.index)
                else:
                    out += ["", "No change to the plan: continue as written."]
                    if not args.no_adapt:
                        _mark_reviewed(store, plan_id, reviewed.index)

        # 3. Has the performance level moved?
        p = await client.call_tool("get_athlete_profile", {"weeks": 12})
        if not p.is_error:
            base = ((p.structured_content or {}).get("profile") or {}).get("baseline") or {}
            now, then = base.get("vdot"), plan.baseline.vdot
            if now and then and abs(now - then) >= VDOT_DRIFT:
                out += [
                    "",
                    f"**Performance level changed:** VDOT {then:g} in the plan, {now:g} now "
                    f"({base.get('vdot_source')}). Regenerate the plan's paces with the "
                    "assistant (regenerate_from next Monday) to train at the new level.",
                ]

        # 4. The coming week on Garmin.
        params: dict[str, Any] = {"plan_id": plan_id, "weeks": args.weeks, "dry_run": True}
        d = await client.call_tool("push_plan_to_garmin", params)
        out += ["", "### Garmin", ""]
        if d.is_error:
            out.append(f"**Dry run failed:** {_text(d)}")
            _report(out, args.summary)
            return 1
        dry = d.structured_content or {}
        send = int(dry.get("to_create", 0)) + int(dry.get("to_schedule", 0))
        delete = int(dry.get("to_delete", 0))
        out.append(dry_run_text(str(dry.get("text", "")), apply=False))
        if args.preview:
            out += [
                "",
                f"If nothing changes, Monday's check-in sends these **{send}** workout(s)"
                + (f" and deletes **{delete}** obsolete one(s)" if delete else "")
                + " (when GARMIN_AUTO_PUSH is on).",
            ]
        elif not args.auto_push:
            out += [
                "",
                "Automatic push is off. To send: *Push to Garmin* with `apply`, "
                f"`plan_id` = `{plan_id}`, `weeks` = {args.weeks}, `expected_count` = "
                f"**{send}**, `expected_delete_count` = **{delete}**.",
            ]
        elif send or delete:
            push = {**params, "dry_run": False, "expected_count": send}
            if delete:
                push["expected_delete_count"] = delete
            res = await client.call_tool("push_plan_to_garmin", push)
            if res.is_error:
                out += ["", f"**Push failed:** {_text(res)}"]
                status = 1
            else:
                data = res.structured_content or {}
                out += ["", str(data.get("text", "")).strip()]
                if not push_complete(data):
                    out.append("**Not everything was sent or deleted: check Garmin Connect.**")
                    status = 1
        else:
            out += ["", "Nothing to send: Garmin is up to date."]
    _report(out, args.summary)
    return status


def _future_changes(changes: list[dict[str, Any]], today: dt.date) -> list[dict[str, Any]]:
    """Session changes dated before today cannot apply (and would refuse the whole update)."""
    return [c for c in changes if "date" not in c or dt.date.fromisoformat(c["date"]) >= today]


def _skip_reason(
    plan: TrainingPlan, week: int, review: dict[str, Any], no_adapt: bool
) -> str | None:
    """Why unattended adaptation must not apply the suggestions (None = apply)."""
    if no_adapt:
        return "review only (--no-adapt)."
    if week in plan.auto_reviewed_weeks:
        return f"week {week} was already reviewed and adapted by an earlier run."
    if not review.get("actual_km_to_date"):
        # Nothing synced (or Garmin still processing Sunday's run): not evidence of a missed
        # week. Cutting volume on that would be wrong; the athlete decides with the assistant.
        return "no activity found in Garmin for that week (not synced yet?)."
    return None


def _mark_reviewed(store: CoachStore, plan_id: str, week: int) -> None:
    plan = store.get_plan(plan_id)
    if week not in plan.auto_reviewed_weeks:
        plan.auto_reviewed_weeks.append(week)
        store.save_plan(plan)


def _report(lines: list[str], summary: Path | None) -> None:
    text = "\n".join(lines) + "\n"
    sys.stdout.write(text)
    if summary is not None:
        with summary.open("a", encoding="utf-8") as fh:
            fh.write(text)


def main(argv: Sequence[str] | None = None) -> int:
    return anyio.run(run, parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
