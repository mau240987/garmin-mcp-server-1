#!/usr/bin/env python3
"""Empirical check of the Garmin Connect Personal API (read-only, GET only).

Run on YOUR machine (this needs network access to personal.connectapi.garmin.com):

    export GARMIN_PERSONAL_API_KEY=...   # never paste it in chat or commit it
    python3 scripts/verify_personal_api.py [YYYY-MM-DD] > personal-api-report.md

Privacy: the report contains only HTTP status codes and whether given field NAMES are
present / non-null. No values, ids, names or tokens are printed. Review it before sharing.
Uses only the Python standard library.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

BASE = "https://personal.connectapi.garmin.com"
TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"  # noqa: S105

# endpoint label -> (path template, field names whose presence matters for coaching)
DAY_CHECKS: dict[str, tuple[str, list[str]]] = {
    "usersummary/daily": (
        "/usersummary-service/usersummary/daily?calendarDate={d}",
        [
            "totalSteps",
            "restingHeartRate",
            "lastSevenDaysAvgRestingHeartRate",
            "averageStressLevel",
            "bodyBatteryHighestValue",
            "bodyBatteryLowestValue",
            "bodyBatteryAtWakeTime",
            "averageSpo2",
        ],
    ),
    "sleep/dailySleep": (
        "/sleep-service/sleep/dailySleep?date={d}",
        ["sleepTimeSeconds", "sleepScores", "deepSleepSeconds", "avgSleepStress"],
    ),
    "hrv/{date}": ("/hrv-service/hrv/{d}", ["hrvSummary"]),
    "hrv/daily range": ("/hrv-service/hrv/daily/{w}/{d}", ["hrvSummaries"]),
    "bodyBattery reports": (
        "/wellness-service/wellness/bodyBattery/reports/daily?startDate={w}&endDate={d}",
        ["charged", "drained", "bodyBatteryValuesArray"],
    ),
    "dailyHeartRate": (
        "/wellness-service/wellness/dailyHeartRate?date={d}",
        ["restingHeartRate", "lastSevenDaysAvgRestingHeartRate"],
    ),
    "heartRateZones": ("/biometric-service/heartRateZones/v2", ["zone1Floor", "sport"]),
    "userprofile/settings": (
        "/userprofile-service/userprofile/settings",
        ["availableTrainingDays", "preferredLongTrainingDays", "measurementSystem"],
    ),
    "activities search": (
        "/activitylist-service/activities/search/activities?startDate={w}&endDate={d}&limit=20",
        [
            "activityId",
            "distance",
            "duration",
            "averageHR",
            "hrTimeInZone_1",
            "hrTimeInZone_2",
            "aerobicTrainingEffect",
            "anaerobicTrainingEffect",
            "activityTrainingLoad",
            "vO2MaxValue",
            "splitSummaries",
            "lapCount",
        ],
    ),
}
ACTIVITY_CHECKS: dict[str, tuple[str, list[str]]] = {
    "activity/{id}": ("/activity-service/activity/{a}", ["summaryDTO", "splitSummaries"]),
    "activity/{id}/workouts": ("/activity-service/activity/{a}/workouts", ["steps"]),
}
# Paths used by the unofficial library for metrics the Personal spec does not list.
# Expected: not available. Checked so the report shows the real answer.
NEGATIVE_CHECKS: dict[str, str] = {
    "training readiness (unofficial path)": "/metrics-service/metrics/trainingreadiness/{d}",
    "max metrics / VO2max (unofficial path)": "/metrics-service/metrics/maxmet/daily/{d}/{d}",
    "workout library (unofficial path)": "/workout-service/workouts?start=0&limit=1",
}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: urllib would re-send the Authorization header."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def request(method: str, path: str, headers: dict[str, str]) -> tuple[int, Any]:
    """Return (status, parsed body). Status 0 = network error; body never contains secrets."""
    req = urllib.request.Request(BASE + path, method=method, headers=headers)  # noqa: S310
    try:
        with _OPENER.open(req, timeout=30) as resp:
            body = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            if "json" in ctype:
                try:
                    return resp.status, json.loads(body or b"null")
                except ValueError:
                    return resp.status, "<invalid JSON>"
            return resp.status, f"<{ctype or 'binary'} {len(body)} bytes>"
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 0, f"<network error: {type(exc).__name__}>"


def field_report(payload: Any, fields: list[str]) -> str:
    sample = payload[0] if isinstance(payload, list) and payload else payload
    if isinstance(payload, list) and not payload:
        return "empty list"
    if not isinstance(sample, dict):
        return f"shape={type(sample).__name__}"
    marks = []
    for f in fields:
        if f not in sample:
            marks.append(f"{f}: ABSENT")
        elif sample[f] in (None, [], {}):
            marks.append(f"{f}: null/empty")
        else:
            marks.append(f"{f}: ok")
    return "; ".join(marks)


def main() -> int:
    key = os.environ.get("GARMIN_PERSONAL_API_KEY")
    if not key:
        print("Set GARMIN_PERSONAL_API_KEY first.", file=sys.stderr)
        return 2
    day = dt.date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else dt.date.today()
    week_ago = day - dt.timedelta(days=6)

    status, token_payload = request("POST", TOKEN_PATH, {"Authorization": f"ApiKey {key}"})
    print(f"# Personal API empirical check ({dt.date.today()}, day={day})\n")
    keys = (
        sorted(token_payload) if isinstance(token_payload, dict) else type(token_payload).__name__
    )
    print(f"- token endpoint: HTTP {status}; response keys: {keys}")
    token = None
    if isinstance(token_payload, dict):
        for k in ("access_token", "accessToken", "token"):
            if isinstance(token_payload.get(k), str):
                token = token_payload[k]
                break
    if not token:
        print("- could not find an access token in the response; stopping.")
        return 1
    auth = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    print("\n| Check | HTTP | Fields |\n|---|---|---|")
    activity_id = None
    for label, (tpl, fields) in DAY_CHECKS.items():
        code, payload = request("GET", tpl.format(d=day, w=week_ago), auth)
        print(f"| {label} | {code} | {field_report(payload, fields) if code == 200 else '-'} |")
        if label == "activities search" and isinstance(payload, list) and payload:
            activity_id = payload[0].get("activityId")
    if activity_id is not None:
        for label, (tpl, fields) in ACTIVITY_CHECKS.items():
            code, payload = request("GET", tpl.format(a=activity_id), auth)
            print(f"| {label} | {code} | {field_report(payload, fields) if code == 200 else '-'} |")
        code, payload = request("GET", f"/download-service/files/activity/{activity_id}", auth)
        print(f"| FIT download | {code} | {payload if code == 200 else '-'} |")
    for label, tpl in NEGATIVE_CHECKS.items():
        code, _ = request("GET", tpl.format(d=day), auth)
        print(f"| {label} | {code} | expected: not available |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
