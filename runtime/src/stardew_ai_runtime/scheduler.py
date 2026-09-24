"""Thin Policy and Task Scheduler for Stardew AI Companion.

Shared entry point for MCP Adapter and future autonomous Planners.
Enforces:
1. Single active task at any time (concurrency rejection).
2. Radius validation (0..2) and coordinate bounds.
3. Deterministic idempotency key generation: {saveId}:{taskId}:{attempt}.
4. Task lifecycle tracking (running, paused, cancelling, terminal).
5. Lazy loopback connection to Companion Mod via transport discovery.
6. Zero token leakage and loopback-only security.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from stardew_ai_runtime.autonomy import AutonomyController
from stardew_ai_runtime.client import TransportClient
from stardew_ai_runtime.protocol import NATIVE_ACTION_TASK_PREFIXES, Envelope

logger = logging.getLogger("stardew_ai_runtime.scheduler")


class SchedulerError(Exception):
    """Base exception for scheduler errors."""

    pass


class PolicyViolationError(SchedulerError):
    """Raised when a request violates scheduling policy (e.g. concurrent task, invalid bounds)."""

    pass


class DiscoveryError(SchedulerError):
    """Raised when transport discovery file is missing, unreachable, or invalid."""

    pass


class NoActiveTaskError(SchedulerError):
    """Raised when attempting task control actions on an idle companion."""

    pass


COMMON_GAME_PATHS: list[str] = [
    r"E:\Game\steam\steamapps\common\Stardew Valley",
    r"C:\Program Files (x86)\Steam\steamapps\common\Stardew Valley",
    r"C:\Program Files\Steam\steamapps\common\Stardew Valley",
    r"D:\SteamLibrary\steamapps\common\Stardew Valley",
    r"E:\SteamLibrary\steamapps\common\Stardew Valley",
    r"F:\SteamLibrary\steamapps\common\Stardew Valley",
    r"D:\Game\steam\steamapps\common\Stardew Valley",
    r"D:\Games\Steam\steamapps\common\Stardew Valley",
]






def expand_water_zone(center_x: int, center_y: int, radius: int = 0) -> list[dict[str, int]]:
    """Expands center coordinates and radius into a list of grid coordinates.

    Radius must be between 0 and 2 inclusive:
    - radius 0: 1x1 (1 tile)
    - radius 1: 3x3 (up to 9 tiles)
    - radius 2: 5x5 (up to 25 tiles)

    Returns coordinates in deterministic row-major order: sorted by (y, x).
    Coordinates strictly filter out negative coordinates.
    """
    if isinstance(radius, bool) or not isinstance(radius, int):
        raise PolicyViolationError(f"radius must be an integer, got {type(radius).__name__}")
    if radius < 0 or radius > 2:
        raise PolicyViolationError(
            f"Invalid radius: {radius}. Radius must be between 0 and 2 (inclusive)."
        )

    if isinstance(center_x, bool) or not isinstance(center_x, int):
        raise PolicyViolationError(f"center_x must be an integer, got {type(center_x).__name__}")
    if isinstance(center_y, bool) or not isinstance(center_y, int):
        raise PolicyViolationError(f"center_y must be an integer, got {type(center_y).__name__}")

    if center_x < 0 or center_y < 0:
        raise PolicyViolationError(
            f"Invalid coordinates ({center_x}, {center_y}). Coordinates must be non-negative."
        )

    tiles: list[dict[str, int]] = []
    for y in range(center_y - radius, center_y + radius + 1):
        for x in range(center_x - radius, center_x + radius + 1):
            if x >= 0 and y >= 0:
                tiles.append({"x": x, "y": y})

    # Deterministic row-major sorting
    tiles.sort(key=lambda t: (t["y"], t["x"]))
    return tiles


def requested_task_id(command_id: str) -> str:
    """Deterministic task id derived from a stable plan command id.

    ``execute_skill`` builds the native idempotency key as
    ``{saveId}:{taskId}:attempt-1``. With a random task id a retried plan step
    produced a different key, so the Mod could not recognise it as the same
    command even though the command id matched. Deriving the task id from the
    stable command id keeps the whole identity chain stable for the same attempt.
    """
    digest = hashlib.sha1(command_id.encode("utf-8")).hexdigest()[:10]
    return f"task-plan-{digest}"


def validate_action_tiles(tiles: Any, max_tiles: int = 64) -> list[dict[str, int]]:
    """Validates and deduplicates tile coordinates for action skills.

    Enforces:
    - tiles must be a non-empty list
    - each tile must be a dict with 'x' and 'y'
    - 'x' and 'y' must be non-negative integers (booleans strictly rejected)
    - deduplication of (x, y) coordinates
    - batch limit: 1..max_tiles after deduplication
    - returns coordinates sorted deterministically in row-major order: (y, x)
    """
    if not isinstance(tiles, list) or len(tiles) == 0:
        raise PolicyViolationError("tiles must be a non-empty list")

    seen: set[tuple[int, int]] = set()
    unique_tiles: list[dict[str, int]] = []

    for t in tiles:
        if not isinstance(t, dict) or "x" not in t or "y" not in t:
            raise PolicyViolationError(f"invalid tile format: {t}")
        x = t["x"]
        y = t["y"]
        if (
            isinstance(x, bool)
            or not isinstance(x, int)
            or isinstance(y, bool)
            or not isinstance(y, int)
        ):
            raise PolicyViolationError(f"tile coordinates must be integers: {t}")
        if x < 0 or y < 0:
            raise PolicyViolationError(f"tile coordinates must be non-negative: {t}")

        coord = (x, y)
        if coord not in seen:
            seen.add(coord)
            unique_tiles.append({"x": x, "y": y})

    if len(unique_tiles) == 0:
        raise PolicyViolationError("tiles list must contain at least 1 coordinate")
    if len(unique_tiles) > max_tiles:
        raise PolicyViolationError(
            f"tiles count ({len(unique_tiles)}) exceeds maximum allowed batch limit ({max_tiles})"
        )

    unique_tiles.sort(key=lambda item: (item["y"], item["x"]))
    return unique_tiles


def validate_seed_item_id(seed_item_id: Any) -> str:
    """Validates seed_item_id parameter.

    Allows qualified IDs like '(O)472' or numeric IDs like '472'.
    Rejects booleans, non-strings, and empty/whitespace strings.
    """
    if isinstance(seed_item_id, bool) or not isinstance(seed_item_id, str):
        raise PolicyViolationError(
            f"seed_item_id must be a string, got {type(seed_item_id).__name__}"
        )
    cleaned = seed_item_id.strip()
    if not cleaned:
        raise PolicyViolationError("seed_item_id cannot be empty")
    return cleaned


def is_tool_item(item: dict[str, Any]) -> bool:
    """Identifies whether an item is a tool (never deposited into chests)."""
    if item.get("isTool") is True:
        return True
    item_id = str(item.get("itemId", ""))
    name = str(item.get("name", "")).lower()
    if item_id.startswith("(T)"):
        return True
    tool_names = (
        "watering can",
        "axe",
        "hoe",
        "pickaxe",
        "scythe",
        "fishing rod",
        "trash can",
        "milk pail",
        "shears",
        "pan",
    )
    if any(k in name for k in tool_names):
        return True
    tool_id_names = {
        "wateringcan",
        "axe",
        "hoe",
        "pickaxe",
        "scythe",
        "fishingrod",
        "trashcan",
        "milkpail",
        "shears",
        "pan",
    }
    if item_id.lower() in tool_id_names:
        return True
    return False


def extract_non_tool_items(slots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extracts non-tool items from inventory slots."""
    result: list[dict[str, Any]] = []
    for s in slots:
        if not is_tool_item(s):
            result.append({
                "itemId": s.get("itemId", ""),
                "name": s.get("name", ""),
                "stack": s.get("stack", 1),
                "quality": s.get("quality", 0),
            })
    return result


def extract_chest_summary(
    chest: dict[str, Any] | None,
    chest_x: int,
    chest_y: int,
    companion_items: Any | None = None,
    detail: bool = False,
) -> dict[str, Any]:
    """Builds a concise summary of a chest for planning and action response."""
    if chest is None:
        return {
            "tile": {"x": chest_x, "y": chest_y},
            "fresh": False,
            "error": f"Chest at ({chest_x}, {chest_y}) not found in snapshot",
        }

    tile = chest.get("tile", {"x": chest_x, "y": chest_y})
    cap = chest.get("capacity", 36)
    free = chest.get("freeSlots", 0)
    contents = chest.get("contents", [])

    # Extract quality-aware matching keys: (itemId, quality)
    comp_keys: set[tuple[str, int]] = set()
    comp_ids_only: set[str] = set()
    if companion_items:
        for elem in companion_items:
            if isinstance(elem, dict):
                comp_keys.add((str(elem.get("itemId", "")), int(elem.get("quality", 0))))
            elif isinstance(elem, (list, tuple)) and len(elem) >= 2:
                comp_keys.add((str(elem[0]), int(elem[1])))
            elif isinstance(elem, str):
                comp_ids_only.add(elem)

    matching_items: list[dict[str, Any]] = []
    item_stacks: dict[tuple[str, int], list[int]] = {}
    chest_items: list[dict[str, Any]] = []

    for item in contents:
        iid = str(item.get("itemId", ""))
        iq = int(item.get("quality", 0))
        iname = str(item.get("name", ""))
        istack = int(item.get("stack", 1))
        key = (iid, iq)
        item_stacks.setdefault(key, []).append(istack)
        item_dict = {
            "itemId": iid,
            "name": iname,
            "stack": istack,
            "quality": iq,
        }
        chest_items.append(item_dict)
        # Quality-aware matching
        if (key in comp_keys) or (not comp_keys and iid in comp_ids_only):
            matching_items.append(item_dict)

    # Without real maxStack metadata from snapshot, repeat (itemId, quality) stacks
    # can only be treated as candidates: hasDuplicateStacks bool, hasMergeableStacks is "unknown";
    # when there are no duplicates, hasMergeableStacks is False.
    has_duplicate_stacks = any(len(stacks) >= 2 for stacks in item_stacks.values())
    has_mergeable: bool | str = "unknown" if has_duplicate_stacks else False

    # canDeposit:
    # - free > 0: definitely True (empty slots available)
    # - free == 0 and not matching_items: definitely False (chest full, no matching items)
    # - free == 0 and matching_items: without real maxStack metadata from snapshot,
    #   cannot guarantee stacks have room; return "unknown" instead of false True.
    if free > 0:
        can_deposit: bool | str = True
    elif not matching_items:
        can_deposit = False
    else:
        can_deposit = "unknown"

    items_truncated = False if detail else (len(chest_items) > 6)

    res: dict[str, Any] = {
        "tile": tile,
        "capacity": cap,
        "freeSlots": free,
        "itemCount": len(contents),
        "items": chest_items if detail else chest_items[:6],
        "itemsTruncated": items_truncated,
        "matchingItems": matching_items,
        "hasDuplicateStacks": has_duplicate_stacks,
        "hasMergeableStacks": has_mergeable,
        "canDeposit": can_deposit,
        "fresh": True,
    }
    if detail:
        res["contents"] = contents
    return res


def get_candidate_discovery_paths(run_dir: Path | str | None = None) -> list[Path]:
    """Returns candidate transport-discovery.json paths in strict priority order.

    Priority order:
    1. Explicit run_dir (highest priority if provided)
    2. STARDEW_RUN_DIR environment variable
    3. STARDEW_GAME_PATH environment variable -> Mods/StardewAI.Companion.Mod/data/
    4. .env.local in current directory, repo root, or parent directories
    5. .kimi-code/mcp.json project configuration
    6. Standard Steam / SMAPI installation paths on Windows (Registry, SteamPath, standard dirs)

    Safety:
    When run_dir is None (auto-discovery), paths inside any '.test-runs' directory are strictly excluded.
    """
    candidates: list[Path] = []

    # 1. Explicit run_dir (highest priority)
    if run_dir:
        run_path = Path(run_dir).resolve()
        candidates.extend([
            run_path / "mods" / "StardewAI.Companion.Mod" / "data" / "transport-discovery.json",
            run_path / "Mods" / "StardewAI.Companion.Mod" / "data" / "transport-discovery.json",
            run_path / "data" / "transport-discovery.json",
            run_path / "transport-discovery.json",
        ])
        return candidates

    # 2. Environment variable: STARDEW_RUN_DIR
    env_run = os.getenv("STARDEW_RUN_DIR")
    if env_run and env_run.strip():
        p = Path(env_run.strip()).resolve()
        candidates.extend([
            p / "mods" / "StardewAI.Companion.Mod" / "data" / "transport-discovery.json",
            p / "Mods" / "StardewAI.Companion.Mod" / "data" / "transport-discovery.json",
            p / "data" / "transport-discovery.json",
            p / "transport-discovery.json",
        ])

    # 3. Environment variable: STARDEW_GAME_PATH
    env_game = os.getenv("STARDEW_GAME_PATH")
    if env_game and env_game.strip():
        p = Path(env_game.strip()).resolve()
        candidates.extend([
            p / "Mods" / "StardewAI.Companion.Mod" / "data" / "transport-discovery.json",
            p / "mods" / "StardewAI.Companion.Mod" / "data" / "transport-discovery.json",
            p / "data" / "transport-discovery.json",
        ])

    # 4. .env.local in current working directory, repo root, or parent directories
    # (skipped in pytest unless opted in, to keep tests hermetic)
    skip_host_scan = bool(
        os.environ.get("PYTEST_CURRENT_TEST") and not os.environ.get("STARDEW_ENABLE_HOST_GAME_SCAN")
    )
    check_roots: list[Path] = []
    if not skip_host_scan:
        check_roots = [Path.cwd()] + list(Path.cwd().parents)
        module_root = Path(__file__).resolve().parent
        for pr in module_root.parents:
            if pr not in check_roots:
                check_roots.append(pr)

    for root in check_roots:
        env_local = root / ".env.local"
        if env_local.is_file():
            try:
                for line in env_local.read_text(encoding="utf-8", errors="replace").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k, v = k.strip(), v.strip()
                        if k == "STARDEW_RUN_DIR" and v:
                            p = Path(v).resolve()
                            candidates.extend([
                                p / "mods" / "StardewAI.Companion.Mod" / "data" / "transport-discovery.json",
                                p / "Mods" / "StardewAI.Companion.Mod" / "data" / "transport-discovery.json",
                                p / "data" / "transport-discovery.json",
                                p / "transport-discovery.json",
                            ])
                        elif k == "STARDEW_GAME_PATH" and v:
                            p = Path(v).resolve()
                            candidates.extend([
                                p / "Mods" / "StardewAI.Companion.Mod" / "data" / "transport-discovery.json",
                                p / "mods" / "StardewAI.Companion.Mod" / "data" / "transport-discovery.json",
                                p / "data" / "transport-discovery.json",
                            ])
            except Exception as ex:
                logger.debug("Failed reading %s: %s", env_local, ex)
            break

    # 5. Project MCP configuration: .kimi-code/mcp.json
    for root in check_roots:
        mcp_file = root / ".kimi-code" / "mcp.json"
        if mcp_file.is_file():
            try:
                mcp_data = json.loads(mcp_file.read_text(encoding="utf-8"))
                srv = mcp_data.get("mcpServers", {}).get("stardew-companion", {})
                args = srv.get("args", [])
                for i, arg in enumerate(args):
                    if arg == "--run-dir" and i + 1 < len(args):
                        p = Path(args[i + 1]).resolve()
                        candidates.extend([
                            p / "data" / "transport-discovery.json",
                            p / "transport-discovery.json",
                        ])
            except Exception as ex:
                logger.debug("Failed reading %s: %s", mcp_file, ex)
            break

    # 6. Standard Steam / SMAPI installation paths on Windows (skipped in pytest unless opted in)
    detected_game_dirs: list[Path] = []

    skip_host_scan = bool(
        os.environ.get("PYTEST_CURRENT_TEST") and not os.environ.get("STARDEW_ENABLE_HOST_GAME_SCAN")
    )
    if not skip_host_scan:
        for cgp in COMMON_GAME_PATHS:
            p = Path(cgp)
            if p.is_dir() and p not in detected_game_dirs:
                detected_game_dirs.append(p)

        # Windows Registry detection for Steam App 413150
        if sys.platform == "win32":
            try:
                import winreg

                for root_key in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                    for sub_key in (
                        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Steam App 413150",
                        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Steam App 413150",
                    ):
                        try:
                            with winreg.OpenKey(root_key, sub_key) as key:
                                val, _ = winreg.QueryValueEx(key, "InstallLocation")
                                if val:
                                    p = Path(str(val)).resolve()
                                    if p.is_dir() and p not in detected_game_dirs:
                                        detected_game_dirs.append(p)
                        except Exception:
                            pass
            except Exception:
                pass

        for gdir in detected_game_dirs:
            has_smapi = (gdir / "StardewModdingAPI.exe").is_file()
            has_game = (gdir / "Stardew Valley.exe").is_file() or (gdir / "Stardew Valley.dll").is_file()
            if has_smapi or has_game:
                candidates.extend([
                    gdir / "Mods" / "StardewAI.Companion.Mod" / "data" / "transport-discovery.json",
                    gdir / "mods" / "StardewAI.Companion.Mod" / "data" / "transport-discovery.json",
                    gdir / "data" / "transport-discovery.json",
                ])

    # De-duplicate while preserving priority, and strictly exclude any .test-runs paths
    unique_candidates: list[Path] = []
    seen: set[str] = set()
    for cand in candidates:
        if any(part.startswith(".test-runs") or part == "test-runs" for part in cand.parts):
            continue
        cand_str = str(cand).lower()
        if cand_str not in seen:
            seen.add(cand_str)
            unique_candidates.append(cand)

    return unique_candidates


def resolve_discovery(
    run_dir: Path | str | None = None,
    timeout_seconds: float = 0.0,
) -> dict[str, Any]:
    """Resolves transport discovery from run_dir or auto-discovered mod locations.

    If run_dir is None, searches candidate locations in priority order:
    1. STARDEW_RUN_DIR
    2. STARDEW_GAME_PATH
    3. .env.local
    4. .kimi-code/mcp.json
    5. Standard Steam / SMAPI installation paths

    Validates loopback endpoint, port, and non-empty session token.
    NEVER logs or exposes the session token.
    """
    candidates = get_candidate_discovery_paths(run_dir)
    if not candidates:
        raise DiscoveryError(
            "未能找到星露谷游戏或 Companion Mod 目录。"
            "请确认游戏已安装并配置好 Mod，或通过 --run-dir 指定目录。"
        )

    deadline = time.monotonic() + max(0.0, timeout_seconds)
    found_path: Path | None = None

    while True:
        for c in candidates:
            if c.is_file():
                found_path = c
                break
        if found_path or time.monotonic() >= deadline:
            break
        time.sleep(0.2)

    if not found_path:
        if run_dir:
            run_path = Path(run_dir).resolve()
            raise DiscoveryError(
                f"Game is not running or transport discovery file not found in '{run_path}'. "
                "Ensure Stardew Valley is running with StardewAI.Companion.Mod."
            )
        dirs_str = ", ".join(f"'{c.parent}'" for c in candidates[:3])
        raise DiscoveryError(
            f"Game is not running or transport discovery file not found in candidate locations: [{dirs_str}]. "
            "Ensure Stardew Valley is running with StardewAI.Companion.Mod and a save is loaded."
        )

    try:
        content = found_path.read_text(encoding="utf-8")
        data = json.loads(content)
    except Exception as ex:
        msg = f"Failed to read transport discovery file at {found_path}: {ex}"
        raise DiscoveryError(msg) from None

    host = data.get("host")
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise DiscoveryError(f"SECURITY VIOLATION: Non-loopback host in discovery: '{host}'")

    port = data.get("port")
    if not isinstance(port, int) or port <= 0 or port > 65535:
        raise DiscoveryError(f"Invalid port in discovery: {port}")

    token = data.get("sessionToken")
    if not token or not isinstance(token, str) or not token.strip():
        raise DiscoveryError("Missing or empty sessionToken in transport discovery")

    save_id = data.get("saveId", "")
    game_session_id = data.get("gameSessionId", "")

    parent = found_path.parent
    mod_dir = parent.parent if parent.name.lower() == "data" else parent

    return {
        "host": host,
        "port": port,
        "sessionToken": token,
        "saveId": save_id,
        "gameSessionId": game_session_id,
        "endpoint": f"ws://{host}:{port}/",
        "discoveryPath": str(found_path),
        "modDir": str(mod_dir),
    }


def _manhattan(a: tuple[int, int], b: tuple[int, int]) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def order_candidates_for_contiguous_work(
    tiles: list[dict[str, int]],
    *,
    anchor: dict[str, int] | None = None,
    preferred_group_size: int | None = None,
    connectivity: int = 8,
) -> list[dict[str, int]]:
    """Order candidate farm tiles so consecutive actions prefer a contiguous patch.

    Candidates are grouped into connected components (8-connected by default, so a
    normal farm plot counts as one patch). Components large enough to satisfy
    ``preferred_group_size`` are visited first (nearest to ``anchor`` first, then
    largest), then the remaining smaller groups. Inside each component a
    nearest-neighbour walk keeps consecutive tiles adjacent, which avoids the
    scattered picking across the field.

    Grouping is a preference, not a hard rule: when no contiguous group is big
    enough the caller still receives every candidate, just ordered sensibly.
    Explicit tiles supplied by the model are never reordered by this helper.
    """
    normalized: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for tile in tiles:
        if not isinstance(tile, dict) or "x" not in tile or "y" not in tile:
            continue
        coord = (int(tile["x"]), int(tile["y"]))
        if coord in seen:
            continue
        seen.add(coord)
        normalized.append(coord)

    if len(normalized) <= 1:
        return [{"x": x, "y": y} for x, y in normalized]

    anchor_coord: tuple[int, int] | None = None
    if isinstance(anchor, dict) and "x" in anchor and "y" in anchor:
        anchor_coord = (int(anchor["x"]), int(anchor["y"]))

    neighbor_offsets = (
        ((1, 0), (-1, 0), (0, 1), (0, -1))
        if connectivity == 4
        else ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1))
    )

    order_index = {coord: i for i, coord in enumerate(normalized)}
    tile_set = set(normalized)
    components: list[list[tuple[int, int]]] = []
    visited: set[tuple[int, int]] = set()

    for coord in normalized:
        if coord in visited:
            continue
        stack = [coord]
        visited.add(coord)
        component: list[tuple[int, int]] = []
        while stack:
            current = stack.pop()
            component.append(current)
            cx, cy = current
            for dx, dy in neighbor_offsets:
                neighbor = (cx + dx, cy + dy)
                if neighbor in tile_set and neighbor not in visited:
                    visited.add(neighbor)
                    stack.append(neighbor)
        components.append(component)

    def anchor_distance(coord: tuple[int, int]) -> int:
        if anchor_coord is None:
            return order_index[coord]
        return _manhattan(coord, anchor_coord)

    def fits_preference(component: list[tuple[int, int]]) -> int:
        if preferred_group_size is None:
            return 0
        return 0 if len(component) >= preferred_group_size else 1

    components.sort(
        key=lambda component: (
            fits_preference(component),
            min(anchor_distance(c) for c in component),
            -len(component),
            min(order_index[c] for c in component),
        )
    )

    ordered: list[tuple[int, int]] = []
    for component in components:
        remaining = set(component)
        current = min(component, key=lambda c: (anchor_distance(c), order_index[c]))
        while remaining:
            ordered.append(current)
            remaining.discard(current)
            if not remaining:
                break
            next_coord = min(
                remaining,
                key=lambda c: (
                    0 if (abs(c[0] - current[0]) <= 1 and abs(c[1] - current[1]) <= 1) else 1,
                    _manhattan(c, current),
                    order_index[c],
                ),
            )
            current = next_coord

    return [{"x": x, "y": y} for x, y in ordered]


def build_unified_outcome(
    *,
    outcome: str,
    goal_satisfied: bool,
    effects: list[dict[str, Any]] | None = None,
    remaining: Any = None,
    reason_code: str | None = None,
    snapshot_revision: int | None = None,
) -> dict[str, Any]:
    """Build the shared composite-result envelope.

    ``outcome`` is one of completed|partial|unknown. ``goalSatisfied`` states whether
    the requested goal is fully met by verified world state (already-satisfied work
    counts). Legacy fields are preserved alongside these keys by the callers.
    """
    if outcome not in {"completed", "partial", "unknown"}:
        raise ValueError(f"invalid outcome '{outcome}'")
    return {
        "outcome": outcome,
        "goalSatisfied": bool(goal_satisfied),
        "effects": list(effects or []),
        "remaining": remaining,
        "reasonCode": reason_code,
        "snapshotRevision": snapshot_revision,
    }


@dataclass
class ActiveTask:
    task_id: str
    command_id: str | None
    skill_id: str
    status: str  # "running", "paused", "cancelling", "succeeded", "failed", "cancelled"
    location_id: str
    tiles: list[dict[str, int]]
    created_at: datetime
    expires_at: datetime
    idempotency_key: str
    requested_command_id: str | None = None
    is_terminal: bool = False


@dataclass
class _FreePurchaseGuard:
    """Result of the free-mode daily-budget gate on one purchase dispatch.

    ``early_response`` is set when the caller must return it instead of
    dispatching (budget rejection, settled-command replay, or a reconciled
    in-flight command); otherwise the purchase proceeds with
    ``effective_budget``, which never exceeds the daily remaining allowance.
    """

    autonomy: AutonomyController | None
    save_id: str | None
    command_id: str | None
    effective_budget: int
    early_response: dict[str, Any] | None = None


class CompanionScheduler:
    """Thin Policy and Task Scheduler for Stardew AI Companion.

    This is the single entry point for task execution and coordination.
    Both MCP tools and future autonomous Planners must execute skills through
    this scheduler, enforcing:
    1. Single active task at any time (rejection of concurrent tasks).
    2. Strict parameter validation (radius 0..2, non-negative coordinates).
    3. Deterministic idempotency key generation: {saveId}:{taskId}:{attempt}.
    4. Task expiration deadlines and cancellation/pause tracking.
    5. Lazy loopback connection to Companion Mod with automatic reconnection.
    6. Zero exposure of session tokens or primitive game manipulation.
    """

    def __init__(
        self,
        run_dir: Path | str | None = None,
        client: TransportClient | None = None,
        discovery_resolver: Callable[[Path | str | None], dict[str, Any]] | None = None,
    ):
        self.run_dir = Path(run_dir) if run_dir else None
        self._client = client
        self._discovery_resolver = discovery_resolver or resolve_discovery
        self._active_task: ActiveTask | None = None
        self._lock = asyncio.Lock()
        self._latest_snapshot_data: dict[str, Any] | None = None

    @property
    def active_task(self) -> ActiveTask | None:
        return self._active_task

    @property
    def has_active_task(self) -> bool:
        return self._active_task is not None and not self._active_task.is_terminal

    async def ensure_connected(self) -> TransportClient:
        """Lazily establishes loopback connection and completes protocol handshake."""
        async with self._lock:
            return await self._ensure_connected_locked()

    async def reconcile_command(self, command_id: str, timeout: float = 0.2) -> dict[str, Any] | None:
        """Recover a terminal result after reconnect without dispatching again.

        A recovered terminal result also settles the free-mode purchase ledger
        for that command id exactly once; ids without a reservation are
        untouched, so this stays a no-op for non-purchase commands.
        """
        client = await self.ensure_connected()
        try:
            envelope = client.get_cached_result(command_id)
            if envelope is None:
                envelope = await client.wait_for_result(command_id, timeout=timeout)
            payload = envelope.payload
        except TimeoutError:
            return None
        self._settle_free_mode_purchase(str(client.save_id or "unknown-save"), command_id, payload)
        return payload

    def _autonomy_store(self) -> AutonomyController:
        return AutonomyController(Path(self.run_dir or ".") / "data" / "autonomy-state.json")

    def _settle_free_mode_purchase(self, save_id: str, command_id: str, payload: Any) -> None:
        """Charge (or release) a reserved purchase exactly once from a confirmed result.

        Only terminal native results settle: success/failure/cancel with a
        confirmed non-negative ``totalCost`` charge the real cost (a failure
        therefore refunds its reservation); anything else keeps the reservation
        for a later reconcile. Settling is idempotent per command id.
        """
        if not isinstance(payload, dict):
            return
        try:
            autonomy = self._autonomy_store()
            state = autonomy.state(save_id)
            if command_id not in state.spend_reservations:
                return
            details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
            total_cost = details.get("totalCost")
            terminal = payload.get("terminalState")
            settleable = (
                terminal in {"succeeded", "failed", "cancelled"}
                and isinstance(total_cost, int)
                and total_cost >= 0
            )
            autonomy.settle_spend(
                save_id,
                command_id,
                total_cost if settleable else None,
                unknown=not settleable,
            )
        except Exception:
            logger.debug("Free-mode purchase settle failed for %s", command_id, exc_info=True)

    async def _ensure_connected_locked(self) -> TransportClient:
        """Connection helper for callers already holding self._lock (not reentrant)."""
        if self._client is not None and self._client.is_connected:
            return self._client

        # If client was disconnected or not created yet
        if self._client is None:
            discovery = self._discovery_resolver(self.run_dir)
            if not self.run_dir and discovery.get("modDir"):
                self.run_dir = Path(discovery["modDir"])
            self._client = TransportClient(
                host=discovery["host"],
                port=discovery["port"],
                session_token=discovery["sessionToken"],
            )

        try:
            if not self._client.is_connected:
                await self._client.connect(timeout=5.0)
                await self._client.handshake(timeout=5.0)
                try:
                    snapshot_env = await self._client.wait_for_snapshot(timeout=3.0)
                    self._cache_snapshot(snapshot_env)
                except Exception:
                    pass
        except Exception as ex:
            try:
                await self._client.close()
            except Exception:
                pass
            self._client = None
            raise SchedulerError(
                f"Failed to connect and handshake with Companion Mod: {ex}"
            ) from None

        return self._client

    def _cache_snapshot(self, envelope: Envelope) -> None:
        if envelope and envelope.message_type == "world.snapshot":
            self._latest_snapshot_data = {
                "payload": envelope.payload,
                "saveId": envelope.save_id,
                "gameSessionId": envelope.game_session_id,
                "worldRevision": envelope.world_revision,
            }

    async def _refresh_snapshot(self, client: TransportClient) -> None:
        """Drains any freshly pushed snapshot (the mod pushes world.snapshot after
        every completed execution); falls back to the last known one on timeout."""
        try:
            snapshot_env = await client.wait_for_snapshot(timeout=0.5)
            self._cache_snapshot(snapshot_env)
        except Exception:
            snapshot_env = client.latest_snapshot
            if snapshot_env is not None:
                self._cache_snapshot(snapshot_env)

        if not self._latest_snapshot_data:
            try:
                snapshot_env = await client.wait_for_snapshot(timeout=3.0)
                self._cache_snapshot(snapshot_env)
            except Exception:
                pass

    @property
    def latest_world_revision(self) -> int:
        snap = self._latest_snapshot_data or {}
        rev = snap.get("worldRevision")
        if rev is not None:
            return int(rev)
        if self._client is not None:
            return int(self._client.world_revision)
        return 0

    @property
    def latest_snapshot(self) -> dict[str, Any] | None:
        """Latest cached world snapshot (read-only); never triggers a new query."""
        return self._latest_snapshot_data

    async def wait_for_fresh_snapshot(
        self, pre_revision: int, timeout: float = 1.0
    ) -> tuple[dict[str, Any] | None, bool]:
        """Waits for a post-action snapshot with worldRevision > pre_revision.

        Returns (snapshot_dict, True) if a fresh snapshot is received,
        or (None, False) if timed out or revision did not increment.
        """
        if self._client is None or not self._client.is_connected:
            return None, False

        # Check if latest_snapshot already advanced past pre_revision
        if (
            self._client.latest_snapshot
            and self._client.latest_snapshot.world_revision > pre_revision
        ):
            self._cache_snapshot(self._client.latest_snapshot)
            return self._latest_snapshot_data, True

        try:
            snapshot_env = await self._client.wait_for_snapshot(timeout=timeout)
            if snapshot_env and snapshot_env.world_revision > pre_revision:
                self._cache_snapshot(snapshot_env)
                return self._latest_snapshot_data, True
        except Exception:
            pass

        if (
            self._client.latest_snapshot
            and self._client.latest_snapshot.world_revision > pre_revision
        ):
            self._cache_snapshot(self._client.latest_snapshot)
            return self._latest_snapshot_data, True

        return None, False

    async def get_work_overview(self, detail: bool = False) -> dict[str, Any]:
        """Provides a consolidated overview of farm work, companion inventory, and chests.

        Reads from the consistent latest world snapshot:
        - Farm work: mature crops and unwatered tilled soil counts.
        - Companion: available backpack space and non-tool items.
        - Chests: candidate locations, capacities, matching items, and mergeable stacks.
        - Explicitly marks missing snapshot sections, revisions, and truncation.
        """
        client = await self.ensure_connected()
        await self._refresh_snapshot(client)

        snap = self._latest_snapshot_data or {}
        payload = snap.get("payload", {})
        companion = payload.get("companion", {})
        world = payload.get("world", {})
        save_id = client.save_id or snap.get("saveId", "unknown")
        game_session_id = client.game_session_id or snap.get("gameSessionId", "unknown")
        world_revision = client.world_revision or snap.get("worldRevision", 0)

        missing_sections: list[str] = []

        # 1. Farm Work
        farm_work = payload.get("farmWork") or world.get("farmWork")
        if farm_work is None:
            missing_sections.append("farmWork")
            farm_work_info: dict[str, Any] = {
                "matureCropCount": None,
                "tilledUnwateredCount": None,
                "isTruncated": False,
                "missing": True,
                "error": "farmWork section not found in snapshot",
            }
        else:
            unwatered_tiles = farm_work.get("tilledUnwateredTiles", [])
            unwatered_count = farm_work.get("tilledUnwateredCount", len(unwatered_tiles))
            mature_crops = farm_work.get("matureCrops", [])
            mature_count = farm_work.get("matureCropCount", len(mature_crops))
            is_truncated = bool(
                farm_work.get("isTruncated", False)
                or farm_work.get("truncated", False)
                or farm_work.get("matureCropsTruncated", False)
            )
            farm_work_info = {
                "matureCropCount": mature_count,
                "tilledUnwateredCount": unwatered_count,
                "cropUnwateredCount": farm_work.get("cropUnwateredCount"),
                # This is an actionable candidate list, so it must survive
                # the compact (detail=False) normalization used by water_auto.
                # Otherwise an empty prepared tile is indistinguishable from
                # a crop tile and the safe crop-only default is lost.
                "cropUnwateredTiles": farm_work.get("cropUnwateredTiles"),
                "isTruncated": is_truncated,
                "missing": False,
            }
            if detail:
                farm_work_info["matureCrops"] = mature_crops
                farm_work_info["tilledUnwateredTiles"] = unwatered_tiles

        # 2. Companion & Inventory
        inventory = payload.get("inventory")
        if inventory is None:
            missing_sections.append("inventory")
            companion_info: dict[str, Any] = {
                "freeSlots": None,
                "capacity": None,
                "nonToolItems": [],
                "activity": companion.get("activity", "idle"),
                "stamina": companion.get("stamina", 0.0),
                "waterCanLevel": companion.get("waterCanLevel", 0),
                "missing": True,
                "error": "inventory section not found in snapshot",
            }
        else:
            capacity = inventory.get("capacity", 0)
            free_slots = inventory.get("freeSlots", 0)
            slots = inventory.get("slots", [])
            non_tool_items = extract_non_tool_items(slots)
            companion_info = {
                "freeSlots": free_slots,
                "capacity": capacity,
                "nonToolItems": non_tool_items,
                "activity": companion.get("activity", "idle"),
                "stamina": companion.get("stamina", 0.0),
                "waterCanLevel": companion.get("waterCanLevel", 0),
                "missing": False,
            }
            if detail:
                companion_info["slots"] = slots
                companion_info["tileX"] = companion.get("tileX", 0)
                companion_info["tileY"] = companion.get("tileY", 0)

        # 3. Chests
        chests_payload = payload.get("chests")
        chests_info: list[dict[str, Any]] = []
        if chests_payload is None:
            missing_sections.append("chests")
            chests_truncated = False
            chests_missing = True
            chests_error = "chests section not found in snapshot"
        else:
            chests_missing = False
            chests_error = None
            chests_items = chests_payload.get("items", [])
            chests_truncated = bool(chests_payload.get("truncated", False))
            comp_non_tool_items = companion_info.get("nonToolItems", [])
            chests_info = [
                extract_chest_summary(
                    c,
                    c.get("tile", {}).get("x", 0),
                    c.get("tile", {}).get("y", 0),
                    comp_non_tool_items,
                    detail=detail,
                )
                for c in chests_items
            ]

        res: dict[str, Any] = {
            "farmWork": farm_work_info,
            "companion": companion_info,
            "chests": chests_info,
            "chestsTruncated": chests_truncated,
            "isTruncated": bool(farm_work_info.get("isTruncated", False) or chests_truncated),
            "missingSections": missing_sections,
            "canSafelyPlan": len(missing_sections) == 0,
            "worldRevision": world_revision,
            "saveId": save_id,
            "gameSessionId": game_session_id,
        }
        if chests_missing:
            res["chestsMissing"] = True
            res["chestsError"] = chests_error
        return res

    async def get_status(self) -> dict[str, Any]:
        """Retrieves companion and world status."""
        client = await self.ensure_connected()

        # Check if active task completed via cached result
        if self.has_active_task and self._active_task and self._active_task.command_id:
            cached_env = client.get_cached_result(self._active_task.command_id)
            if cached_env:
                self._active_task.status = cached_env.payload.get("terminalState", "succeeded")
                self._active_task.is_terminal = True
                self._active_task = None

        await self._refresh_snapshot(client)

        # If companion is now idle, check if the running task just finished
        if self.has_active_task and self._active_task and self._active_task.command_id:
            snap = self._latest_snapshot_data or {}
            comp_act = snap.get("payload", {}).get("companion", {}).get("activity")
            if comp_act == "idle":
                try:
                    res_env = await client.wait_for_result(self._active_task.command_id, timeout=0.2)
                    self._active_task.status = res_env.payload.get("terminalState", "succeeded")
                    self._active_task.is_terminal = True
                    self._active_task = None
                except Exception:
                    pass

        snap = self._latest_snapshot_data or {}
        payload = snap.get("payload", {})
        companion = payload.get("companion", {})
        world = payload.get("world", {})

        current_task = None
        if self.has_active_task and self._active_task:
            current_task = {
                "taskId": self._active_task.task_id,
                "commandId": self._active_task.command_id,
                "skillId": self._active_task.skill_id,
                "status": self._active_task.status,
            }

        farm_work = payload.get("farmWork") or world.get("farmWork") or {}
        unwatered_tiles = farm_work.get("tilledUnwateredTiles", [])
        unwatered_count = farm_work.get("tilledUnwateredCount", len(unwatered_tiles))
        is_truncated = bool(
            farm_work.get("isTruncated", False) or farm_work.get("truncated", False)
        )
        mature_crop_count = farm_work.get("matureCropCount", 0)
        mature_crops = farm_work.get("matureCrops", [])
        mature_crops_truncated = bool(farm_work.get("matureCropsTruncated", False))

        return {
            "companion": {
                "locationId": companion.get("locationId", "Farm"),
                "tileX": companion.get("tileX", 0),
                "tileY": companion.get("tileY", 0),
                "facingDirection": companion.get("facingDirection", 2),
                "stamina": companion.get("stamina", 0.0),
                "maxStamina": companion.get("maxStamina", 270.0),
                "waterCanLevel": companion.get("waterCanLevel", 0),
                "maxWaterCanLevel": companion.get("maxWaterCanLevel", 40),
                "hasWateringCan": companion.get("hasWateringCan", True),
                "activity": companion.get("activity", "idle"),
                "currentTask": current_task,
                # Thin-observer facts the compact decision context needs. Absent
                # stays absent (never a fabricated zero/clear).
                "availableMoney": companion.get("availableMoney"),
                "moneyStatus": companion.get("moneyStatus"),
            },
            "world": {
                "currentLocation": world.get("currentLocation", "Farm"),
                "timeOfDay": world.get("timeOfDay", 600),
                "season": world.get("season", "spring"),
                "dayOfMonth": world.get("dayOfMonth", 1),
                "year": world.get("year"),
                "isRaining": world.get("isRaining", False),
                # Real native weather icon; ``isRaining=false`` is not "clear".
                "weatherIcon": world.get("weatherIcon"),
            },
            "farmWork": {
                "tilledUnwateredTiles": unwatered_tiles,
                "tilledUnwateredCount": unwatered_count,
                "cropUnwateredTiles": farm_work.get("cropUnwateredTiles"),
                "cropUnwateredCount": farm_work.get("cropUnwateredCount"),
                "isTruncated": is_truncated,
                "matureCropCount": mature_crop_count,
                "matureCrops": mature_crops,
                "matureCropsTruncated": mature_crops_truncated,
            },
            "saveId": client.save_id or snap.get("saveId", "unknown"),
            "gameSessionId": client.game_session_id or snap.get("gameSessionId", "unknown"),
            "worldRevision": client.world_revision or snap.get("worldRevision", 0),
        }

    async def query_farm_work(self) -> dict[str, Any]:
        """Queries farm work availability from the latest world snapshot.

        Returns:
        - farmWork: tilledUnwateredTiles, tilledUnwateredCount, isTruncated,
          matureCropCount, matureCrops, matureCropsTruncated
        - companion: status summary (location, stamina, water level, activity)
        - world: current location, time of day, season, and weather
        - saveId, gameSessionId, worldRevision
        """
        status = await self.get_status()
        return {
            "farmWork": status["farmWork"],
            "companion": status["companion"],
            "world": status["world"],
            "saveId": status["saveId"],
            "gameSessionId": status["gameSessionId"],
            "worldRevision": status["worldRevision"],
        }

    async def execute_tiles(
        self,
        tiles: list[dict[str, int]],
        location_id: str = "Farm",
        timeout_seconds: float = 30.0,
        task_id: str | None = None,
        command_id: str | None = None,
    ) -> dict[str, Any]:
        """Executes watering on an explicit list of tiles through policy validation and tracking.

        Enforces:
        - tiles count between 1 and 64
        - coordinate non-negative integers
        - deterministic row-major ordering (y, x)
        - single active task concurrency check
        - idempotency key generation: {saveId}:{taskId}:attempt-1
        """
        if not isinstance(tiles, list) or len(tiles) == 0:
            raise PolicyViolationError("tiles must be a non-empty list")
        if len(tiles) > 64:
            raise PolicyViolationError(
                f"tiles count ({len(tiles)}) exceeds maximum allowed batch limit (64)"
            )

        validated_tiles: list[dict[str, int]] = []
        for t in tiles:
            if not isinstance(t, dict) or "x" not in t or "y" not in t:
                raise PolicyViolationError(f"invalid tile format: {t}")
            x = t["x"]
            y = t["y"]
            if (
                isinstance(x, bool)
                or not isinstance(x, int)
                or isinstance(y, bool)
                or not isinstance(y, int)
            ):
                raise PolicyViolationError(f"tile coordinates must be integers: {t}")
            if x < 0 or y < 0:
                raise PolicyViolationError(f"tile coordinates must be non-negative: {t}")
            validated_tiles.append({"x": x, "y": y})

        # Sort deterministically row-major: y, then x
        validated_tiles.sort(key=lambda item: (item["y"], item["x"]))

        async def dispatch(
            client: TransportClient,
            *,
            task_id: str,
            idempotency_key: str,
            expires_seconds: float,
        ) -> str:
            water_kwargs: dict[str, Any] = {
                "location_id": location_id,
                "tiles": validated_tiles,
                "task_id": task_id,
                "idempotency_key": idempotency_key,
                "expires_seconds": expires_seconds,
            }
            if command_id is not None:
                water_kwargs["command_id"] = command_id
            return await client.execute_water_zone(**water_kwargs)

        return await self.execute_skill(
            skill_id="water-zone",
            task_prefix="task-water-",
            dispatch=dispatch,
            location_id=location_id,
            tiles=validated_tiles,
            timeout_seconds=timeout_seconds,
            task_id=task_id,
            requested_command_id=command_id,
        )

    async def execute_skill(
        self,
        *,
        skill_id: str,
        task_prefix: str,
        dispatch: Callable[..., Awaitable[str]],
        location_id: str = "Farm",
        tiles: list[dict[str, int]] | None = None,
        timeout_seconds: float = 30.0,
        task_id: str | None = None,
        requested_command_id: str | None = None,
    ) -> dict[str, Any]:
        """Generic single-active-task execution core shared by all skills.

        Enforces:
        - single active task concurrency check
        - task id generation with the skill-specific prefix
        - idempotency key generation: {saveId}:{taskId}:attempt-1
        - task expiration deadline and lifecycle tracking

        `dispatch` is invoked as
        `dispatch(client, task_id=..., idempotency_key=..., expires_seconds=...)`
        and must send the skill.execute envelope, returning its command id.
        """
        from stardew_ai_runtime.compatibility import assert_native_compatible
        assert_native_compatible(self.run_dir)
        async with self._lock:
            is_same_task = False
            if self.has_active_task and self._active_task:
                same_task_id = task_id is not None and self._active_task.task_id == task_id
                # A retry that reuses the same stable plan command id addresses the
                # very same native task: reconcile its in-flight result instead of
                # re-dispatching (the Mod enforces idempotency on idempotencyKey).
                same_requested_id = bool(
                    requested_command_id
                    and self._active_task.requested_command_id == requested_command_id
                )
                if same_task_id or same_requested_id:
                    is_same_task = True
                    cmd_id = self._active_task.command_id
                    client = await self._ensure_connected_locked()
                else:
                    raise PolicyViolationError(
                        f"Cannot execute task: another task '{self._active_task.task_id}' "
                        f"is currently active (status: {self._active_task.status}). "
                        "Concurrent tasks are not permitted."
                    )
            else:
                client = await self._ensure_connected_locked()

                tsk_id = task_id or (
                    requested_task_id(requested_command_id)
                    if requested_command_id
                    else f"{task_prefix}{uuid.uuid4().hex[:8]}"
                )
                save_id = client.save_id or "unknown-save"
                idem_key = f"{save_id}:{tsk_id}:attempt-1"
                now = datetime.now(UTC)

                self._active_task = ActiveTask(
                    task_id=tsk_id,
                    command_id=None,
                    skill_id=skill_id,
                    status="running",
                    location_id=location_id,
                    tiles=list(tiles or []),
                    created_at=now,
                    expires_at=now + timedelta(seconds=timeout_seconds),
                    idempotency_key=idem_key,
                    requested_command_id=requested_command_id,
                )

        try:
            if not is_same_task:
                cmd_id = await dispatch(
                    client,
                    task_id=self._active_task.task_id,
                    idempotency_key=self._active_task.idempotency_key,
                    expires_seconds=timeout_seconds,
                )
                self._active_task.command_id = cmd_id
            elif not cmd_id:
                cmd_id = self._active_task.command_id

            if not cmd_id:
                raise SchedulerError("Active task missing command ID for wait")

            result_env = await client.wait_for_result(
                command_id=cmd_id, timeout=timeout_seconds
            )
            payload = result_env.payload
            terminal_state = payload.get("terminalState", "unknown")
            self._active_task.status = terminal_state
            self._active_task.is_terminal = True

            return {
                "status": "executed",
                "terminalState": terminal_state,
                "taskId": self._active_task.task_id,
                "commandId": cmd_id,
                "completedCount": payload.get("completedCount", 0),
                "skippedCount": payload.get("skippedCount", 0),
                "failedCount": payload.get("failedCount", 0),
                "effects": payload.get("effects", []),
                "details": payload.get("details"),
                "error": payload.get("error"),
                "worldRevision": payload.get("worldRevision"),
            }
        except TimeoutError:
            # The mod task is still executing on the companion in the background.
            # Do NOT mark as failed or terminal!
            # Single active task state is preserved to prevent blind re-dispatch.
            if self._active_task:
                self._active_task.status = "running"
                self._active_task.is_terminal = False
            cur_tid = self._active_task.task_id if self._active_task else (task_id or "")
            return {
                "status": "executing",
                "terminalState": "running",
                "taskId": cur_tid,
                "commandId": cmd_id if "cmd_id" in locals() else None,
                "inProgress": True,
                "completedCount": 0,
                "skippedCount": 0,
                "failedCount": 0,
                "effects": [],
                "details": {
                    "taskId": cur_tid,
                    "status": "running",
                    "inProgress": True,
                    "timeoutSeconds": timeout_seconds,
                    "message": f"Task '{cur_tid}' is still executing on companion after {timeout_seconds}s (not failed). Use get_status or pass taskId to query completion.",
                },
                "message": f"Task '{cur_tid}' is still executing on companion after {timeout_seconds}s (in progress).",
                "error": None,
            }
        except Exception as ex:
            if self._active_task:
                self._active_task.status = "failed"
                self._active_task.is_terminal = True
            raise SchedulerError(f"Task execution error: {ex}") from None
        finally:
            if self._active_task and self._active_task.is_terminal:
                self._active_task = None


    async def query_inventory(self) -> dict[str, Any]:
        """Projects the companion inventory section from the latest world snapshot.

        Returns inventory, worldRevision, saveId, and gameSessionId.
        Raises SchedulerError if the snapshot lacks the inventory section
        (the running game build is too old).
        """
        client = await self.ensure_connected()
        await self._refresh_snapshot(client)

        snap = self._latest_snapshot_data or {}
        payload = snap.get("payload", {})
        inventory = payload.get("inventory")
        if inventory is None:
            raise SchedulerError(
                "World snapshot does not include an 'inventory' section. "
                "The running game build is too old; upgrade StardewAI.Companion.Mod "
                "to a build that publishes inventory snapshots."
            )

        return {
            "inventory": inventory,
            "worldRevision": client.world_revision or snap.get("worldRevision", 0),
            "saveId": client.save_id or snap.get("saveId", "unknown"),
            "gameSessionId": client.game_session_id or snap.get("gameSessionId", "unknown"),
        }

    async def query_chests(self) -> dict[str, Any]:
        """Projects the farm chests section from the latest world snapshot.

        Returns chests (list of chest summaries), isTruncated, worldRevision,
        saveId, and gameSessionId. Raises SchedulerError if the snapshot lacks
        the chests section (the running game build is too old).
        """
        client = await self.ensure_connected()
        await self._refresh_snapshot(client)

        snap = self._latest_snapshot_data or {}
        payload = snap.get("payload", {})
        chests = payload.get("chests")
        if chests is None:
            raise SchedulerError(
                "World snapshot does not include a 'chests' section. "
                "The running game build is too old; upgrade StardewAI.Companion.Mod "
                "to a build that publishes chest snapshots."
            )

        return {
            "chests": chests.get("items", []),
            "isTruncated": bool(chests.get("truncated", False)),
            "worldRevision": client.world_revision or snap.get("worldRevision", 0),
            "saveId": client.save_id or snap.get("saveId", "unknown"),
            "gameSessionId": client.game_session_id or snap.get("gameSessionId", "unknown"),
        }

    async def query_planting_options(self, detail: bool = False) -> dict[str, Any]:
        """Projects planting options (seeds, season, tillable dirt, hoed empty tiles) from snapshot.

        Enforces:
        - explicit unknown/error if snapshot sections are missing (never pretends 0)
        - concise candidate sample & seed summary if detail=False; full lists if detail=True
        - returns season, dayOfMonth, companionHasHoe, seeds, candidateTiles, searchBounds,
          worldRevision, saveId, gameSessionId
        """
        client = await self.ensure_connected()
        await self._refresh_snapshot(client)

        snap = self._latest_snapshot_data or {}
        payload = snap.get("payload", {})
        planting = payload.get("planting")
        if planting is None:
            raise SchedulerError(
                "World snapshot does not include a 'planting' section. "
                "The running game build is too old; upgrade StardewAI.Companion.Mod "
                "to a build that publishes planting snapshots."
            )
        if not isinstance(planting, dict):
            raise SchedulerError("World snapshot contains an invalid 'planting' section")

        candidate_tiles = planting.get("candidateTiles")
        if candidate_tiles is None or not isinstance(candidate_tiles, dict):
            raise SchedulerError(
                "Planting snapshot is missing a valid 'candidateTiles' section"
            )

        season = planting.get("season", "unknown")
        day_of_month = planting.get("dayOfMonth")
        if day_of_month is None:
            day_of_month = "unknown"
        companion_has_hoe = planting.get("companionHasHoe")
        if companion_has_hoe is None:
            companion_has_hoe = "unknown"

        raw_seeds = planting.get("seeds")
        if raw_seeds is None or not isinstance(raw_seeds, list):
            seeds: list[dict[str, Any]] | str = "unknown"
        else:
            seeds = []
            for s in raw_seeds:
                seed_dict: dict[str, Any] = {
                    "itemId": s.get("itemId", ""),
                    "name": s.get("name", ""),
                    "stack": s.get("stack", 0),
                    "canPlantCurrentSeason": s.get("canPlantCurrentSeason", False),
                    "seasons": s.get("seasons", []),
                }
                if detail:
                    seed_dict["growthDays"] = s.get("growthDays")
                    seed_dict["regrows"] = s.get("regrows")
                    seed_dict["isRaised"] = s.get("isRaised")
                seeds.append(seed_dict)

        tilled_empty_tiles = candidate_tiles.get("tilledEmptyTiles")
        tillable_tiles = candidate_tiles.get("tillableTiles")

        tilled_empty_count = candidate_tiles.get("tilledEmptyCount")
        if tilled_empty_count is None:
            tilled_empty_count = (
                len(tilled_empty_tiles) if isinstance(tilled_empty_tiles, list) else "unknown"
            )

        tillable_count = candidate_tiles.get("tillableCount")
        if tillable_count is None:
            tillable_count = (
                len(tillable_tiles) if isinstance(tillable_tiles, list) else "unknown"
            )

        tilled_empty_truncated = candidate_tiles.get("tilledEmptyTruncated", False)
        tillable_truncated = candidate_tiles.get("tillableTruncated", False)

        if not isinstance(tilled_empty_tiles, list):
            tilled_empty_tiles_out: list[dict[str, int]] | str = "unknown"
            tilled_empty_trunc_out = tilled_empty_truncated
        else:
            tilled_empty_tiles_out = (
                tilled_empty_tiles if detail else tilled_empty_tiles[:6]
            )
            tilled_empty_trunc_out = (
                tilled_empty_truncated
                if detail
                else (tilled_empty_truncated or len(tilled_empty_tiles) > 6)
            )

        if not isinstance(tillable_tiles, list):
            tillable_tiles_out: list[dict[str, int]] | str = "unknown"
            tillable_trunc_out = tillable_truncated
        else:
            tillable_tiles_out = tillable_tiles if detail else tillable_tiles[:6]
            tillable_trunc_out = (
                tillable_truncated
                if detail
                else (tillable_truncated or len(tillable_tiles) > 6)
            )

        candidate_result: dict[str, Any] = {
            "tilledEmptyCount": tilled_empty_count,
            "tilledEmptyTiles": tilled_empty_tiles_out,
            "tilledEmptyTruncated": tilled_empty_trunc_out,
            "tillableCount": tillable_count,
            "tillableTiles": tillable_tiles_out,
            "tillableTruncated": tillable_trunc_out,
        }

        search_bounds = planting.get("searchBounds")
        if search_bounds is None or not isinstance(search_bounds, dict):
            search_bounds_result: dict[str, Any] | str = "unknown"
        else:
            search_bounds_result = {
                "center": search_bounds.get("center", {"x": 0, "y": 0}),
                "radius": search_bounds.get("radius", 0),
            }

        return {
            "season": season,
            "dayOfMonth": day_of_month,
            "companionHasHoe": companion_has_hoe,
            "seeds": seeds,
            "candidateTiles": candidate_result,
            "searchBounds": search_bounds_result,
            "worldRevision": client.world_revision or snap.get("worldRevision", 0),
            "saveId": client.save_id or snap.get("saveId", "unknown"),
            "gameSessionId": client.game_session_id or snap.get("gameSessionId", "unknown"),
        }

    async def query_shop(
        self,
        shop_id: str = "SeedShop",
        detail: bool = False,
        item_id: str | None = None,
        name: str | None = None,
        is_seed: bool | None = None,
    ) -> dict[str, Any]:
        """Projects shop information (items, stock, price, isOpen, availableMoney) from snapshot.

        Enforces:
        - explicit unknown/error if snapshot sections are missing (never pretends 0)
        - raises clear error if requested shop_id is not published in current snapshot
        - supports filtering items by item_id, name, or is_seed to prevent dumping full catalog
        - concise item projection by default (detail=False); complete item metadata when detail=True
        - returns shopId, status, isOpen, ownerPresent, closedMessage, owners,
          currency, availableMoney, moneyStatus, itemsCount, items,
          worldRevision, saveId, gameSessionId
        """
        client = await self.ensure_connected()
        await self._refresh_snapshot(client)

        snap = self._latest_snapshot_data or {}
        payload = snap.get("payload", {})
        shop_snap = payload.get("shop")
        if shop_snap is None:
            raise SchedulerError(
                "World snapshot does not include a 'shop' section. "
                "The running game build is too old; upgrade StardewAI.Companion.Mod "
                "to a build that publishes shop snapshots."
            )
        if not isinstance(shop_snap, dict):
            raise SchedulerError("World snapshot contains an invalid 'shop' section")

        published_id = shop_snap.get("shopId")
        if not published_id or (
            shop_id != published_id
            and shop_id.lower() != str(published_id).lower()
        ):
            published_list = [published_id] if published_id else []
            raise SchedulerError(
                f"Shop '{shop_id}' is not published in current snapshot. "
                f"Published shop ID(s): {published_list}."
            )

        status = shop_snap.get("status", "unknown")
        is_open = bool(shop_snap.get("isOpen", False))
        owner_present = bool(shop_snap.get("ownerPresent", False))
        closed_message = shop_snap.get("closedMessage")
        owners = shop_snap.get("owners", [])
        currency = shop_snap.get("currency", 0)
        available_money = shop_snap.get("availableMoney")
        money_status = shop_snap.get("moneyStatus", "missing")
        error_message = shop_snap.get("errorMessage")

        raw_items = shop_snap.get("items")
        if raw_items is None or not isinstance(raw_items, list):
            items: list[dict[str, Any]] | str = "unknown"
            items_count: int | str = "unknown"
        else:
            items = []
            for it in raw_items:
                if not isinstance(it, dict):
                    continue
                iid = str(it.get("itemId", ""))
                iname = str(it.get("name", ""))

                if item_id is not None:
                    target_id = item_id.strip().lower()
                    if iid.lower() != target_id and not iid.lower().endswith(target_id):
                        continue
                if name is not None:
                    if name.strip().lower() not in iname.lower():
                        continue
                if is_seed is not None:
                    native_flag = it.get("isSeed")
                    is_it_seed = native_flag if isinstance(native_flag, bool) else (
                        it.get("category") == -74
                        or "seed" in iname.lower()
                        or "starter" in iname.lower()
                        or "spores" in iname.lower()
                        or "seed" in iid.lower()
                    )
                    if is_it_seed != is_seed:
                        continue

                item_dict: dict[str, Any] = {
                    "itemId": iid,
                    "name": iname,
                    "price": it.get("price", 0),
                    "stock": it.get("stock", 0),
                    "isInfiniteStock": bool(it.get("isInfiniteStock", False)),
                }
                if it.get("tradeItem"):
                    item_dict["tradeItem"] = it.get("tradeItem")
                if it.get("tradeItemCount") is not None:
                    item_dict["tradeItemCount"] = it.get("tradeItemCount")

                if detail:
                    if it.get("limitedStockMode"):
                        item_dict["limitedStockMode"] = it.get("limitedStockMode")
                    if it.get("actionsOnPurchase"):
                        item_dict["actionsOnPurchase"] = it.get("actionsOnPurchase")

                items.append(item_dict)

            items_count = len(items)

        location_id = shop_snap.get("locationId")
        interaction_tile = shop_snap.get("interactionTile")

        res: dict[str, Any] = {
            "shopId": published_id,
            "status": status,
            "isOpen": is_open,
            "ownerPresent": owner_present,
            "closedMessage": closed_message,
            "owners": owners,
            "currency": currency,
            "availableMoney": available_money,
            "moneyStatus": money_status,
            "itemsCount": items_count,
            "items": items,
            "worldRevision": client.world_revision or snap.get("worldRevision", 0),
            "saveId": client.save_id or snap.get("saveId", "unknown"),
            "gameSessionId": client.game_session_id or snap.get("gameSessionId", "unknown"),
        }
        if location_id:
            res["locationId"] = location_id
        if interaction_tile is not None:
            res["interactionTile"] = interaction_tile
        if error_message:
            res["errorMessage"] = error_message
        return res


    async def harvest_auto(
        self,
        max_tiles: int = 16,
        timeout_seconds: float = 30.0,
        task_id: str | None = None,
        command_id: str | None = None,
    ) -> dict[str, Any]:
        """Automatically harvests mature crops without manual coordinates.

        Policy rules:
        - max_tiles must be an integer between 1 and 64 (inclusive).
        - Rejects execution if another task is already active.
        - Reads mature crops from the snapshot farmWork.matureCrops section
          (already sorted Y-X by the mod).
        - If no mature crops: returns status 'no-work' without sending command.
        - Takes up to max_tiles and executes via the harvest-zone skill.
        - Returns execution outcome with targeted tiles and remaining count
          (an estimate; re-query after the task for ground truth).
        """
        if isinstance(max_tiles, bool) or not isinstance(max_tiles, int):
            raise PolicyViolationError(
                f"max_tiles must be an integer, got {type(max_tiles).__name__}"
            )
        if max_tiles < 1 or max_tiles > 64:
            raise PolicyViolationError(
                f"Invalid max_tiles: {max_tiles}. Value must be between 1 and 64 (inclusive)."
            )

        if self.has_active_task and self._active_task:
            raise PolicyViolationError(
                f"Cannot execute auto harvest: another task '{self._active_task.task_id}' "
                f"is currently active (status: {self._active_task.status}). "
                "Concurrent tasks are not permitted."
            )

        work_info = await self.query_farm_work()
        farm_work = work_info.get("farmWork", {})
        mature_crops = farm_work.get("matureCrops", [])
        total_mature = farm_work.get("matureCropCount", len(mature_crops))

        if not mature_crops:
            return {
                **build_unified_outcome(
                    outcome="completed",
                    goal_satisfied=True,
                    effects=[],
                    remaining={"matureCrops": 0},
                    reason_code="NO_WORK",
                    snapshot_revision=work_info.get("worldRevision"),
                ),
                "status": "no-work",
                "message": "No mature crops ready for harvest on the farm. "
                "Companion remains idle.",
                "targetTiles": [],
                "targetCount": 0,
                "remainingMatureCount": 0,
                "terminalState": "none",
            }

        selected = mature_crops[:max_tiles]
        target_tiles = [{"x": c["x"], "y": c["y"]} for c in selected]
        target_count = len(target_tiles)
        remaining = max(0, total_mature - target_count)
        is_truncated = bool(farm_work.get("matureCropsTruncated", False))
        location_id = work_info.get("companion", {}).get("locationId", "Farm")

        async def dispatch(
            client: TransportClient,
            *,
            task_id: str,
            idempotency_key: str,
            expires_seconds: float,
        ) -> str:
            harvest_kwargs: dict[str, Any] = {
                "location_id": location_id,
                "tiles": target_tiles,
                "task_id": task_id,
                "idempotency_key": idempotency_key,
                "expires_seconds": expires_seconds,
            }
            if command_id is not None:
                harvest_kwargs["command_id"] = command_id
            return await client.execute_harvest_zone(**harvest_kwargs)

        exec_res = await self.execute_skill(
            skill_id="harvest-zone",
            task_prefix="task-harvest-",
            dispatch=dispatch,
            location_id=location_id,
            tiles=target_tiles,
            timeout_seconds=timeout_seconds,
            task_id=task_id,
            requested_command_id=command_id,
        )

        terminal_state = exec_res["terminalState"]
        if terminal_state == "succeeded":
            outcome_name, goal_satisfied, reason_code = "completed", remaining <= 0, "OK"
        elif terminal_state in {"running", "unknown", None}:
            outcome_name, goal_satisfied, reason_code = "unknown", False, "IN_PROGRESS"
        else:
            completed_now = exec_res.get("completedCount", 0) or 0
            outcome_name = "partial" if completed_now > 0 else "unknown"
            goal_satisfied, reason_code = False, "HARVEST_INCOMPLETE"

        return {
            **build_unified_outcome(
                outcome=outcome_name,
                goal_satisfied=goal_satisfied,
                effects=exec_res.get("effects", []),
                remaining={"matureCrops": remaining},
                reason_code=reason_code,
                snapshot_revision=work_info.get("worldRevision"),
            ),
            "status": "executed",
            "terminalState": terminal_state,
            "completedCount": exec_res["completedCount"],
            "skippedCount": exec_res["skippedCount"],
            "failedCount": exec_res["failedCount"],
            "targetTiles": target_tiles,
            "targetCount": target_count,
            "remainingMatureCount": remaining,
            "isTruncated": is_truncated,
            "effects": exec_res.get("effects", []),
            "details": exec_res.get("details"),
            "error": exec_res.get("error"),
        }

    async def deposit_to_chest(
        self,
        chest_x: int,
        chest_y: int,
        item_ids: list[str] | None = None,
        timeout_seconds: float = 30.0,
        task_id: str | None = None,
        command_id: str | None = None,
    ) -> dict[str, Any]:
        """Deposits companion inventory items into a farm chest via deposit-chest.

        Policy rules:
        - chest_x/chest_y must be non-negative integers (booleans rejected).
        - item_ids, when provided, must be a list of at most 36 non-empty
          strings; omitted means all non-tool items. Item ids are sorted
          before dispatch for a canonical wire shape.
        - single active task concurrency check.
        """
        self._validate_chest_coords(chest_x, chest_y)
        validated_item_ids = self._validate_item_ids(item_ids)

        async def dispatch(
            client: TransportClient,
            *,
            task_id: str,
            idempotency_key: str,
            expires_seconds: float,
        ) -> str:
            deposit_kwargs: dict[str, Any] = {
                "location_id": "Farm",
                "chest_x": chest_x,
                "chest_y": chest_y,
                "item_ids": validated_item_ids,
                "task_id": task_id,
                "idempotency_key": idempotency_key,
                "expires_seconds": expires_seconds,
            }
            if command_id is not None:
                deposit_kwargs["command_id"] = command_id
            return await client.execute_deposit_chest(**deposit_kwargs)

        return await self.execute_skill(
            skill_id="deposit-chest",
            task_prefix="task-deposit-",
            dispatch=dispatch,
            location_id="Farm",
            tiles=[{"x": chest_x, "y": chest_y}],
            timeout_seconds=timeout_seconds,
            task_id=task_id,
            requested_command_id=command_id,
        )

    async def organize_chest(
        self,
        chest_x: int,
        chest_y: int,
        timeout_seconds: float = 30.0,
        task_id: str | None = None,
        command_id: str | None = None,
    ) -> dict[str, Any]:
        """Merges same-item stacks inside a farm chest via organize-chest.

        Policy rules:
        - chest_x/chest_y must be non-negative integers (booleans rejected).
        - single active task concurrency check.
        """
        self._validate_chest_coords(chest_x, chest_y)

        async def dispatch(
            client: TransportClient,
            *,
            task_id: str,
            idempotency_key: str,
            expires_seconds: float,
        ) -> str:
            organize_kwargs: dict[str, Any] = {
                "location_id": "Farm",
                "chest_x": chest_x,
                "chest_y": chest_y,
                "task_id": task_id,
                "idempotency_key": idempotency_key,
                "expires_seconds": expires_seconds,
            }
            if command_id is not None:
                organize_kwargs["command_id"] = command_id
            return await client.execute_organize_chest(**organize_kwargs)

        return await self.execute_skill(
            skill_id="organize-chest",
            task_prefix="task-organize-",
            dispatch=dispatch,
            location_id="Farm",
            tiles=[{"x": chest_x, "y": chest_y}],
            timeout_seconds=timeout_seconds,
            task_id=task_id,
            requested_command_id=command_id,
        )

    async def withdraw_from_chest(
        self,
        chest_x: int,
        chest_y: int,
        items: list[dict[str, Any]] | None = None,
        item_id: str | None = None,
        count: int = 1,
        location_id: str = "Farm",
        timeout_seconds: float = 30.0,
        task_id: str | None = None,
        command_id: str | None = None,
    ) -> dict[str, Any]:
        """Withdraws items from a farm chest into companion backpack via withdraw-chest.

        Policy rules:
        - chest_x/chest_y must be non-negative integers (booleans rejected).
        - items or item_id must be provided with positive count.
        - single active task concurrency check.
        """
        self._validate_chest_coords(chest_x, chest_y)
        normalized_items = self._validate_withdraw_items(items=items, item_id=item_id, count=count)

        async def dispatch(
            client: TransportClient,
            *,
            task_id: str,
            idempotency_key: str,
            expires_seconds: float,
        ) -> str:
            withdraw_kwargs: dict[str, Any] = {
                "location_id": location_id,
                "chest_x": chest_x,
                "chest_y": chest_y,
                "items": normalized_items,
                "task_id": task_id,
                "idempotency_key": idempotency_key,
                "expires_seconds": expires_seconds,
            }
            if command_id is not None:
                withdraw_kwargs["command_id"] = command_id
            return await client.execute_withdraw_chest(**withdraw_kwargs)

        return await self.execute_skill(
            skill_id="withdraw-chest",
            task_prefix="task-withdraw-",
            dispatch=dispatch,
            location_id=location_id,
            tiles=[{"x": chest_x, "y": chest_y}],
            timeout_seconds=timeout_seconds,
            task_id=task_id,
            requested_command_id=command_id,
        )

    @staticmethod
    def _validate_withdraw_items(
        items: list[dict[str, Any]] | None = None,
        item_id: str | None = None,
        count: int = 1,
    ) -> list[dict[str, Any]]:
        target_items: list[dict[str, Any]] = []
        if items is not None:
            if not isinstance(items, list) or len(items) == 0:
                raise PolicyViolationError("items must be a non-empty list of {itemId, count} dictionaries")
            if len(items) > 36:
                raise PolicyViolationError(f"items count ({len(items)}) exceeds maximum allowed (36)")
            for entry in items:
                if not isinstance(entry, dict):
                    raise PolicyViolationError("Each item in items must be a dictionary with 'itemId' and 'count'")
                iid = entry.get("itemId") or entry.get("item_id")
                c = entry.get("count", 1)
                if not isinstance(iid, str) or not iid.strip():
                    raise PolicyViolationError(f"itemId must be a non-empty string, got {iid!r}")
                if isinstance(c, bool) or not isinstance(c, int) or c < 1:
                    raise PolicyViolationError(f"count must be an integer >= 1, got {c!r}")
                target_items.append({"itemId": iid.strip(), "count": c})
        elif item_id is not None:
            if not isinstance(item_id, str) or not item_id.strip():
                raise PolicyViolationError(f"item_id must be a non-empty string, got {item_id!r}")
            if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                raise PolicyViolationError(f"count must be an integer >= 1, got {count!r}")
            target_items.append({"itemId": item_id.strip(), "count": count})
        else:
            raise PolicyViolationError("Either items list or item_id must be provided for withdraw_from_chest")

        return target_items

    @staticmethod
    def _validate_chest_coords(chest_x: int, chest_y: int) -> None:
        for name, value in (("chest_x", chest_x), ("chest_y", chest_y)):
            if isinstance(value, bool) or not isinstance(value, int):
                raise PolicyViolationError(
                    f"{name} must be an integer, got {type(value).__name__}"
                )
            if value < 0:
                raise PolicyViolationError(
                    f"Invalid {name}: {value}. Chest coordinates must be non-negative."
                )

    @staticmethod
    def _validate_item_ids(item_ids: list[str] | None) -> list[str] | None:
        if item_ids is None:
            return None
        if not isinstance(item_ids, list):
            raise PolicyViolationError(
                f"item_ids must be a list of non-empty strings, got {type(item_ids).__name__}"
            )
        if len(item_ids) > 36:
            raise PolicyViolationError(
                f"item_ids count ({len(item_ids)}) exceeds maximum allowed (36)"
            )
        for item in item_ids:
            if not isinstance(item, str) or not item:
                raise PolicyViolationError(
                    f"item_ids entries must be non-empty strings, got {item!r}"
                )
        return sorted(item_ids)

    @staticmethod
    def _validate_ship_items(items: Any) -> list[dict[str, Any]]:
        if not isinstance(items, list) or len(items) == 0:
            raise PolicyViolationError("items must be a non-empty list of 1..36 items")
        if len(items) > 36:
            raise PolicyViolationError(
                f"items count ({len(items)}) exceeds maximum allowed (36)"
            )
        validated: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                raise PolicyViolationError(
                    f"item entry must be a dictionary, got {type(item).__name__}"
                )
            raw_id = item.get("itemId") if "itemId" in item else item.get("item_id")
            if isinstance(raw_id, bool) or not isinstance(raw_id, str):
                raise PolicyViolationError(
                    f"itemId must be a non-empty string, got {type(raw_id).__name__}"
                )
            cleaned_id = raw_id.strip()
            if not cleaned_id:
                raise PolicyViolationError("itemId cannot be empty")
            raw_count = item.get("count", 1)
            if isinstance(raw_count, bool) or not isinstance(raw_count, int):
                raise PolicyViolationError(
                    f"count must be an integer, got {type(raw_count).__name__}"
                )
            if raw_count < 1:
                raise PolicyViolationError(
                    f"count must be at least 1, got {raw_count}"
                )
            validated.append({"itemId": cleaned_id, "count": raw_count})
        return validated

    async def execute_ship_items(
        self,
        items: list[dict[str, Any]],
        location_id: str = "Farm",
        timeout_seconds: float = 30.0,
        task_id: str | None = None,
        command_id: str | None = None,
    ) -> dict[str, Any]:
        """Executes ship-items skill to deposit items into farm shipping bin.

        Policy rules:
        - items must be a list of 1..36 items with valid itemId and count >= 1.
        - single active task concurrency check.
        """
        validated_items = self._validate_ship_items(items)

        async def dispatch(
            client: TransportClient,
            *,
            task_id: str,
            idempotency_key: str,
            expires_seconds: float,
        ) -> str:
            ship_kwargs: dict[str, Any] = {
                "location_id": location_id,
                "items": validated_items,
                "task_id": task_id,
                "idempotency_key": idempotency_key,
                "expires_seconds": expires_seconds,
            }
            if command_id is not None:
                ship_kwargs["command_id"] = command_id
            return await client.execute_ship_items(**ship_kwargs)

        return await self.execute_skill(
            skill_id="ship-items",
            task_prefix="task-ship-",
            dispatch=dispatch,
            location_id=location_id,
            tiles=None,
            timeout_seconds=timeout_seconds,
            task_id=task_id,
            requested_command_id=command_id,
        )

    @staticmethod
    def _validate_purchase_items(items: Any) -> list[dict[str, Any]]:
        if not isinstance(items, list) or len(items) == 0:
            raise PolicyViolationError("items must be a non-empty list of 1..36 items")
        if len(items) > 36:
            raise PolicyViolationError(
                f"items count ({len(items)}) exceeds maximum allowed (36)"
            )
        validated: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                raise PolicyViolationError(
                    f"item entry must be a dictionary, got {type(item).__name__}"
                )
            raw_id = item.get("itemId") if "itemId" in item else item.get("item_id")
            if isinstance(raw_id, bool) or not isinstance(raw_id, str):
                raise PolicyViolationError(
                    f"itemId must be a non-empty string, got {type(raw_id).__name__}"
                )
            cleaned_id = raw_id.strip()
            if not cleaned_id:
                raise PolicyViolationError("itemId cannot be empty")
            raw_count = item.get("count", 1)
            if isinstance(raw_count, bool) or not isinstance(raw_count, int):
                raise PolicyViolationError(
                    f"count must be an integer, got {type(raw_count).__name__}"
                )
            if raw_count < 1:
                raise PolicyViolationError(
                    f"count must be at least 1, got {raw_count}"
                )
            validated.append({"itemId": cleaned_id, "count": raw_count})
        return validated

    async def _guard_free_mode_purchase(
        self,
        *,
        items: list[dict[str, Any]],
        budget_limit: int,
        shop_id: str,
        command_id: str | None,
    ) -> _FreePurchaseGuard:
        """Enforce the free-mode daily purchase budget on the execution layer.

        Every real purchase path (plan worker, model surface, direct scheduler
        call) funnels through ``execute_purchase_items``, so the daily limit is
        a program guarantee here instead of a prompt hint. Free mode reserves
        the native shop quote before dispatch (idempotent per ``command_id``),
        clamps the forwarded call allowance to the daily remaining budget, and
        refuses new spend while an older reservation is still unverified.
        Command mode passes through untouched with the caller's budget_limit.
        """
        client = await self.ensure_connected()
        save_id = str(client.save_id or "unknown-save")
        autonomy = self._autonomy_store()
        auto_state = autonomy.state(save_id)
        if auto_state.mode != "free":
            return _FreePurchaseGuard(None, None, command_id, budget_limit)
        if not command_id:
            command_id = f"purchase-{uuid.uuid4().hex[:16]}"
        if command_id in auto_state.settled_spend_commands:
            return _FreePurchaseGuard(autonomy, save_id, command_id, budget_limit, {
                "status": "replayed", "terminalState": "succeeded", "commandId": command_id,
                "totalCost": 0, "message": "该购买 commandId 已结算，未重复购买。",
            })
        # Before accepting a different purchase, reconcile every older
        # reservation. An unverified spend blocks further shopping.
        for pending_id in list(auto_state.spend_reservations):
            if pending_id == command_id:
                continue
            recovered = await self.reconcile_command(pending_id)
            recovered_payload = recovered if isinstance(recovered, dict) else {}
            recovered_terminal = recovered_payload.get("terminalState")
            recovered_details = (
                recovered_payload.get("details")
                if isinstance(recovered_payload.get("details"), dict)
                else {}
            )
            recovered_cost = recovered_details.get("totalCost")
            settleable = (
                recovered_terminal in {"succeeded", "failed", "cancelled"}
                and isinstance(recovered_cost, int)
                and recovered_cost >= 0
            )
            if not settleable:
                return _FreePurchaseGuard(autonomy, save_id, command_id, budget_limit, {
                    "status": "rejected", "terminalState": "unknown", "commandId": command_id,
                    "error": {
                        "code": "AUTONOMY_PENDING_RECONCILIATION",
                        "message": "存在未核对的购买命令，核对完成前不会继续购物。",
                    },
                })
        auto_state = autonomy.state(save_id)
        active_matches = bool(
            self._active_task and command_id == self._active_task.requested_command_id
        )
        if command_id in auto_state.spend_reservations and not active_matches:
            # A retry must first recover the original command; never blind-
            # dispatch a second purchase for the same id.
            recovered = await self.reconcile_command(command_id)
            recovered_payload = recovered if isinstance(recovered, dict) else {}
            recovered_terminal = recovered_payload.get("terminalState")
            recovered_details = (
                recovered_payload.get("details")
                if isinstance(recovered_payload.get("details"), dict)
                else {}
            )
            recovered_cost = recovered_details.get("totalCost")
            settleable = (
                recovered_terminal in {"succeeded", "failed", "cancelled"}
                and isinstance(recovered_cost, int)
                and recovered_cost >= 0
            )
            if not settleable:
                return _FreePurchaseGuard(autonomy, save_id, command_id, budget_limit, {
                    "status": "executing", "terminalState": "running", "inProgress": True,
                    "commandId": command_id,
                    "message": "原购买命令仍待核对，已保留预算；核对完成前不会重新购物。",
                })
            return _FreePurchaseGuard(autonomy, save_id, command_id, budget_limit, {
                "status": "executed", "terminalState": recovered_terminal,
                "commandId": command_id, "taskId": recovered_payload.get("taskId"),
                "totalCost": recovered_cost, "details": recovered_details,
                "error": recovered_payload.get("error"),
            })
        remaining = max(
            0,
            (auto_state.budget_limit or 0)
            - auto_state.daily_spend
            - sum(auto_state.spend_reservations.values()),
        )
        effective_budget = min(budget_limit, remaining)
        if effective_budget <= 0:
            return _FreePurchaseGuard(autonomy, save_id, command_id, effective_budget, {
                "status": "rejected", "terminalState": "rejected", "commandId": command_id,
                "error": {
                    "code": "AUTONOMY_BUDGET_EXHAUSTED",
                    "message": "自由模式每日购买预算不足。",
                },
            })
        # Quote from the native shop snapshot. Never infer a price.
        shop = await self.query_shop(shop_id=shop_id, detail=True)
        native_items = {
            str(item.get("itemId")): item
            for item in (shop.get("items") or [])
            if isinstance(item, dict)
        }
        quote = 0
        for requested in items:
            native = native_items.get(str(requested.get("itemId")))
            price = native.get("price") if native else None
            count = requested.get("count")
            if not isinstance(price, int) or price < 0 or not isinstance(count, int):
                raise SchedulerError("无法取得原生报价，已阻止自由模式购买。")
            quote += price * count
        # reserve_spend applies the shared daily remaining formula again under
        # its lock; a quote above the clamped allowance rejects the purchase.
        try:
            autonomy.reserve_spend(save_id, command_id, quote, limit=effective_budget)
        except ValueError:
            return _FreePurchaseGuard(autonomy, save_id, command_id, effective_budget, {
                "status": "rejected", "terminalState": "rejected", "commandId": command_id,
                "error": {
                    "code": "AUTONOMY_BUDGET_EXHAUSTED",
                    "message": "自由模式每日购买预算不足。",
                },
            })
        return _FreePurchaseGuard(autonomy, save_id, command_id, effective_budget)

    async def execute_purchase_items(
        self,
        items: list[dict[str, Any]],
        budget_limit: int,
        shop_id: str = "SeedShop",
        location_id: str | None = None,
        timeout_seconds: float = 30.0,
        task_id: str | None = None,
        command_id: str | None = None,
    ) -> dict[str, Any]:
        """Executes purchase-items skill to buy items from a shop counter.

        Policy rules:
        - items must be a list of 1..36 items with valid itemId and count >= 1.
        - budget_limit must be a positive integer.
        - shop_id must be non-empty string (defaults to "SeedShop").
        - single active task concurrency check.
        - free mode: the daily purchase budget is enforced here on the shared
          execution path — the native quote is reserved before dispatch, the
          forwarded allowance is clamped to the daily remaining budget, and the
          reservation is settled exactly once from the confirmed result.
        """
        validated_items = self._validate_purchase_items(items)
        if command_id and task_id is None and self._active_task and self._active_task.requested_command_id == command_id:
            task_id = self._active_task.task_id
        if isinstance(budget_limit, bool) or not isinstance(budget_limit, int) or budget_limit <= 0:
            raise PolicyViolationError("budget_limit must be a positive integer")

        target_shop_id = (shop_id or "SeedShop").strip()
        if not target_shop_id:
            raise PolicyViolationError("shop_id cannot be empty")
        target_location_id = (
            location_id.strip() if location_id and location_id.strip() else target_shop_id
        )

        guard = await self._guard_free_mode_purchase(
            items=validated_items,
            budget_limit=budget_limit,
            shop_id=target_shop_id,
            command_id=command_id,
        )
        if guard.early_response is not None:
            return guard.early_response
        effective_budget = guard.effective_budget
        if guard.command_id:
            command_id = guard.command_id

        async def dispatch(
            client: TransportClient,
            *,
            task_id: str,
            idempotency_key: str,
            expires_seconds: float,
        ) -> str:
            purchase_kwargs: dict[str, Any] = {
                "shop_id": target_shop_id,
                "location_id": target_location_id,
                "items": validated_items,
                "budget_limit": effective_budget,
                "task_id": task_id,
                "idempotency_key": idempotency_key,
                "expires_seconds": expires_seconds,
            }
            if command_id is not None:
                purchase_kwargs["command_id"] = command_id
            return await client.execute_purchase_items(**purchase_kwargs)

        result = await self.execute_skill(
            skill_id="purchase-items",
            task_prefix="task-purchase-",
            dispatch=dispatch,
            location_id=target_location_id,
            tiles=None,
            timeout_seconds=timeout_seconds,
            task_id=task_id,
            requested_command_id=command_id,
        )
        if guard.autonomy is not None and guard.save_id and command_id:
            self._settle_free_mode_purchase(guard.save_id, command_id, result)
        return result

    async def execute_navigate_to(
        self,
        location_id: str,
        tile: dict[str, Any] | None = None,
        landmark: str | None = None,
        timeout_seconds: float = 60.0,
        task_id: str | None = None,
        command_id: str | None = None,
    ) -> dict[str, Any]:
        """Executes navigate-to skill to move companion across maps to a destination tile.

        If tile is omitted, resolves known native landmarks (e.g. 'entrance', 'shipping_bin', 'default')
        from COMMON_LOCATION_LANDMARKS.
        """
        if not isinstance(location_id, str) or not location_id.strip():
            raise SchedulerError("location_id must be a non-empty string.")

        clean_loc = location_id.strip()

        # Resolve landmark dynamically from active game snapshot if tile is not specified
        if tile is None:
            resolved_coord = None
            client = await self.ensure_connected()
            await self._refresh_snapshot(client)
            snap = self._latest_snapshot_data or {}
            payload = snap.get("payload", {})
            shop_data = payload.get("shop")
            if isinstance(shop_data, dict):
                shop_loc = shop_data.get("locationId") or "SeedShop"
                interaction_tile = shop_data.get("interactionTile")
                if interaction_tile and isinstance(interaction_tile, dict):
                    clean_landmark = (landmark or "").strip().lower()
                    if (
                        clean_loc.lower() == str(shop_loc).lower()
                        or clean_landmark in ("counter", "shop", "seedshop", "store")
                    ):
                        resolved_coord = {
                            "x": int(interaction_tile["x"]),
                            "y": int(interaction_tile["y"]),
                        }

            if resolved_coord is not None:
                tile = resolved_coord
            else:
                landmark_hint = f" (landmark='{landmark}')" if landmark else ""
                raise SchedulerError(
                    f"No destination tile provided for '{clean_loc}'{landmark_hint} and no native dynamic "
                    f"interaction data available in current game snapshot. "
                    f"Please provide 'tile' with non-negative 'x' and 'y' coordinates; "
                    f"static hardcoded landmark coordinates are unsupported."
                )

        if not isinstance(tile, dict):
            raise SchedulerError("tile must be a dictionary with 'x' and 'y' coordinates.")

        x = tile.get("x")
        y = tile.get("y")
        if (
            x is None
            or y is None
            or isinstance(x, bool)
            or isinstance(y, bool)
            or not isinstance(x, int)
            or not isinstance(y, int)
            or x < 0
            or y < 0
        ):
            raise SchedulerError(
                f"Invalid coordinate in tile {tile}: 'x' and 'y' must be non-negative integers."
            )

        validated_tile = {"x": x, "y": y}


        async def dispatch(
            client: TransportClient,
            *,
            task_id: str,
            idempotency_key: str,
            expires_seconds: float,
        ) -> str:
            nav_kwargs: dict[str, Any] = {
                "location_id": clean_loc,
                "tile": validated_tile,
                "task_id": task_id,
                "idempotency_key": idempotency_key,
                "expires_seconds": expires_seconds,
            }
            if command_id is not None:
                nav_kwargs["command_id"] = command_id
            return await client.execute_navigate_to(**nav_kwargs)

        return await self.execute_skill(
            skill_id="navigate-to",
            task_prefix="task-nav-",
            dispatch=dispatch,
            location_id=clean_loc,
            tiles=[validated_tile],
            timeout_seconds=timeout_seconds,
            task_id=task_id,
            requested_command_id=command_id,
        )

    async def execute_hoe_tiles(
        self,
        tiles: list[dict[str, Any]],
        location_id: str = "Farm",
        timeout_seconds: float = 30.0,
        task_id: str | None = None,
        command_id: str | None = None,
    ) -> dict[str, Any]:
        """Executes hoe-tiles skill on validated dirt tiles.

        Validates 1..64 coordinates, non-negative integers (no booleans), deduplicates,
        and dispatches via single-active-task scheduler.
        """
        validated_tiles = validate_action_tiles(tiles, max_tiles=64)

        async def dispatch(
            client: TransportClient,
            *,
            task_id: str,
            idempotency_key: str,
            expires_seconds: float,
        ) -> str:
            hoe_kwargs: dict[str, Any] = {
                "location_id": location_id,
                "tiles": validated_tiles,
                "task_id": task_id,
                "idempotency_key": idempotency_key,
                "expires_seconds": expires_seconds,
            }
            if command_id is not None:
                hoe_kwargs["command_id"] = command_id
            return await client.execute_hoe_tiles(**hoe_kwargs)

        return await self.execute_skill(
            skill_id="hoe-tiles",
            task_prefix="task-hoe-",
            dispatch=dispatch,
            location_id=location_id,
            tiles=validated_tiles,
            timeout_seconds=timeout_seconds,
            task_id=task_id,
            requested_command_id=command_id,
        )

    async def execute_plant_seeds(
        self,
        seed_item_id: str,
        tiles: list[dict[str, Any]],
        location_id: str = "Farm",
        timeout_seconds: float = 30.0,
        task_id: str | None = None,
        command_id: str | None = None,
    ) -> dict[str, Any]:
        """Executes plant-seeds skill on validated hoed empty tiles.

        Validates seed_item_id, 1..64 coordinates, non-negative integers (no booleans),
        deduplicates, and dispatches via single-active-task scheduler.
        """
        clean_seed_id = validate_seed_item_id(seed_item_id)
        validated_tiles = validate_action_tiles(tiles, max_tiles=64)

        async def dispatch(
            client: TransportClient,
            *,
            task_id: str,
            idempotency_key: str,
            expires_seconds: float,
        ) -> str:
            plant_kwargs: dict[str, Any] = {
                "location_id": location_id,
                "tiles": validated_tiles,
                "seed_item_id": clean_seed_id,
                "task_id": task_id,
                "idempotency_key": idempotency_key,
                "expires_seconds": expires_seconds,
            }
            if command_id is not None:
                plant_kwargs["command_id"] = command_id
            return await client.execute_plant_seeds(**plant_kwargs)

        return await self.execute_skill(
            skill_id="plant-seeds",
            task_prefix="task-plant-",
            dispatch=dispatch,
            location_id=location_id,
            tiles=validated_tiles,
            timeout_seconds=timeout_seconds,
            task_id=task_id,
            requested_command_id=command_id,
        )

    # ------------------------------------------------------------------
    # Explicit native agricultural / husbandry actions.
    # Every method dispatches through the single-active-task scheduler with the
    # same allow-listed skill contract; nothing is simulated client-side.
    # ------------------------------------------------------------------

    async def _execute_native_action(
        self,
        skill_id: str,
        parameters: dict[str, Any],
        *,
        tiles: list[dict[str, int]],
        timeout_seconds: float = 30.0,
        task_id: str | None = None,
        command_id: str | None = None,
    ) -> dict[str, Any]:
        async def dispatch(
            client: TransportClient,
            *,
            task_id: str,
            idempotency_key: str,
            expires_seconds: float,
        ) -> str:
            kwargs: dict[str, Any] = {
                "skill_id": skill_id,
                "parameters": parameters,
                "task_id": task_id,
                "idempotency_key": idempotency_key,
                "expires_seconds": expires_seconds,
            }
            if command_id is not None:
                kwargs["command_id"] = command_id
            return await client.execute_native_action(**kwargs)

        return await self.execute_skill(
            skill_id=skill_id,
            task_prefix=NATIVE_ACTION_TASK_PREFIXES.get(skill_id, "task-native-"),
            dispatch=dispatch,
            location_id=str(parameters.get("locationId", "Farm")),
            tiles=tiles,
            timeout_seconds=timeout_seconds,
            task_id=task_id,
            requested_command_id=command_id,
        )

    def _snapshot_farming_refill_tiles(self, max_tiles: int) -> list[dict[str, int]]:
        """Reads the companion's own map refill tiles from the latest native snapshot.

        Never guesses: when the snapshot has no farming section the caller gets an
        actionable error instead of an invented coordinate.
        """
        snap = self._latest_snapshot_data or {}
        payload = snap.get("payload", {}) if isinstance(snap, dict) else {}
        farming = payload.get("farming") if isinstance(payload, dict) else None
        if not isinstance(farming, dict):
            raise SchedulerError(
                "World snapshot does not include a 'farming' section. The running game "
                "build is too old; upgrade StardewAI.Companion.Mod to a build that "
                "publishes farming observation."
            )
        raw = farming.get("refillWaterTiles")
        if not isinstance(raw, list) or not raw:
            raise SchedulerError(
                "No native watering-can refill tile is known near the companion on this map."
            )
        tiles = [{"x": int(t["x"]), "y": int(t["y"])} for t in raw if isinstance(t, dict)]
        if not tiles:
            raise SchedulerError("No usable refill tile in the latest snapshot.")
        return tiles[: max(1, max_tiles)]

    async def query_farming_helpers(self, location_id: str = "Farm") -> dict[str, Any]:
        """Projects the farming observation group: refill tiles, ground items, fertilized tiles."""
        if not isinstance(location_id, str) or not location_id.strip():
            raise PolicyViolationError("location_id must be a non-empty string")
        client = await self.ensure_connected()
        await self._refresh_snapshot(client)
        snap = self._latest_snapshot_data or {}
        payload = snap.get("payload", {})
        farming = payload.get("farming") if isinstance(payload, dict) else None
        if not isinstance(farming, dict):
            raise SchedulerError(
                "World snapshot does not include a 'farming' section. The running game "
                "build is too old; upgrade StardewAI.Companion.Mod."
            )
        return {
            "location": farming.get("location", location_id),
            "refillWaterTiles": farming.get("refillWaterTiles", []),
            "groundItems": farming.get("groundItems", []),
            "groundItemsTruncated": bool(farming.get("groundItemsTruncated", False)),
            "fertilizedTiles": farming.get("fertilizedTiles", []),
            # Choppable wild trees / giant stumps / hollow logs near the companion.
            "choppableTrees": farming.get("choppableTrees", []),
            "choppableTreesTruncated": bool(farming.get("choppableTreesTruncated", False)),
            # Tools the companion really carries (native inventory). Empty means the
            # player has not provided that tool yet; nothing is granted by the Mod.
            "companionTools": [
                str(t) for t in (farming.get("companionTools") or []) if isinstance(t, str) and t
            ],
            "worldRevision": client.world_revision or snap.get("worldRevision", 0),
            "saveId": client.save_id or snap.get("saveId", "unknown"),
        }

    async def query_machines(self, location_id: str = "Farm") -> dict[str, Any]:
        """Projects the machine observation group (idle / processing / ready output)."""
        if not isinstance(location_id, str) or not location_id.strip():
            raise PolicyViolationError("location_id must be a non-empty string")
        client = await self.ensure_connected()
        await self._refresh_snapshot(client)
        snap = self._latest_snapshot_data or {}
        payload = snap.get("payload", {})
        machines = payload.get("machines") if isinstance(payload, dict) else None
        if not isinstance(machines, dict):
            raise SchedulerError(
                "World snapshot does not include a 'machines' section. The running game "
                "build is too old; upgrade StardewAI.Companion.Mod."
            )
        items = machines.get("items") or []
        return {
            "machines": items,
            "count": len(items),
            "readyCount": sum(1 for m in items if isinstance(m, dict) and m.get("isReady")),
            "truncated": bool(machines.get("truncated", False)),
            "worldRevision": client.world_revision or snap.get("worldRevision", 0),
            "saveId": client.save_id or snap.get("saveId", "unknown"),
        }

    async def query_livestock(self) -> dict[str, Any]:
        """Projects the livestock observation group (buildings, feed, animals, produce)."""
        client = await self.ensure_connected()
        await self._refresh_snapshot(client)
        snap = self._latest_snapshot_data or {}
        payload = snap.get("payload", {})
        livestock = payload.get("livestock") if isinstance(payload, dict) else None
        if not isinstance(livestock, dict):
            raise SchedulerError(
                "World snapshot does not include a 'livestock' section. The running game "
                "build is too old; upgrade StardewAI.Companion.Mod."
            )
        buildings = livestock.get("buildings") or []
        roaming = livestock.get("roamingAnimals") or []
        return {
            "buildings": buildings,
            "roamingAnimals": roaming,
            "buildingCount": len(buildings),
            "animalCount": sum(
                len(b.get("animals") or []) for b in buildings if isinstance(b, dict)
            ) + len(roaming),
            "buildingsTruncated": bool(livestock.get("buildingsTruncated", False)),
            "animalsTruncated": bool(livestock.get("animalsTruncated", False)),
            "worldRevision": client.world_revision or snap.get("worldRevision", 0),
            "saveId": client.save_id or snap.get("saveId", "unknown"),
        }

    async def refill_watering_can(
        self,
        tiles: list[dict[str, Any]] | None = None,
        location_id: str = "Farm",
        max_tiles: int = 4,
        timeout_seconds: float = 30.0,
        task_id: str | None = None,
        command_id: str | None = None,
    ) -> dict[str, Any]:
        """Refills the companion's watering can at native refill tiles.

        With no explicit tiles, the native snapshot's refillWaterTiles for the
        companion's own map are used (never an invented coordinate).
        """
        if not isinstance(location_id, str) or not location_id.strip():
            raise PolicyViolationError("location_id must be a non-empty string")
        if isinstance(max_tiles, bool) or not isinstance(max_tiles, int) or max_tiles < 1:
            raise PolicyViolationError("max_tiles must be a positive integer")

        if tiles:
            validated = validate_action_tiles(tiles, max_tiles=8)
        else:
            client = await self.ensure_connected()
            await self._refresh_snapshot(client)
            # An already-full can is a satisfied goal, not work: answer NO_WORK
            # without dispatching, instead of pathing to refill tiles just to be
            # told already-full (or worse, unreachable) per target.
            snap = self._latest_snapshot_data or {}
            payload = snap.get("payload", {}) if isinstance(snap, dict) else {}
            companion = payload.get("companion") if isinstance(payload, dict) else None
            if isinstance(companion, dict):
                level = companion.get("waterCanLevel")
                max_level = companion.get("maxWaterCanLevel")
                if (
                    isinstance(level, int)
                    and isinstance(max_level, int)
                    and max_level > 0
                    and level >= max_level
                ):
                    return {
                        **build_unified_outcome(
                            outcome="completed",
                            goal_satisfied=True,
                            effects=[],
                            remaining={"refillTiles": 0},
                            reason_code="NO_WORK",
                            snapshot_revision=self.latest_world_revision,
                        ),
                        "status": "no-work",
                        "message": "Watering can is already full; no refill needed.",
                        "terminalState": "none",
                    }
            validated = self._snapshot_farming_refill_tiles(max_tiles)

        parameters = {"locationId": location_id, "tiles": validated}
        return await self._execute_native_action(
            "refill-watering-can",
            parameters,
            tiles=validated,
            timeout_seconds=timeout_seconds,
            task_id=task_id,
            command_id=command_id,
        )

    async def apply_fertilizer(
        self,
        tiles: list[dict[str, Any]],
        fertilizer_item_id: str,
        location_id: str = "Farm",
        timeout_seconds: float = 30.0,
        task_id: str | None = None,
        command_id: str | None = None,
    ) -> dict[str, Any]:
        """Applies one explicit fertilizer item to explicit tilled tiles."""
        validated = validate_action_tiles(tiles, max_tiles=64)
        if not isinstance(fertilizer_item_id, str) or not fertilizer_item_id.strip():
            raise PolicyViolationError("fertilizer_item_id must be a non-empty string")
        parameters = {
            "locationId": location_id,
            "tiles": validated,
            "fertilizerItemId": fertilizer_item_id.strip(),
        }
        return await self._execute_native_action(
            "apply-fertilizer",
            parameters,
            tiles=validated,
            timeout_seconds=timeout_seconds,
            task_id=task_id,
            command_id=command_id,
        )

    async def clear_debris(
        self,
        tiles: list[dict[str, Any]],
        location_id: str = "Farm",
        timeout_seconds: float = 30.0,
        task_id: str | None = None,
        command_id: str | None = None,
    ) -> dict[str, Any]:
        """Clears explicitly selected native weeds, stones or twigs.

        The native tool is chosen by the game's own rule (Hoe/Axe for weeds, Pickaxe
        for stones, Axe/Pickaxe for twigs); other objects return an unsupported reason
        and a missing tool returns an actionable ``missing-tool:<Tool>`` precondition.
        """
        validated = validate_action_tiles(tiles, max_tiles=64)
        parameters = {"locationId": location_id, "tiles": validated}
        return await self._execute_native_action(
            "clear-debris",
            parameters,
            tiles=validated,
            timeout_seconds=timeout_seconds,
            task_id=task_id,
            command_id=command_id,
        )

    async def pickup_items(
        self,
        tiles: list[dict[str, Any]],
        location_id: str = "Farm",
        timeout_seconds: float = 30.0,
        task_id: str | None = None,
        command_id: str | None = None,
    ) -> dict[str, Any]:
        """Picks up dropped debris / spawned items on explicitly selected tiles."""
        validated = validate_action_tiles(tiles, max_tiles=64)
        parameters = {"locationId": location_id, "tiles": validated}
        return await self._execute_native_action(
            "pickup-items",
            parameters,
            tiles=validated,
            timeout_seconds=timeout_seconds,
            task_id=task_id,
            command_id=command_id,
        )

    async def chop_tree(
        self,
        tiles: list[dict[str, Any]],
        location_id: str = "Farm",
        timeout_seconds: float = 30.0,
        task_id: str | None = None,
        command_id: str | None = None,
    ) -> dict[str, Any]:
        """Chops explicitly selected wild trees, giant stumps or hollow logs.

        Fruit trees are protected (``protected-tree``); tiles without a choppable
        target return ``no-tree`` and a missing Axe returns ``missing-tool:Axe``.
        """
        validated = validate_action_tiles(tiles, max_tiles=64)
        parameters = {"locationId": location_id, "tiles": validated}
        return await self._execute_native_action(
            "chop-tree",
            parameters,
            tiles=validated,
            timeout_seconds=timeout_seconds,
            task_id=task_id,
            command_id=command_id,
        )

    async def insert_machine(
        self,
        tile: dict[str, Any],
        item_id: str,
        item_count: int = 1,
        location_id: str = "Farm",
        timeout_seconds: float = 30.0,
        task_id: str | None = None,
        command_id: str | None = None,
    ) -> dict[str, Any]:
        """Inserts an explicit companion item stack into an explicit machine."""
        validated = validate_action_tiles([tile], max_tiles=1)
        if not isinstance(item_id, str) or not item_id.strip():
            raise PolicyViolationError("item_id must be a non-empty string")
        if isinstance(item_count, bool) or not isinstance(item_count, int) or not 1 <= item_count <= 36:
            raise PolicyViolationError("item_count must be an integer between 1 and 36")
        parameters = {
            "locationId": location_id,
            "tile": validated[0],
            "itemId": item_id.strip(),
            "itemCount": item_count,
        }
        return await self._execute_native_action(
            "insert-machine",
            parameters,
            tiles=validated,
            timeout_seconds=timeout_seconds,
            task_id=task_id,
            command_id=command_id,
        )

    async def collect_machine(
        self,
        tiles: list[dict[str, Any]],
        location_id: str = "Farm",
        timeout_seconds: float = 30.0,
        task_id: str | None = None,
        command_id: str | None = None,
    ) -> dict[str, Any]:
        """Collects ready machine output from explicit machine tiles."""
        validated = validate_action_tiles(tiles, max_tiles=64)
        parameters = {"locationId": location_id, "tiles": validated}
        return await self._execute_native_action(
            "collect-machine",
            parameters,
            tiles=validated,
            timeout_seconds=timeout_seconds,
            task_id=task_id,
            command_id=command_id,
        )

    async def pet_animal(
        self,
        animal_name: str,
        tile: dict[str, Any],
        location_id: str = "Farm",
        timeout_seconds: float = 30.0,
        task_id: str | None = None,
        command_id: str | None = None,
    ) -> dict[str, Any]:
        """Pets one named animal at its last observed tile."""
        validated = validate_action_tiles([tile], max_tiles=1)
        if not isinstance(animal_name, str) or not animal_name.strip():
            raise PolicyViolationError("animal_name must be a non-empty string")
        parameters = {
            "locationId": location_id,
            "tiles": validated,
            "animalName": animal_name.strip(),
        }
        return await self._execute_native_action(
            "pet-animal",
            parameters,
            tiles=validated,
            timeout_seconds=timeout_seconds,
            task_id=task_id,
            command_id=command_id,
        )

    async def collect_animal_produce(
        self,
        animal_name: str,
        tile: dict[str, Any],
        location_id: str = "Farm",
        timeout_seconds: float = 30.0,
        task_id: str | None = None,
        command_id: str | None = None,
    ) -> dict[str, Any]:
        """Collects one animal's produce through its applicable native path."""
        validated = validate_action_tiles([tile], max_tiles=1)
        if not isinstance(animal_name, str) or not animal_name.strip():
            raise PolicyViolationError("animal_name must be a non-empty string")
        parameters = {
            "locationId": location_id,
            "tiles": validated,
            "animalName": animal_name.strip(),
        }
        return await self._execute_native_action(
            "collect-animal-produce",
            parameters,
            tiles=validated,
            timeout_seconds=timeout_seconds,
            task_id=task_id,
            command_id=command_id,
        )

    async def feed_animals(
        self,
        building_name: str,
        timeout_seconds: float = 30.0,
        task_id: str | None = None,
        command_id: str | None = None,
    ) -> dict[str, Any]:
        """Feeds every animal inside one animal building through the native feed path."""
        if not isinstance(building_name, str) or not building_name.strip():
            raise PolicyViolationError("building_name must be a non-empty string")
        parameters = {"locationId": building_name.strip(), "buildingName": building_name.strip()}
        return await self._execute_native_action(
            "feed-animals",
            parameters,
            tiles=[],
            timeout_seconds=timeout_seconds,
            task_id=task_id,
            command_id=command_id,
        )

    async def toggle_animal_door(
        self,
        tiles: list[dict[str, Any]],
        location_id: str = "Farm",
        timeout_seconds: float = 30.0,
        task_id: str | None = None,
        command_id: str | None = None,
    ) -> dict[str, Any]:
        """Opens/closes the animal door of explicit buildings/door tiles."""
        validated = validate_action_tiles(tiles, max_tiles=8)
        parameters = {"locationId": location_id, "tiles": validated}
        return await self._execute_native_action(
            "toggle-animal-door",
            parameters,
            tiles=validated,
            timeout_seconds=timeout_seconds,
            task_id=task_id,
            command_id=command_id,
        )

    async def execute_water_zone(
        self,
        center_x: int,
        center_y: int,
        radius: int = 0,
        include_empty_tiles: bool = False,
        timeout_seconds: float = 30.0,
        task_id: str | None = None,
        command_id: str | None = None,
    ) -> dict[str, Any]:
        """Executes a water-zone task through policy validation and tracking.

        - Validates radius (0..2) and coordinates.
        - Expands into tiles.
        - Dispatches via execute_tiles.
        """
        tiles = expand_water_zone(center_x, center_y, radius)
        # A water-zone call names an explicit area. Empty soil is therefore an
        # intentional target selection; auto watering remains crop-only by default.
        return await self.execute_tiles(
            tiles=tiles,
            location_id="Farm",
            timeout_seconds=timeout_seconds,
            task_id=task_id,
            command_id=command_id,
        )

    async def water_auto(
        self,
        max_tiles: int = 25,
        include_empty_tiles: bool = False,
        timeout_seconds: float = 30.0,
        task_id: str | None = None,
        command_id: str | None = None,
    ) -> dict[str, Any]:
        """Automatically waters unwatered tilled farm tiles without manual coordinates.

        Policy rules:
        - max_tiles must be an integer between 1 and 64 (inclusive).
        - Rejects execution if another task is already active.
        - Queries available farm work from snapshot.
        - If no unwatered tiles: returns status 'no-work' without sending command.
        - Takes up to max_tiles and executes via execute_tiles.
        - Returns execution outcome with targeted tiles and remaining count.
        """
        if isinstance(max_tiles, bool) or not isinstance(max_tiles, int):
            raise PolicyViolationError(
                f"max_tiles must be an integer, got {type(max_tiles).__name__}"
            )
        if max_tiles < 1 or max_tiles > 64:
            raise PolicyViolationError(
                f"Invalid max_tiles: {max_tiles}. Value must be between 1 and 64 (inclusive)."
            )

        if self.has_active_task and self._active_task:
            if task_id is not None and self._active_task.task_id == task_id:
                pass
            else:
                raise PolicyViolationError(
                    f"Cannot execute auto watering: another task '{self._active_task.task_id}' "
                    f"is currently active (status: {self._active_task.status}). "
                    "Concurrent tasks are not permitted."
                )

        work_info = await self.query_farm_work()
        farm_work = work_info.get("farmWork", {})
        if include_empty_tiles:
            unwatered_tiles = farm_work.get("tilledUnwateredTiles", [])
            total_unwatered = farm_work.get("tilledUnwateredCount", len(unwatered_tiles))
        else:
            # Crop-only is the safe default. Older mods lack the native crop list;
            # fail closed rather than silently watering empty prepared soil.
            native_crop_tiles = farm_work.get("cropUnwateredTiles")
            if not isinstance(native_crop_tiles, list):
                native_crop_tiles = farm_work.get("tilledUnwateredTiles", [])
            unwatered_tiles = native_crop_tiles
            crop_count = farm_work.get("cropUnwateredCount")
            total_unwatered = crop_count if isinstance(crop_count, int) else len(unwatered_tiles)

        if not unwatered_tiles:
            return {
                **build_unified_outcome(
                    outcome="completed",
                    goal_satisfied=True,
                    effects=[],
                    remaining={"unwateredTiles": 0},
                    reason_code="NO_WORK",
                    snapshot_revision=work_info.get("worldRevision"),
                ),
                "status": "no-work",
                "message": "No unwatered tilled tiles found on the farm. Companion remains idle.",
                "targetTiles": [],
                "targetCount": 0,
                "remainingUnwateredCount": 0,
                "terminalState": "none",
            }

        target_tiles = unwatered_tiles[:max_tiles]
        target_count = len(target_tiles)
        remaining = max(0, total_unwatered - target_count)
        is_truncated = bool(farm_work.get("isTruncated", False) or remaining > 0)
        location_id = work_info.get("companion", {}).get("locationId", "Farm")

        effective_timeout = max(timeout_seconds, target_count * 2.5 + 15.0)

        exec_res = await self.execute_tiles(
            tiles=target_tiles,
            location_id=location_id,
            timeout_seconds=effective_timeout,
            task_id=task_id,
            command_id=command_id,
        )

        terminal_state = exec_res.get("terminalState", "unknown")
        completed = exec_res.get("completedCount", 0)
        if terminal_state == "succeeded" and remaining <= 0:
            outcome_name, goal_satisfied, reason_code = "completed", True, "OK"
        elif terminal_state in {"running", "unknown", None}:
            outcome_name, goal_satisfied, reason_code = "unknown", False, "IN_PROGRESS"
        elif terminal_state == "succeeded":
            outcome_name, goal_satisfied, reason_code = "partial", False, "REMAINING_WORK"
        else:
            outcome_name = "partial" if (completed or 0) > 0 or terminal_state in {"failed", "rejected"} else "unknown"
            goal_satisfied, reason_code = False, "WATER_INCOMPLETE"

        return {
            **build_unified_outcome(
                outcome=outcome_name,
                goal_satisfied=goal_satisfied,
                effects=exec_res.get("effects", []),
                remaining={"unwateredTiles": remaining},
                reason_code=reason_code,
                snapshot_revision=work_info.get("worldRevision"),
            ),
            "status": exec_res.get("status", "executed"),
            "taskId": exec_res.get("taskId") or (self._active_task.task_id if self._active_task else task_id),
            "terminalState": terminal_state,
            "completedCount": completed,
            "skippedCount": exec_res.get("skippedCount", 0),
            "failedCount": exec_res.get("failedCount", 0),
            "targetTiles": target_tiles,
            "targetCount": target_count,
            "remainingUnwateredCount": remaining,
            "isTruncated": is_truncated,
            "effects": exec_res.get("effects", []),
            "details": exec_res.get("details"),
            "message": exec_res.get("message"),
            "error": exec_res.get("error"),
        }

    async def pause_task(self) -> dict[str, Any]:
        """Pauses the currently active task."""
        if not self.has_active_task or not self._active_task:
            raise NoActiveTaskError("No active task to pause. Companion is currently idle.")

        if self._active_task.status == "paused":
            return {
                "status": "paused",
                "taskId": self._active_task.task_id,
                "message": "Task is already paused.",
            }

        client = await self.ensure_connected()
        pause_cmd_id = f"cmd-pause-{uuid.uuid4().hex[:8]}"
        corr_id = self._active_task.command_id or self._active_task.task_id
        await client.pause_skill(
            command_id=pause_cmd_id,
            correlation_id=corr_id,
            reason="Client requested pause via MCP",
        )
        self._active_task.status = "paused"
        return {
            "status": "paused",
            "taskId": self._active_task.task_id,
            "message": f"Task '{self._active_task.task_id}' has been paused.",
        }

    async def resume_task(self) -> dict[str, Any]:
        """Resumes the currently paused task."""
        if not self.has_active_task or not self._active_task:
            raise NoActiveTaskError("No active task to resume. Companion is currently idle.")

        if self._active_task.status != "paused":
            st = self._active_task.status
            return {
                "status": st,
                "taskId": self._active_task.task_id,
                "message": f"Task '{self._active_task.task_id}' is not paused (status: {st}).",
            }

        client = await self.ensure_connected()
        resume_cmd_id = f"cmd-resume-{uuid.uuid4().hex[:8]}"
        corr_id = self._active_task.command_id or self._active_task.task_id
        await client.resume_skill(
            command_id=resume_cmd_id,
            correlation_id=corr_id,
        )
        self._active_task.status = "running"
        return {
            "status": "resumed",
            "taskId": self._active_task.task_id,
            "message": f"Task '{self._active_task.task_id}' has been resumed.",
        }

    async def cancel_task(self, reason: str = "User cancelled via MCP") -> dict[str, Any]:
        """Cancels the currently active task."""
        if not self.has_active_task or not self._active_task:
            raise NoActiveTaskError("No active task to cancel. Companion is currently idle.")

        client = await self.ensure_connected()
        cancel_cmd_id = f"cmd-cancel-{uuid.uuid4().hex[:8]}"
        corr_id = self._active_task.command_id or self._active_task.task_id
        await client.cancel_skill(
            command_id=cancel_cmd_id,
            correlation_id=corr_id,
            reason=reason,
        )
        self._active_task.status = "cancelling"
        return {
            "status": "cancelling",
            "taskId": self._active_task.task_id,
            "message": f"Cancellation requested for task '{self._active_task.task_id}'.",
        }

    async def plant_crop_workflow(
        self,
        crop_name_or_id: str,
        count: int = 1,
        target_tiles: list[dict[str, Any]] | None = None,
        auto_till: bool = True,
        auto_water: bool = True,
        withdraw_from_chest: bool = True,
        chest_tile: dict[str, int] | None = None,
        location_id: str = "Farm",
        water: bool | None = None,
        command_id: str | None = None,
    ) -> dict[str, Any]:
        """Thin composite planting workflow tool.

        Executes: locate seed (backpack or chest) -> identify tiles (tilled or tillable)
        -> hoe if needed -> plant seeds -> water planted tiles (if auto_water=True).
        Returns unified outcome and explicit pending decision if blocked.

        ``command_id`` is the stable plan command id. Each sub-skill gets a
        deterministic derived id (``<command_id>:withdraw``/``:hoe``/``:plant``/
        ``:water``) so a retried composite step maps to the same native commands.
        """

        def sub_command(suffix: str) -> str | None:
            return f"{command_id}:{suffix}" if command_id else None
        if water is not None:
            auto_water = water
        if not crop_name_or_id or not isinstance(crop_name_or_id, str):
            raise PolicyViolationError("crop_name_or_id must be a non-empty string.")
        if isinstance(count, bool) or not isinstance(count, int) or count < 1 or count > 64:
            raise PolicyViolationError(f"count must be an integer between 1 and 64, got {count}.")

        clean_target = crop_name_or_id.strip().lower()
        snapshot_revision: int | None = None

        def outcome_envelope(
            outcome: str, satisfied: bool, reason: str, remaining: Any = None
        ) -> dict[str, Any]:
            return build_unified_outcome(
                outcome=outcome,
                goal_satisfied=satisfied,
                effects=[],
                remaining=remaining if remaining is not None else {"requested": count},
                reason_code=reason,
                snapshot_revision=snapshot_revision,
            )

        def matches_seed(item: dict[str, Any]) -> bool:
            iid = str(item.get("itemId", "")).lower()
            name = str(item.get("name", "")).lower()
            disp = str(item.get("displayName", "")).lower()
            t = clean_target.replace("(o)", "").replace("seeds", "").replace("种子", "").strip()
            return (
                clean_target in iid
                or clean_target in name
                or clean_target in disp
                or (bool(t) and (t in name or t in disp or t in iid))
            )

        # Step 1: Check inventory
        inv_res = await self.query_inventory()
        inv_slots = inv_res.get("inventory", {}).get("slots", [])

        backpack_seed = None
        backpack_count = 0
        for slot in inv_slots:
            if matches_seed(slot):
                backpack_seed = slot
                backpack_count += int(slot.get("stack", 1))

        needed_from_chest = max(0, count - backpack_count)
        seed_id = str(backpack_seed.get("itemId", "")) if backpack_seed else ""
        withdrawn_info = None

        # Withdraw from chest if needed and allowed
        if withdraw_from_chest and needed_from_chest > 0:
            chests_res = await self.query_chests()
            all_chests = chests_res.get("chests", [])
            target_chest = None
            chest_matching_item = None

            if chest_tile:
                ctx, cty = chest_tile.get("x"), chest_tile.get("y")
                for c in all_chests:
                    t = c.get("tile", {})
                    if t.get("x") == ctx and t.get("y") == cty:
                        target_chest = c
                        break

            if not target_chest:
                for c in all_chests:
                    for it in c.get("contents", []):
                        if matches_seed(it):
                            target_chest = c
                            chest_matching_item = it
                            break
                    if target_chest:
                        break
            else:
                for it in target_chest.get("contents", []):
                    if matches_seed(it):
                        chest_matching_item = it
                        break

            if target_chest and chest_matching_item:
                cx = target_chest.get("tile", {}).get("x", 0)
                cy = target_chest.get("tile", {}).get("y", 0)
                seed_id = str(chest_matching_item.get("itemId", ""))
                avail_in_chest = int(chest_matching_item.get("stack", 1))
                withdraw_qty = min(needed_from_chest, avail_in_chest)

                with_res = await self.withdraw_from_chest(
                    chest_x=cx,
                    chest_y=cy,
                    item_id=seed_id,
                    count=withdraw_qty,
                    location_id=location_id,
                    command_id=sub_command("withdraw"),
                )

                # If withdraw is executing/running, return executing immediately; do not fake success or continue
                if with_res.get("status") == "executing" or with_res.get("terminalState") == "running":
                    return {
                        **outcome_envelope("unknown", False, "WITHDRAW_IN_PROGRESS"),
                        "status": "executing",
                        "terminalState": "running",
                        "taskId": with_res.get("taskId"),
                        "inProgress": True,
                        "crop": crop_name_or_id,
                        "seedId": seed_id,
                        "seedsPlanted": 0,
                        "tilesHoed": 0,
                        "tilesWatered": 0,
                        "targetTiles": [],
                        "details": with_res.get("details"),
                        "message": f"Withdrawing seeds from chest is in progress (taskId={with_res.get('taskId')}).",
                    }

                # Credit actual completed stack quantity from effects or completedCount
                actual_withdrawn = 0
                for eff in with_res.get("effects", []):
                    if isinstance(eff, dict) and eff.get("state") == "withdrawn":
                        eff_id = str(eff.get("itemId", ""))
                        if not seed_id or eff_id.lower() == seed_id.lower() or matches_seed(eff):
                            actual_withdrawn += int(eff.get("stack", 1))

                # Fallback if effects empty/missing but terminalState is succeeded
                if actual_withdrawn == 0 and with_res.get("terminalState") in ("succeeded", "partially-succeeded"):
                    comp = with_res.get("completedCount", 0)
                    if comp > 0:
                        actual_withdrawn = withdraw_qty

                withdrawn_info = {
                    "chestTile": {"x": cx, "y": cy},
                    "itemId": seed_id,
                    "count": actual_withdrawn,
                    "status": with_res.get("status"),
                    "terminalState": with_res.get("terminalState"),
                    "error": with_res.get("error"),
                }
                backpack_count += actual_withdrawn

        if backpack_count <= 0 or not seed_id:
            reason = (
                f"No seeds matching '{crop_name_or_id}' found in backpack or chests. "
                "Please store seeds in a farm chest or purchase seeds from Pierre's General Store."
            )
            if withdrawn_info and withdrawn_info.get("terminalState") in ("failed", "rejected"):
                reason = f"Failed to withdraw seeds from chest: {withdrawn_info.get('error') or 'chest action failed'}."
            return {
                **outcome_envelope("partial", False, "SEEDS_MISSING"),
                "status": "blocked",
                "crop": crop_name_or_id,
                "seedId": seed_id or "unknown",
                "seedsPlanted": 0,
                "tilesHoed": 0,
                "tilesWatered": 0,
                "targetTiles": [],
                "withdrawnFromChest": withdrawn_info,
                "pendingDecision": reason,
            }

        actual_plan_count = min(count, backpack_count)

        # Step 2: Determine target tiles and necessary hoeing based on native soil status
        tilled_set: set[tuple[int, int]] = set()
        tilled_empty: list[dict[str, int]] = []
        tillable: list[dict[str, int]] = []

        options = await self.query_planting_options(detail=True)
        snapshot_revision = options.get("worldRevision")
        candidate_tiles = options.get("candidateTiles", {})
        if isinstance(candidate_tiles, dict):
            tilled_empty = candidate_tiles.get("tilledEmptyTiles", [])
            tillable = candidate_tiles.get("tillableTiles", [])
            if not isinstance(tilled_empty, list):
                tilled_empty = []
            if not isinstance(tillable, list):
                tillable = []
            tilled_set = {
                (int(t["x"]), int(t["y"]))
                for t in tilled_empty
                if isinstance(t, dict) and "x" in t and "y" in t
            }

        # Prefer contiguous legal patches. Explicit model coordinates are never
        # reordered; only auto-selected candidates go through this grouping.
        search_bounds = options.get("searchBounds")
        anchor = search_bounds.get("center") if isinstance(search_bounds, dict) else None
        if not target_tiles:
            tilled_empty = order_candidates_for_contiguous_work(
                tilled_empty, anchor=anchor, preferred_group_size=actual_plan_count
            )
            tillable = order_candidates_for_contiguous_work(
                tillable, anchor=anchor, preferred_group_size=actual_plan_count
            )

        tiles_to_hoe: list[dict[str, int]] = []
        final_target_tiles: list[dict[str, int]] = []

        if target_tiles:
            for t in target_tiles[:actual_plan_count]:
                coord = {"x": int(t["x"]), "y": int(t["y"])}
                coord_tuple = (coord["x"], coord["y"])
                if coord_tuple in tilled_set:
                    final_target_tiles.append(coord)
                elif auto_till:
                    tiles_to_hoe.append(coord)
                    final_target_tiles.append(coord)
                else:
                    pass
        else:
            if len(tilled_empty) >= actual_plan_count:
                final_target_tiles = [{"x": int(t["x"]), "y": int(t["y"])} for t in tilled_empty[:actual_plan_count]]
            elif auto_till:
                final_target_tiles = [{"x": int(t["x"]), "y": int(t["y"])} for t in tilled_empty]
                needed_hoe = actual_plan_count - len(final_target_tiles)
                tiles_to_hoe = [{"x": int(t["x"]), "y": int(t["y"])} for t in tillable[:needed_hoe]]
                final_target_tiles.extend(tiles_to_hoe)
            else:
                final_target_tiles = [{"x": int(t["x"]), "y": int(t["y"])} for t in tilled_empty]

        if not final_target_tiles:
            return {
                **outcome_envelope("partial", False, "NO_TILES"),
                "status": "blocked",
                "crop": crop_name_or_id,
                "seedId": seed_id,
                "seedsPlanted": 0,
                "tilesHoed": 0,
                "tilesWatered": 0,
                "targetTiles": [],
                "pendingDecision": (
                    "No tilled or tillable tiles available near companion for planting. "
                    "Specify target_tiles or clear farm obstacles."
                ),
            }

        # Step 3: Hoe tiles if needed
        hoed_count = 0
        if tiles_to_hoe:
            hoe_res = await self.execute_hoe_tiles(
                tiles=tiles_to_hoe, location_id=location_id, command_id=sub_command("hoe")
            )
            if hoe_res.get("status") == "executing" or hoe_res.get("terminalState") == "running":
                return {
                    **outcome_envelope("unknown", False, "HOE_IN_PROGRESS"),
                    "status": "executing",
                    "terminalState": "running",
                    "taskId": hoe_res.get("taskId"),
                    "inProgress": True,
                    "crop": crop_name_or_id,
                    "seedId": seed_id,
                    "seedsPlanted": 0,
                    "tilesHoed": 0,
                    "tilesWatered": 0,
                    "targetTiles": final_target_tiles,
                    "details": hoe_res.get("details"),
                    "message": f"Hoeing tiles is in progress (taskId={hoe_res.get('taskId')}).",
                }

            hoed_count = hoe_res.get("completedCount", 0)
            if hoe_res.get("terminalState") != "succeeded" and hoed_count == 0:
                return {
                    **outcome_envelope("partial", False, "HOE_FAILED"),
                    "status": "partial" if (backpack_count > 0 and len(final_target_tiles) > len(tiles_to_hoe)) else "failed",
                    "crop": crop_name_or_id,
                    "seedId": seed_id,
                    "seedsPlanted": 0,
                    "tilesHoed": 0,
                    "tilesWatered": 0,
                    "targetTiles": [],
                    "pendingDecision": f"Failed hoeing tiles: {hoe_res.get('error')}. Companion may be missing a Hoe.",
                }

            # Filter final_target_tiles to exclude tiles that failed hoeing
            hoed_tiles = [
                e["tile"] for e in hoe_res.get("effects", [])
                if isinstance(e.get("tile"), dict) and (e.get("state") == "hoed" or e.get("tilled"))
            ]
            if hoed_tiles:
                actual_hoed_set = {(int(t["x"]), int(t["y"])) for t in hoed_tiles}
                final_target_tiles = [
                    t for t in final_target_tiles
                    if (t["x"], t["y"]) in tilled_set or (t["x"], t["y"]) in actual_hoed_set
                ]
            elif hoed_count < len(tiles_to_hoe):
                final_target_tiles = [
                    t for t in final_target_tiles if (t["x"], t["y"]) in tilled_set
                ] + tiles_to_hoe[:hoed_count]

        # Step 4: Plant seeds
        if not final_target_tiles:
            return {
                **outcome_envelope("partial", False, "NO_TILLED"),
                "status": "failed",
                "crop": crop_name_or_id,
                "seedId": seed_id,
                "seedsPlanted": 0,
                "tilesHoed": hoed_count,
                "tilesWatered": 0,
                "targetTiles": [],
                "pendingDecision": "No workable tilled tiles available for planting.",
            }

        plant_res = await self.execute_plant_seeds(
            seed_item_id=seed_id,
            tiles=final_target_tiles,
            location_id=location_id,
            command_id=sub_command("plant"),
        )
        if plant_res.get("status") == "executing" or plant_res.get("terminalState") == "running":
            return {
                **outcome_envelope("unknown", False, "PLANT_IN_PROGRESS"),
                "status": "executing",
                "terminalState": "running",
                "taskId": plant_res.get("taskId"),
                "inProgress": True,
                "crop": crop_name_or_id,
                "seedId": seed_id,
                "seedsPlanted": 0,
                "tilesHoed": hoed_count,
                "tilesWatered": 0,
                "targetTiles": final_target_tiles,
                "details": plant_res.get("details"),
                "message": f"Planting seeds is in progress (taskId={plant_res.get('taskId')}).",
            }

        real_planted_tiles: list[dict[str, int]] = []
        details_planted = plant_res.get("details", {}).get("plantedTiles") if isinstance(plant_res.get("details"), dict) else None
        if isinstance(details_planted, list) and len(details_planted) > 0:
            real_planted_tiles = [{"x": int(t["x"]), "y": int(t["y"])} for t in details_planted if isinstance(t, dict)]
        else:
            for eff in plant_res.get("effects", []):
                if eff.get("state") == "planted" and isinstance(eff.get("tile"), dict):
                    real_planted_tiles.append({"x": int(eff["tile"]["x"]), "y": int(eff["tile"]["y"])})

        planted_count = plant_res.get("completedCount", len(real_planted_tiles))
        if len(real_planted_tiles) < planted_count:
            seen = {(t["x"], t["y"]) for t in real_planted_tiles}
            for t in final_target_tiles:
                if (t["x"], t["y"]) not in seen:
                    real_planted_tiles.append(t)
                    seen.add((t["x"], t["y"]))
                if len(real_planted_tiles) >= planted_count:
                    break

        if planted_count == 0:
            err_msg = plant_res.get("error")
            skipped = plant_res.get("details", {}).get("skippedTiles", []) if isinstance(plant_res.get("details"), dict) else []
            if not err_msg and skipped:
                reasons_list = [f"({s.get('x')},{s.get('y')}:{s.get('reason')})" for s in skipped if isinstance(s, dict)]
                err_msg = f"Tiles skipped ({', '.join(reasons_list)})"
            if not err_msg:
                err_msg = "Unknown planting failure"
            return {
                **outcome_envelope("partial", False, "PLANT_FAILED"),
                "status": "failed",
                "crop": crop_name_or_id,
                "seedId": seed_id,
                "seedsPlanted": 0,
                "tilesHoed": hoed_count,
                "tilesWatered": 0,
                "targetTiles": [],
                "details": plant_res.get("details"),
                "pendingDecision": f"Failed planting seeds: {err_msg}.",
            }

        # Step 5: Water planted tiles
        watered_count = 0
        water_res = {}
        watering_succeeded = True
        if auto_water and real_planted_tiles:
            water_targets = real_planted_tiles
            water_res = await self.execute_tiles(
                tiles=water_targets, location_id=location_id, command_id=sub_command("water")
            )
            if water_res.get("status") == "executing" or water_res.get("terminalState") == "running":
                return {
                    **outcome_envelope("unknown", False, "WATER_IN_PROGRESS"),
                    "status": "executing",
                    "terminalState": "running",
                    "taskId": water_res.get("taskId"),
                    "inProgress": True,
                    "crop": crop_name_or_id,
                    "seedId": seed_id,
                    "seedsPlanted": planted_count,
                    "tilesHoed": hoed_count,
                    "tilesWatered": 0,
                    "targetTiles": real_planted_tiles,
                    "details": water_res.get("details"),
                    "message": f"Watering planted crops is in progress (taskId={water_res.get('taskId')}).",
                }

            watered_count = water_res.get("completedCount", 0)
            watering_term = water_res.get("terminalState")
            # The Mod reports "succeeded" only when every requested tile ended the
            # task watered. Tiles skipped as AlreadyWatered count as goal satisfied
            # and consume no water, so completedCount can legitimately be 0 while the
            # goal is met. Trust that verdict rather than comparing
            # completedCount against the target count.
            watering_succeeded = watering_term == "succeeded" or (
                isinstance(watered_count, int) and watered_count >= len(water_targets)
            )

        all_effects = list(plant_res.get("effects", []))
        if auto_water and water_res:
            all_effects.extend(water_res.get("effects", []))

        is_fully_successful = (
            planted_count >= count
            and (not auto_water or watering_succeeded)
        )
        final_status = "succeeded" if is_fully_successful else "partial"
        pending_decision = None
        reason_code = "OK"
        if not is_fully_successful:
            reasons = []
            if planted_count < count:
                reason_code = "PLANT_INCOMPLETE"
                skip_info = ""
                skipped = plant_res.get("details", {}).get("skippedTiles", []) if isinstance(plant_res.get("details"), dict) else []
                if skipped:
                    reasons_list = [f"({s.get('x')},{s.get('y')}:{s.get('reason')})" for s in skipped if isinstance(s, dict)]
                    skip_info = f" (skipped: {', '.join(reasons_list)})"
                reasons.append(f"Planted {planted_count}/{count} seeds{skip_info}")
            if auto_water and not watering_succeeded:
                reason_code = "WATER_INCOMPLETE"
                reasons.append(f"Watered {watered_count}/{len(real_planted_tiles)} tiles ({water_res.get('terminalState', 'failed')})")
            pending_decision = "; ".join(reasons)

        envelope = build_unified_outcome(
            outcome="completed" if is_fully_successful else "partial",
            goal_satisfied=is_fully_successful,
            effects=all_effects,
            remaining={
                "requested": count,
                "seedsPlanted": planted_count,
                "unwateredPlantedTiles": max(0, len(real_planted_tiles) - watered_count) if auto_water else 0,
                "remainingSeedsInBackpack": max(0, backpack_count - planted_count),
            },
            reason_code=reason_code,
            snapshot_revision=snapshot_revision,
        )

        return {
            **envelope,
            "status": final_status,
            "crop": crop_name_or_id,
            "seedId": seed_id,
            "seedsPlanted": planted_count,
            "tilesHoed": hoed_count,
            "tilesWatered": watered_count,
            "targetTiles": real_planted_tiles,
            "withdrawnFromChest": withdrawn_info,
            "remainingSeedsInBackpack": max(0, backpack_count - planted_count),
            "pendingDecision": pending_decision,
            "details": {
                "plantDetails": plant_res.get("details"),
                "waterDetails": water_res.get("details") if auto_water else None,
            },
            "effects": all_effects,
        }

    async def close(self) -> None:
        """Closes the underlying transport client if connected."""
        if self._client:
            try:
                await self._client.close()
            except Exception:
                pass
            self._client = None
