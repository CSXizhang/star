"""Offline envelope tests; no game effects or live acceptance are simulated."""
import asyncio
import pytest

from stardew_ai_runtime.client import TransportClient, TransportClientError


def test_native_replay_preserves_revision_but_new_toggle_uses_current_revision():
    asyncio.run(_revision_scenario())


async def _revision_scenario():
    client = TransportClient()
    client.save_id, client.game_session_id = "save", "session"
    sent = []

    async def capture(envelope):
        sent.append(envelope)

    client.send_envelope = capture
    params = {"locationId": "Farm", "tiles": [{"x": 60, "y": 14}]}
    client.world_revision = 10
    await client.execute_native_action("toggle-animal-door", params, command_id="open", task_id="first")
    client.world_revision = 20
    await client.execute_native_action("toggle-animal-door", params, command_id="close", task_id="second")
    client.world_revision = 30
    await client.execute_native_action("toggle-animal-door", params, command_id="open", task_id="first")
    assert sent[0].payload == sent[2].payload
    assert sent[0].idempotency_key == sent[2].idempotency_key
    assert sent[1].payload["expectedWorldRevision"] == 20
    assert sent[0].message_id != sent[2].message_id

    # Explicitly changed semantics remain different, so the native conflict gate applies.
    await client.execute_native_action("toggle-animal-door", params, command_id="open", task_id="first", world_revision=40)
    assert sent[-1].payload["expectedWorldRevision"] == 40
    client.game_session_id = "next-session"
    await client.execute_native_action("toggle-animal-door", params, command_id="open", task_id="first")
    assert sent[-1].payload["expectedWorldRevision"] == 30


def test_native_replay_cache_never_evicts_used_commands():
    asyncio.run(_capacity_scenario())


async def _capacity_scenario():
    client = TransportClient()
    client.save_id, client.game_session_id = "save", "session"
    client._native_revision_session = ("save", "session")
    client._native_command_revisions = {str(i): i for i in range(256)}
    with pytest.raises(TransportClientError, match="cache is full"):
        await client.execute_native_action("toggle-animal-door", {"locationId": "Farm", "tiles": [{"x": 1, "y": 1}]}, command_id="new")
    assert client._native_command_revisions["0"] == 0
