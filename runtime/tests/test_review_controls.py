"""Review regressions at the human chat / durable work / native control boundary."""
import asyncio
import json
import os
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock

import pytest

from stardew_ai_runtime import agent_instructions
from stardew_ai_runtime.autonomy import AutonomyController
from stardew_ai_runtime.chat_bridge import ChatBridge
from stardew_ai_runtime.mcp_server import LIFE_TOOLS, create_mcp_server
from stardew_ai_runtime.protocol import Envelope


def test_explicit_resume_reconsiders_interrupted_world_once_after_restart(tmp_path):
    path = tmp_path / "autonomy.json"
    ctl = AutonomyController(path)
    ctl.set_mode("farm", "free")
    snapshot = {"world": {"year": 1, "season": "summer", "dayOfMonth": 26}}
    candidate = ctl.next_candidate("farm", snapshot)
    fingerprint = ctl.fingerprint("farm", snapshot, candidate, ctl.state("farm"))
    assert ctl.record_world_event("farm", fingerprint, 10)
    ctl.control("farm", "pause")  # Model was interrupted after consuming the event.
    restored = AutonomyController(path)
    assert restored.next_candidate("farm", snapshot) is None
    restored.control("farm", "resume")
    candidate = restored.next_candidate("farm", snapshot)
    assert candidate is not None
    fingerprint = restored.fingerprint("farm", snapshot, candidate, restored.state("farm"))
    assert restored.record_world_event("farm", fingerprint, 10)
    assert not restored.record_world_event("farm", fingerprint, 10)
    assert restored.next_candidate("farm", snapshot) is None

SAVE = "review-save"
ROOT = "review-work-root"


class FakeNativeClient:
    """Records operations without opening a game socket."""

    def __init__(self):
        self.operations = []

    async def close(self):
        pass

    async def current_save_id(self):
        return SAVE

    async def call_tool(self, name, params):
        self.operations.append((name, deepcopy(params)))
        return {"status": "completed", "effects": [{"kind": "cancelled"}]}


def work_bridge(tmp_path, *, paused=True, mode="free", finished=False):
    native = FakeNativeClient()
    bridge = ChatBridge(run_dir=tmp_path, backend="fake", internal_plan_client=native)
    store = bridge._work_store
    selected = store.add_goal(SAVE, "照料鸡群", source="user")
    unrelated = store.add_goal(SAVE, "明天浇水", source="user")
    trigger = {"type": "calendar", "year": 1, "season": "spring", "day": 2}
    selected_todo = store.add_todo(SAVE, intent="继续照料", trigger=trigger, goal_id=selected.id)
    other_todo = store.add_todo(SAVE, intent="浇水", trigger=trigger, goal_id=unrelated.id)
    store.begin_decision(SAVE, "existing-work-token", goal_scope=selected.id)
    store.submit_plan(SAVE, goal_id=selected.id, decision_token="existing-work-token", tasks=[{
        "id": "animal-job", "title": "抚摸鸡", "steps": [{
            "id": "pet-step", "operation": "pet_animal", "params": {"animal_id": 77},
        }],
    }])
    if finished:
        store.finish_job(SAVE, {"status": "completed"}, task_id="animal-job")
    bridge._autonomy.set_mode(SAVE, mode)
    bridge._autonomy.set_goal_scope(SAVE, selected.id)
    bridge._autonomy.set_preferences(SAVE, goal="照料鸡群")
    bridge._autonomy.set_paused(SAVE, paused)
    store.set_paused(SAVE, paused)
    bridge._register_command_chain(SAVE, "照料鸡群", None, ROOT)
    bridge._command_chains[SAVE].waiting_task_id = "animal-job"
    bridge._job_reply_binding = (ROOT, SAVE, "animal-job", None, ROOT)
    return bridge, native, selected.id, unrelated.id, selected_todo.id, other_todo.id


def replies(ws):
    return [json.loads(call.args[0]) for call in ws.send_text.call_args_list]


@pytest.mark.parametrize("paused", [False, True])
@pytest.mark.parametrize("mode", ["command", "free"])
def test_explicit_cancel_preserves_mode_and_pause_and_only_cancels_selected_goal(tmp_path, paused, mode):
    async def run():
        bridge, native, selected, other, todo, other_todo = work_bridge(tmp_path, paused=paused, mode=mode)
        # A genuinely dispatched job must receive a native cancellation, while
        # its unconfirmed per-step outcome remains reconcilable in WorkStore.
        def dispatched(state):
            state.tasks[0].status = "running"
            state.tasks[0].steps[0].status = "running"
            state.tasks[0].steps[0].command_id = "native-existing-pet"
        bridge._work_store._mutate(SAVE, dispatched)
        before = bridge._autonomy.state(SAVE)
        ws = AsyncMock()
        await bridge._handle_autonomy_control(ws, Envelope.create_autonomy_control(
            "test-ui", "cancel-request", SAVE, "cancel", commandId=ROOT,
        ), SAVE)
        state = bridge._autonomy.state(SAVE)
        assert (state.mode, state.enabled, state.paused) == (before.mode, before.enabled, paused)
        assert state.goal == "" and state.goal_scope is None and not state.pending
        work = bridge._work_store.state(SAVE)
        assert work.paused is paused
        assert {g.id: g.status for g in work.goals} == {selected: "cancelled", other: "active"}
        assert {t.id: t.status for t in work.todos} == {todo: "cancelled", other_todo: "pending"}
        assert work.tasks[0].status == "cancelled"
        assert work.tasks[0].steps[0].command_id == "native-existing-pet"
        assert not work.decision and SAVE not in bridge._command_chains
        assert [p["operation"] for name, p in native.operations if name == "dispatch_plan_operation"] == ["cancel_task"]
        ack = replies(ws)[-1]
        assert ack["messageType"] == "autonomy.state"
        assert ack["payload"]["requestId"] == "cancel-request"
        assert ack["payload"]["status"] == "confirmed"
    asyncio.run(run())


@pytest.mark.parametrize("text", ["今天鸡群怎么样？", "继续讲讲"])
@pytest.mark.parametrize("finished", [False, True])
def test_paused_chat_uses_life_surface_and_preserves_existing_work(tmp_path, monkeypatch, text, finished):
    monkeypatch.setattr("stardew_ai_runtime.compatibility.assert_native_compatible", lambda _: None)
    monkeypatch.setenv("STARDEW_DECISION_TOKEN", "outer-work-token")
    monkeypatch.setenv("STARDEW_MCP_SURFACE", "full")

    async def run():
        bridge, native, *_ = work_bridge(tmp_path, finished=finished)
        work_before = deepcopy(bridge._work_store.state(SAVE))
        chain_before = deepcopy(bridge._command_chains[SAVE])
        binding_before = bridge._job_reply_binding
        observed = []

        class FakeProvider:
            def run(self, task, cid, prompt):
                assert "STARDEW_DECISION_TOKEN" not in os.environ
                assert os.environ["STARDEW_MCP_SURFACE"] == "life"
                assert os.environ["STARDEW_LIFE_MODE"] == "chat"
                async def tools():
                    server = create_mcp_server(scheduler=MagicMock(), surface="full")
                    return {tool.name for tool in await server.list_tools()}
                names = asyncio.run(tools())
                assert names == LIFE_TOOLS
                assert not {"submit_plan", "dispatch_plan_operation", "resume_task", "cancel_task"} & names
                observed.append(task.request_id)
                return {"success": True, "response": "鸡群照料进度保留，工作仍暂停。"}

        bridge._backend = FakeProvider()
        ws = AsyncMock()
        await bridge.handle_chat_submit(ws, "human-readonly", text, SAVE)
        assert observed == ["human-readonly"]
        assert bridge._work_store.state(SAVE) == work_before
        assert bridge._autonomy.state(SAVE).paused is True
        assert bridge._command_chains[SAVE] == chain_before
        assert bridge._job_reply_binding == binding_before
        assert native.operations == []
        final = replies(ws)[-1]["payload"]
        assert final["status"] == "completed"
        assert final["requestId"] == "human-readonly"
        assert final["commandId"] == "human-readonly" and final["commandComplete"] is True
        assert final["readOnly"] is True
        assert all(r["payload"]["readOnly"] is True for r in replies(ws) if r["messageType"] == "chat.reply")
        assert not final.get("resumeWorkRequested", False)
        assert os.environ["STARDEW_DECISION_TOKEN"] == "outer-work-token"
        assert os.environ["STARDEW_MCP_SURFACE"] == "full"
    asyncio.run(run())


def test_exact_continue_applies_resume_and_rejects_stale_controls(tmp_path):
    async def run():
        bridge, native, *_ = work_bridge(tmp_path)
        bridge._backend = MagicMock()
        chain_before = deepcopy(bridge._command_chains[SAVE])
        ws = AsyncMock()
        await bridge.handle_chat_submit(ws, "human-resume", "继续工作", SAVE)
        bridge._backend.run.assert_not_called()
        final = replies(ws)[-1]["payload"]
        assert final["requestId"] == "human-resume" and final["status"] == "completed"
        assert "resumeWorkRequested" not in final
        assert final["commandId"] == "human-resume"
        assert bridge._autonomy.state(SAVE).paused is False
        assert bridge._work_store.state(SAVE).paused is False
        assert bridge._command_chains[SAVE] == chain_before
        assert native.operations == []
        # A control for another save cannot serve as this resume acknowledgement.
        await bridge._handle_autonomy_control(ws, Envelope.create_autonomy_control(
            "test-ui", "wrong-save-resume", "other-save", "resume", commandId=ROOT,
        ), SAVE)
        assert bridge._work_store.state(SAVE).paused is False
        await bridge._handle_autonomy_control(ws, Envelope.create_autonomy_control(
            "test-ui", "stale-command-resume", SAVE, "resume", commandId="older-work-root",
        ), SAVE)
        rejected = replies(ws)[-1]["payload"]
        assert rejected["status"] == "rejected" and "COMMAND_MISMATCH" in rejected["reason"]
        assert bridge._autonomy.state(SAVE).paused is False
        assert bridge._work_store.state(SAVE).paused is False
        await bridge._handle_autonomy_control(ws, Envelope.create_autonomy_control(
            "test-ui", "resume-control", SAVE, "resume", commandId=ROOT,
        ), SAVE)
        assert bridge._autonomy.state(SAVE).paused is False
        assert bridge._work_store.state(SAVE).paused is False
        ack = replies(ws)[-1]
        assert ack["messageType"] == "autonomy.state"
        assert ack["payload"]["requestId"] == "resume-control"
        assert ack["payload"]["status"] == "confirmed"
        assert native.operations == []
    asyncio.run(run())


@pytest.mark.parametrize("backend", ["codex", "kimi", "agy"])
def test_autonomous_submit_injects_current_core_once_for_each_backend(tmp_path, monkeypatch, backend):
    """Exercise the real turn entry point, not only a prompt formatter."""
    monkeypatch.setattr("stardew_ai_runtime.compatibility.assert_native_compatible", lambda _: None)
    # The fake provider emits no provider-owned meter files or database rows.
    for name, value in {"get_max_gen_idx": -1, "get_max_step_idx": -1,
                        "wire_offset": 0, "read_usage_since": None}.items():
        monkeypatch.setattr(f"stardew_ai_runtime.chat_bridge.{name}", lambda *a, _value=value, **k: _value)

    async def run():
        bridge = ChatBridge(run_dir=tmp_path, backend=backend, enable_plan_worker=False)
        bridge._autonomy.set_mode(SAVE, "free")
        request_id = "autonomy-review-core"
        bridge._autonomy_requests[request_id] = "review-core-fingerprint"
        bundle = agent_instructions.load_instruction_bundle()
        seen = []

        class FakeProvider:
            def run(self, task, cid, prompt):
                seen.append(prompt)
                assert task.instructions_text == bundle.text
                assert prompt.count(bundle.text) == 1
                assert "remainingBudget" not in prompt
                assert prompt.count("主动汇报阶段成果、实质取舍和阻塞，每次不超过三句") == 1
                assert "接续、等待与完成" in prompt
                assert os.environ.get("STARDEW_DECISION_TOKEN") == task.decision_token
                assert bridge._work_store.state(SAVE).decision["token"] == task.decision_token
                return {"success": True, "response": "等待加工完成，之后继续。"}

        bridge._backend = FakeProvider()
        ws = AsyncMock()
        sent = AsyncMock(wraps=bridge._send_reply)
        monkeypatch.setattr(bridge, "_send_reply", sent)
        await bridge.handle_chat_submit(ws, request_id, "按当前目标推进，保留等待条件。", SAVE)
        assert len(seen) == 1
        # Successful autonomous chatter is intentionally suppressed at transport;
        # inspect the real reply before that quiet-mode delivery boundary.
        final = [call.args[1].payload for call in sent.await_args_list
                 if call.args[1].message_type == "chat.reply"][-1]
        assert final["requestId"] == request_id
        assert final["status"] == "decision-completed"
        assert not bridge._autonomy.state(SAVE).paused
    asyncio.run(run())


def test_document_edit_updates_bundle_and_session_fingerprint_in_same_process(tmp_path, monkeypatch):
    source_root = agent_instructions.ROOT
    instruction_root = tmp_path / "instructions"
    for relative in agent_instructions.INSTRUCTION_FILES:
        destination = instruction_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((source_root / relative).read_bytes())
    monkeypatch.setattr(agent_instructions, "ROOT", instruction_root)
    bridge = ChatBridge(run_dir=tmp_path / "run", backend="fake", enable_plan_worker=False)
    first = agent_instructions.load_instruction_bundle()
    first_fingerprint = bridge.profile_fingerprint(SAVE)
    assert first_fingerprint == bridge.profile_fingerprint(SAVE, instruction_revision=first.revision)
    core_path = instruction_root / agent_instructions.CORE_PATH
    marker = "同进程资料修订回归标记：照料前核对剩余动物。"
    core_path.write_bytes(core_path.read_bytes() + ("\n\n" + marker).encode("utf-8"))
    second = agent_instructions.load_instruction_bundle()
    assert marker not in first.text and second.text.count(marker) == 1
    assert first.revision != second.revision
    assert agent_instructions.runtime_instructions() == second.text
    assert agent_instructions.instructions_revision() == second.revision
    second_fingerprint = bridge.profile_fingerprint(SAVE)
    assert second_fingerprint != first_fingerprint
    assert second_fingerprint == bridge.profile_fingerprint(SAVE, instruction_revision=second.revision)
