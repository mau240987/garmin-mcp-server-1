"""garmin-coach-mcp — a Model Context Protocol server for Garmin Connect.

The package exposes a small, stable surface:

```python
from garmin_coach_mcp import GarminSession, Settings, build_server

settings = Settings.from_env()
server = build_server(GarminSession(settings))
server.run(transport="stdio")
```

Everything below the top level is lazily imported, so ``import garmin_coach_mcp``
(and therefore ``garmin-coach-mcp --version``) stays cheap and never pulls in the
Garmin HTTP stack until it is actually needed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__version__ = "0.1.0"

__all__ = [
    "AuthenticationError",
    "ConfigurationError",
    "GarminMCPError",
    "GarminSession",
    "InvalidRequestError",
    "NotFoundError",
    "RateLimitError",
    "ReadOnlyModeError",
    "Settings",
    "UpstreamServiceError",
    "__version__",
    "build_server",
    "main",
]

# Public name -> submodule that defines it. Kept in one place so the lazy loader
# and the static re-export block below cannot drift apart.
_EXPORTS: dict[str, str] = {
    "AuthenticationError": "errors",
    "ConfigurationError": "errors",
    "GarminMCPError": "errors",
    "InvalidRequestError": "errors",
    "NotFoundError": "errors",
    "RateLimitError": "errors",
    "ReadOnlyModeError": "errors",
    "UpstreamServiceError": "errors",
    "Settings": "config",
    "GarminSession": "client",
    "build_server": "server",
    "main": "cli",
}

if TYPE_CHECKING:  # pragma: no cover - import-time cost avoided at runtime
    from garmin_coach_mcp.cli import main
    from garmin_coach_mcp.client import GarminSession
    from garmin_coach_mcp.config import Settings
    from garmin_coach_mcp.errors import (
        AuthenticationError,
        ConfigurationError,
        GarminMCPError,
        InvalidRequestError,
        NotFoundError,
        RateLimitError,
        ReadOnlyModeError,
        UpstreamServiceError,
    )
    from garmin_coach_mcp.server import build_server


def __getattr__(name: str) -> Any:
    """Resolve a public attribute by importing its submodule on first access."""
    module_name = _EXPORTS.get(name)
    if module_name is None:
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)
    from importlib import import_module

    value = getattr(import_module(f"{__name__}.{module_name}"), name)
    globals()[name] = value  # cache so subsequent lookups skip this hook
    return value


def __dir__() -> list[str]:
    """List the public names, including the lazily loaded ones."""
    return sorted(__all__)
