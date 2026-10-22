"""Deliver current environment snapshots and acknowledge shared-session guidance."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

AGENT_INTRO = "你是星露谷伙伴，通过 Mod 工具完成玩家的游戏目标。"
AGENT_GUIDANCE = "结合当前状态安排工作，按实际结果与玩家交流。"


class PromptDelivery:
    def __init__(self, path: Path):
        self.path = path
        try:
            self.state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.state = {}

    def prepare(self, key: str, session: str | None, prompt: str, rules: str,
                *, context: dict[str, str] | None = None) -> tuple[str, dict[str, Any]]:
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
        # State already comes from the runtime's latest merged snapshot. Sending
        # it whole avoids asking the model to apply a second JSON patch protocol.
        sent = "\n".join([*additions, prefix.strip()])
        if player:
            sent += "\n\n" + player
        pending = {"session": session, "static": static,
                   "version": old.get("version", 0) + 1,
                   "originalPromptBytes": len(prompt.encode()), "newPromptBytes": len(sent.encode())}
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
