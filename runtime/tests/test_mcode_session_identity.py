import hashlib
import json
import sqlite3

import pytest

from stardew_ai_runtime.mcode_session_identity import (
    IdentityVerificationError,
    verify_session_identity,
)


@pytest.fixture
def original(tmp_path):
    workspace = tmp_path / "original-workspace"
    workspace.mkdir()
    data = tmp_path / "provider-data"
    db = data / "v2/sqlite/runtime-state.sqlite"
    db.parent.mkdir(parents=True)
    executable = tmp_path / "bin/mcode.cmd"
    executable.parent.mkdir()
    executable.write_text("@echo off\n", encoding="utf-8")
    package = executable.parent / "node_modules/@minimax-ai/code/package.json"
    package.parent.mkdir(parents=True)
    package.write_text(json.dumps({"name": "@minimax-ai/code", "version": "0.6.2"}))
    role = data / "agents/old-friend/agent.md"
    role.parent.mkdir(parents=True)
    role.write_text("---\nname: old-friend\ndescription: Original friend\n---\nPRIVATE ROLE TEXT", encoding="utf-8")
    with sqlite3.connect(db) as connection:
        connection.executescript("""
            CREATE TABLE local_runtime_sessions (
                session_id TEXT PRIMARY KEY, columnar_version INTEGER,
                agent_name TEXT, workspace_dir TEXT, session_kind TEXT, record_json TEXT);
            CREATE TABLE agents (agent_name TEXT PRIMARY KEY, creation_source TEXT,
                agent_role INTEGER, framework_type TEXT);
            CREATE TABLE local_runtime_session_agent_definitions (
                session_id TEXT PRIMARY KEY, definition_json TEXT);
            CREATE TABLE local_runtime_messages (private_history TEXT);
            INSERT INTO local_runtime_messages VALUES ('PRIVATE HISTORY');
            INSERT INTO agents VALUES ('old-friend','manual',1,'pi-agent');
        """)
        connection.execute("INSERT INTO local_runtime_sessions VALUES (?,?,?,?,?,?)",
            ("original-session", 3, "old-friend", str(workspace), "conversation",
             json.dumps({"agentName": "stale-wrong-owner", "private": "PRIVATE HISTORY"})))
    binding = {"adapter": "mcode-acp", "providerSessionId": "original-session",
               "cwd": str(workspace), "dataDir": str(data), "command": str(executable)}
    return binding, db, role, package


def test_original_identity_uses_authoritative_columns_without_history_or_writes(original, monkeypatch):
    binding, db, role, _ = original
    before = hashlib.sha256(db.read_bytes()).hexdigest(), role.read_bytes()
    connect = sqlite3.connect

    def restricted_connect(*args, **kwargs):
        assert "mode=ro" in args[0] and kwargs["uri"]
        connection = connect(*args, **kwargs)

        def authorize(action, table, column, *_):
            if action == sqlite3.SQLITE_READ and (table == "local_runtime_messages" or column == "record_json"):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorize)
        return connection

    monkeypatch.setattr(sqlite3, "connect", restricted_connect)
    identity = verify_session_identity(binding)
    assert identity["agentName"] == "old-friend"
    assert identity["providerSessionId"] == "original-session"
    assert "PRIVATE" not in json.dumps(identity)
    assert verify_session_identity({**binding, "identity": identity}) == identity
    assert before == (hashlib.sha256(db.read_bytes()).hexdigest(), role.read_bytes())


def test_deleted_custom_role_cannot_fall_back_to_builtin_even_with_stale_index(original):
    binding, db, role, _ = original
    binding["identity"] = verify_session_identity(binding)
    role.unlink()
    with sqlite3.connect(db) as connection:
        connection.execute("INSERT INTO agents VALUES ('mavis','builtin',0,'pi-agent')")
    with pytest.raises(IdentityVerificationError, match="默认角色"):
        verify_session_identity(binding)


def test_changed_definition_requires_explicit_rebinding(original):
    binding, _, role, _ = original
    binding["identity"] = verify_session_identity(binding)
    role.write_text(role.read_text() + "\nDIFFERENT PRIVATE PERSONALITY", encoding="utf-8")
    with pytest.raises(IdentityVerificationError, match="已改变") as error:
        verify_session_identity(binding)
    assert "PRIVATE" not in str(error.value)


def test_changed_session_workspace_and_missing_session_are_not_migrations(original, tmp_path):
    binding, db, _, _ = original
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE local_runtime_sessions SET workspace_dir=?", (str(tmp_path),))
    with pytest.raises(IdentityVerificationError, match="目录不一致"):
        verify_session_identity(binding)
    with sqlite3.connect(db) as connection:
        connection.execute("DELETE FROM local_runtime_sessions")
    with pytest.raises(IdentityVerificationError, match="原会话已不存在"):
        verify_session_identity(binding)


def test_unknown_provider_or_projection_versions_fail_closed(original):
    binding, db, _, package = original
    package.write_text(json.dumps({"name": "@minimax-ai/code", "version": "99.0.0"}))
    with pytest.raises(IdentityVerificationError, match="版本"):
        verify_session_identity(binding)
    package.write_text(json.dumps({"name": "@minimax-ai/code", "version": "0.6.2"}))
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE local_runtime_sessions SET columnar_version=4")
    with pytest.raises(IdentityVerificationError, match="版本"):
        verify_session_identity(binding)


def test_frozen_task_uses_original_definition_and_rejects_missing_or_old_definition(original):
    binding, db, role, _ = original
    role.unlink()  # Frozen task identity is self-contained, per the provider.
    definition = {"definitionVersion": 2, "exactOwnerName": "old-friend",
        "systemPrompt": "PRIVATE FROZEN ROLE", "model": {"providerId": "minimax", "modelId": "test"},
        "project": {"workspaceDir": binding["cwd"], "isDefaultWorkspace": False},
        "capabilities": {"tools": [], "mcpServers": [], "skills": [], "extensionSkills": []}}
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE local_runtime_sessions SET session_kind='task'")
        connection.execute("INSERT INTO local_runtime_session_agent_definitions VALUES (?,?)",
                           (binding["providerSessionId"], json.dumps(definition)))
    result = verify_session_identity(binding)
    assert result["definitionSource"] == "frozen-session-agent"
    assert "PRIVATE" not in json.dumps(result)
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE local_runtime_session_agent_definitions SET definition_json=?",
                           (json.dumps({**definition, "definitionVersion": 1}),))
    with pytest.raises(IdentityVerificationError, match="版本"):
        verify_session_identity(binding)
    with sqlite3.connect(db) as connection:
        connection.execute("DELETE FROM local_runtime_session_agent_definitions")
    with pytest.raises(IdentityVerificationError, match="缺少"):
        verify_session_identity(binding)


def test_legacy_primary_alias_does_not_silently_change_execution_owner(original):
    binding, db, _, _ = original
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE local_runtime_sessions SET agent_name='main'")
        connection.executemany("INSERT INTO agents VALUES (?,'builtin',0,'pi-agent')", [("main",), ("mavis",)])
    with pytest.raises(IdentityVerificationError, match="另一主角色"):
        verify_session_identity(binding)


def test_corrupt_role_reports_safe_error_without_parser_private_text(original):
    binding, _, role, _ = original
    role.write_text("---\nname: [PRIVATE INVALID ROLE\n---\n", encoding="utf-8")
    with pytest.raises(IdentityVerificationError) as error:
        verify_session_identity(binding)
    assert "PRIVATE" not in str(error.value)
