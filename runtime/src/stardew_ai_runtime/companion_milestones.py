"""Companion milestone planning: near-term key game nodes per save.

The companion proposes data-driven milestone nodes (Egg Festival strawberry
run, Spring Crops bundle retention) based on the real game date and the
player's play style; the player adopts/revises/defers them in plan mode, and
adopted capability prep items enter the existing WorkStore goal/todo system.
Completion is decided only by verifiable state on day settlement.

Persistence: run_dir/data/companion-milestones.json
  {saveId: {revision, lastGameDate, nodes: {id: node},
            history: [{at, gameDate, action, note}]}}

The persistence pattern (cross-process .lock, reload-before-mutate, tmp+replace
atomic write) mirrors companion_care.py. Calendar arithmetic reuses the
work_state.py conventions (spring/summer/fall/winter, 28 days per season,
``current >= trigger`` semantics); nothing here invents its own ordering.

Node facts come from the verified Wiki snapshots in the interface contract
(§1, official English Wiki, checked 2026-09-26); every template carries its
sourceUrl and the prompts must tell the model about the language/version gap.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

from stardew_ai_runtime.work_state import (
    SEASON_ORDER,
    WorkStateError,
    WorkStore,
    _calendar_key,
)

logger = logging.getLogger("stardew_ai_runtime.companion_milestones")

_DAYS_PER_SEASON = 28
_SEASONS = tuple(sorted(SEASON_ORDER, key=SEASON_ORDER.get))  # spring..winter
MAX_WIRE_NODES = 12
_HISTORY_LIMIT = 100

NODE_STATUSES = frozenset({"suggested", "adopted", "deferred", "completed", "missed"})

# Model-writable transitions only; completed/missed are set exclusively by
# on_day_settled from verifiable state (contract §0/§3.1).
_TRANSITIONS = {
    "adopt": frozenset({"suggested", "deferred"}),
    "revise": frozenset({"suggested", "adopted"}),
    "defer": frozenset({"suggested", "adopted"}),
    "reopen": frozenset({"deferred", "missed"}),
}

# ---------------------------------------------------------------------------
# Data-driven node catalogue (facts: contract §1, official English Wiki).
# ---------------------------------------------------------------------------
_TEMPLATES: dict[str, dict[str, Any]] = {
    "spring-egg-festival-strawberry": {
        "playStyle": "earn",
        "title": "蛋蛋节草莓种子准备",
        "summary": (
            "春13蛋蛋节鹈鹕镇广场9:00-14:00开摊，草莓种子100g/个；种下8天成熟后每4天结果一次，"
            "基础售价120g，春13当晚种下无加速最多收2茬；Wiki建议节前翻地备地。"
            "节日期间不能向皮埃尔出售物品，彩蛋寻宝后无法再购买。"
            "本节点完成仅指种子数量准备，后续种植和浇水需另看实际工作结果。"
        ),
        "sourceUrl": "https://stardewvalleywiki.com/Egg_Festival",
        "windowDays": 7,  # suggest window: spring 6..13 (8 days, §3.1)
        "targetSeason": "spring",
        "targetDay": 13,
        "verifyItems": [{"itemName": "Strawberry Seeds", "minCount": 1}],
        "prepItems": [
            {
                "key": "reserve-funds",
                "label": "计划保留购买草莓种子的资金（仅预算提醒，不冻结资金）",
                "support": "manual",
                "status": "pending",
                "note": None,
            },
            {
                "key": "buy-at-festival",
                "label": "春13当天9:00-14:00亲自到鹈鹕镇广场节日摊位购买（节日购物伙伴无法代劳）",
                "support": "manual",
                "status": "pending",
                "note": "仅核验种子准备，不代表种植完成；彩蛋寻宝后无法再购买",
                "verifyItem": "Strawberry Seeds",
                "minCount": 1,
            },
            {
                "key": "pre-till",
                "label": "节前翻地施肥浇水备好地（Wiki建议）",
                "support": "capability",
                "status": "pending",
                "note": None,
                "leadDays": 1,
            },
            {
                "key": "plant-after",
                "label": "把草莓种子放进农场箱子后，伙伴可接手种植和浇水",
                "support": "capability",
                "status": "pending",
                "leadDays": 0,
                "note": "种植和浇水需另看真实执行结果，持有种子不代表已种下",
            },
        ],
    },
    "spring-crops-bundle-retention": {
        "playStyle": "community",
        "title": "春季作物收集包保留",
        "summary": (
            "春季作物收集包（储藏室）：防风草、青豆、花椰菜、土豆各1，奖励20个Speed-Gro；"
            "草莓不属于任何收集包。献祭实际进度读不到，属未知。"
        ),
        "sourceUrl": "https://stardewvalleywiki.com/Bundles",
        "windowDays": 13,  # second half of spring through the season check
        "targetSeason": "spring",
        "targetDay": 28,
        "verifyItems": [
            {"itemName": "Parsnip", "minCount": 1},
            {"itemName": "Green Bean", "minCount": 1},
            {"itemName": "Cauliflower", "minCount": 1},
            {"itemName": "Potato", "minCount": 1},
        ],
        "prepItems": [
            {
                "key": "retain-parsnip",
                "label": "保留防风草×1（献祭用，先别卖掉）",
                "support": "manual",
                "status": "pending",
                "note": None,
                "verifyItem": "Parsnip",
                "minCount": 1,
            },
            {
                "key": "retain-green-bean",
                "label": "保留青豆×1（献祭用，先别卖掉）",
                "support": "manual",
                "status": "pending",
                "note": None,
                "verifyItem": "Green Bean",
                "minCount": 1,
            },
            {
                "key": "retain-cauliflower",
                "label": "保留花椰菜×1（献祭用，先别卖掉）",
                "support": "manual",
                "status": "pending",
                "note": None,
                "verifyItem": "Cauliflower",
                "minCount": 1,
            },
            {
                "key": "retain-potato",
                "label": "保留土豆×1（献祭用，先别卖掉）",
                "support": "manual",
                "status": "pending",
                "note": None,
                "verifyItem": "Potato",
                "minCount": 1,
            },
        ],
    },
}


_PREPARATION_LABELS = {
    "water": "照料当前缺水作物，按实际体力和水量完成一小段浇水",
    "harvest": "收取眼前成熟作物；不自动出售或处理献祭保留品",
    "clear": "清理农场眼前少量杂物，保留资源并遵守体力保护",
    "plant": "用现有可用种子小规模补种并浇水；不因此自动买种子",
    "animals": "照料现有动物的日常喂食和抚摸，不购买动物",
    "machines": "收取现有机器成品，不自动出售或追加采购",
    "store": "把玩家指定保留的物品整理进获准使用的箱子，不出售",
    "ship": "仅出货玩家已明确认可可出售的物品，保留献祭和其他约定保留品",
    "pickup": "拾取授权范围内可见掉落物，保留材料，不自动砍树或出售",
}


class MilestoneError(ValueError):
    """Invalid milestone mutation (validation / state-machine failure)."""


# --------------------------------------------------------------------------- dates
def _day_index(year: Any, season_index: int, day: Any) -> int | None:
    try:
        return (int(year) - 1) * len(_SEASONS) * _DAYS_PER_SEASON + (
            int(season_index) - 1
        ) * _DAYS_PER_SEASON + int(day)
    except (TypeError, ValueError):
        return None


def _index_to_day(index: int) -> dict[str, Any]:
    index = max(1, int(index))
    year = (index - 1) // (len(_SEASONS) * _DAYS_PER_SEASON) + 1
    rem = (index - 1) % (len(_SEASONS) * _DAYS_PER_SEASON)
    season_index = rem // _DAYS_PER_SEASON + 1
    day = rem % _DAYS_PER_SEASON + 1
    return {"year": year, "season": _SEASONS[season_index - 1], "day": day}


def _shift_days(year: Any, season: Any, day: Any, delta: int) -> dict[str, Any]:
    key = _calendar_key(year, season, day)
    index = _day_index(*key) if key else None
    if index is None:
        raise MilestoneError(f"invalid calendar date {year}:{season}:{day}")
    return _index_to_day(index + int(delta))


def _days_until(current_key: tuple[int, int, int] | None, target: dict[str, Any]) -> int | None:
    if current_key is None:
        return None
    current_index = _day_index(*current_key)
    target_index = _day_index(
        target.get("year"), SEASON_ORDER.get(str(target.get("season")).lower(), 0), target.get("day")
    )
    if current_index is None or target_index is None:
        return None
    return target_index - current_index


def _parse_target_date(target_date: str) -> dict[str, Any]:
    parts = str(target_date or "").split(":")
    key = _calendar_key(parts[0], parts[1], parts[2]) if len(parts) == 3 else None
    if key is None or not (1 <= int(parts[2]) <= _DAYS_PER_SEASON):
        raise MilestoneError(
            f"target_date must look like '1:spring:13' (year:season:day 1..28), got {target_date!r}"
        )
    return {"year": int(parts[0]), "season": str(parts[1]).lower(), "day": int(parts[2])}


def _game_date_key(game_date: dict[str, Any] | None) -> tuple[int, int, int] | None:
    if not isinstance(game_date, dict):
        return None
    return _calendar_key(game_date.get("year"), game_date.get("season"), game_date.get("day"))


# --------------------------------------------------------------------------- items
def _normalise_player_items(player_items: Any) -> list[dict[str, Any]] | None:
    """Aggregate ``[{name, quantity}]`` into a casefolded name -> count map source list.

    Returns None when no item data is available at all (verification degrades to
    ``unverified`` instead of pretending the item is absent).
    """
    if not isinstance(player_items, list):
        return None
    items: list[dict[str, Any]] = []
    for entry in player_items:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        try:
            quantity = int(entry.get("quantity", 0))
        except (TypeError, ValueError):
            continue
        if quantity > 0:
            items.append({"name": name.casefold(), "quantity": quantity})
    return items


def _items_contain(items: list[dict[str, Any]], name: str, min_count: int) -> bool:
    wanted = str(name).strip().casefold()
    return sum(it["quantity"] for it in items if it["name"] == wanted) >= int(min_count)


def _node_verifier(node: dict[str, Any]) -> list[dict[str, Any]] | None:
    explicit = node.get("verifyItems")
    if isinstance(explicit, list) and explicit:
        return [
            {"itemName": v.get("itemName"), "minCount": (int(node.get("plannedCount") or 1)
                         if v.get("itemName") == "Strawberry Seeds" else int(v.get("minCount") or 1))}
            for v in explicit
            if isinstance(v, dict) and v.get("itemName")
        ]
    derived = [
        {"itemName": p.get("verifyItem"), "minCount": int(p.get("minCount") or 1)}
        for p in node.get("prepItems", [])
        if isinstance(p, dict) and p.get("verifyItem")
    ]
    return derived or None


# --------------------------------------------------------------------------- templates
def _template_target(template: dict[str, Any], current_key: tuple[int, int, int] | None) -> dict[str, Any]:
    season_name = str(template["targetSeason"])
    target_day = int(template["targetDay"])
    if current_key is None:
        return {"year": 1, "season": season_name, "day": target_day}
    year, season_index, day = current_key
    if (season_index, day) <= (SEASON_ORDER[season_name], target_day):
        return {"year": year, "season": season_name, "day": target_day}
    return {"year": year + 1, "season": season_name, "day": target_day}


def _template_node(template_id: str, current_key: tuple[int, int, int] | None) -> dict[str, Any]:
    template = _TEMPLATES[template_id]
    target = _template_target(template, current_key)
    now = time.time()
    return {
        "id": f"{template_id}:y{target['year']}",
        "templateId": template_id,
        "title": template["title"],
        "status": "suggested",
        "verification": None,
        "target": target,
        "targetDate": f"{target['year']}:{target['season']}:{target['day']}",
        "daysUntil": _days_until(current_key, target),
        "summary": template["summary"],
        "sourceUrl": template["sourceUrl"],
        "prepItems": [dict(p) for p in template["prepItems"]],
        "reservedFunds": None,
        "plannedCount": None,
        "termsNote": None,
        "verifyItems": [dict(v) for v in template.get("verifyItems", [])],
        "goalId": None,
        "todoIds": {},
        "createdAt": now,
        "updatedAt": now,
    }


def _template_node_by_id(node_id: str, current_key: tuple[int, int, int] | None) -> dict[str, Any] | None:
    """Rebuild a catalogue node from its ``<templateId>:y<N>`` id (any window)."""
    if ":" not in node_id:
        return None
    template_id, _, suffix = node_id.rpartition(":y")
    template = _TEMPLATES.get(template_id)
    if template is None or not suffix.isdigit():
        return None
    node = _template_node(template_id, current_key)
    target = {"year": int(suffix), "season": node["target"]["season"], "day": node["target"]["day"]}
    node["target"] = target
    node["targetDate"] = f"{target['year']}:{target['season']}:{target['day']}"
    node["daysUntil"] = _days_until(current_key, target)
    return node


def _sort_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """§2.2 ordering: adopted -> suggested (daysUntil asc) -> deferred -> completed/missed (newest first)."""

    def key(node: dict[str, Any]) -> tuple[int, float, str]:
        status = str(node.get("status") or "")
        days_until = node.get("daysUntil")
        du_key = float(days_until) if isinstance(days_until, int) else float(10**9)
        updated = float(node.get("updatedAt") or 0)
        if status in {"completed", "missed"}:
            return (3, -updated, str(node.get("id") or ""))
        order = {"adopted": 0, "suggested": 1, "deferred": 2}.get(status, 4)
        return (order, du_key, str(node.get("id") or ""))

    return sorted(nodes, key=key)


def wire_node(node: dict[str, Any]) -> dict[str, Any]:
    """Project an internal node dict to the §2.2 ``MilestoneNodeDto`` wire shape."""
    return {
        "id": node.get("id"),
        "title": node.get("title"),
        "status": node.get("status"),
        "verification": node.get("verification"),
        "targetDate": node.get("targetDate"),
        "daysUntil": node.get("daysUntil"),
        "summary": node.get("summary"),
        "sourceUrl": node.get("sourceUrl"),
        "prepItems": [
            {
                "key": p.get("key"),
                "label": p.get("label"),
                "support": p.get("support"),
                "status": p.get("status"),
                "note": p.get("note"),
            }
            for p in node.get("prepItems", [])
            if isinstance(p, dict)
        ],
        "reservedFunds": node.get("reservedFunds"),
        "plannedCount": node.get("plannedCount"),
        "termsNote": node.get("termsNote"),
        "updatedAt": node.get("updatedAt"),
    }


# --------------------------------------------------------------------------- store
class CompanionMilestoneStore:
    """Per-save milestone node store with cross-process safe persistence."""

    def __init__(self, state_path: Path | str) -> None:
        self.state_path = Path(state_path)
        self._data: dict[str, dict[str, Any]] = {}
        self._load()

    # ------------------------------------------------------------------ I/O
    def _load(self) -> None:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._data = {str(k): dict(v) for k, v in raw.items() if isinstance(v, dict)}
        except (FileNotFoundError, OSError, ValueError, TypeError):
            self._data = {}

    @staticmethod
    def _lock(lock: Any) -> None:
        if os.name == "nt":
            import msvcrt

            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)

    @staticmethod
    def _unlock(lock: Any) -> None:
        with contextlib.suppress(Exception):
            if os.name == "nt":
                import msvcrt

                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _write_unlocked(self) -> None:
        temp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temp.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temp.replace(self.state_path)

    def _mutate(self, save_id: str, fn: Any) -> Any:
        if not save_id:
            raise MilestoneError("save_id is required")
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.state_path.with_suffix(self.state_path.suffix + ".lock")
        with open(lock_path, "a+b") as lock:
            self._lock(lock)
            try:
                self._load()
                record = self._data.setdefault(save_id, self._default_record())
                result = fn(record)
                self._write_unlocked()
                return result
            finally:
                self._unlock(lock)

    @staticmethod
    def _default_record() -> dict[str, Any]:
        return {"revision": 0, "lastGameDate": None, "nodes": {}, "history": []}

    def _record(self, save_id: str) -> dict[str, Any]:
        self._load()
        record = self._data.get(save_id)
        if not isinstance(record, dict):
            return self._default_record()
        record.setdefault("nodes", {})
        record.setdefault("history", [])
        return record

    @staticmethod
    def _bump(record: dict[str, Any], game_date: str | None, action: str, note: str | None) -> None:
        record["revision"] = int(record.get("revision") or 0) + 1
        record.setdefault("history", []).append(
            {"at": time.time(), "gameDate": game_date, "action": action, "note": note}
        )
        record["history"] = record["history"][-_HISTORY_LIMIT:]

    # ------------------------------------------------------------- queries
    def revision(self, save_id: str) -> int:
        return int(self._record(save_id).get("revision") or 0)

    def list_nodes(self, save_id: str) -> list[dict[str, Any]]:
        """Persisted nodes in §2.2 display order (read-only)."""
        nodes = [dict(n) for n in self._record(save_id).get("nodes", {}).values() if isinstance(n, dict)]
        return _sort_nodes(nodes)

    def suggest(
        self,
        game_date: dict[str, Any] | None,
        play_style: str | None,
    ) -> list[dict[str, Any]]:
        """Fresh catalogue suggestions for the current date + play style (pure)."""
        current_key = _game_date_key(game_date)
        if current_key is None:
            return []
        suggested: list[dict[str, Any]] = []
        for template_id, template in _TEMPLATES.items():
            if template.get("playStyle") != play_style:
                continue
            node = _template_node(template_id, current_key)
            days_until = node.get("daysUntil")
            if isinstance(days_until, int) and 0 <= days_until <= int(template["windowDays"]):
                suggested.append(node)
        return suggested

    def merged_nodes(
        self,
        save_id: str,
        game_date: dict[str, Any] | None,
        play_style: str | None,
        limit: int = MAX_WIRE_NODES,
    ) -> list[dict[str, Any]]:
        """Persisted nodes merged with current catalogue suggestions (persisted wins)."""
        by_id: dict[str, dict[str, Any]] = {n["id"]: n for n in self.list_nodes(save_id) if n.get("id")}
        for node in by_id.values():
            node["daysUntil"] = _days_until(_game_date_key(game_date), node.get("target") or {})
        for node in self.suggest(game_date, play_style):
            by_id.setdefault(node["id"], node)
        return _sort_nodes(list(by_id.values()))[:limit]

    # ------------------------------------------------------------- mutations
    def propose(
        self,
        save_id: str,
        *,
        title: str,
        target_date: str,
        summary: str | None = None,
        source_url: str | None = None,
        preparation: list[str] | None = None,
        game_date: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Model-proposed custom node during plan discussion (must carry a target date)."""
        clean = (title or "").strip()
        if not clean:
            raise MilestoneError("propose: title is required")
        if len(clean) > 60:
            raise MilestoneError("propose: title must be at most 60 chars")
        preparation = list(dict.fromkeys(preparation or []))
        if any(key not in _PREPARATION_LABELS for key in preparation):
            raise MilestoneError("preparation must use " + "|".join(_PREPARATION_LABELS))
        target = _parse_target_date(target_date)
        current_key = _game_date_key(game_date)
        now = time.time()
        node: dict[str, Any] = {
            "id": f"custom-{uuid.uuid4().hex[:8]}",
            "templateId": None,
            "title": clean,
            "status": "suggested",
            "verification": None,
            "target": target,
            "targetDate": f"{target['year']}:{target['season']}:{target['day']}",
            "daysUntil": _days_until(current_key, target),
            "summary": (summary or "").strip() or None,
            "sourceUrl": (source_url or "").strip() or None,
            "prepItems": [{"key": key, "label": _PREPARATION_LABELS[key],
                           "support": "capability", "status": "pending", "leadDays": 0,
                           "note": "按实际观察执行；采纳是安排，完成需真实结果"}
                          for key in preparation],
            "reservedFunds": None,
            "plannedCount": None,
            "termsNote": None,
            "verifyItems": [],
            "goalId": None,
            "todoIds": {},
            "createdAt": now,
            "updatedAt": now,
        }

        def mutate(record: dict[str, Any]) -> dict[str, Any]:
            record["nodes"][node["id"]] = node
            self._bump(record, node["targetDate"], "propose", node["title"])
            return node

        return self._mutate(save_id, mutate)

    def _resolve_for_write(
        self, record: dict[str, Any], node_id: str, current_key: tuple[int, int, int] | None
    ) -> dict[str, Any]:
        node = record["nodes"].get(node_id)
        if node is None:
            node = _template_node_by_id(node_id, current_key)
            if node is not None:
                record["nodes"][node_id] = node
        if node is None:
            raise MilestoneError(f"unknown node '{node_id}'")
        return node

    @staticmethod
    def _check_transition(action: str, node: dict[str, Any]) -> None:
        allowed = _TRANSITIONS[action]
        status = str(node.get("status") or "")
        if status not in allowed:
            raise MilestoneError(
                f"cannot {action} node '{node.get('id')}' from status '{status}' "
                f"(allowed from: {sorted(allowed)})"
            )

    def adopt(
        self,
        save_id: str,
        node_id: str,
        *,
        reserved_funds: int | None = None,
        planned_count: int | None = None,
        terms_note: str | None = None,
        work_store: WorkStore | None = None,
        game_date: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Adopt a suggested/deferred node; capability prep items enter the WorkStore.

        A one-time ``reservedFunds`` constraint is recorded on the goal; it is never
        mapped into the per-day purchase budget. Never submits plans or decisions.
        """
        current_key = _game_date_key(game_date)

        def mutate(record: dict[str, Any]) -> dict[str, Any]:
            node = self._resolve_for_write(record, node_id, current_key)
            self._check_transition("adopt", node)
            if reserved_funds is not None:
                if int(reserved_funds) < 0:
                    raise MilestoneError("reserved_funds must be non-negative")
                node["reservedFunds"] = int(reserved_funds)
            if planned_count is not None:
                if int(planned_count) <= 0:
                    raise MilestoneError("planned_count must be positive")
                node["plannedCount"] = int(planned_count)
            if terms_note is not None:
                node["termsNote"] = str(terms_note).strip() or None
            if work_store is not None:
                self._wire_work_items(node, work_store, save_id)
            node["status"] = "adopted"
            node["daysUntil"] = _days_until(current_key, node["target"])
            node["updatedAt"] = time.time()
            self._bump(record, node.get("targetDate"), "adopt", node.get("title"))
            return node

        return self._mutate(save_id, mutate)

    def revise(
        self,
        save_id: str,
        node_id: str,
        *,
        title: str | None = None,
        summary: str | None = None,
        reserved_funds: int | None = None,
        planned_count: int | None = None,
        terms_note: str | None = None,
        work_store: WorkStore | None = None,
        target_date: str | None = None,
        game_date: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        current_key = _game_date_key(game_date)

        def mutate(record: dict[str, Any]) -> dict[str, Any]:
            node = self._resolve_for_write(record, node_id, current_key)
            self._check_transition("revise", node)
            if target_date is not None:
                target = _parse_target_date(target_date)
                if node.get("templateId") and target != node["target"]:
                    raise MilestoneError("节日/季末固定日期不可修改；请另提自定义节点")
                node["target"] = target
                node["targetDate"] = target_date
            if title is not None:
                clean = title.strip()
                if not clean:
                    raise MilestoneError("revise: title cannot be empty")
                node["title"] = clean[:60]
            if summary is not None:
                node["summary"] = summary.strip() or None
            if reserved_funds is not None:
                if int(reserved_funds) < 0:
                    raise MilestoneError("reserved_funds must be non-negative")
                node["reservedFunds"] = int(reserved_funds)
            if planned_count is not None:
                if int(planned_count) <= 0:
                    raise MilestoneError("planned_count must be positive")
                node["plannedCount"] = int(planned_count)
            if terms_note is not None:
                node["termsNote"] = str(terms_note).strip() or None
            if work_store is not None and node.get("status") == "adopted":
                self._wire_work_items(node, work_store, save_id)
            node["daysUntil"] = _days_until(current_key, node["target"])
            node["updatedAt"] = time.time()
            self._bump(record, node.get("targetDate"), "revise", node.get("title"))
            return node

        return self._mutate(save_id, mutate)

    def defer(
        self,
        save_id: str,
        node_id: str,
        *,
        reason: str | None = None,
        work_store: WorkStore | None = None,
        game_date: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        current_key = _game_date_key(game_date)

        def mutate(record: dict[str, Any]) -> dict[str, Any]:
            node = self._resolve_for_write(record, node_id, current_key)
            self._check_transition("defer", node)
            if work_store is not None:
                self._unwire_work_items(node, work_store, save_id)
            node["status"] = "deferred"
            node["daysUntil"] = _days_until(current_key, node["target"])
            node["updatedAt"] = time.time()
            self._bump(record, node.get("targetDate"), "defer", reason or node.get("title"))
            return node

        return self._mutate(save_id, mutate)

    def reopen(
        self,
        save_id: str,
        node_id: str,
        *,
        reason: str | None = None,
        work_store: WorkStore | None = None,
        game_date: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        current_key = _game_date_key(game_date)

        def mutate(record: dict[str, Any]) -> dict[str, Any]:
            node = self._resolve_for_write(record, node_id, current_key)
            self._check_transition("reopen", node)
            node["status"] = "suggested"
            node["verification"] = None
            node["daysUntil"] = _days_until(current_key, node["target"])
            node["updatedAt"] = time.time()
            self._bump(record, node.get("targetDate"), "reopen", reason or node.get("title"))
            return node

        return self._mutate(save_id, mutate)

    # ------------------------------------------------------------- work wiring
    @staticmethod
    def _wire_work_items(node: dict[str, Any], work_store: WorkStore, save_id: str) -> None:
        capability_items = [p for p in node.get("prepItems", [])
                            if p.get("support") == "capability"]
        if not capability_items:
            return
        constraints = {
            "reservedFunds": node.get("reservedFunds"),
            "reservedFundsNote": "计划保留金额（非每日预算，仅提醒，不冻结资金）",
            "plannedCount": node.get("plannedCount"),
            "termsNote": node.get("termsNote"),
        }
        target = node["target"]
        specs = []
        for prep in capability_items:
            trigger = _shift_days(target["year"], target["season"], target["day"],
                                  -int(prep.get("leadDays") or 0))
            specs.append({
                "key": prep["key"],
                "intent": f'{prep["key"]}：{node["title"]}：{prep["label"]}；'
                          f'范围={node.get("summary") or "仅当前可确认范围，不扩大到其他工作"}；'
                          f'计划数量={node.get("plannedCount") or "待商量"}；'
                          f'约定={node.get("termsNote") or "无"}',
                "trigger": {"type": "calendar", **trigger}, "expiry": dict(target),
            })
        try:
            goal_id, todo_ids = work_store.sync_milestone_work(
                save_id, node["id"], text=f'节点准备：{node["title"]}（{node["targetDate"]}）',
                constraints=constraints, todos=specs, active=True,
                existing_goal_id=node.get("goalId"),
            )
            node["goalId"], node["todoIds"] = goal_id, todo_ids
        except WorkStateError as ex:
            raise MilestoneError(f"work system rejected milestone: {ex}") from None

    @staticmethod
    def _unwire_work_items(node: dict[str, Any], work_store: WorkStore, save_id: str) -> None:
        goal_id = node.get("goalId")
        if not goal_id:
            return
        goal = next((g for g in work_store.state(save_id).goals if g.id == goal_id), None)
        if goal is None:
            return
        spec = goal.constraints.get("milestoneSpec")
        if spec:
            work_store.sync_milestone_work(
                save_id, node["id"], text=spec["text"],
                constraints=spec["constraints"], todos=spec["todos"], active=False,
            )
        else:
            # Upgrade a node created before atomic reconciliation existed.
            work_store.cancel_goal(save_id, goal_id)
            work_store.revise_goal(save_id, goal_id, status="paused")

    # ------------------------------------------------------------- day settle
    def on_day_settled(
        self,
        save_id: str,
        *,
        year: Any,
        season: Any,
        day: Any,
        player_items: list[dict[str, Any]] | None = None,
    ) -> tuple[bool, list[dict[str, Any]]]:
        """Verify nodes against the settled day; return (changed, reminder candidates).

        Positive quantity evidence verifies seed readiness / bundle retention only.
        Missing inventory is incomplete evidence, so overdue nodes stay unverified
        and can be checked again. Planting and actual bundle donation remain unknown.
        """
        current_key = _calendar_key(year, season, day)
        if current_key is None:
            return False, []
        game_date = f"{year}:{season}:{day}"
        items = _normalise_player_items(player_items)
        changed_flags: list[bool] = [False]
        candidates: list[dict[str, Any]] = []

        def mutate(record: dict[str, Any]) -> None:
            record["lastGameDate"] = game_date
            for node in record.get("nodes", {}).values():
                if not isinstance(node, dict):
                    continue
                target_key = _calendar_key(
                    (node.get("target") or {}).get("year"),
                    (node.get("target") or {}).get("season"),
                    (node.get("target") or {}).get("day"),
                )
                days_until = _days_until(current_key, node.get("target") or {})
                if node.get("daysUntil") != days_until:
                    node["daysUntil"] = days_until
                    changed_flags[0] = True
                if self._verify_prep_items(node, items):
                    changed_flags[0] = True
                if target_key is not None and current_key > target_key and node.get("status") in {
                    "suggested",
                    "adopted",
                }:
                    verifier = _node_verifier(node)
                    satisfied = bool(verifier and items is not None and all(
                        _items_contain(items, v["itemName"], v["minCount"]) for v in verifier
                    ))
                    # A backpack is a partial observation: absent seeds may be in
                    # a chest or planted already. Never infer a missed purchase.
                    verification = "verified" if satisfied else "unverified"
                    if satisfied or node.get("verification") != verification:
                        if satisfied:
                            node["status"] = "completed"
                        node["verification"] = verification
                        node["updatedAt"] = time.time()
                        changed_flags[0] = True
                if node.get("status") in {"suggested", "adopted"} and isinstance(days_until, int) and -1 <= days_until <= 2:
                    gap = next(
                        (
                            p.get("label")
                            for p in node.get("prepItems", [])
                            if isinstance(p, dict) and p.get("status") in {"pending", "unknown"}
                        ),
                        None,
                    )
                    candidates.append(
                        {
                            "id": node.get("id"),
                            "title": node.get("title"),
                            "targetDate": node.get("targetDate"),
                            "daysUntil": days_until,
                            "firstGap": ("日期已过，背包证据不足；请核对已购买、箱中或已种植数量"
                                         if days_until < 0 else gap),
                        }
                    )
            if changed_flags[0]:
                notes = [
                    f'{n.get("id")}→{n.get("status")}({n.get("verification") or "none"})'
                    for n in record.get("nodes", {}).values()
                    if isinstance(n, dict) and n.get("status") in {"completed", "missed"}
                ]
                self._bump(record, game_date, "settle", "; ".join(notes) or "prep items verified")

        self._mutate(save_id, mutate)
        return changed_flags[0], candidates

    @staticmethod
    def _verify_prep_items(node: dict[str, Any], items: list[dict[str, Any]] | None) -> bool:
        changed = False
        for prep in node.get("prepItems", []):
            if not isinstance(prep, dict) or not prep.get("verifyItem"):
                continue
            if items is None:
                if prep.get("status") != "unknown":
                    prep["status"] = "unknown"
                    changed = True
                continue
            status = (
                "done"
                if _items_contain(items, prep["verifyItem"], (int(node.get("plannedCount") or 1)
                    if prep.get("verifyItem") == "Strawberry Seeds" else int(prep.get("minCount") or 1)))
                else "unknown"
            )
            if prep.get("status") != status:
                prep["status"] = status
                changed = True
        return changed
