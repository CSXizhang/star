"""Stardew AI Companion runtime baseline."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from stardew_ai_runtime.client import TransportClient, TransportClientError
from stardew_ai_runtime.scheduler import (
    CompanionScheduler,
    DiscoveryError,
    NoActiveTaskError,
    PolicyViolationError,
    SchedulerError,
    expand_water_zone,
    resolve_discovery,
)

if TYPE_CHECKING:
    from stardew_ai_runtime.mcp_server import create_mcp_server

__version__ = "0.1.0"

__all__ = [
    "CompanionScheduler",
    "DiscoveryError",
    "NoActiveTaskError",
    "PolicyViolationError",
    "SchedulerError",
    "TransportClient",
    "TransportClientError",
    "create_mcp_server",
    "expand_water_zone",
    "resolve_discovery",
]


def __getattr__(name: str) -> Any:
    if name == "create_mcp_server":
        from stardew_ai_runtime.mcp_server import create_mcp_server

        return create_mcp_server
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
