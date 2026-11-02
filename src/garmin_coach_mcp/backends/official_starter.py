"""Official Garmin Connect Developer Program **Starter** backend — interface stub (A4).

Not functional: it declares no capabilities, so the router never selects it. It exists to fix
the seam a real implementation will fill once a Starter account is approved. Requirements and
open questions: docs/starter-requirements.md.

What Starter would add over the Personal API [DOC, developer.garmin.com overview]:
- the **Training API** (push workouts and schedules to the user's Garmin calendar), which is
  the official replacement for the unofficial writes;
- Activity/Health data delivered by **push only** (webhooks): a public HTTPS endpoint receives
  the data, which does not fit the local stdio server. Reads therefore stay on the Personal
  backend in Phase 1, and this backend is planned for writes only.
"""

from __future__ import annotations

from dataclasses import dataclass

from garmin_coach_mcp.backends.base import BackendBase, Capability

#: What the backend is meant to provide once implemented. Not declared by `capabilities()`.
PLANNED_CAPABILITIES = frozenset(
    {
        Capability.WORKOUT_CREATE,
        Capability.WORKOUT_SCHEDULE,
        Capability.WORKOUT_DELETE,
    }
)


@dataclass(frozen=True)
class StarterCredentials:
    """App credentials issued with Starter access, plus the user's OAuth grant.

    Field names are placeholders [DEDOTTO]: Garmin's Starter onboarding documentation is not
    publicly readable, so the exact OAuth flow and parameters are unverified.
    """

    client_id: str
    client_secret: str
    user_access_token: str | None = None

    def __repr__(self) -> str:  # never expose secrets
        return f"StarterCredentials(client_id={self.client_id[:4]}…)"


class OfficialStarterBackend(BackendBase):
    """Official Training API backend. Stub: no capability is available yet."""

    name = "official-starter"
    official = True

    def __init__(self, credentials: StarterCredentials | None = None) -> None:
        self.credentials = credentials

    def capabilities(self) -> frozenset[Capability]:
        # Deliberately empty until implemented and verified against the Training API.
        return frozenset()

    @staticmethod
    def planned_capabilities() -> frozenset[Capability]:
        return PLANNED_CAPABILITIES
