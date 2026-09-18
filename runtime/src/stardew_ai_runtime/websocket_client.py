from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import struct


class WebSocketError(Exception):
    """Raised on WebSocket protocol or connection errors."""


class ConnectionClosed(WebSocketError):
    """Raised when the WebSocket connection is closed."""


WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WebSocketClient:
    """Minimal, self-contained RFC 6455 WebSocket client over asyncio."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._reader = reader
        self._writer = writer
        self._closed = False

    @classmethod
    async def connect(
        cls,
        host: str,
        port: int,
        path: str = "/",
        headers: dict[str, str] | None = None,
        timeout: float = 5.0,
    ) -> WebSocketClient:
        coro = asyncio.open_connection(host, port)
        reader, writer = await asyncio.wait_for(coro, timeout=timeout)

        # Generate random 16-byte key
        raw_key = os.urandom(16)
        sec_key = base64.b64encode(raw_key).decode("ascii")
        expected_accept = base64.b64encode(
            hashlib.sha1((sec_key + WS_GUID).encode("ascii")).digest()
        ).decode("ascii")

        req_headers = {
            "Host": f"{host}:{port}",
            "Upgrade": "websocket",
            "Connection": "Upgrade",
            "Sec-WebSocket-Key": sec_key,
            "Sec-WebSocket-Version": "13",
        }
        if headers:
            req_headers.update(headers)

        lines = [f"GET {path} HTTP/1.1"]
        for key, value in req_headers.items():
            lines.append(f"{key}: {value}")
        raw_request = "\r\n".join(lines) + "\r\n\r\n"

        writer.write(raw_request.encode("latin-1"))
        await writer.drain()

        # Read handshake response status line
        status_line = await asyncio.wait_for(reader.readline(), timeout=timeout)
        if not status_line:
            writer.close()
            raise WebSocketError("Server closed connection during handshake")

        status_text = status_line.decode("latin-1").strip()
        parts = status_text.split(" ", 2)
        if len(parts) < 2 or parts[1] != "101":
            # Collect error body if any
            writer.close()
            raise WebSocketError(f"WebSocket upgrade failed: {status_text}")

        # Read headers
        resp_headers: dict[str, str] = {}
        while True:
            header_line = await asyncio.wait_for(reader.readline(), timeout=timeout)
            line = header_line.decode("latin-1").strip()
            if not line:
                break
            if ":" in line:
                hk, hv = line.split(":", 1)
                resp_headers[hk.strip().lower()] = hv.strip()

        accept_val = resp_headers.get("sec-websocket-accept")
        if accept_val != expected_accept:
            writer.close()
            raise WebSocketError("Sec-WebSocket-Accept mismatch")

        return cls(reader, writer)

    async def send_text(self, message: str) -> None:
        if self._closed:
            raise ConnectionClosed("WebSocket is closed")

        payload = message.encode("utf-8")
        length = len(payload)
        mask_key = os.urandom(4)

        # Byte 0: FIN (0x80) | Text (0x01)
        header = bytearray([0x81])

        # Byte 1: Mask bit (0x80) | length
        if length < 126:
            header.append(0x80 | length)
        elif length <= 0xFFFF:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))

        header.extend(mask_key)

        # Apply 4-byte XOR mask
        masked_payload = bytearray(payload)
        for i in range(length):
            masked_payload[i] ^= mask_key[i % 4]

        self._writer.write(header + masked_payload)
        await self._writer.drain()

    async def receive_text(self, timeout: float | None = None) -> str:
        if self._closed:
            raise ConnectionClosed("WebSocket is closed")

        while True:
            if timeout is not None:
                frame = await asyncio.wait_for(self._read_frame(), timeout=timeout)
            else:
                frame = await self._read_frame()

            opcode, payload = frame
            if opcode == 0x1:  # Text frame
                return payload.decode("utf-8")
            elif opcode == 0x8:  # Close frame
                self._closed = True
                try:
                    # Echo close frame
                    await self._send_close()
                except Exception:
                    pass
                raise ConnectionClosed("Received close frame from server")
            elif opcode == 0x9:  # Ping
                await self._send_pong(payload)
            elif opcode == 0xA:  # Pong
                continue

    async def _read_frame(self) -> tuple[int, bytes]:
        head = await self._reader.readexactly(2)
        b1, b2 = head[0], head[1]

        opcode = b1 & 0x0F
        has_mask = (b2 & 0x80) != 0
        length = b2 & 0x7F

        if length == 126:
            ext_len = await self._reader.readexactly(2)
            length = struct.unpack("!H", ext_len)[0]
        elif length == 127:
            ext_len = await self._reader.readexactly(8)
            length = struct.unpack("!Q", ext_len)[0]

        mask = None
        if has_mask:
            mask = await self._reader.readexactly(4)

        data = await self._reader.readexactly(length)
        if has_mask and mask:
            unmasked = bytearray(data)
            for i in range(length):
                unmasked[i] ^= mask[i % 4]
            data = bytes(unmasked)

        return opcode, data

    async def _send_pong(self, payload: bytes) -> None:
        if self._closed:
            return
        mask_key = os.urandom(4)
        header = bytearray([0x8A, 0x80 | (len(payload) & 0x7F)])
        header.extend(mask_key)
        masked = bytearray(payload)
        for i in range(len(payload)):
            masked[i] ^= mask_key[i % 4]
        self._writer.write(header + masked)
        await self._writer.drain()

    async def _send_close(self) -> None:
        mask_key = os.urandom(4)
        header = bytearray([0x88, 0x80])
        header.extend(mask_key)
        self._writer.write(header)
        await self._writer.drain()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._send_close()
        except Exception:
            pass
        finally:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass
