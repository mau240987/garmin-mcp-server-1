"""Core routine (bodyweight, about 10 minutes) done after a run on the plan's core day.

Done after the run, not before: a tired trunk before intervals or tempo makes the hard part
worse. The routine is fixed and short, at home without equipment: two rounds of six timed
exercises. On Garmin it is a separate strength workout scheduled on the same day, so the watch
shows each exercise with its animation and the run stays a clean running activity.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

ROUNDS = 2
REST_S = 15  # between exercises
ROUND_REST_S = 30  # between the two rounds
RACE_WEEK_DAYS = 7  # no core in the last week before the race


@dataclass(frozen=True)
class CoreExercise:
    name_it: str
    seconds: int
    category: str  # Garmin exercise category / name (garminconnect.exercises)
    exercise: str
    cue_it: str


ROUTINE: tuple[CoreExercise, ...] = (
    CoreExercise(
        "Plank frontale",
        40,
        "PLANK",
        "PLANK",
        "Gomiti sotto le spalle, corpo in linea, addome e glutei contratti.",
    ),
    CoreExercise(
        "Plank laterale sinistro",
        30,
        "PLANK",
        "SIDE_PLANK",
        "Gomito sotto la spalla, anche alte, corpo in linea.",
    ),
    CoreExercise(
        "Plank laterale destro",
        30,
        "PLANK",
        "SIDE_PLANK",
        "Gomito sotto la spalla, anche alte, corpo in linea.",
    ),
    CoreExercise(
        "Dead bug",
        40,
        "HIP_STABILITY",
        "DEAD_BUG",
        "Schiena a terra; allunga lento braccio e gamba opposti, poi cambia.",
    ),
    CoreExercise(
        "Ponte glutei",
        40,
        "HIP_RAISE",
        "HIP_RAISE",
        "Spingi coi talloni, anche su fino a spalle-anche-ginocchia in linea, 2 s in alto.",
    ),
    CoreExercise(
        "Bird dog",
        40,
        "HYPEREXTENSION",
        "KNEELING_SUPERMAN",
        "In quadrupedia, braccio e gamba opposti tesi, bacino fermo; alterna i lati.",
    ),
)


def duration_s() -> int:
    one_round = sum(e.seconds for e in ROUTINE) + REST_S * (len(ROUTINE) - 1)
    return ROUNDS * one_round + (ROUNDS - 1) * ROUND_REST_S


def minutes() -> int:
    return round(duration_s() / 60)


def wants_core(core_weekday: int | None, day: dt.date, race_date: dt.date) -> bool:
    """A core workout follows the run on `core_weekday`, except in the race week."""
    return (
        core_weekday is not None
        and day.weekday() == core_weekday
        and day <= race_date - dt.timedelta(days=RACE_WEEK_DAYS)
    )


def workout_name(day: dt.date, week_index: int) -> str:
    return f"RC W{week_index} {day:%a %d/%m} core {minutes()}min"


def summary_it() -> str:
    """One line: '2 giri: Plank frontale 40″ · …'."""
    items = " · ".join(f"{e.name_it} {e.seconds}″" for e in ROUTINE)
    return f"{ROUNDS} giri ({REST_S}″ di pausa tra gli esercizi): {items}"


def _time(seconds: int) -> dict[str, Any]:
    return {
        "endCondition": {
            "conditionTypeId": 2,
            "conditionTypeKey": "time",
            "displayOrder": 2,
            "displayable": True,
        },
        "endConditionValue": float(seconds),
        "targetType": {
            "workoutTargetTypeId": 1,
            "workoutTargetTypeKey": "no.target",
            "displayOrder": 1,
        },
    }


def core_workout_payload(name: str) -> dict[str, Any]:
    """Garmin strength workout: one repeat group of ROUNDS rounds."""
    strength = {"sportTypeId": 5, "sportTypeKey": "strength_training", "displayOrder": 5}
    order = 1
    children: list[dict[str, Any]] = []
    for i, ex in enumerate(ROUTINE):
        order += 1
        children.append(
            {
                "type": "ExecutableStepDTO",
                "stepOrder": order,
                "stepType": {"stepTypeId": 3, "stepTypeKey": "interval", "displayOrder": 3},
                "category": ex.category,
                "exerciseName": ex.exercise,
                "description": f"{ex.name_it}: {ex.cue_it}",
                **_time(ex.seconds),
            }
        )
        order += 1
        children.append(
            {
                "type": "ExecutableStepDTO",
                "stepOrder": order,
                "stepType": {"stepTypeId": 5, "stepTypeKey": "rest", "displayOrder": 5},
                **_time(ROUND_REST_S if i == len(ROUTINE) - 1 else REST_S),
            }
        )
    group = {
        "type": "RepeatGroupDTO",
        "stepOrder": 1,
        "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat", "displayOrder": 6},
        "numberOfIterations": ROUNDS,
        "endCondition": {
            "conditionTypeId": 7,
            "conditionTypeKey": "iterations",
            "displayOrder": 7,
            "displayable": False,
        },
        "endConditionValue": float(ROUNDS),
        "smartRepeat": False,
        "skipLastRestStep": True,
        "workoutSteps": children,
    }
    return {
        "workoutName": name,
        "description": "Core a corpo libero dopo la corsa: " + summary_it(),
        "sportType": strength,
        "estimatedDurationInSecs": duration_s(),
        "workoutSegments": [{"segmentOrder": 1, "sportType": strength, "workoutSteps": [group]}],
    }


def build_core_workout(name: str) -> Any:
    """A `garminconnect.workout.StrengthWorkout` ready for `upload_strength_workout`."""
    from garminconnect.workout import StrengthWorkout

    return StrengthWorkout.model_validate(core_workout_payload(name))
