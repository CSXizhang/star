"""Read-only identity checks for MiniMax Code 0.6.2's local v2 projection.

The provider's columnar session decoder requires version 3. Conversation execution
uses the exact persisted owner, but silently falls back to the primary agent when
that owner is gone. Custom owners are defined by agents/<name>/agent.md, not by
the legacy agents index alone. Task sessions use their frozen v2 definition.
No message/history table is read; role text is only hashed in memory.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import yaml


class IdentityVerificationError(RuntimeError):
    """The original provider identity cannot be safely resumed."""


def _fail(message: str) -> None:
    raise IdentityVerificationError(message)


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _provider_version(binding: dict) -> str:
    command = binding.get("command", "mcode")
    if not isinstance(command, str) or not command.strip():
        _fail("原客户端命令无效，请重新连接。")
    executable = shutil.which(command)
    if not executable:
        _fail("找不到原 MiniMax Code 客户端，无法核对伙伴身份。")
    path = Path(executable).resolve()
    candidates = [path.parent / "package.json",
                  Path(executable).parent / "node_modules/@minimax-ai/code/package.json"]
    for candidate in candidates:
        if not candidate.is_file():
            continue
        package = json.loads(candidate.read_text(encoding="utf-8"))
        if not isinstance(package, dict):
            _fail("原客户端版本元数据无效，无法核对伙伴身份。")
        if package.get("name") != "@minimax-ai/code":
            continue
        if package.get("version") != "0.6.2":
            _fail("尚不支持此 MiniMax Code 版本的身份校验；请等待适配，原会话未启动。")
        return package["version"]
    _fail("无法确认原客户端版本，原会话未启动。")


def _path(value: Any) -> Path:
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        _fail("原会话目录信息不完整，请从原客户端重新连接。")
    return Path(value).resolve()


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail("原会话的身份字段不完整，无法安全继续。")
    return value


def _file_bytes(path: Path, root: Path, *, optional: bool = False) -> bytes | None:
    if not path.exists():
        if optional:
            return None
        _fail("原伙伴角色定义已不存在；不会使用默认角色替代，请在原客户端恢复后重连。")
    # Match the provider's canonical-file safety rule; never follow a role link.
    current = path
    while current != root:
        if current.is_symlink() or not current.is_relative_to(root):
            _fail("原角色定义路径不受支持，请在原客户端修复后重连。")
        current = current.parent
    if not path.is_file() or path.stat().st_size > 1024 * 1024:
        _fail("原角色定义格式不受支持，无法核对身份。")
    with path.open("rb") as stream:
        contents = stream.read(1024 * 1024 + 1)
    if len(contents) > 1024 * 1024:
        _fail("原角色定义过大，无法核对身份。")
    return contents


def _conversation_owner(connection: sqlite3.Connection, root: Path, agent: str) -> dict:
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", agent):
        _fail("原角色名称格式不受支持，无法核对身份。")
    row = connection.execute(
        "SELECT creation_source,agent_role,framework_type FROM agents WHERE agent_name=?",
        (agent,)).fetchone()
    builtin = row is not None and row[0] == "builtin"
    if agent in {"main", "mavis"}:
        family = connection.execute(
            "SELECT agent_name,creation_source FROM agents WHERE agent_name IN ('main','mavis')"
        ).fetchall()
        if any(source != "builtin" for _, source in family):
            _fail("原客户端的主角色归属存在冲突，请在原客户端修复后重连。")
        if agent == "main" and any(name == "mavis" for name, _ in family):
            _fail("原客户端会将此旧角色转交另一主角色；请明确确认迁移后重新连接。")
        if not builtin:
            _fail("原伙伴主角色已不存在；不会使用默认角色替代。")
    directory = root / "agents" / (".builtin" if builtin else "") / agent
    content = _file_bytes(directory / "agent.md", root, optional=builtin)
    if content is not None:
        decoded = content.decode("utf-8-sig")
        match = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", decoded, re.S)
        if not match:
            _fail("原角色仍是旧版配置或定义已损坏，请先在原客户端修复。")
        fields = yaml.safe_load(match[1])
        if not isinstance(fields, dict) or fields.get("name") != agent or not isinstance(fields.get("description"), str):
            _fail("原角色定义与其归属不符，请在原客户端修复后重连。")
    # A custom agent can exist without an index row; the canonical file is the
    # provider's authority. Do not treat a stale index row as a live definition.
    fingerprint = _hash({"kind": "builtin" if builtin else "custom", "agent": agent,
        "builtinRow": list(row) if builtin else None,
        "instance": _hash((_file_bytes(directory / ".agent-instance-id", root, optional=True) or b"").hex()) if not builtin else None,
        "document": hashlib.sha256(content).hexdigest() if content is not None else None})
    return {"ownerName": agent, "definitionFingerprint": fingerprint,
            "definitionSource": "builtin-agent" if builtin else "canonical-agent"}


def _task_owner(connection: sqlite3.Connection, session: str, cwd: Path) -> dict:
    row = connection.execute(
        "SELECT definition_json FROM local_runtime_session_agent_definitions WHERE session_id=?",
        (session,)).fetchone()
    if row is None:
        _fail("原任务会话缺少冻结的角色定义，无法作为原伙伴恢复。")
    definition = json.loads(row[0])
    if not isinstance(definition, dict) or definition.get("definitionVersion") != 2:
        _fail("尚不支持此任务会话的角色定义版本，请在原客户端完成迁移。")
    owner = _text(definition.get("exactOwnerName"))
    model, project, capabilities = (definition.get(key) for key in ("model", "project", "capabilities"))
    if (not isinstance(definition.get("systemPrompt"), str) or not isinstance(model, dict)
            or not isinstance(project, dict) or not isinstance(capabilities, dict)):
        _fail("原任务会话的角色定义不完整，无法安全继续。")
    _text(model.get("providerId"))
    _text(model.get("modelId"))
    if _path(project.get("workspaceDir")) != cwd or type(project.get("isDefaultWorkspace")) is not bool:
        _fail("原任务角色绑定的工作目录已改变，请从原客户端重新连接。")
    for key in ("tools", "mcpServers", "skills", "extensionSkills"):
        values = capabilities.get(key)
        if not isinstance(values, list) or not all(isinstance(value, str) and value.strip() for value in values):
            _fail("原任务角色能力定义不受支持，无法安全继续。")
    return {"ownerName": owner, "definitionFingerprint": _hash(definition),
            "definitionSource": "frozen-session-agent"}


def verify_session_identity(binding: dict) -> dict[str, Any]:
    """Return safe identity metadata; compare binding['identity'] when present.

    The caller stores the first successful result in the binding. Subsequent
    checks reject deleted/replaced roles and changed workspaces without opening
    a provider process. Unknown versions/schemas fail closed, never migrate.
    """
    try:
        session = _text(binding.get("providerSessionId"))
        cwd = _path(binding.get("cwd"))
        root = _path(str(binding.get("dataDir") or os.environ.get("MINIMAX_DATA_DIR")
                         or Path.home() / ".minimax"))
        version = _provider_version(binding)
        database = root / "v2/sqlite/runtime-state.sqlite"
        if not database.is_file():
            _fail("找不到原客户端的会话元数据，请确认原数据目录后重连。")
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1) as connection:
            connection.execute("PRAGMA query_only=ON")
            # Select only identity columns. record_json and message/history
            # tables are deliberately excluded, even if legacy JSON is stale.
            row = connection.execute(
                "SELECT columnar_version,agent_name,workspace_dir,session_kind "
                "FROM local_runtime_sessions WHERE session_id=?", (session,)).fetchone()
            if row is None:
                _fail("原会话已不存在；不会创建替代会话，请从原客户端重新连接。")
            columnar, agent, workspace, kind = row
            if columnar != 3 or kind not in {"conversation", "task"}:
                _fail("尚不支持此会话的元数据版本或类型，原会话未启动。")
            agent = _text(agent)
            if _path(workspace) != cwd:
                _fail("原会话工作目录与连接目录不一致，请从原客户端重新连接。")
            owner = (_task_owner(connection, session, cwd) if kind == "task"
                     else _conversation_owner(connection, root, agent))
        metadata = {"source": "mcode-local-v2", "providerVersion": version,
            "providerSessionId": session, "cwd": str(cwd), "sessionKind": kind,
            "agentName": agent, **owner}
        metadata["identityFingerprint"] = _hash({**metadata, "dataRoot": str(root)})
        expected = binding.get("identity")
        if expected is not None and (not isinstance(expected, dict)
                or expected.get("identityFingerprint") != metadata["identityFingerprint"]):
            _fail("原伙伴的角色定义或连接环境已改变；请在原客户端确认后重新连接。")
        return metadata
    except IdentityVerificationError:
        raise
    except (OSError, ValueError, TypeError, sqlite3.Error, yaml.YAMLError):
        # Never leak provider paths, role text, SQL data, or parser excerpts.
        raise IdentityVerificationError("无法只读核对原会话身份；元数据格式不受支持或暂不可读，请在原客户端检查后重连。") from None
