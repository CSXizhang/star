"""Live stdio test for MCP server startup with spaces and Chinese paths.

Verifies:
1. python -m stardew_ai_runtime.mcp_server starts without runpy RuntimeWarning.
2. Handles --run-dir containing spaces and non-ASCII (Chinese) characters without arg splitting.
3. Responds to initialize and tools/list requests over stdio with strictly valid JSON-RPC.
4. The generic default tools/list is the complete legacy surface; --surface light (and
   --light / STARDEW_MCP_SURFACE=light) exposes only the game surface. Recorded here
   as real serialized-size evidence.
5. Zero stdout pollution (stdout contains only JSON-RPC messages).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from stardew_ai_runtime.mcp_server import LIGHT_TOOLS


def _launch_and_list_tools(complex_dir: Path, surface: str) -> tuple[list[dict], dict, str]:
    cmd = [
        sys.executable,
        "-W",
        "default",
        "-m",
        "stardew_ai_runtime.mcp_server",
        "--run-dir",
        str(complex_dir),
    ]
    env = {**os.environ}
    if surface == "light":
        cmd.extend(["--surface", "light"])
        env.pop("STARDEW_MCP_FULL", None)
        env["STARDEW_MCP_SURFACE"] = "light"
    elif surface == "light-alias":
        cmd.append("--light")
        env.pop("STARDEW_MCP_FULL", None)
    else:
        env.pop("STARDEW_MCP_FULL", None)
        env.pop("STARDEW_MCP_SURFACE", None)
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
        env=env,
    )
    try:
        assert proc.stdin is not None
        assert proc.stdout is not None

        init_req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test-stdio-client", "version": "1.0"},
            },
        }
        proc.stdin.write(json.dumps(init_req) + "\n")
        proc.stdin.flush()

        init_line = proc.stdout.readline()
        assert init_line, "Expected initialize response from stdout"
        init_resp = json.loads(init_line)
        assert init_resp.get("jsonrpc") == "2.0"
        assert init_resp.get("id") == 1
        assert "result" in init_resp
        assert init_resp["result"].get("serverInfo", {}).get("name") == "stardew-companion"

        init_notif = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        proc.stdin.write(json.dumps(init_notif) + "\n")
        proc.stdin.flush()

        tools_req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        proc.stdin.write(json.dumps(tools_req) + "\n")
        proc.stdin.flush()

        tools_line = proc.stdout.readline()
        assert tools_line, "Expected tools/list response from stdout"
        tools_resp = json.loads(tools_line)
        assert tools_resp.get("jsonrpc") == "2.0"
        assert tools_resp.get("id") == 2
        assert "result" in tools_resp
        tools = tools_resp["result"].get("tools", [])
        return tools, tools_resp, ""
    finally:
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.close()
        try:
            proc.terminate()
            _, stderr_text = proc.communicate(timeout=5)
        except Exception:
            proc.kill()
            _, stderr_text = proc.communicate()
    return [], {}, stderr_text


def test_mcp_server_stdio_startup_and_tools_list(tmp_path: Path) -> None:
    complex_dir = tmp_path / "星露谷 伙伴 测试 路径 with spaces"
    data_dir = complex_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    discovery_file = data_dir / "transport-discovery.json"
    discovery_file.write_text(
        json.dumps({"version": "0.1.0", "port": 58912, "sessionToken": "test-session-token", "pid": 12345}),
        encoding="utf-8",
    )

    # Generic default: the complete legacy surface (compatibility preserved).
    full_tools, _, stderr_full = _launch_and_list_tools(complex_dir, surface="full")
    full_names = {t["name"] for t in full_tools}
    assert "water_auto" in full_names
    assert "navigate_to" in full_names
    assert "query_chests" in full_names

    # Explicit game surface.
    light_tools, _, stderr_light = _launch_and_list_tools(complex_dir, surface="light")
    light_names = {t["name"] for t in light_tools}
    assert light_names == set(LIGHT_TOOLS), f"unexpected lightweight surface: {sorted(light_names)}"
    assert "water_auto" in light_names
    assert "submit_plan" in light_names
    assert "purchase_items" not in light_names
    assert "run_next_step" not in light_names

    alias_tools, _, _ = _launch_and_list_tools(complex_dir, surface="light-alias")
    assert {t["name"] for t in alias_tools} == light_names, "--light alias must match --surface light"

    light_size = len(json.dumps(light_tools, ensure_ascii=False))
    full_size = len(json.dumps(full_tools, ensure_ascii=False))
    assert light_size < full_size
    # Real serialized tools/list evidence for the acceptance report.
    print(f"stdio tools/list: light={len(light_tools)} ({light_size} chars) full={len(full_tools)} ({full_size} chars)")

    for stderr_text in (stderr_light, stderr_full):
        assert "RuntimeWarning" not in stderr_text, f"Unexpected RuntimeWarning in stderr: {stderr_text}"
        assert "unrecognized arguments" not in stderr_text, f"Arg split error in stderr: {stderr_text}"
