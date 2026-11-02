"""Exception taxonomy. Every error carries a remediation an LLM can act on."""

from __future__ import annotations

from mcp.server.mcpserver.exceptions import ToolError


class GarminMCPError(Exception):
    """Base class for every error this server raises on purpose."""

    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        remediation: str | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.remediation = remediation
        if cause is not None:
            self.__cause__ = cause

    def __str__(self) -> str:
        if self.remediation:
            return f"{self.message} Next step: {self.remediation}"
        return self.message

    def to_tool_error(self) -> ToolError:
        """Render as a protocol-level tool error (is_error=True) the model can read."""
        return ToolError(f"[{type(self).__name__}] {self}")


class ConfigurationError(GarminMCPError):
    """Missing or invalid settings."""


class AuthenticationError(GarminMCPError):
    """Bad credentials, expired tokens, or MFA required."""


class RateLimitError(GarminMCPError):
    """Garmin answered HTTP 429."""


class UpstreamServiceError(GarminMCPError):
    """Garmin 5xx, network failure or timeout. Safe to retry for reads."""

    retryable = True


class NotFoundError(GarminMCPError):
    """Unknown activity or workout id."""


class InvalidRequestError(GarminMCPError):
    """Semantically invalid input that passed schema validation."""


class ReadOnlyModeError(GarminMCPError):
    """A write was attempted while the server runs in read-only mode."""
