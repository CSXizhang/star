"""D4: an explicitly set STARDEW_MCP_SURFACE env var must override the CLI surface.

The installed game provider profile (.kimi-code/mcp.json, written by
tools/register-mcp.ps1) always launches the server with ``--surface light``.
The life-chat bridge injects ``STARDEW_MCP_SURFACE=life`` into the model backend
environment, so the MCP server spawned for a life turn must expose exactly the
read-only LIFE_TOOLS despite the CLI flag. When the env var is absent,
``--surface light`` keeps its original behaviour.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from stardew_ai_runtime.mcp_server import (
    LIFE_TOOLS,
    LIGHT_TOOLS,
    _build_arg_parser,
    create_mcp_server,
    resolve_surface,
)


@pytest.fixture
def mock_scheduler():
    """Minimal scheduler stand-in (same shape as test_mcp_server's fixture)."""
    scheduler = MagicMock()
    scheduler.latest_world_revision = 1
    scheduler.wait_for_fresh_snapshot = AsyncMock(return_value=(None, False))
    return scheduler


def _server_from_argv(argv: list[str], scheduler):
    """Build a real server the same way main() does, from raw argv."""
    args = _build_arg_parser().parse_args(argv)
    surface = "light" if args.light else args.surface
    return create_mcp_server(
        run_dir=args.run_dir,
        scheduler=scheduler,
        server_name=args.name,
        full=args.full or None,
        surface=surface,
    )


def test_env_life_overrides_cli_surface_light(mock_scheduler, tmp_path, monkeypatch):
    """argv --surface light + STARDEW_MCP_SURFACE=life => exactly the read-only set."""
    monkeypatch.setenv("STARDEW_MCP_SURFACE", "life")
    server = _server_from_argv(
        ["--run-dir", str(tmp_path), "--surface", "light"], mock_scheduler
    )
    names = {t.name for t in server._tool_manager.list_tools()}
    assert names == set(LIFE_TOOLS)
    # No write entry points, no autonomy controls, no capability indirection.
    assert "submit_plan" not in names
    assert "remember_intent" not in names
    assert "set_autonomy" not in names
    assert "call_capability" not in names
    assert "discover_capabilities" not in names
    assert server.exposure_surface == "life"


def test_cli_surface_light_unchanged_when_env_unset(mock_scheduler, tmp_path, monkeypatch):
    monkeypatch.delenv("STARDEW_MCP_SURFACE", raising=False)
    server = _server_from_argv(
        ["--run-dir", str(tmp_path), "--surface", "light"], mock_scheduler
    )
    names = {t.name for t in server._tool_manager.list_tools()}
    assert names == set(LIGHT_TOOLS)
    assert "submit_plan" in names
    assert "remember_intent" in names
    assert server.exposure_surface == "light"


def test_resolve_surface_priority(monkeypatch):
    monkeypatch.delenv("STARDEW_MCP_SURFACE", raising=False)
    assert resolve_surface(surface="light") == "light"
    assert resolve_surface(full=False) == "light"
    assert resolve_surface() == "full"

    monkeypatch.setenv("STARDEW_MCP_SURFACE", "life")
    assert resolve_surface(surface="light") == "life"
    assert resolve_surface(full=False) == "life"
    assert resolve_surface() == "life"
    # The harness-only internal surface is never downgraded by an env var.
    assert resolve_surface(surface="internal") == "internal"
    # The explicit --full operator override still wins.
    assert resolve_surface(full=True) == "full"

    monkeypatch.setenv("STARDEW_MCP_SURFACE", "internal")
    assert resolve_surface(surface="light") == "internal"
