"""Acknowledged per-provider-session context delivery (not token-cache accounting)."""
from __future__ import annotations

import hashlib
import json
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

DELTA_PROTOCOL = (
    "状态采用版本化增量：replaceFields覆盖同名字段，removeFields撤销旧字段；replacePaths按path数组仅覆盖指定嵌套字段，removePaths撤销指定路径；"
    "spliceListPaths依给定顺序在path所指列表的start索引删除deleteCount项，再插入items，其他项保留。"
    "未出现字段沿用已确认版本，unknown/null仍是未知。snapshot替换整个状态；delta仅更新baseVersion对应状态。"
    "receiptReference指向本轮terminalFacts里同一commandId的完整回执；failedBranchesSource指向本轮状态中的同一失败分支列表。"
)

AGENT_INTRO = (
    "你是星露谷伙伴智能体，请直接通过已接入的 stardew-companion MCP 工具操作游戏，完成玩家的指令。\n"
    "实时上下文按本会话的版本化增量协议解释；工具结果是执行后的最新事实，"
    "不要为了确认再重复查询。"
)
AGENT_GUIDANCE = (
    "用submit_plan提交一段可中断的连续行动；导航和不同类型的合法操作由运行时依序执行。"
    "remember_intent记录目标与待办，它们不是执行授权。已知条件允许时可组合多种操作；"
    "实际结果与预期不符时，回到模型决策调整。job-selected仅表示已选择，未执行成功；"
    "下一次输入lastResult是实际终态，信任它，不重复核查。\n"
    "自主执行，不要向玩家询问坐标或请求额外确认。不要读写代码文件或执行终端命令。"
)


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
        terminal_receipts = {}
        for line in prompt.splitlines():
            if line.startswith("终态事实："):
                terminal = json.loads(line.split("：", 1)[1])
                for event in terminal.get("terminalFacts", []):
                    if isinstance(event, dict) and isinstance(event.get("feedback"), dict) and event.get("commandId"):
                        terminal_receipts[event["commandId"]] = event["feedback"]
        blocks: dict[str, Any] = {}
        lines = []
        for line in prompt.splitlines():
            if line.startswith("实时上下文："):
                data = json.loads(line.split("：", 1)[1])
                # Long ledgers remain available through tools; only relevant
                # terminal events are delivered in the dedicated result block.
                data.pop("confirmedRequestTotals", None)
                data.pop("requestHistory", None)
                last = data.get("lastResult")
                if isinstance(last, dict) and last.get("commandId") in terminal_receipts:
                    receipt = terminal_receipts[last["commandId"]]
                    anchors = {"commandId", "operation", "status", "reasonCode", "actualSummary"}
                    # Only remove exact duplicates already delivered in this
                    # same request's terminal receipt. Unique facts remain here.
                    concise = {k: v for k, v in last.items()
                               if k in anchors or k not in receipt or receipt[k] != v}
                    if len(concise) < len(last):
                        concise["receiptReference"] = {"block": "terminalFacts", "commandId": last["commandId"]}
                        data["lastResult"] = concise
                continuity = data.get("continuity")
                current = data.get("currentTask")
                if (isinstance(continuity, dict) and isinstance(current, dict)
                        and isinstance(continuity.get("failedBranches"), list)
                        and continuity["failedBranches"] == current.get("blockedBranches")):
                    continuity.pop("failedBranches")
                    continuity["failedBranchesSource"] = "currentTask.blockedBranches"
                blocks["live"] = data
                previous = old.get("blocks", {}).get("live", {}) if continuing else {}
                changed = {k: v for k, v in data.items() if k not in previous or previous[k] != v}
                removed = [k for k in previous if k not in data]
                patches = []
                removals = []
                splices = []
                def changed_value(before, after, path, patches=patches, splices=splices):
                    if isinstance(before, dict) and isinstance(after, dict):
                        difference(before, after, path)
                    elif isinstance(before, list) and isinstance(after, list) and max(len(before), len(after)) <= 256:
                        # Lists such as nearby objects and rolling intent/memory
                        # records otherwise resend all unchanged entries. Apply
                        # edits from the tail so earlier indices stay valid.
                        def signature(value):
                            return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                        edits = [{"path": path, "start": i1, "deleteCount": i2 - i1, "items": after[j1:j2]}
                                 for kind, i1, i2, j1, j2 in reversed(SequenceMatcher(
                                     a=[signature(value) for value in before],
                                     b=[signature(value) for value in after], autojunk=False).get_opcodes())
                                 if kind != "equal"]
                        replacement = {"path": path, "value": after}
                        if len(json.dumps(edits, ensure_ascii=False)) < len(json.dumps(replacement, ensure_ascii=False)):
                            splices.extend(edits)
                        else:
                            patches.append(replacement)
                    else:
                        patches.append({"path": path, "value": after})
                def difference(before, after, path, patches=patches, removals=removals):
                    for name, value in after.items():
                        if name not in before:
                            patches.append({"path": path + [name], "value": value})
                        elif before[name] != value:
                            changed_value(before[name], value, path + [name])
                    removals.extend(path + [name] for name in before if name not in after)
                if continuing:
                    for name in list(changed):
                        if (isinstance(previous.get(name), dict) and isinstance(data[name], dict)
                                or isinstance(previous.get(name), list) and isinstance(data[name], list)):
                            changed_value(previous[name], data[name], [name])
                            changed.pop(name)
                frame = {"kind": "delta" if continuing else "snapshot", "baseVersion": old.get("version", 0) if continuing else 0,
                         "version": old.get("version", 0) + 1, "replaceFields": changed,
                         "removeFields": removed, "replacePaths": patches, "removePaths": removals}
                if splices:
                    frame["spliceListPaths"] = splices
                if not continuing and data.get("actorMode") == "player":
                    frame["environment"] = {"application": "Stardew Valley", "interface": "SMAPI",
                                            "domain": "video-game", "actor": "saved-game Game1.player"}
                lines.append("实时上下文：" + json.dumps(frame, ensure_ascii=False, separators=(",", ":")))
            else:
                lines.append(line)
        sent = "\n".join(lines)
        # Only trusted prompt scaffolding is eligible for removal. Never dedupe
        # player text, receipts or per-turn authority/pausing directives.
        prefix, separator, player_text = sent.partition("玩家指令：")
        static = dict(old.get("static", {})) if continuing else {}
        for name, content in {"agentIntro": AGENT_INTRO, "agentGuidance": AGENT_GUIDANCE,
                              "rules": rules}.items():
            if not content or content not in prefix:
                continue
            digest = hashlib.sha256(content.encode()).hexdigest()
            if static.get(name) == digest or (name == "rules" and continuing and old.get("rules") == digest):
                prefix = prefix.replace(content, "", 1)
            static[name] = digest
        sent = prefix + separator + player_text
        additions = []
        for name, content in {"deltaProtocol": DELTA_PROTOCOL, **(context or {})}.items():
            digest = hashlib.sha256(content.encode()).hexdigest()
            if static.get(name) != digest:
                additions.append(content)
            static[name] = digest
        sent = "\n".join([*additions, sent.strip()])
        pending = {"session": session, "blocks": blocks,
                   "rules": static.get("rules", old.get("rules") if continuing else None),
                   "static": static,
                   "version": old.get("version", 0) + 1, "originalPromptBytes": len(prompt.encode()),
                   "newPromptBytes": len(sent.encode())}
        return sent, pending

    def acknowledge(self, key: str, pending: dict[str, Any], session: str) -> None:
        self.state[key] = {**pending, "session": session}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def invalidate_session(self, key: str, session: str) -> None:
        """Native compaction can remove the acknowledged delta base and rules."""
        old = self.state.get(key, {})
        if old.get("session") != session:
            return
        self.state[key] = {"version": old.get("version", 0), "session": None}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)
