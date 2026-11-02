"""Capability router: picks a backend per capability, official first.

Policy (strategy A, docs/api-coverage.md §7):

1. An official backend that declares the capability always wins (Personal for reads; a future
   Starter backend for writes).
2. Otherwise the unofficial backend may serve it when
   - it is a **write** and GARMIN_ALLOW_UNOFFICIAL_WRITES=true (off by default, A3), or
   - it is a read and no official backend is configured (pre-migration behaviour), or
   - it is a read and GARMIN_ALLOW_UNOFFICIAL_READS=true.
3. Otherwise a structured, actionable `CapabilityUnavailableError` is raised.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from garmin_coach_mcp.backends.base import (
    GARMIN_ATTRIBUTION,
    WRITE_CAPABILITIES,
    Capability,
    GarminBackend,
)
from garmin_coach_mcp.errors import CapabilityUnavailableError

_READ_WHAT: dict[Capability, str] = {
    Capability.TRAINING_READINESS: (
        "Training Readiness is not offered by the official Garmin Personal API. Use HRV, "
        "resting HR, sleep and Body Battery instead"
    ),
    Capability.WORKOUTS_LIST: "The workout library is not offered by the official Garmin "
    "Personal API",
}
_WRITE_WHAT = (
    "Creating, scheduling or deleting workouts needs Garmin's Training API (Starter tier, "
    "not available yet) or the unofficial backend, which is disabled by default"
)
_WRITE_FALLBACK = "Otherwise, create the workout manually in Garmin Connect from the plan."


class BackendRouter:
    def __init__(
        self,
        backends: Sequence[GarminBackend],
        *,
        allow_unofficial_reads: bool = False,
        allow_unofficial_writes: bool = False,
        notices: Callable[[], list[str]] | None = None,
    ) -> None:
        if not backends:
            raise ValueError("at least one backend is required")
        self.backends = list(backends)
        self.allow_unofficial_reads = allow_unofficial_reads
        self.allow_unofficial_writes = allow_unofficial_writes
        self._notices = notices or (lambda: [])

    @property
    def has_official(self) -> bool:
        return any(b.official for b in self.backends)

    def notices(self) -> list[str]:
        return self._notices()

    @property
    def has_unofficial(self) -> bool:
        return any(not b.official for b in self.backends)

    def _unofficial_allowed(self, cap: Capability) -> bool:
        if cap in WRITE_CAPABILITIES:
            return self.allow_unofficial_writes
        return not self.has_official or self.allow_unofficial_reads

    def select(self, cap: Capability, *also: Capability) -> GarminBackend:
        """Backend for `cap`. With `also`, one backend that serves ALL the capabilities, so
        e.g. a workout is scheduled by the same backend that created it."""
        caps = frozenset((cap, *also))
        for backend in self.backends:
            if backend.official and caps <= backend.capabilities():
                return backend
        if all(self._unofficial_allowed(c) for c in caps):
            for backend in self.backends:
                if not backend.official and caps <= backend.capabilities():
                    return backend
        missing = sorted(c.value for c in caps)
        hint = (
            self._write_hint()
            if caps & WRITE_CAPABILITIES
            else self._read_hint(next(iter(sorted(caps))))
        )
        raise CapabilityUnavailableError(
            f"No configured Garmin backend may provide {', '.join(repr(m) for m in missing)}.",
            remediation=hint,
        )

    def _write_hint(self) -> str:
        login = "a Garmin login (GARMIN_EMAIL/GARMIN_PASSWORD or `garmin-coach-mcp --login`)"
        if self.allow_unofficial_writes and not self.has_unofficial:
            return (
                "Unofficial workout writes are enabled (GARMIN_ALLOW_UNOFFICIAL_WRITES=true) but "
                f"no unofficial backend is configured: the athlete (not the assistant) must add "
                f"{login} and restart the server. {_WRITE_FALLBACK}"
            )
        how = (
            "the athlete (not the assistant) can enable it by setting "
            f"GARMIN_ALLOW_UNOFFICIAL_WRITES=true (it also needs {login}) and restarting the "
            "server"
        )
        return f"{_WRITE_WHAT}; {how}. See docs/api-coverage.md (risk R4). {_WRITE_FALLBACK}"

    def _read_hint(self, cap: Capability) -> str:
        what = _READ_WHAT.get(cap, f"'{cap.value}' is not offered by the configured backends")
        if self.has_unofficial:
            how = (
                "the athlete (not the assistant) can set GARMIN_ALLOW_UNOFFICIAL_READS=true "
                "(unofficial access)"
            )
        else:
            how = (
                "the athlete (not the assistant) can configure the unofficial backend "
                "(GARMIN_EMAIL/GARMIN_PASSWORD) and set GARMIN_ALLOW_UNOFFICIAL_READS=true"
            )
        return f"{what}; or {how}. See docs/api-coverage.md."

    def can(self, cap: Capability) -> bool:
        try:
            self.select(cap)
        except CapabilityUnavailableError:
            return False
        return True

    @staticmethod
    def attribution(backend: GarminBackend) -> str | None:
        # Data is Garmin device-sourced whichever backend served it.
        return GARMIN_ATTRIBUTION
