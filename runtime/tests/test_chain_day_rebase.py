"""A real rollover may rebuild an unselected provider decision once."""

import asyncio
from unittest.mock import MagicMock, patch

from stardew_ai_runtime.chat_bridge import ActiveChatTask, ChatBridge
from stardew_ai_runtime.work_state import WorkStore


def setup_bridge(tmp_path):
    bridge = ChatBridge(run_dir=tmp_path)
    bridge._work_store = WorkStore(tmp_path / "work.json")
    store = bridge._work_store
    goal = store.add_goal("save", "照料农场", source="user")
    store.settle_game_day("save", year=1, season="summer", day=15)
    bridge._register_command_chain("save", "照料农场", None, "root")
    return bridge, store, goal


def test_rollover_reenters_provider_with_new_date_token_once(tmp_path):
    async def scenario():
        bridge, store, _ = setup_bridge(tmp_path)
        backend = MagicMock()
        tokens = []

        def run(active, cid, prompt):
            tokens.append(store.state("save").decision["token"])
            if len(tokens) == 1:
                store.settle_game_day("save", year=1, season="summer", day=16)
            return {"success": True}

        backend.run.side_effect = run
        active = ActiveChatTask("chain-old", "save")
        next_active = []

        async def submit(ws, request_id, instruction, save_id):
            following = ActiveChatTask(request_id, save_id)
            next_active.append(following)
            bridge._execute_turn(following, None, instruction)

        with patch("stardew_ai_runtime.compatibility.assert_native_compatible"), patch.object(
            bridge, "_get_backend", return_value=backend
        ), patch.object(bridge, "handle_chat_submit", side_effect=submit):
            bridge._execute_turn(active, None, "old day")
            assert bridge._rebase_chain_after_day_advance(active, "completed")
            assert not bridge._rebase_chain_after_day_advance(active, "completed")
            await bridge.wait_for_chains()
        assert len(tokens) == 2 and tokens[0] != tokens[1]
        assert next_active[0].decision_day == "1:summer:16"
        assert store.state("save").tasks == []  # No rejected command was replayed.

    asyncio.run(scenario())


def test_rollover_preserves_pause_cancel_and_selected_unknown_safety(tmp_path):
    async def scenario():
        bridge, store, goal = setup_bridge(tmp_path)
        chain = bridge._command_chains["save"]
        store.begin_decision("save", "old")
        active = ActiveChatTask("chain-old", "save", decision_token="old",
                                decision_day="1:summer:15", chain_generation=chain.generation)
        # Same-day refusal does not reenter, regardless of the error text.
        assert not bridge._rebase_chain_after_day_advance(active, "completed")
        store.submit_plan("save", goal_id=goal.id, decision_token="old", tasks=[{
            "title": "吃食物", "steps": [{"operation": "eat_food", "params": {}}],
        }])
        store.settle_game_day("save", year=1, season="summer", day=16)
        assert not bridge._rebase_chain_after_day_advance(active, "completed")
        # A selected task stays disqualifying even if its outcome is unknown.
        def uncertain(state):
            state.tasks[0].status = "unknown"
        store._mutate("save", uncertain)
        assert not bridge._rebase_chain_after_day_advance(active, "completed")
        def completed(state):
            state.tasks[0].status = "completed"
        store._mutate("save", completed)
        store.settle_game_day("save", year=1, season="summer", day=17)
        assert store.state("save").tasks == []
        assert not bridge._rebase_chain_after_day_advance(active, "completed")
        unselected = ActiveChatTask("chain-new", "save", decision_token="unselected",
                                    decision_day="1:summer:15", chain_generation=chain.generation)
        store.set_paused("save", True)
        with patch.object(bridge, "handle_chat_submit") as submit:
            assert bridge._rebase_chain_after_day_advance(unselected, "completed")
            assert chain.pending_continuation
            submit.assert_not_called()
        bridge._break_command_chain("save")
        assert not bridge._rebase_chain_after_day_advance(unselected, "completed")

    asyncio.run(scenario())
