"""Native progress renews the chain guard; failures never create endless retries."""

import asyncio
from unittest.mock import AsyncMock, patch

from stardew_ai_runtime.chat_bridge import ChatBridge
from stardew_ai_runtime.plan_executor import StepExecution


def terminal(operation, effects, outcome="completed", reason=None):
    return StepExecution("executed", task_id="owned", operation=operation,
                         outcome=outcome, task_status=outcome, effects=effects,
                         reason_code=reason)


def test_matched_native_progress_callback_starts_next_provider_even_after_eight_turns(tmp_path):
    async def scenario():
        bridge = ChatBridge(run_dir=tmp_path)
        provider = AsyncMock()
        bridge._register_command_chain("save", "照料农场", None, "root")
        chain = bridge._command_chains["save"]
        with patch.object(bridge, "handle_chat_submit", provider):
            for change in [terminal("plant_seeds", [{"state": "planted"}]),
                           terminal("navigate_to", [{"location": "Farm", "pathLength": 35}]),
                           terminal("pickup_items", [{"state": "picked-up", "stack": 5},
                                                     {"state": "failed"}], "partial", "unreachable")]:
                chain.chain_count = 8
                chain.waiting_task_id = "owned"
                await bridge._on_job_terminal("save", change, "SHORT_JOB_TERMINAL")
                await bridge.wait_for_chains()
                assert chain.chain_count == 1
        assert provider.await_count == 3
        assert all(call.args[1].startswith("chain-") for call in provider.await_args_list)

    asyncio.run(scenario())


def test_failed_skipped_readonly_and_unknown_callbacks_do_not_renew_limit(tmp_path):
    async def scenario():
        cases = [terminal("navigate_to", [], "partial", "NO_ROUTE"),
                 terminal("pet_animal", [{"state": "failed"}], "partial", "TARGET_FAILED"),
                 terminal("cut_grass", [{"state": "skipped"}]),
                 terminal("query_shop", []),
                 terminal("navigate_to", [{"state": "navigated", "pathLength": 0}]),
                 terminal("eat_food", [], "unknown", "NATIVE_TERMINAL_UNCONFIRMED")]
        for change in cases:
            bridge = ChatBridge(run_dir=tmp_path)
            bridge._register_command_chain("save", "照料农场", None, "root")
            chain = bridge._command_chains["save"]
            chain.chain_count = 8
            chain.waiting_task_id = "owned"
            provider, reply = AsyncMock(), AsyncMock()
            with patch.object(bridge, "handle_chat_submit", provider), patch.object(bridge, "_send_reply", reply):
                await bridge._on_job_terminal("save", change, "SHORT_JOB_TERMINAL")
                await bridge.wait_for_chains()
            provider.assert_not_awaited()
            assert "save" not in bridge._command_chains
            if change.outcome != "unknown":
                assert "连续8次" in reply.await_args.args[1].payload["replyText"]
        # A stale terminal cannot renew or advance a different owned job.
        bridge = ChatBridge(run_dir=tmp_path)
        bridge._register_command_chain("save", "new work", None, "root")
        chain = bridge._command_chains["save"]
        chain.chain_count, chain.waiting_task_id = 8, "different"
        with patch.object(bridge, "handle_chat_submit", AsyncMock()) as provider:
            await bridge._on_job_terminal("save", cases[0], "SHORT_JOB_TERMINAL")
            await bridge.wait_for_chains()
            provider.assert_not_awaited()
        assert chain.chain_count == 8 and chain.waiting_task_id == "different"

    asyncio.run(scenario())
