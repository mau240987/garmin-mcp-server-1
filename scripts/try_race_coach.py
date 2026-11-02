#!/usr/bin/env python3
"""Practical test on YOUR Garmin data: profile -> goal -> plan -> exports. Read-only.

Runs the real MCP tools in-process (the same code paths an assistant uses), with Garmin writes
forced off: nothing is created, scheduled or deleted on Garmin. Goals, plans and exports go to
a separate folder (default ./race-coach-trial), not to your normal data directory.

    pip install -e .                               # from the repo (editable)
    export GARMIN_PERSONAL_API_KEY=...             # never paste it in chat
    python3 scripts/try_race_coach.py --race-type ultra --distance 100 \\
        --date 2027-05-29 --name Passatore --days 5 > race-coach-trial.md

The report contains your training summary (volumes, paces, recovery signals): review it before
sharing it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import inspect
import json
import sys
from pathlib import Path
from typing import Any

STALE_INSTALL = (
    "The installed garmin_coach_mcp package ({where}) is older than this script.\n"
    "A plain `pip install .` copies a snapshot, so `git pull` does not update it.\n"
    "From the repository root, in the same virtualenv, run:\n\n"
    '    pip install -e ".[dev]"   # or: pip install -e .\n\n'
    "then run this script again."
)

try:
    import anyio
    from mcp.client import Client

    import garmin_coach_mcp
    from garmin_coach_mcp.cli import build_router
    from garmin_coach_mcp.coach.storage import CoachStore
    from garmin_coach_mcp.config import Settings
    from garmin_coach_mcp.server import build_server
except ImportError as exc:  # missing package or a pre-Race-Coach install
    sys.exit(f"Cannot import the Race Coach server ({exc}).\n\n" + STALE_INSTALL.format(where="?"))

if "store" not in inspect.signature(build_server).parameters:
    sys.exit(STALE_INSTALL.format(where=Path(garmin_coach_mcp.__file__).parent))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--race-type",
        required=True,
        choices=["5k", "10k", "half_marathon", "marathon", "ultra", "other"],
    )
    p.add_argument("--date", required=True, help="Race date YYYY-MM-DD")
    p.add_argument("--distance", type=float, help="km (required for ultra/other)")
    p.add_argument("--name", help="Race name")
    p.add_argument("--target-time", help="H:MM:SS (omit for 'just finish')")
    p.add_argument("--days", type=int, default=4, help="Running days per week (2-7)")
    p.add_argument("--long-run-day", default="sunday")
    p.add_argument("--weeks", type=int, default=12, help="Weeks of history for the profile")
    p.add_argument("--out", type=Path, default=Path("race-coach-trial"))
    return p.parse_args()


async def run(args: argparse.Namespace) -> int:
    # Read-only on purpose: writes stay disabled whatever the environment says.
    settings = Settings.from_env(
        read_only=True,
        allow_unofficial_writes=False,
        coach_data_dir=args.out.absolute() / "data",
    )
    router = build_router(settings)
    app = build_server(router, read_only=True, store=CoachStore(settings.coach_data_dir))
    backends = ", ".join(b.name for b in router.backends)
    print(f"# Race Coach — practical test ({dt.date.today()})\n")
    print(f"- Backends: {backends}; Garmin writes: disabled for this test")
    print(f"- Output folder: {args.out.absolute()}\n")

    goal: dict[str, Any] = {
        "race_type": args.race_type,
        "race_date": args.date,
        "days_per_week": args.days,
        "long_run_day": args.long_run_day,
    }
    for key, value in (
        ("distance_km", args.distance),
        ("race_name", args.name),
        ("target_time", args.target_time),
    ):
        if value is not None:
            goal[key] = value

    steps: list[tuple[str, str, dict[str, Any]]] = [
        ("1. Athlete profile", "get_athlete_profile", {"weeks": args.weeks}),
        ("2. Race goal", "set_race_goal", goal),
        ("3. Training plan", "generate_training_plan", {}),
        ("4. Export Markdown", "export_plan", {"format": "md"}),
        ("5. Export calendar", "export_plan", {"format": "ics"}),
        (
            "6. Export FIT workouts (next 2 weeks)",
            "export_plan",
            {"format": "fit", "to_date": str(dt.date.today() + dt.timedelta(days=14))},
        ),
    ]
    ok = True
    no_history = False
    async with Client(app) as client:
        for title, tool, params in steps:
            if tool == "generate_training_plan" and no_history:
                params = {"use_garmin_history": False}
                print("_Garmin history unavailable: planning without it (conservative)._\n")
            print(f"## {title}\n")
            result = await client.call_tool(tool, params)
            if result.is_error:
                ok = False
                text = getattr(result.content[0], "text", "") if result.content else "(no message)"
                print(f"**Error:** {text}\n")
                if tool == "get_athlete_profile":
                    no_history = True
                    continue
                if tool in ("set_race_goal", "generate_training_plan"):
                    break
                continue
            data = result.structured_content or {}
            print(str(data.get("text", "")).strip() or json.dumps(data, indent=2)[:2000])
            print()
    print("## Next\n")
    print("- Open the Markdown plan and import the .ics in a calendar to review it.")
    print("- Try one FIT file on the watch (see docs/practical-tests.md, test T3).")
    return 0 if ok else 1


def main() -> int:
    return anyio.run(run, parse_args())


if __name__ == "__main__":
    sys.exit(main())
