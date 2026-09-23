import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

from stardew_ai_runtime.autonomy import AutonomyController
from stardew_ai_runtime.chat_bridge import ChatBridge
from stardew_ai_runtime.plan_executor import StepExecution
from stardew_ai_runtime.protocol import Envelope


def test_consecutive_failures_trip_breaker_and_cooldown(tmp_path: Path):
    path = tmp_path / "autonomy.json"
    ctl = AutonomyController(path)
    ctl.set_enabled("save-1", True)
    snap = {"farmWork": {"cropUnwateredTiles": [{"x": 1, "y": 2}]}}

    candidate = ctl.next_candidate("save-1", snap, now=1000.0)
    assert candidate is not None
    fp1 = ctl.fingerprint("save-1", snap, candidate)
    ctl.record_world_event("save-1", fp1, 1)
    ctl.record_action_result("save-1", fp1, False, reason="LOCATION_MISMATCH", now=1000.0)
    assert ctl.state("save-1").failure_count == 1
    assert ctl.state("save-1").breaker_tripped is False
    assert ctl.is_cooling_down("save-1", now=1000.0) is False

    candidate = ctl.next_candidate("save-1", snap, now=1010.0)
    fp2 = ctl.fingerprint("save-1", snap, candidate)
    ctl.record_world_event("save-1", fp2, 2)
    ctl.record_action_result("save-1", fp2, False, reason="LOCATION_MISMATCH", now=1010.0)
    assert ctl.state("save-1").failure_count == 2
    assert ctl.state("save-1").breaker_tripped is False

    candidate = ctl.next_candidate("save-1", snap, now=1020.0)
    fp3 = ctl.fingerprint("save-1", snap, candidate)
    ctl.record_world_event("save-1", fp3, 3)
    ctl.record_action_result("save-1", fp3, False, reason="LOCATION_MISMATCH", now=1020.0)
    state = ctl.state("save-1")
    assert state.failure_count == 3
    assert state.breaker_tripped is True
    assert state.breaker_cooldown_until == 1320.0
    assert state.breaker_reason == "LOCATION_MISMATCH"
    assert ctl.is_cooling_down("save-1", now=1030.0) is True

    assert ctl.next_candidate("save-1", snap, now=1030.0) is None

    assert ctl.is_cooling_down("save-1", now=1321.0) is False
    probe_candidate = ctl.next_candidate("save-1", snap, now=1321.0)
    assert probe_candidate is not None

    probe_fp = ctl.fingerprint("save-1", snap, probe_candidate)
    ctl.record_world_event("save-1", probe_fp, 4)
    ctl.record_action_result("save-1", probe_fp, False, reason="STILL_BLOCKED", now=1322.0)
    state_after_probe = ctl.state("save-1")
    assert state_after_probe.failure_count == 4
    assert state_after_probe.breaker_tripped is True
    assert state_after_probe.breaker_cooldown_until == 1622.0
    assert state_after_probe.breaker_reason == "STILL_BLOCKED"
    assert ctl.is_cooling_down("save-1", now=1325.0) is True


def test_turn_and_job_success_resets_breaker(tmp_path: Path):
    path = tmp_path / "autonomy.json"
    ctl = AutonomyController(path)
    ctl.set_enabled("save-1", True)
    snap = {"farmWork": {"cropUnwateredTiles": [{"x": 1, "y": 2}]}}

    for i in range(3):
        candidate = ctl.next_candidate("save-1", snap, now=1000.0)
        fp = ctl.fingerprint("save-1", snap, candidate)
        ctl.record_world_event("save-1", fp, i + 1)
        ctl.record_action_result("save-1", fp, False, reason="ERR", now=1000.0)

    assert ctl.state("save-1").breaker_tripped is True

    probe_candidate = ctl.next_candidate("save-1", snap, now=1000.0)
    assert probe_candidate is None

    success_candidate = {"kind": "water", "tiles": [{"x": 1, "y": 2}]}
    success_fp = ctl.fingerprint("save-1", snap, success_candidate)
    ctl.record_world_event("save-1", success_fp, 10)
    ctl.record_action_result("save-1", success_fp, True, now=1350.0)

    recovered = ctl.state("save-1")
    assert recovered.failure_count == 0
    assert recovered.breaker_tripped is False
    assert recovered.breaker_cooldown_until is None
    assert recovered.breaker_reason is None


def test_standby_decision_does_not_count_as_failure(tmp_path: Path):
    bridge = ChatBridge(run_dir=tmp_path)
    bridge._autonomy.set_enabled("save-1", True)
    bridge._autonomy.record_world_event("save-1", "fp-1", 1)
    bridge._autonomy.record_action_result("save-1", "fp-1", False, reason="OLD_FAIL")
    assert bridge._autonomy.state("save-1").failure_count == 1

    bridge._autonomy.record_world_event("save-1", "fp-standby", 2)
    bridge._autonomy_requests["autonomy-req-1"] = "fp-standby"

    with patch.object(bridge, "_send_reply", new=AsyncMock()):
        with patch.object(bridge, "_execute_turn", return_value={"success": True, "response": "待命，无工作。", "status": "completed"}):
            asyncio.run(bridge.handle_chat_submit(None, "autonomy-req-1", "待命", "save-1"))

    state = bridge._autonomy.state("save-1")
    assert state.failure_count == 0
    assert state.breaker_tripped is False


def test_job_level_failure_via_work_state(tmp_path: Path):
    bridge = ChatBridge(run_dir=tmp_path)
    bridge._autonomy.set_enabled("save-1", True)
    save_id = "save-1"

    bridge._work_store.begin_decision(save_id, "tok-1")
    goal = bridge._work_store.add_goal(save_id, "浇水", source="user")
    bridge._work_store.submit_plan(
        save_id,
        tasks=[
            {
                "id": "t1",
                "title": "浇水",
                "completionCondition": "浇完",
                "dependencies": [],
                "steps": [{"id": "s1", "operation": "water_auto", "params": {"max_tiles": 5}}],
            }
        ],
        goal_id=goal.id,
        decision_token="tok-1",
    )
    bridge._work_store.finish_job(
        save_id,
        {"status": "partial", "reasonCode": "LOCATION_MISMATCH"},
        task_id="t1",
    )

    bridge._autonomy.record_world_event(save_id, "fp-fail-1", 1)
    bridge._autonomy_requests["autonomy-req-2"] = "fp-fail-1"

    with patch.object(bridge, "_send_reply", new=AsyncMock()):
        with patch.object(bridge, "_execute_turn", return_value={"success": True, "response": "执行浇水", "status": "completed"}):
            asyncio.run(bridge.handle_chat_submit(None, "autonomy-req-2", "执行", save_id))

    state = bridge._autonomy.state(save_id)
    assert state.failure_count == 1
    assert state.breaker_reason == "LOCATION_MISMATCH"


def test_on_job_terminal_records_async_failure(tmp_path: Path):
    bridge = ChatBridge(run_dir=tmp_path)
    bridge._autonomy.set_enabled("save-1", True)
    save_id = "save-1"

    bridge._autonomy.record_world_event(save_id, "fp-async-1", 1)
    bridge._autonomy_pending_task_fingerprints["t-async-1"] = (save_id, "fp-async-1")

    execution = StepExecution(
        status="executed",
        operation="water_auto",
        outcome="partial",
        task_status="partial",
        task_id="t-async-1",
        step_id="s1",
        reason_code="LOCATION_MISMATCH",
    )
    asyncio.run(bridge._on_job_terminal(save_id, execution, "SHORT_JOB_TERMINAL"))

    state = bridge._autonomy.state(save_id)
    assert state.failure_count == 1
    assert state.breaker_reason == "LOCATION_MISMATCH"


def test_breaker_reset_on_player_chat_control_and_day_advance(tmp_path: Path):
    path = tmp_path / "autonomy.json"
    ctl = AutonomyController(path)
    ctl.set_enabled("save-1", True)
    snap = {"farmWork": {"cropUnwateredTiles": [{"x": 1, "y": 2}]}}

    for i in range(3):
        candidate = ctl.next_candidate("save-1", snap, now=100.0)
        fp = ctl.fingerprint("save-1", snap, candidate)
        ctl.record_world_event("save-1", fp, i + 1)
        ctl.record_action_result("save-1", fp, False, reason="ERR", now=100.0)

    assert ctl.state("save-1").breaker_tripped is True

    ctl.control("save-1", "pause")
    assert ctl.state("save-1").breaker_tripped is False
    assert ctl.state("save-1").failure_count == 0

    ctl.control("save-1", "resume")
    for i in range(3):
        candidate = ctl.next_candidate("save-1", snap, now=100.0)
        fp = ctl.fingerprint("save-1", snap, candidate)
        ctl.record_world_event("save-1", fp, i + 10)
        ctl.record_action_result("save-1", fp, False, reason="ERR", now=100.0)
    assert ctl.state("save-1").breaker_tripped is True

    ctl.control("save-1", "set_mode", mode="command")
    assert ctl.state("save-1").breaker_tripped is False
    assert ctl.state("save-1").failure_count == 0

    ctl.set_mode("save-1", "free")
    for i in range(3):
        candidate = ctl.next_candidate("save-1", snap, now=100.0)
        fp = ctl.fingerprint("save-1", snap, candidate)
        ctl.record_world_event("save-1", fp, i + 20)
        ctl.record_action_result("save-1", fp, False, reason="ERR", now=100.0)
    assert ctl.state("save-1").breaker_tripped is True

    ctl.on_day_started("save-1", 5, game_date="1:spring:5")
    assert ctl.state("save-1").breaker_tripped is False
    assert ctl.state("save-1").failure_count == 0

    bridge = ChatBridge(run_dir=tmp_path)
    bridge._autonomy.set_enabled("save-1", True)
    bridge._autonomy.record_world_event("save-1", "fp-tripped", 1)
    for _ in range(3):
        bridge._autonomy.record_action_result("save-1", "fp-tripped", False, reason="ERR")
    assert bridge._autonomy.state("save-1").breaker_tripped is True

    with patch.object(bridge, "_send_reply", new=AsyncMock()):
        with patch.object(bridge, "_execute_turn", return_value={"success": True, "response": "你好", "status": "completed"}):
            asyncio.run(bridge.handle_chat_submit(None, "player-req-1", "今天种防风草", "save-1"))

    assert bridge._autonomy.state("save-1").breaker_tripped is False
    assert bridge._autonomy.state("save-1").failure_count == 0


def test_breaker_state_persistence_roundtrip(tmp_path: Path):
    path = tmp_path / "data" / "autonomy-state.json"
    ctl1 = AutonomyController(path)
    ctl1.set_enabled("save-1", True)
    ctl1.record_world_event("save-1", "fp-persist-1", 1)
    ctl1.record_action_result("save-1", "fp-persist-1", False, reason="LOCATION_MISMATCH", now=2000.0)
    ctl1.record_world_event("save-1", "fp-persist-2", 2)
    ctl1.record_action_result("save-1", "fp-persist-2", False, reason="LOCATION_MISMATCH", now=2001.0)
    ctl1.record_world_event("save-1", "fp-persist-3", 3)
    ctl1.record_action_result("save-1", "fp-persist-3", False, reason="LOCATION_MISMATCH", now=2002.0)

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["save-1"]["breakerTripped"] is True
    assert raw["save-1"]["breakerCooldownUntil"] == 2302.0
    assert raw["save-1"]["breakerReason"] == "LOCATION_MISMATCH"
    assert raw["save-1"]["failureCount"] == 3

    ctl2 = AutonomyController(path)
    s = ctl2.state("save-1")
    assert s.breaker_tripped is True
    assert s.breaker_cooldown_until == 2302.0
    assert s.breaker_reason == "LOCATION_MISMATCH"
    assert s.failure_count == 3


def test_f8_payload_exposes_breaker_cooldown_status_line(tmp_path: Path):
    bridge = ChatBridge(run_dir=tmp_path)
    bridge._autonomy.set_enabled("save-1", True)
    now = time.time()
    bridge._autonomy.record_world_event("save-1", "fp-f8-1", 1)
    bridge._autonomy.record_action_result("save-1", "fp-f8-1", False, reason="LOCATION_MISMATCH", now=now)
    bridge._autonomy.record_world_event("save-1", "fp-f8-2", 2)
    bridge._autonomy.record_action_result("save-1", "fp-f8-2", False, reason="LOCATION_MISMATCH", now=now)
    bridge._autonomy.record_world_event("save-1", "fp-f8-3", 3)
    bridge._autonomy.record_action_result("save-1", "fp-f8-3", False, reason="LOCATION_MISMATCH", now=now)

    payload = bridge._autonomy_state_payload("save-1", bridge._autonomy.state("save-1"))
    assert payload["breakerTripped"] is True
    assert payload["failureCount"] == 3
    assert payload["planWaitReason"] == "自动模式已熔断冷却"
    assert payload["breakerReason"] == "LOCATION_MISMATCH"


def test_scheduling_blocked_during_cooldown(tmp_path: Path):
    bridge = ChatBridge(run_dir=tmp_path)
    bridge._autonomy.set_enabled("save-1", True)
    bridge._autonomy.record_world_event("save-1", "fp-1", 1)
    for _ in range(3):
        bridge._autonomy.record_action_result("save-1", "fp-1", False, reason="LOCATION_MISMATCH")

    envelope = Envelope.from_mapping({
        "protocolVersion": "0.1",
        "messageType": "world.snapshot",
        "messageId": "msg-snap-1",
        "senderInstanceId": "mod",
        "sequenceNumber": 1,
        "worldRevision": 1,
        "sentAt": "2026-01-01T00:00:00Z",
        "saveId": "save-1",
        "gameSessionId": "session-1",
        "payload": {
            "world": {"season": "spring", "dayOfMonth": 1, "year": 1},
            "farmWork": {"cropUnwateredTiles": [{"x": 5, "y": 5}]},
        },
    })

    tasks = set()
    asyncio.run(bridge._maybe_schedule_autonomy(envelope, "save-1", tasks, None, force=True))
    assert len(tasks) == 0
    assert len(bridge._autonomy_requests) == 0
