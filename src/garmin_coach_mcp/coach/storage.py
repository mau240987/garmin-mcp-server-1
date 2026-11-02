"""Local JSON storage for goals and plans (C1). Lives in the user's data dir, never the repo.

Single-user, local process: one JSON file per object, written atomically (temp file +
os.replace) with owner-only permissions.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from garmin_coach_mcp.coach.models import SCHEMA_VERSION, RaceGoal, TrainingPlan
from garmin_coach_mcp.errors import ConfigurationError, NotFoundError, UpstreamServiceError

_ID_RE = re.compile(r"^(goal|plan)_[0-9a-f]{12}$")

M = TypeVar("M", bound=BaseModel)


class _NewerSchemaError(Exception):
    pass


class CoachStore:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().absolute()
        self._lock = threading.Lock()

    # -- internals ----------------------------------------------------------------------
    def _dir(self, kind: str) -> Path:
        path = self.root / kind
        try:
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            raise ConfigurationError(
                f"Cannot create the coach data directory {path}.",
                remediation="Set GARMIN_COACH_DATA_DIR to a writable directory.",
                cause=exc,
            ) from exc
        return path

    @staticmethod
    def _check_id(object_id: str, prefix: str) -> None:
        if not _ID_RE.fullmatch(object_id) or not object_id.startswith(prefix + "_"):
            raise NotFoundError(
                f"'{object_id}' is not a valid {prefix} id.",
                remediation=f"Use an id returned by the tools (it starts with '{prefix}_').",
            )

    def _write(self, kind: str, object_id: str, model: BaseModel) -> Path:
        directory = self._dir(kind)
        target = directory / f"{object_id}.json"
        data = model.model_dump_json(indent=2)
        with self._lock:
            fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())  # durable before the rename
                os.chmod(tmp, 0o600)
                os.replace(tmp, target)
            except OSError as exc:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise ConfigurationError(
                    f"Cannot write {target}.",
                    remediation="Check free disk space and permissions of GARMIN_COACH_DATA_DIR.",
                    cause=exc,
                ) from exc
        return target

    @staticmethod
    def _migrate(kind: str, data: dict[str, object]) -> dict[str, object]:
        """Upgrade older files to SCHEMA_VERSION. Version 1 is the first: nothing to do yet."""
        return data

    def _parse(self, kind: str, raw: str, model: type[M], path: Path) -> M:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
        version = data.get("schema_version", 1)
        if not isinstance(version, int) or version > SCHEMA_VERSION:
            raise _NewerSchemaError(path)
        return model.model_validate(self._migrate(kind, data))

    def _read(self, kind: str, object_id: str, model: type[M]) -> M:
        path = self.root / kind / f"{object_id}.json"
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise NotFoundError(
                f"No saved {kind[:-1]} with id '{object_id}'.",
                remediation=f"List saved {kind} or create a new one.",
                cause=exc,
            ) from exc
        except (OSError, UnicodeDecodeError) as exc:
            raise ConfigurationError(
                f"Cannot read the saved {kind[:-1]} '{object_id}'.",
                remediation=f"Check permissions and contents of {path}.",
                cause=exc,
            ) from exc
        try:
            return self._parse(kind, raw, model, path)
        except _NewerSchemaError as exc:
            raise ConfigurationError(
                f"The saved {kind[:-1]} '{object_id}' was written by a newer version of this "
                "server.",
                remediation="Upgrade garmin-coach-mcp-server to read it.",
            ) from exc
        except (ValueError, ValidationError) as exc:
            raise UpstreamServiceError(
                f"The saved {kind[:-1]} '{object_id}' is corrupted.",
                remediation=f"Create it again; the damaged file is {path}.",
                cause=exc,
            ) from exc

    def _list(self, kind: str, model: type[M]) -> list[M]:
        directory = self.root / kind
        if not directory.is_dir():
            return []
        out: list[M] = []
        for path in sorted(directory.glob("*.json")):
            try:
                out.append(self._parse(kind, path.read_text(encoding="utf-8"), model, path))
            except (OSError, ValueError, ValidationError, _NewerSchemaError):
                continue  # a damaged/newer file must not hide the others; reported on read
        return out

    # -- goals --------------------------------------------------------------------------
    def save_goal(self, goal: RaceGoal) -> Path:
        self._check_id(goal.id, "goal")
        return self._write("goals", goal.id, goal)

    def get_goal(self, goal_id: str) -> RaceGoal:
        self._check_id(goal_id, "goal")
        return self._read("goals", goal_id, RaceGoal)

    def list_goals(self) -> list[RaceGoal]:
        return sorted(self._list("goals", RaceGoal), key=lambda g: g.created_at)

    # -- plans --------------------------------------------------------------------------
    def save_plan(self, plan: TrainingPlan) -> Path:
        self._check_id(plan.id, "plan")
        return self._write("plans", plan.id, plan)

    def get_plan(self, plan_id: str) -> TrainingPlan:
        self._check_id(plan_id, "plan")
        return self._read("plans", plan_id, TrainingPlan)

    def list_plans(self, goal_id: str | None = None) -> list[TrainingPlan]:
        plans = self._list("plans", TrainingPlan)
        if goal_id is not None:
            plans = [p for p in plans if p.goal_id == goal_id]
        return sorted(plans, key=lambda p: p.created_at)
