"""Daniels VDOT: checked against the published tables (Daniels' Running Formula)."""

from __future__ import annotations

import pytest

from garmin_coach_mcp.coach.vdot import (
    easy_pace,
    pace_at_share,
    race_time,
    threshold_pace,
    vdot_from_race,
)


@pytest.mark.parametrize(
    ("vdot", "distance", "table_s"),
    [
        (50, 5000, 19 * 60 + 57),
        (50, 10000, 41 * 60 + 21),
        (50, 21097.5, 3600 + 31 * 60 + 35),
        (50, 42195, 3 * 3600 + 10 * 60 + 49),
        (40, 42195, 3 * 3600 + 49 * 60 + 45),
        (60, 5000, 17 * 60 + 3),
    ],
)
def test_race_times_match_daniels_tables(vdot: float, distance: float, table_s: int) -> None:
    assert race_time(vdot, distance) == pytest.approx(table_s, rel=0.004)
    assert vdot_from_race(distance, table_s) == pytest.approx(vdot, abs=0.3)


@pytest.mark.parametrize("vdot", [30.0, 39.5, 50.0, 65.0])
def test_training_paces_are_ordered(vdot: float) -> None:
    marathon = race_time(vdot, 42195) / 42.195
    assert easy_pace(vdot) > marathon > threshold_pace(vdot) > pace_at_share(vdot, 0.98)
    assert 30 <= easy_pace(vdot) - marathon <= 90  # easy runs 0:30-1:30/km slower than M pace
