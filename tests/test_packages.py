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
    """Parse with a strict YAML parser: claude.ai rejects frontmatter that is not valid YAML."""
    yaml = pytest.importorskip("yaml")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert match, "SKILL.md must start with YAML frontmatter"
    data = yaml.safe_load(match.group(1))
    assert isinstance(data, dict) and all(isinstance(v, str) for v in data.values())
    return data


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


def _schema_words(node: object, out: set[str]) -> None:
    """All property names, enum values and const values in a JSON schema."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "properties" and isinstance(value, dict):
                out.update(value)
            if key in ("enum",) and isinstance(value, list):
                out.update(str(v) for v in value)
            if key == "const":
                out.add(str(value))
            _schema_words(value, out)
    elif isinstance(node, list):
        for item in node:
            _schema_words(item, out)


def test_instructions_only_cite_real_parameters(tmp_path: Path) -> None:
    router = BackendRouter([FakeGarminReads(), FakeGarminWriter()], allow_unofficial_writes=True)
    app = build_server(router, store=CoachStore(tmp_path))
    tools = anyio.run(app.list_tools)
    known: set[str] = {t.name for t in tools}
    for t in tools:
        _schema_words(t.input_schema, known)
        _schema_words(t.output_schema, known)
    text = (ROOT / "coach" / "race-coach.md").read_text()
    problems = []
    words = [w for chunk in re.findall(r"`([^`\n]+)`", text) for w in chunk.split()]
    for token in words:
        if "=" in token:
            key, value = token.split("=", 1)
            if key not in known or (value not in ("true", "false") and value not in known):
                problems.append(token)
        elif re.fullmatch(r"[a-z]+(?:_[a-z]+)+", token) and token not in known:
            problems.append(token)
    assert not problems, f"instructions cite unknown parameters/values: {problems}"


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


def test_registry_manifest_matches_the_package() -> None:
    """server.json must describe THIS package (a different 'garmin-mcp-server' exists on PyPI)."""
    pyproject = (ROOT / "pyproject.toml").read_text()
    name = re.search(r'^name = "([^"]+)"', pyproject, re.M)
    version = re.search(r'^version = "([^"]+)"', pyproject, re.M)
    assert name and version
    manifest = json.loads((ROOT / "server.json").read_text())
    pkg = manifest["packages"][0]
    assert pkg["registry_type"] == "pypi"
    assert pkg["identifier"] == name.group(1) and pkg["version"] == version.group(1)
    config = (ROOT / "src" / "garmin_coach_mcp" / "config.py").read_text()
    for env in pkg["environment_variables"]:
        assert f'"{env["name"]}"' in config, f"{env['name']} is not read by the server"
    readme = (ROOT / "README.md").read_text()
    assert "pip install garmin-mcp-server" not in readme and "uvx garmin-mcp-server" not in readme


def test_push_workflow_template_is_safe() -> None:
    """The data-repo workflow is manual, dry run by default, and never interpolates inputs
    into shell code (they reach the script only through environment variables)."""
    import yaml

    path = ROOT / "templates" / "garmin-coach-data" / ".github" / "workflows" / "push-to-garmin.yml"
    wf = yaml.safe_load(path.read_text(encoding="utf-8"))
    triggers = wf[True] if True in wf else wf["on"]  # YAML 1.1 reads "on" as a boolean
    assert list(triggers) == ["workflow_dispatch"]
    assert triggers["workflow_dispatch"]["inputs"]["apply"]["default"] is False
    for step in wf["jobs"]["push"]["steps"]:
        assert "${{" not in step.get("run", ""), step.get("name")
    gitignore = (ROOT / "templates" / "garmin-coach-data" / ".gitignore").read_text()
    assert "garmin_tokens.json" in gitignore


def test_weekly_workflow_template_is_safe() -> None:
    """Weekly coach: scheduled + manual, inputs only through env, Garmin writes only when the
    repository variable GARMIN_AUTO_PUSH is "true"."""
    import yaml

    path = ROOT / "templates" / "garmin-coach-data" / ".github" / "workflows" / "weekly-coach.yml"
    text = path.read_text(encoding="utf-8")
    wf = yaml.safe_load(text)
    triggers = wf[True] if True in wf else wf["on"]
    assert set(triggers) == {"schedule", "workflow_dispatch"}
    steps = wf["jobs"]["weekly"]["steps"]
    for step in steps:
        assert "${{" not in step.get("run", ""), step.get("name")
    checkin = next(s for s in steps if s.get("name") == "Weekly check-in")
    assert checkin["env"]["AUTO_PUSH"] == "${{ vars.GARMIN_AUTO_PUSH }}"
    assert '[ "$AUTO_PUSH" = "true" ]' in checkin["run"]
