"""Garmin Connect MCP server for running coaches."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("garmin-coach-mcp-server")
except PackageNotFoundError:  # running from a source checkout without install
    __version__ = "0.0.0+source"

__all__ = ["__version__"]
