"""Offline cross-language fixture: contract §1/§2 life.* wire messages.

Real ChatBridge stores (work/autonomy/profile/memory/milestones) + protocol.py
Envelope factories produce the example JSON consumed by LifeWireContractTests.
No sockets, provider, game, or native execution. Writes only the supplied dir.
"""
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from stardew_ai_runtime.chat_bridge import ChatBridge
from stardew_ai_runtime.companion_milestones import wire_node
from stardew_ai_runtime.protocol import (
    Envelope,
    LifeChatSubmitPayload,
    LifeMemoryEditPayload,
    LifeMemoryListPayload,
    LifeMilestonesGetPayload,
    LifeProfileGetPayload,
    LifeProfileSetPayload,
    MilestoneNode,
    MilestonePrepItem,
)

SAVE = "wire-save"
REQ = "wire-req-1"
LMG_REQ = "lmg-wire-1"


def main() -> None:
    output = Path(sys.argv[1])
    output.mkdir(parents=True, exist_ok=True)
    bridge = ChatBridge(run_dir=output, backend="agy", enable_plan_worker=False)

    def dump(name: str, envelope: Envelope) -> None:
        (output / name).write_text(
            json.dumps(envelope.to_mapping(), ensure_ascii=False), encoding="utf-8"
        )

    def cs_to_py(message_type: str, payload: dict) -> Envelope:
        return Envelope(
            protocol_version="0.1",
            message_type=message_type,
            message_id=f"msg-wire-{message_type}",
            sender_instance_id="wire-csharp",
            sequence_number=0,
            world_revision=0,
            sent_at=datetime.now(UTC),
            save_id=SAVE,
            payload=payload,
        )

    # ------------------------------------------------------------------
    # Populate real production state so the §1.3 work projection carries
    # non-default values for every field, including a waiting condition.
    # ------------------------------------------------------------------
    store = bridge._work_store
    store.begin_decision(SAVE, "wire-decision")
    store.submit_plan(
        SAVE,
        goal_text="离线线契约作业",
        decision_token="wire-decision",
        tasks=[{
            "id": "wire-task-1",
            "title": "等待补水",
            "steps": [{"id": "wire-step-1", "operation": "refill_watering_can"}],
        }],
    )
    store.mark_task_waiting(
        SAVE, "wire-task-1", step_id="wire-step-1",
        condition={"type": "inventory", "params": {"itemId": "WateringCan"}},
        reason_code="WIRE_WAIT",
    )
    store.add_goal(SAVE, "优先赚钱：收获出货", source="user")
    store.add_todo(
        SAVE, intent="晴天给作物浇水",
        trigger={"type": "calendar", "year": 1, "season": "spring", "day": 3},
    )
    bridge._autonomy.set_preferences(
        SAVE, goal="优先赚钱：收获出货", budget_limit=500, box_preference="shipping"
    )
    work_projection = bridge._life_work_projection(SAVE)

    # ------------------------------------------------------------------
    # Companion profile + memory stores (production persistence paths).
    # ------------------------------------------------------------------
    bridge._profile_store.set(
        SAVE,
        {"onboarded": True, "skipped": False, "playStyle": "earn",
         "personality": "lively", "careFrequency": "chatty", "companionName": "阿星"},
        expected_revision=0,
    )
    profile = bridge._profile_store.get(SAVE)["profile"]
    bridge._memory_store.add(
        SAVE, kind="agreement", text="每天给鸡喂食", source="player",
        game_date="1:spring:2", expected_revision=0,
    )
    bridge._memory_store.add(
        SAVE, kind="event", text="完成了「给胡萝卜浇水」", source="system",
        game_date="1:spring:2", expected_revision=0, command_id="cmd-42",
    )
    memory = bridge._memory_store.list(SAVE)
    entries = memory["entries"]
    memory_revision = int(memory["memoryRevision"])

    # ------------------------------------------------------------------
    # §1.1 life.chat.submit (C#→Py)
    # ------------------------------------------------------------------
    dump("life-chat-submit-chat.json", cs_to_py(
        "life.chat.submit",
        LifeChatSubmitPayload(REQ, SAVE, "chat", "今天过得怎么样？", "life-menu").to_mapping(),
    ))
    dump("life-chat-submit-plan.json", cs_to_py(
        "life.chat.submit",
        LifeChatSubmitPayload(REQ, SAVE, "plan", "明天先浇水再收获，怎么样？", "life-menu").to_mapping(),
    ))

    # ------------------------------------------------------------------
    # §1.2 life.chat.reply (Py→C#), one fixture per status
    # ------------------------------------------------------------------
    dump("life-chat-reply-processing.json", Envelope.create_life_chat_reply(
        "wire-py", REQ, SAVE, "processing", profile_revision=1, memory_revision=memory_revision,
    ))
    dump("life-chat-reply-queued.json", Envelope.create_life_chat_reply(
        "wire-py", REQ, SAVE, "queued", profile_revision=1, memory_revision=memory_revision,
        queue_position=1,
    ))
    dump("life-chat-reply-completed.json", Envelope.create_life_chat_reply(
        "wire-py", REQ, SAVE, "completed", profile_revision=1, memory_revision=memory_revision,
        reply_text="早上好呀，今天也一起加油吧",
    ))
    dump("life-chat-reply-failed.json", Envelope.create_life_chat_reply(
        "wire-py", REQ, SAVE, "failed", profile_revision=1, memory_revision=memory_revision,
        error="RESOURCE_EXHAUSTED",
    ))

    # ------------------------------------------------------------------
    # §1.3 life.profile.get / life.profile.state
    # ------------------------------------------------------------------
    dump("life-profile-get.json", cs_to_py(
        "life.profile.get", LifeProfileGetPayload(REQ, SAVE).to_mapping(),
    ))
    dump("life-profile-state.json", Envelope.create_life_profile_state(
        "wire-py", REQ, SAVE, profile, 1, work=work_projection,
    ))
    dump("life-profile-state-null.json", Envelope.create_life_profile_state(
        "wire-py", "wire-req-0", SAVE, None, 0, work=work_projection,
    ))
    dump("life-profile-state-rejected.json", Envelope.create_life_profile_state(
        "wire-py", "wire-req-2", SAVE, profile, 1,
        status="rejected", reason="STALE_REVISION", work=work_projection,
    ))

    # ------------------------------------------------------------------
    # §1.4 life.profile.set (C#→Py)
    # ------------------------------------------------------------------
    dump("life-profile-set.json", cs_to_py(
        "life.profile.set",
        LifeProfileSetPayload(REQ, SAVE, 1, {"onboarded": True, "playStyle": "workhorse"}).to_mapping(),
    ))

    # ------------------------------------------------------------------
    # §1.5 life.memory.list / life.memory.state
    # ------------------------------------------------------------------
    dump("life-memory-list.json", cs_to_py(
        "life.memory.list", LifeMemoryListPayload(REQ, SAVE).to_mapping(),
    ))
    dump("life-memory-state.json", Envelope.create_life_memory_state(
        "wire-py", REQ, SAVE, entries=entries, memory_revision=memory_revision,
    ))

    # ------------------------------------------------------------------
    # §1.6 life.memory.edit (C#→Py), one fixture per op
    # ------------------------------------------------------------------
    agreement_id = next(e["id"] for e in entries if e["kind"] == "agreement")
    dump("life-memory-edit-add.json", cs_to_py(
        "life.memory.edit",
        LifeMemoryEditPayload(
            REQ, SAVE, memory_revision, "add", kind="agreement", text="晚上八点一起钓鱼",
        ).to_mapping(),
    ))
    dump("life-memory-edit-correct.json", cs_to_py(
        "life.memory.edit",
        LifeMemoryEditPayload(
            REQ, SAVE, memory_revision, "correct", entry_id=agreement_id,
            text="每天给鸡喂食并收蛋",
        ).to_mapping(),
    ))
    dump("life-memory-edit-delete.json", cs_to_py(
        "life.memory.edit",
        LifeMemoryEditPayload(REQ, SAVE, memory_revision, "delete", entry_id=agreement_id).to_mapping(),
    ))

    # ------------------------------------------------------------------
    # §1.7 life.care (Py→C#, one-way push)
    # ------------------------------------------------------------------
    dump("life-care.json", Envelope.create_life_care(
        "wire-py", SAVE, "work-done", "work-done:1:spring:2:cmd-42",
        "辛苦啦，喝口水吧", "1:spring:2",
    ))

    # ------------------------------------------------------------------
    # §2.1 life.milestones.get (C#→Py, lmg- request prefix)
    # ------------------------------------------------------------------
    dump("life-milestones-get.json", cs_to_py(
        "life.milestones.get", LifeMilestonesGetPayload(LMG_REQ, SAVE).to_mapping(),
    ))

    # ------------------------------------------------------------------
    # §2.2 life.milestones.state (Py→C#), built from the real milestone
    # store: the player adopted the Egg Festival strawberry node in plan
    # mode, so the state carries the real goal/todo wiring result.
    # ------------------------------------------------------------------
    milestone_store = bridge._milestone_store
    milestone_store.adopt(
        SAVE,
        "spring-egg-festival-strawberry:y1",
        reserved_funds=1000,
        planned_count=10,
        terms_note="预留1000g仅用于蛋蛋节当天购买草莓种子",
        work_store=bridge._work_store,
        game_date={"year": 1, "season": "spring", "day": 11},
    )
    merged = milestone_store.merged_nodes(
        SAVE, {"year": 1, "season": "spring", "day": 11}, "earn"
    )
    wire_nodes = [MilestoneNode.from_mapping(wire_node(n)) for n in merged]
    completed_node = MilestoneNode(
        id="spring-crops-bundle-retention:y1",
        title="春季作物收集包保留",
        status="completed",
        verification="verified",
        target_date="1:spring:28",
        days_until=0,
        summary="春季作物收集包（储藏室）：防风草、青豆、花椰菜、土豆各1；献祭实际进度读不到，属未知。",
        source_url="https://stardewvalleywiki.com/Bundles",
        prep_items=(
            MilestonePrepItem(
                key="retain-parsnip", label="保留防风草×1（献祭用，先别卖掉）",
                support="manual", status="done", note="背包中已确认",
            ),
        ),
        reserved_funds=0,
        planned_count=4,
        terms_note="季末核对",
        # Non-integral on purpose: whole-number floats re-serialize differently
        # across Python json (1727340000.0) and System.Text.Json (1727340000),
        # which would break the byte-stable cross-language lock.
        updated_at=1727340000.5,
    )
    dump("life-milestones-state.json", Envelope.create_life_milestones_state(
        "wire-py", LMG_REQ, SAVE, status="ok", game_date="1:spring:11",
        nodes=wire_nodes + [completed_node],
    ))
    # Failure surface: status=failed carries error and an empty node list.
    dump("life-milestones-state-failed.json", Envelope.create_life_milestones_state(
        "wire-py", LMG_REQ, SAVE, status="failed", error="MILESTONE_STORE_UNAVAILABLE",
    ))
    # Null-omitted variant / proactive push example: requestId="", no gameDate,
    # and a bare suggested node carrying only the required field set.
    minimal_node = MilestoneNode(
        id="spring-egg-festival-strawberry:y1",
        title="蛋蛋节买草莓种子",
        status="suggested",
        target_date="1:spring:13",
        summary="春13蛋蛋节摊位100g/个；8天成熟后每4天结果",
        updated_at=1727340000.5,
    )
    dump("life-milestones-state-minimal.json", Envelope.create_life_milestones_state(
        "wire-py", "", SAVE, status="ok", nodes=[minimal_node],
    ))
    dump("life-care-milestone.json", Envelope.create_life_care(
        "wire-py", SAVE, "milestone",
        "milestone:1:spring:12:spring-egg-festival-strawberry:y1",
        "明天就是蛋蛋节啦，记得预留好买草莓种子的钱，到广场摊位亲自买哦", "1:spring:12",
    ))

    # ------------------------------------------------------------------
    # §2.4 world.snapshot (C#→Py) world block with the playerItems
    # aggregate the runtime verifies milestones against. Authored here on
    # the Python side so the C# LockPayload pins the same field surface.
    # ------------------------------------------------------------------
    dump("world-snapshot-player-items.json", cs_to_py(
        "world.snapshot",
        {
            "capturedRevision": 7,
            "companion": {
                "locationId": "Farm",
                "tileX": 64,
                "tileY": 10,
                "facingDirection": 2,
                "stamina": 270,
                "maxStamina": 270,
                "waterCanLevel": 40,
                "maxWaterCanLevel": 40,
                "hasWateringCan": True,
                "activity": "idle",
            },
            "world": {
                "currentLocation": "Farm",
                "timeOfDay": 600,
                "season": "spring",
                "dayOfMonth": 13,
                "isRaining": False,
                "year": 1,
                "playerMoney": 500,
                "playerStamina": 180.5,
                "playerMaxStamina": 270,
                "playerItems": [
                    {"name": "Strawberry Seeds", "quantity": 10},
                    {"name": "Parsnip", "quantity": 3},
                ],
            },
        },
    ))


if __name__ == "__main__":
    main()
