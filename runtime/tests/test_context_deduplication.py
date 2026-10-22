"""Model prompts keep one current resource projection and one native receipt."""

import copy
import json

import pytest

from stardew_ai_runtime.chat_bridge import ChatBridge
from stardew_ai_runtime.decision_context import build_decision_context
from stardew_ai_runtime.life_chat import LifeChatService
from stardew_ai_runtime.protocol import Envelope


@pytest.fixture
def bridge(tmp_path):
    instance = ChatBridge(run_dir=tmp_path)
    instance._latest_snapshot_revision = 17
    instance._latest_snapshot_payload = {
        "world": {"year": 2, "season": "summer", "dayOfMonth": 8,
                  "timeOfDay": 900, "playerMoney": 812, "playerStamina": 91},
        "companion": {"locationId": "Farm", "waterCanLevel": 7,
                      "maxWaterCanLevel": 55, "stamina": 123, "maxStamina": 271,
                      "availableMoney": 409, "moneyStatus": "ok"},
        "inventory": {"freeSlots": 8, "slots": []},
        "farmWork": {"locationId": "Farm", "matureCropCount": 1},
    }
    return instance


def prompt_context(prompt):
    line = next(line for line in prompt.splitlines() if line.startswith("实时上下文："))
    return json.loads(line.removeprefix("实时上下文："))


@pytest.mark.parametrize("surface", ["agent", "delivery", "autonomy"])
def test_execution_prompts_have_one_resource_source(bridge, surface):
    if surface == "autonomy":
        bridge._autonomy.set_enabled("save", True)
        envelope = Envelope.from_mapping({
            "protocolVersion": "0.1", "messageType": "world.snapshot", "messageId": "m1",
            "senderInstanceId": "mod", "sequenceNumber": 1, "worldRevision": 17,
            "sentAt": "2026-01-01T00:00:00Z", "saveId": "save", "gameSessionId": "g1",
            "payload": bridge._latest_snapshot_payload,
        })
        prepared = bridge._prepare_autonomy_decision(envelope, "save")
        assert prepared is not None
        prompt = prepared[1]
    elif surface == "agent":
        prompt = bridge._format_agent_prompt("care for the garden", "save")
    else:
        prompt = bridge._format_delivery_prompt([], "save", midflight=[], read_only=False)
    facts = prompt_context(prompt)
    assert not {"inventory", "stamina", "funds", "fundsStatus"}.intersection(facts)
    resources = facts["resources"]
    assert resources["player"]["money"] == 812
    assert resources["companion"]["spendableMoney"] == 409
    assert resources["companion"]["stamina"] == {"current": 123, "max": 271}
    assert resources["companion"]["inventory"]["toolResources"]["waterCan"] == {"currentWater": 7, "capacity": 55, "isFull": False, "isEmpty": False, "unit": "浇水次数", "upgradeLevel": None, "note": "currentWater 是壶内剩余水量，capacity 是装满后的容量；工具升级等级未被原生快照发布，不可从水量推断。"}
    assert prompt.count('"waterCan"') == 1
    assert facts["worldRevision"] == 17


def test_delivery_carries_request_and_terminal_fact_once(bridge):
    instruction = "Store the blue berries, then return two packets to their original chest."
    store = bridge._work_store
    store.record_request("save", "req-unique", instruction, origin="player")
    store.finish_job("save", {
        "commandId": "native-receipt-unique", "status": "completed",
        "worldRevision": 16, "actualSummary": "native-summary-unique",
    })
    event = store.append_outbox_event("save", {
        "commandId": "native-receipt-unique", "requestId": "req-unique",
        "outcome": "completed", "feedback": {"progressSummary": "native-summary-unique"},
    })
    prompt = bridge._format_delivery_prompt([event], "save", midflight=[], read_only=False)
    # The original request text reaches the model exactly once: as the fact's
    # requestText, never duplicated inside the live context.
    assert prompt.count(instruction) == 1
    facts_line = next(line for line in prompt.splitlines() if line.startswith("终态事实："))
    payload = json.loads(facts_line.removeprefix("终态事实："))
    assert len(payload["terminalFacts"]) == 1
    fact = payload["terminalFacts"][0]
    assert fact["commandId"] == "native-receipt-unique"
    assert fact["requestText"] == instruction
    assert fact["feedback"]["progressSummary"] == "native-summary-unique"
    facts = prompt_context(prompt)
    assert facts["lastResult"]["worldRevision"] == 16
    assert facts["worldRevision"] == 17
    assert instruction not in json.dumps(facts)


def test_prompt_projection_preserves_public_context_and_life_compaction(bridge, monkeypatch):
    facts = build_decision_context({"payload": bridge._latest_snapshot_payload, "worldRevision": 17})
    before = copy.deepcopy(facts)
    monkeypatch.setattr(bridge, "_decision_context", lambda *args, **kwargs: facts)
    bridge._format_agent_prompt("observe", "save")
    assert facts == before
    assert facts["inventory"] == facts["resources"]["companion"]["inventory"]
    life = LifeChatService.compact_live_context(facts)
    assert life["resources"] == facts["resources"]
    assert "inventory" not in life


def test_missing_native_resource_values_remain_unknown():
    bridge = ChatBridge()
    facts = prompt_context(bridge._format_delivery_prompt([], "save", midflight=[], read_only=False))
    assert facts["provenance"] == "unknown"
    assert facts["inventory"]["freeSlots"] == "unknown"
    assert "toolResources" not in facts["inventory"]
    assert facts["stamina"] == "unknown"
    assert facts["funds"] == "unknown"
