#!/usr/bin/env python3
"""Authorized write test for V-1/V-2 (docs/api-coverage.md §6): workout units and pace target.

Creates ONE synthetic test workout on your Garmin Connect account with this server's payload
builder, reads it back from Garmin, compares what Garmin stored with what we meant, lets you
check it in Garmin Connect, then DELETES it (also on errors). Nothing is scheduled.

Run on your machine, from the repo, with the package installed (`pip install .`):

    # tokens from a previous login (GARMIN_TOKEN_DIR, default ~/.garmin-coach-mcp/tokens)
    # or GARMIN_EMAIL / GARMIN_PASSWORD in the environment; never paste them in chat
    python3 scripts/verify_workout_write.py --i-understand-this-creates-and-deletes-a-workout \
        > workout-write-report.md

The report contains only the synthetic workout's structure: no ids, names or personal data.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from pathlib import Path
from typing import Any

from garmin_coach_mcp.models import RepeatStep, WorkoutStep
from garmin_coach_mcp.workouts import build_running_workout

TEST_NAME = f"ZZ TEST garmin-coach-mcp {dt.datetime.now():%Y%m%d-%H%M%S}"

# What we mean, in human units. Expectations below are what Garmin should store.
STEPS: list[WorkoutStep | RepeatStep] = [
    WorkoutStep(type="warmup", duration_type="time", duration_value=600, hr_low=120, hr_high=140),
    WorkoutStep(
        type="run",
        duration_type="distance",
        duration_value=1000,
        pace_low="6:00",
        pace_high="5:30",
        hr_low=140,
        hr_high=160,
        primary="pace",
    ),
    WorkoutStep(type="cooldown", duration_type="open"),
]
EXPECTED = [
    {
        "step": "warmup",
        "end": "time",
        "value": 600.0,
        "target": "heart.rate.zone",
        "t1": 120.0,
        "t2": 140.0,
    },
    {
        "step": "interval",
        "end": "distance",
        "value": 1000.0,
        "target": "pace.zone",
        "t1": round(1000 / 360, 4),
        "t2": round(1000 / 330, 4),
        "secondary": "heart.rate.zone",
    },
    {"step": "cooldown", "end": "lap.button", "value": None, "target": "no.target"},
]


def _pace(mps: Any) -> str:
    if not isinstance(mps, (int, float)) or mps <= 0:
        return "-"
    s = round(1000 / mps)
    return f"{s // 60}:{s % 60:02d}/km"


def _flatten(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for st in steps:
        if st.get("workoutSteps"):
            out.extend(_flatten(st["workoutSteps"]))
        else:
            out.append(st)
    return out


def _login() -> Any:
    from garminconnect import Garmin

    token_dir = Path(
        os.environ.get("GARMIN_TOKEN_DIR")
        or os.environ.get("GARTH_TOKEN_DIR")
        or "~/.garmin-coach-mcp/tokens"
    ).expanduser()
    client = Garmin(
        os.environ.get("GARMIN_EMAIL"),
        os.environ.get("GARMIN_PASSWORD"),
        prompt_mfa=lambda: input("MFA code: "),
        retry_attempts=0,
    )
    token_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    client.login(str(token_dir))
    return client


def _compare(stored: list[dict[str, Any]]) -> tuple[list[str], bool]:
    rows = ["| # | Field | Expected | Stored by Garmin | OK |", "|---|---|---|---|---|"]
    all_ok = True

    def row(i: int, field: str, exp: Any, got: Any) -> None:
        nonlocal all_ok
        if isinstance(exp, float) and isinstance(got, (int, float)):
            ok = abs(exp - float(got)) <= max(0.01, abs(exp) * 0.005)
        else:
            ok = exp == got
        all_ok &= ok
        rows.append(f"| {i} | {field} | {exp} | {got} | {'yes' if ok else '**NO**'} |")

    if len(stored) != len(EXPECTED):
        rows.append(f"| - | step count | {len(EXPECTED)} | {len(stored)} | **NO** |")
        return rows, False
    for i, (exp, got) in enumerate(zip(EXPECTED, stored, strict=True), start=1):
        row(i, "step type", exp["step"], (got.get("stepType") or {}).get("stepTypeKey"))
        row(i, "end condition", exp["end"], (got.get("endCondition") or {}).get("conditionTypeKey"))
        if exp["value"] is not None:
            row(i, "end value", exp["value"], got.get("endConditionValue"))
        row(i, "target", exp["target"], (got.get("targetType") or {}).get("workoutTargetTypeKey"))
        if "t1" in exp:
            row(i, "target low", exp["t1"], got.get("targetValueOne"))
            row(i, "target high", exp["t2"], got.get("targetValueTwo"))
            if exp["target"] == "pace.zone":
                rows.append(
                    f"| {i} | pace as stored | 6:00/km - 5:30/km | "
                    f"{_pace(got.get('targetValueOne'))} - {_pace(got.get('targetValueTwo'))} | |"
                )
        if "secondary" in exp:
            sec = (got.get("secondaryTargetType") or {}).get("workoutTargetTypeKey")
            row(i, "secondary target", exp["secondary"], sec)
    return rows, all_ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--i-understand-this-creates-and-deletes-a-workout", dest="ok", action="store_true"
    )
    parser.add_argument("--no-pause", action="store_true", help="Delete without waiting.")
    args = parser.parse_args()
    if not args.ok:
        print(
            "Refusing to run without --i-understand-this-creates-and-deletes-a-workout",
            file=sys.stderr,
        )
        return 2

    client = _login()
    workout = build_running_workout(TEST_NAME, STEPS, 15 * 60)
    created = client.upload_running_workout(workout)
    workout_id = created.get("workoutId") if isinstance(created, dict) else None
    print(f"# Workout write check ({dt.date.today()})\n")
    if workout_id is None:
        print(
            "- create: no workoutId returned; nothing to delete. Check Garmin Connect for "
            f"'{TEST_NAME}' and delete it manually if present."
        )
        return 1
    print("- create: OK")
    exit_code = 1
    try:
        stored = client.get_workout_by_id(workout_id)
        segments = (stored or {}).get("workoutSegments") or [{}]
        steps = _flatten(segments[0].get("workoutSteps") or [])
        rows, all_ok = _compare(steps)
        print("- read back: OK\n")
        print("\n".join(rows))
        print(f"\n**Result: {'all fields match' if all_ok else 'MISMATCH — see rows marked NO'}**")
        exit_code = 0 if all_ok else 1
        if not args.no_pause:
            print(
                f"\nOpen Garmin Connect > Training > Workouts > '{TEST_NAME}'. Expected: "
                "warm-up 10:00 (HR 120-140), interval 1.00 km at 6:00-5:30/km (HR 140-160 "
                "secondary), cool-down until lap button. Press Enter to delete it...",
                file=sys.stderr,
            )
            input()
    finally:
        try:
            client.delete_workout(workout_id)
            print("- delete: OK")
        except Exception as exc:  # noqa: BLE001 - report and let the user clean up
            print(f"- delete: FAILED ({type(exc).__name__}); delete '{TEST_NAME}' manually.")
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
