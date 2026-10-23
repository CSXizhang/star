"""Deliver current environment snapshots and acknowledge shared-session guidance."""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any

AGENT_INTRO = "你是星露谷伙伴，通过 Mod 工具完成玩家的游戏目标。"
AGENT_GUIDANCE = "结合当前状态安排工作，按实际结果与玩家交流。"
logger = logging.getLogger(__name__)


def _changes(before: Any, after: Any, path: list[str | int] | None = None) -> list[dict[str, Any]]:
    path = path or []
    if type(before) is type(after) and before == after:
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        changes = [{"remove": path + [key]} for key in before if key not in after]
        for key, value in after.items():
            changes.extend(_changes(before[key], value, path + [key]) if key in before
                           else [{"set": path + [key], "value": value}])
        return changes
    if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        changes = [change for i, value in enumerate(after) for change in _changes(before[i], value, path + [i])]
        replacement = [{"set": path, "value": after}]
        return min((changes, replacement), key=lambda value: len(json.dumps(value, ensure_ascii=False)))
    return [{"set": path, "value": after}]


class PromptDelivery:
    def __init__(self, path: Path):
        self.path = path
        self._reload()

    def _reload(self) -> None:
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
            self.state = state if isinstance(state, dict) else {}
        except (OSError, ValueError):
            self.state = {}

    def prepare(self, key: str, session: str | None, prompt: str, rules: str,
                *, context: dict[str, str] | None = None) -> tuple[str, dict[str, Any]]:
        # Native context maintenance may invalidate this persisted baseline.
        self._reload()
        old = self.state.get(key, {})
        continuing = bool(session) and old.get("session") == session
        static = dict(old.get("static", {})) if continuing else {}
        # Player text is opaque: labels, newlines and whitespace are preserved.
        marker = re.search(r"(?m)^(?:玩家说：|玩家指令：)", prompt)
        boundary = marker.start() if marker else len(prompt)
        prefix, player = prompt[:boundary], prompt[boundary:]
        for name, content in {"agentIntro": AGENT_INTRO, "agentGuidance": AGENT_GUIDANCE,
                              "rules": rules}.items():
            if not content or content not in prefix:
                continue
            digest = hashlib.sha256(content.encode()).hexdigest()
            if static.get(name) == digest:
                prefix = prefix.replace(content, "", 1)
            static[name] = digest
        additions = []
        for name, content in (context or {}).items():
            digest = hashlib.sha256(content.encode()).hexdigest()
            if static.get(name) != digest:
                additions.append(content)
            static[name] = digest
        # Diff only runtime-owned JSON blocks, never text supplied by the player.
        # The baseline advances only when the provider acknowledges this turn.
        prior = old.get("snapshots", {}) if continuing else {}
        snapshots = copy.deepcopy(prior)
        decoder = json.JSONDecoder()
        index = 0
        def replace_state(match: re.Match[str]) -> str:
            nonlocal index
            raw = match.group(1)
            try:
                current, end = decoder.raw_decode(raw)
            except ValueError:
                return match.group(0)
            block = str(index)
            index += 1
            snapshots[block] = current
            encoded = json.dumps(current, ensure_ascii=False, separators=(",", ":"))
            full = "实时上下文：" + encoded
            if block in prior:
                delta = _changes(prior[block], current)
                compact = "上下文增量：" + json.dumps({"block": block, "changes": delta}, ensure_ascii=False, separators=(",", ":"))
                if len(compact.encode()) < len(full.encode()):
                    return compact + raw[end:]
            return full + raw[end:]
        prefix = re.sub(r"(?m)^实时上下文：([^\r\n]+)", replace_state, prefix)
        protocol = ("上下文规则：状态块按出现顺序从0编号；实时上下文完整替换该块；上下文增量只修改指定路径，"
                    "路径中的数组下标从0开始；set替换值（含null和数组），remove删除字段；未列出的字段沿用上次，空changes表示无变化。")
        if snapshots and not (continuing and old.get("deltaProtocol") == 1):
            additions.append(protocol)
        sent = "\n".join([*additions, prefix.strip()])
        if player:
            sent += "\n\n" + player
        pending = {"session": session, "static": static, "snapshots": copy.deepcopy(snapshots),
                   "deltaProtocol": 1 if snapshots else 0,
                   "version": old.get("version", 0) + 1,
                   "originalPromptBytes": len(prompt.encode()), "newPromptBytes": len(sent.encode())}
        logger.info("Prompt payload originalBytes=%s sentBytes=%s savedBytes=%s baseline=%s",
                    pending["originalPromptBytes"], pending["newPromptBytes"],
                    pending["originalPromptBytes"] - pending["newPromptBytes"],
                    "acknowledged-session" if prior else "full-new-session-or-reset")
        return sent, pending

    def acknowledge(self, key: str, pending: dict[str, Any], session: str) -> None:
        self.state[key] = {**pending, "session": session}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def invalidate_session(self, key: str, session: str) -> None:
        if self.state.get(key, {}).get("session") == session:
            self.acknowledge(key, {"version": self.state[key].get("version", 0), "static": {}}, session)
