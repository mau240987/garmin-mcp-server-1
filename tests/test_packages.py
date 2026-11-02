"""S1/S2: the generated client packages are in sync, well-formed and match the server."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import zipfile
from pathlib import Path
from types import ModuleType

import anyio
import pytest

from garmin_coach_mcp.backends import BackendRouter
from garmin_coach_mcp.coach.storage import CoachStore
from garmin_coach_mcp.server import build_server
from tests.test_coach_tools import FakeGarminReads, FakeGarminWriter

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "garmin-race-coach"
SKILL = PLUGIN / "skills" / "race-coach" / "SKILL.md"
GEMINI = ROOT / "extensions" / "gemini" / "garmin-race-coach"
UPLOAD_KEYS = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}


def generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "build_pkgs", ROOT / "scripts" / "build_coach_packages.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def frontmatter(text: str) -> dict[str, str]:
    match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert match, "SKILL.md must start with YAML frontmatter"
    return dict(line.split(": ", 1) for line in match.group(1).splitlines())


def test_generated_files_are_in_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["build", "--check"])
    assert generator().main() == 0, "run: python scripts/build_coach_packages.py"


def test_skill_frontmatter_follows_the_spec() -> None:
    fm = frontmatter(SKILL.read_text())
    assert fm["name"] == SKILL.parent.name == "race-coach"
    assert re.fullmatch(r"[a-z0-9-]{1,64}", fm["name"])
    assert 0 < len(fm["description"]) <= 1024
    assert set(fm) <= UPLOAD_KEYS


def test_instructions_cover_exactly_the_server_tools(tmp_path: Path) -> None:
    router = BackendRouter([FakeGarminReads(), FakeGarminWriter()], allow_unofficial_writes=True)
    app = build_server(router, store=CoachStore(tmp_path))
    tools = {t.name for t in anyio.run(app.list_tools)}
    text = (ROOT / "coach" / "race-coach.md").read_text()
    mentioned = set(re.findall(r"`([a-z]+(?:_[a-z]+)+)`", text))
    tool_like = {
        m
        for m in mentioned
        if m in tools
        or m.startswith(
            ("get_", "push_", "delete_", "set_", "apply_", "export_", "review_", "generate_")
        )
    }
    assert tool_like <= tools, f"unknown tools in instructions: {tool_like - tools}"
    assert tools <= mentioned, f"tools missing from instructions: {tools - mentioned}"


def test_manifests() -> None:
    gen = generator()
    plugin = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    assert plugin["name"] == PLUGIN.name and plugin["version"] == gen.version()
    mcp = json.loads((PLUGIN / ".mcp.json").read_text())["mcpServers"]["garmin-coach"]
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert f'{mcp["command"]} = "garmin_coach_mcp.cli:main"' in pyproject
    assert all(v.startswith("${") and v.endswith(":-}") for v in mcp["env"].values())
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    entry = market["plugins"][0]
    assert entry["name"] == plugin["name"] and (ROOT / entry["source"]).resolve() == PLUGIN
    gem = json.loads((GEMINI / "gemini-extension.json").read_text())
    assert gem["name"] == GEMINI.name and gem["contextFileName"] == "GEMINI.md"
    assert (GEMINI / "GEMINI.md").exists()
    sensitive = {s["envVar"] for s in gem["settings"] if s["sensitive"]}
    assert sensitive == {"GARMIN_PERSONAL_API_KEY"}
    assert set(mcp["env"]) == {s["envVar"] for s in gem["settings"]}


def test_claude_ai_zip(tmp_path: Path) -> None:
    path = generator().build_zip(tmp_path)
    names = zipfile.ZipFile(path).namelist()
    assert names == ["race-coach/SKILL.md"]
    fm = frontmatter(zipfile.ZipFile(path).read("race-coach/SKILL.md").decode())
    assert set(fm) <= UPLOAD_KEYS
