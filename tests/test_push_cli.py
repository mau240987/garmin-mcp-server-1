"""garmin-coach-push: dry run by default, real push only with --apply and matching counts."""

from __future__ import annotations

import functools
import re
from pathlib import Path
from typing import Any

import pytest

from garmin_coach_mcp import push_cli
from garmin_coach_mcp.backends import BackendRouter
from garmin_coach_mcp.coach.storage import CoachStore
from garmin_coach_mcp.server import build_server
from tests.test_coach_tools import (
    TODAY,
    FakeGarminReads,
    FakeGarminWriter,
    make_app,
    setup_plan,
)


class Writer(FakeGarminWriter):
    def __init__(self) -> None:
        super().__init__()
        self.listed = 0

    def list_workouts(self, start: int, limit: int) -> list[dict[str, Any]]:
        self.listed += 1
        return []


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Writer]:
    data = tmp_path / "coach"
    setup_plan(make_app(FakeGarminReads(), CoachStore(data)))
    writer = Writer()
    for name in ("GARMIN_PERSONAL_API_KEY", "GARMIN_ALLOW_UNOFFICIAL_WRITES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GARMIN_EMAIL", "athlete@example.invalid")
    monkeypatch.setenv("GARMIN_PASSWORD", "not-a-real-password")
    monkeypatch.setenv("GARMIN_TOKEN_DIR", str(tmp_path / "tokens"))
    monkeypatch.setattr(
        push_cli,
        "build_router",
        lambda s: BackendRouter([writer], allow_unofficial_writes=s.allow_unofficial_writes),
    )
    monkeypatch.setattr(
        push_cli, "build_server", functools.partial(build_server, today=lambda: TODAY)
    )
    return data, writer


def test_dry_run_checks_login_and_sends_nothing(env: tuple[Path, Writer], tmp_path: Path) -> None:
    data, writer = env
    summary = tmp_path / "summary.md"
    assert push_cli.main(["--data-dir", str(data), "--summary", str(summary)]) == 0
    text = summary.read_text()
    assert "DRY RUN" in text and "Garmin login: OK" in text
    assert writer.listed == 1 and writer.created == [] and writer.scheduled == []
    assert re.search(r"expected_count` = \*\*(\d+)\*\*", text)
    # Writes are disabled in a dry run on purpose: no misleading "not available" note.
    assert "not available in this configuration" not in text
    assert "enabled only with apply" in text


def test_apply_needs_the_dry_run_counts(env: tuple[Path, Writer], tmp_path: Path) -> None:
    data, writer = env
    summary = tmp_path / "summary.md"
    push_cli.main(["--data-dir", str(data), "--summary", str(summary)])
    count = int(re.search(r"expected_count` = \*\*(\d+)\*\*", summary.read_text())[1])
    assert count > 0
    assert push_cli.main(["--data-dir", str(data), "--apply"]) == 2  # no count given
    wrong = ["--data-dir", str(data), "--apply", "--expected-count", str(count + 1)]
    assert push_cli.main(wrong) == 1
    assert writer.created == []
    ok = ["--data-dir", str(data), "--apply", "--expected-count", str(count)]
    assert push_cli.main(ok) == 0
    assert len(writer.created) == count and len(writer.scheduled) == count
    # The plan now records the workout ids: a second push has nothing left to send.
    summary2 = tmp_path / "again.md"
    push_cli.main(["--data-dir", str(data), "--summary", str(summary2)])
    assert "expected_count` = **0**" in summary2.read_text()


def test_dry_run_without_login_fails_clearly(
    env: tuple[Path, Writer], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data, _writer = env
    monkeypatch.delenv("GARMIN_EMAIL")
    monkeypatch.delenv("GARMIN_PASSWORD")
    summary = tmp_path / "s.md"
    assert push_cli.main(["--data-dir", str(data), "--summary", str(summary)]) == 2
    assert "GARMIN_EMAIL" in summary.read_text()


def test_writes_are_enabled_only_with_apply(env: tuple[Path, Writer]) -> None:
    data, _writer = env
    seen: list[bool] = []
    real = push_cli.Settings.from_env

    def spy(**kw: Any) -> Any:
        seen.append(kw["allow_unofficial_writes"])
        return real(**kw)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(push_cli.Settings, "from_env", staticmethod(spy))
        push_cli.main(["--data-dir", str(data)])
    assert seen == [False]
