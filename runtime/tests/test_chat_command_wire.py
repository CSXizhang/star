"""Command-level completion must survive serialization independently of a turn."""

import pytest

from stardew_ai_runtime.protocol import ChatReplyPayload, Envelope


@pytest.mark.parametrize("complete", [False, True, None])
def test_command_completion_roundtrip(complete):
    envelope = Envelope.create_chat_reply(
        "bridge", "chain-turn-2", "job-completed", "这一项已完成",
        save_id="farm", command_id="player-command", command_complete=complete,
    )
    restored = Envelope.from_mapping(envelope.to_mapping())
    reply = ChatReplyPayload.from_mapping(restored.payload)
    assert reply.request_id == "chain-turn-2"
    assert reply.command_id == "player-command"
    assert reply.command_complete is complete
    if complete is None:
        assert "commandComplete" not in reply.to_mapping()
    else:
        assert reply.to_mapping()["commandComplete"] is complete


def test_legacy_reply_does_not_invent_command_completion():
    envelope = Envelope.create_chat_reply("bridge", "old-turn", "completed", "完成")
    reply = ChatReplyPayload.from_mapping(envelope.payload)
    assert reply.command_id is None
    assert reply.command_complete is None
    assert "commandId" not in reply.to_mapping()
    assert "commandComplete" not in reply.to_mapping()
