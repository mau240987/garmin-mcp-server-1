"""Garmin backends behind one interface, chosen per capability by `BackendRouter`."""

from garmin_coach_mcp.backends.base import (
    GARMIN_ATTRIBUTION,
    WRITE_CAPABILITIES,
    Capability,
    GarminBackend,
)
from garmin_coach_mcp.backends.router import BackendRouter

__all__ = [
    "GARMIN_ATTRIBUTION",
    "WRITE_CAPABILITIES",
    "BackendRouter",
    "Capability",
    "GarminBackend",
]
