"""Local stream tests, not gameplay evidence: polling must preserve frame boundaries."""
from __future__ import annotations

import asyncio
import struct
from unittest.mock import AsyncMock, Mock

import pytest

from stardew_ai_runtime.websocket_client import WebSocketClient


def frame(payload: bytes, opcode: int = 1) -> bytes:
    if len(payload) < 126:
        header = bytes((0x80 | opcode, len(payload)))
    elif len(payload) <= 65535:
        header = bytes((0x80 | opcode, 126)) + struct.pack("!H", len(payload))
    else:
        header = bytes((0x80 | opcode, 127)) + struct.pack("!Q", len(payload))
    return header + payload


@pytest.mark.parametrize("split", [2, 3, 4, 20])
def test_receive_timeout_preserves_incomplete_frame(split: int) -> None:
    async def run():
        reader = asyncio.StreamReader()
        writer = Mock()
        writer.drain = AsyncMock()
        writer.wait_closed = AsyncMock()
        client = WebSocketClient(reader, writer)
        message = '{"messageType":"world.snapshot","value":"' + "水" * 100 + '"}'
        wire = frame(message.encode())
        reader.feed_data(wire[:split])
        with pytest.raises(TimeoutError):
            await client.receive_text(timeout=0.01)
        reader.feed_data(wire[split:] + frame(b"next message"))
        assert await client.receive_text(timeout=0.05) == message
        assert await client.receive_text(timeout=0.05) == "next message"
        await client.close()
    asyncio.run(run())


def test_receive_cancellation_preserves_frame_and_close_releases_reader() -> None:
    async def run():
        reader = asyncio.StreamReader()
        writer = Mock()
        async def drain():
            pass
        writer.drain = drain
        writer.wait_closed = drain
        client = WebSocketClient(reader, writer)
        wire = frame(b"unfinished snapshot")
        reader.feed_data(wire[:2])
        polling = asyncio.create_task(client.receive_text())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        polling.cancel()
        with pytest.raises(asyncio.CancelledError):
            await polling
        reader.feed_data(wire[2:])
        assert await client.receive_text(timeout=0.05) == "unfinished snapshot"
        with pytest.raises(TimeoutError):
            await client.receive_text(timeout=0.01)
        await client.close()
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        writer.close.assert_called_once()
    asyncio.run(run())
