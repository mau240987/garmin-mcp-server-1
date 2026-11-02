"""`garmin-coach-push`: send a saved plan's next workouts to Garmin from a terminal or CI.

Runs the `push_plan_to_garmin` MCP tool in-process, so every safety rule of the tool applies:
a dry run lists what would be created, scheduled and deleted; a real push (`--apply`) must
repeat the counts the dry run reported. Garmin writes are enabled only with `--apply`.

Designed for a private "coach data" repository (goals and plans saved by the assistant) and a
manually triggered GitHub Actions workflow: see docs/garmin-push-actions.md.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import anyio
from mcp.client import Client

from garmin_coach_mcp.cli import build_router
from garmin_coach_mcp.coach.storage import CoachStore
from garmin_coach_mcp.coach.tools import WRITES_UNAVAILABLE_NOTE
from garmin_coach_mcp.config import Settings
from garmin_coach_mcp.errors import ConfigurationError, GarminMCPError, NotFoundError
from garmin_coach_mcp.server import build_server


def active_plan_id(store: CoachStore, today: dt.date) -> str:
    """Current plan of the next race: the goal with the earliest race date not in the past
    (the most recently saved one when a goal was re-set for the same date), its latest plan
    that was not replaced. A finished tune-up race never hides the main goal."""
    goals = [g for g in store.list_goals() if g.race_date >= today]
    newest_first = sorted(goals, key=lambda g: g.created_at, reverse=True)
    upcoming = sorted(newest_first, key=lambda g: g.race_date)  # stable: newest first per date
    for goal in upcoming:
        plans = [p for p in store.list_plans(goal.id) if not p.superseded_by]
        if plans:
            return plans[-1].id
    raise NotFoundError(
        "No plan for an upcoming race.",
        remediation="Generate a plan with the assistant (generate_training_plan).",
    )


def push_complete(data: dict[str, Any]) -> bool:
    """A real push sent everything: no failed create/schedule, no obsolete workout left,
    no uncertain session."""
    failed = [r for r in data.get("created", []) if isinstance(r, dict) and not r.get("ok", True)]
    left = int(data.get("to_delete", 0)) - len(data.get("deleted", []))
    return not failed and left <= 0 and not data.get("uncertain")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="garmin-coach-push", description=__doc__.splitlines()[0])
    p.add_argument("--data-dir", type=Path, required=True, help="Coach data (goals/, plans/).")
    p.add_argument("--plan-id", help="Plan to send (default: the active plan, next race first).")
    p.add_argument("--from-date", type=dt.date.fromisoformat, help="First day (default today).")
    p.add_argument("--weeks", type=int, default=2, choices=range(1, 5), metavar="1-4")
    p.add_argument("--apply", action="store_true", help="Really send (default: dry run).")
    p.add_argument("--expected-count", type=int, help="Workouts to send, from the dry run.")
    p.add_argument("--expected-delete-count", type=int, help="Obsolete workouts, from the dry run.")
    p.add_argument("--retry-uncertain", action="store_true")
    p.add_argument("--summary", type=Path, help="Append the Markdown report to this file.")
    return p.parse_args(argv)


def dry_run_text(text: str, apply: bool) -> str:
    """The tool's text; in a dry run, without the "writes not available" note, which here only
    reflects that writes are enabled solely by --apply."""
    if apply:
        return text.strip()
    lines = [line for line in text.strip().splitlines() if line != WRITES_UNAVAILABLE_NOTE]
    lines.append("_Dry run: nothing was sent. Garmin writes are enabled only with apply._")
    return "\n".join(lines)


def _report(lines: list[str], summary: Path | None) -> None:
    text = "\n".join(lines) + "\n"
    sys.stdout.write(text)
    if summary is not None:
        with summary.open("a", encoding="utf-8") as fh:
            fh.write(text)


async def run(args: argparse.Namespace) -> int:
    settings = Settings.from_env(
        read_only=False,
        allow_unofficial_writes=args.apply,
        coach_data_dir=args.data_dir.absolute(),
    )
    router = build_router(settings)
    store = CoachStore(settings.coach_data_dir)
    app = build_server(router, store=store)
    mode = "PUSH" if args.apply else "DRY RUN"
    lines = [f"## Garmin push — {mode}", ""]

    if not args.apply:
        # Check the Garmin login now (read-only), so a blocked login shows up in the dry run.
        login = next((b for b in router.backends if not b.official), None)
        try:
            if login is None or not settings.has_unofficial_session:
                raise ConfigurationError(
                    "No Garmin login configured.",
                    remediation="Set GARMIN_EMAIL and GARMIN_PASSWORD (Actions secrets).",
                )
            await anyio.to_thread.run_sync(lambda: login.list_workouts(0, 1))
            lines.append("- Garmin login: OK (read-only check)")
        except GarminMCPError as exc:
            lines.append(f"- Garmin login: **FAILED** — {exc}")
            _report(lines, args.summary)
            return 2

    try:
        plan_id = args.plan_id or active_plan_id(store, dt.date.today())
    except NotFoundError as exc:
        _report([*lines, f"**Error:** {exc}"], args.summary)
        return 1
    params: dict[str, Any] = {"plan_id": plan_id, "weeks": args.weeks, "dry_run": not args.apply}
    if args.from_date:
        params["from_date"] = args.from_date.isoformat()
    if args.retry_uncertain:
        params["retry_uncertain"] = True
    if args.apply:
        params["expected_count"] = args.expected_count
        if args.expected_delete_count is not None:
            params["expected_delete_count"] = args.expected_delete_count

    async with Client(app) as client:
        result = await client.call_tool("push_plan_to_garmin", params)
    if result.is_error:
        text = getattr(result.content[0], "text", "") if result.content else "(no message)"
        _report([*lines, f"**Error:** {text}"], args.summary)
        return 1
    data = result.structured_content or {}
    lines += [
        f"- Plan: `{data.get('plan_id')}`",
        f"- To create: {data.get('to_create')} · to schedule: {data.get('to_schedule')} · "
        f"obsolete to delete: {data.get('to_delete')}",
        "",
        dry_run_text(str(data.get("text", "")), args.apply),
    ]
    if not args.apply:
        send = int(data.get("to_create", 0)) + int(data.get("to_schedule", 0))
        lines += [
            "",
            "To send these workouts, run the workflow again with `apply` checked, "
            f"`expected_count` = **{send}** and `expected_delete_count` = "
            f"**{data.get('to_delete', 0)}** (same plan, from_date and weeks).",
        ]
    _report(lines, args.summary)
    return 0 if not args.apply or push_complete(data) else 1


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.apply and args.expected_count is None:
        sys.stderr.write("--apply needs --expected-count from the dry run.\n")
        return 2
    return anyio.run(run, args)


if __name__ == "__main__":
    sys.exit(main())
