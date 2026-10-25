import asyncio
import base64
import hashlib
import json
from datetime import UTC, datetime

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class MockModTransportServer:
    """Mock Stardew Mod Transport Server implementing the Stage 0 contract."""

    def __init__(self, session_token: str = "mock-secret-token"):
        self.session_token = session_token
        self.server: asyncio.Server | None = None
        self.port: int = 0
        self.save_id = "mock-save-hash-123"
        self.game_session_id = "mock-session-456"
        self.world_revision = 1
        self.sequence_number = 0
        self.active_command_id: str | None = None
        self.executed_commands: list[dict] = []
        self.cancelled_commands: list[dict] = []
        self.paused_commands: list[dict] = []
        self.resumed_commands: list[dict] = []
        self.delay_execution: bool = False
        self.pending_commands: list[tuple[dict, asyncio.StreamWriter]] = []
        self.idempotency_records: dict[str, dict] = {}
        self.supports_result_query = False
        self.result_queries: list[dict] = []

    def _next_seq(self) -> int:
        seq = self.sequence_number
        self.sequence_number += 1
        return seq

    def _compute_fingerprint(self, payload: dict) -> str:
        cid = payload.get("commandId", "")
        tid = payload.get("taskId", "")
        skid = payload.get("skillId", "")
        skver = payload.get("skillVersion", "0.1.0")
        rev = str(payload.get("expectedWorldRevision", ""))
        cpol = payload.get("cancelPolicy", "immediate")
        pdec = payload.get("policyDecisionId", "")
        params = payload.get("parameters", {})
        loc = params.get("locationId", "")
        raw_tiles = params.get("tiles") or []
        sorted_tiles = sorted(raw_tiles, key=lambda t: (t.get("x", 0), t.get("y", 0)))
        tiles_str = ",".join(f"{t.get('x', 0)}:{t.get('y', 0)}" for t in sorted_tiles)
        chest = params.get("chestTile")
        chest_str = f"{chest.get('x', '')}:{chest.get('y', '')}" if chest else ""
        raw_item_ids = params.get("itemIds") or []
        item_ids_str = ",".join(sorted(str(i) for i in raw_item_ids))
        seed_item_id = params.get("seedItemId", "")
        gm = params.get("maxGameMinutes", 0)
        st = params.get("maxStamina", 0.0)
        w = params.get("maxWater", 0)
        budgets = f"gm={gm}|st={st}|w={w}"
        raw_key = (
            f"{cid}|{tid}|{skid}|{skver}|{rev}|{cpol}|{pdec}|{loc}"
            f"|{tiles_str}|{chest_str}|{item_ids_str}|{seed_item_id}|{budgets}"
        )
        return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle_connection, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        request_line = await reader.readline()
        if not request_line:
            writer.close()
            return
        line = request_line.decode("latin-1").strip()
        parts = line.split(" ")
        path = parts[1] if len(parts) > 1 else "/"

        headers: dict[str, str] = {}
        while True:
            header_line = await reader.readline()
            hline = header_line.decode("latin-1").strip()
            if not hline:
                break
            if ":" in hline:
                k, v = hline.split(":", 1)
                headers[k.strip().lower()] = v.strip()

        # Token validation: check query param ?token= or Authorization header
        auth_ok = False
        if f"token={self.session_token}" in path:
            auth_ok = True
        auth_header = headers.get("authorization", "")
        if auth_header == f"Bearer {self.session_token}":
            auth_ok = True

        if not auth_ok:
            resp = "HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\n\r\n"
            writer.write(resp.encode("latin-1"))
            await writer.drain()
            writer.close()
            return

        sec_key = headers.get("sec-websocket-key")
        if not sec_key:
            writer.close()
            return

        accept_val = base64.b64encode(
            hashlib.sha1((sec_key + WS_GUID).encode("ascii")).digest()
        ).decode("ascii")

        handshake_resp = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept_val}\r\n\r\n"
        )
        writer.write(handshake_resp.encode("latin-1"))
        await writer.drain()

        # Message loop
        try:
            while True:
                head = await reader.readexactly(2)
                b1, b2 = head[0], head[1]
                opcode = b1 & 0x0F
                if opcode == 0x8:  # Close
                    break

                length = b2 & 0x7F
                if length == 126:
                    ext = await reader.readexactly(2)
                    length = int.from_bytes(ext, "big")
                elif length == 127:
                    ext = await reader.readexactly(8)
                    length = int.from_bytes(ext, "big")

                has_mask = (b2 & 0x80) != 0
                mask = await reader.readexactly(4) if has_mask else None
                payload = await reader.readexactly(length)
                if mask:
                    unmasked = bytearray(payload)
                    for i in range(length):
                        unmasked[i] ^= mask[i % 4]
                    payload = bytes(unmasked)

                if opcode == 0x1:  # Text
                    raw_text = payload.decode("utf-8")
                    msg = json.loads(raw_text)
                    await self._process_message(msg, writer)
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            writer.close()

    async def _send_json(self, writer: asyncio.StreamWriter, data: dict) -> None:
        raw = json.dumps(data).encode("utf-8")
        length = len(raw)
        header = bytearray([0x81])
        if length < 126:
            header.append(length)
        elif length <= 0xFFFF:
            header.append(126)
            header.extend(length.to_bytes(2, "big"))
        else:
            header.append(127)
            header.extend(length.to_bytes(8, "big"))
        writer.write(header + raw)
        await writer.drain()

    async def _process_message(self, msg: dict, writer: asyncio.StreamWriter) -> None:
        mtype = msg.get("messageType")
        msg_id = msg.get("messageId", "msg-unknown")

        if mtype == "runtime.hello":
            welcome = {
                "protocolVersion": "0.1",
                "messageType": "mod.welcome",
                "messageId": f"msg-welcome-{self._next_seq()}",
                "correlationId": msg_id,
                "saveId": self.save_id,
                "gameSessionId": self.game_session_id,
                "senderInstanceId": "mock-mod-001",
                "sequenceNumber": self._next_seq(),
                "worldRevision": self.world_revision,
                "sentAt": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                "payload": {
                    "selectedProtocolVersion": "0.1",
                    "modVersion": "0.1.0",
                    "gameVersion": "1.6.15",
                    "smapiVersion": "4.2.1",
                    "lifecycleState": "ready",
                    "skills": [
                        "water-zone",
                        "harvest-zone",
                        "deposit-chest",
                        "organize-chest",
                        "hoe-tiles",
                        "plant-seeds",
                    ],
                },
            }
            if self.supports_result_query:
                welcome["payload"]["commandResultQuery"] = True
            await self._send_json(writer, welcome)

            snapshot = {
                "protocolVersion": "0.1",
                "messageType": "world.snapshot",
                "messageId": f"msg-snapshot-{self._next_seq()}",
                "correlationId": msg_id,
                "saveId": self.save_id,
                "gameSessionId": self.game_session_id,
                "senderInstanceId": "mock-mod-001",
                "sequenceNumber": self._next_seq(),
                "worldRevision": self.world_revision,
                "sentAt": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                "payload": {
                    "capturedRevision": self.world_revision,
                    "companion": {
                        "locationId": "Farm",
                        "tileX": 64,
                        "tileY": 15,
                        "facingDirection": 2,
                        "stamina": 270.0,
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
                        "dayOfMonth": 1,
                        "isRaining": False,
                        "farmWork": {
                            "tilledUnwateredTiles": [
                                {"x": 64, "y": 15},
                                {"x": 64, "y": 16},
                            ],
                            "tilledUnwateredCount": 2,
                            "isTruncated": False,
                            "matureCropCount": 0,
                        },
                    },
                    "farmWork": {
                        "tilledUnwateredTiles": [
                            {"x": 64, "y": 15},
                            {"x": 64, "y": 16},
                        ],
                        "tilledUnwateredCount": 2,
                        "isTruncated": False,
                        "matureCropCount": 0,
                    },
                    "planting": {
                        "season": "spring",
                        "dayOfMonth": 1,
                        "companionHasHoe": True,
                        "seeds": [
                            {
                                "itemId": "(O)472",
                                "name": "Parsnip Seeds",
                                "stack": 15,
                                "canPlantCurrentSeason": True,
                                "seasons": ["spring"],
                                "growthDays": 4,
                                "regrows": False,
                                "isRaised": False,
                            }
                        ],
                        "candidateTiles": {
                            "tilledEmptyCount": 2,
                            "tilledEmptyTiles": [{"x": 64, "y": 14}],
                            "tilledEmptyTruncated": False,
                            "tillableCount": 10,
                            "tillableTiles": [{"x": 65, "y": 14}],
                            "tillableTruncated": False,
                        },
                        "searchBounds": {
                            "center": {"x": 64, "y": 15},
                            "radius": 15,
                        },
                    },
                },
            }
            await self._send_json(writer, snapshot)

        elif mtype == "skill.execute":
            payload = msg.get("payload", {})
            cmd_id = payload.get("commandId", "cmd-unknown")
            task_id = payload.get("taskId", "task-unknown")
            idem_key = msg.get("idempotencyKey")

            if not idem_key:
                err_result = {
                    "protocolVersion": "0.1",
                    "messageType": "protocol.error",
                    "messageId": f"msg-err-{self._next_seq()}",
                    "correlationId": msg_id,
                    "saveId": self.save_id,
                    "gameSessionId": self.game_session_id,
                    "senderInstanceId": "mock-mod-001",
                    "sequenceNumber": self._next_seq(),
                    "worldRevision": self.world_revision,
                    "sentAt": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                    "payload": {
                        "code": "MISSING_IDEMPOTENCY_KEY",
                        "message": "idempotencyKey is required for skill.execute",
                    },
                }
                await self._send_json(writer, err_result)
                return

            fp = self._compute_fingerprint(payload)
            if idem_key in self.idempotency_records:
                rec = self.idempotency_records[idem_key]
                if rec["fingerprint"] != fp:
                    # Conflict: reject without mutating original record or active command
                    conflict_result = {
                        "protocolVersion": "0.1",
                        "messageType": "skill.result",
                        "messageId": f"msg-conflict-{self._next_seq()}",
                        "correlationId": msg_id,
                        "saveId": self.save_id,
                        "gameSessionId": self.game_session_id,
                        "senderInstanceId": "mock-mod-001",
                        "sequenceNumber": self._next_seq(),
                        "worldRevision": self.world_revision,
                        "sentAt": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                        "payload": {
                            "commandId": cmd_id,
                            "taskId": task_id,
                            "terminalState": "rejected",
                            "completedCount": 0,
                            "skippedCount": 0,
                            "failedCount": 0,
                            "finalWorldRevision": self.world_revision,
                            "effects": [],
                            "error": "IDEMPOTENCY_CONFLICT",
                            "retryRecommended": False,
                            "playerActionRequired": False,
                        },
                    }
                    await self._send_json(writer, conflict_result)
                    return

                # Duplicate replay: replay cached result with new envelope sequence
                cached_res = dict(rec["result"])
                cached_res["messageId"] = f"msg-replay-{self._next_seq()}"
                cached_res["correlationId"] = msg_id
                cached_res["sequenceNumber"] = self._next_seq()
                cached_res["sentAt"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                await self._send_json(writer, cached_res)
                return

            if self.delay_execution:
                self.pending_commands.append((msg, writer))
                self.idempotency_records[idem_key] = {
                    "fingerprint": fp,
                    "status": "pending",
                    "commandId": cmd_id,
                    "payload": payload,
                    "writer": writer,
                    "msgId": msg_id,
                }
                return

            self.active_command_id = cmd_id
            self.executed_commands.append(payload)

            self.world_revision += 1
            res_payload = {
                "commandId": cmd_id,
                "taskId": task_id,
                "terminalState": "succeeded",
                "completedCount": len(payload.get("parameters", {}).get("tiles", [])),
                "skippedCount": 0,
                "failedCount": 0,
                "finalWorldRevision": self.world_revision,
                "effects": [
                    {"tile": t, "state": "watered"}
                    for t in payload.get("parameters", {}).get("tiles", [])
                ],
                "resources": {
                    "staminaUsed": 4.0,
                    "waterUsed": 2,
                    "gameMinutesElapsed": 10,
                },
                "error": None,
                "retryRecommended": False,
                "playerActionRequired": False,
            }
            result = {
                "protocolVersion": "0.1",
                "messageType": "skill.result",
                "messageId": f"msg-result-{self._next_seq()}",
                "correlationId": msg_id,
                "saveId": self.save_id,
                "gameSessionId": self.game_session_id,
                "senderInstanceId": "mock-mod-001",
                "sequenceNumber": self._next_seq(),
                "worldRevision": self.world_revision,
                "sentAt": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                "payload": res_payload,
            }
            self.idempotency_records[idem_key] = {
                "fingerprint": fp,
                "status": "completed",
                "commandId": cmd_id,
                "result": result,
            }
            await self._send_json(writer, result)
            self.active_command_id = None

        elif mtype == "skill.result.query":
            self.result_queries.append(msg)
            cmd_id = msg["payload"]["commandId"]
            record = next((rec for rec in self.idempotency_records.values() if rec["commandId"] == cmd_id), None)
            if record and record.get("result"):
                reply = dict(record["result"])
                reply["correlationId"] = msg_id
            else:
                reply = {"protocolVersion": "0.1", "messageType": "skill.result.status",
                    "messageId": f"query-status-{self._next_seq()}", "correlationId": msg_id,
                    "saveId": self.save_id, "gameSessionId": self.game_session_id,
                    "senderInstanceId": "mock-mod-001", "sequenceNumber": self._next_seq(),
                    "worldRevision": self.world_revision, "sentAt": datetime.now(UTC).isoformat(),
                    "payload": {"commandId": cmd_id, "status": "in-flight" if record else "unknown"}}
            await self._send_json(writer, reply)

        elif mtype == "skill.cancel":
            payload = msg.get("payload", {})
            cmd_id = payload.get("commandId", "cmd-unknown")
            self.cancelled_commands.append(payload)

            # Check if command is in pending queue (cancel-before-execute)
            pending_idx = -1
            for i, (pmsg, _) in enumerate(self.pending_commands):
                if pmsg.get("payload", {}).get("commandId") == cmd_id:
                    pending_idx = i
                    break

            if pending_idx >= 0:
                self.pending_commands.pop(pending_idx)
                for rec in self.idempotency_records.values():
                    if rec.get("commandId") == cmd_id:
                        rec["status"] = "cancelled"

            result = {
                "protocolVersion": "0.1",
                "messageType": "skill.result",
                "messageId": f"msg-cancel-result-{self._next_seq()}",
                "correlationId": msg_id,
                "saveId": self.save_id,
                "gameSessionId": self.game_session_id,
                "senderInstanceId": "mock-mod-001",
                "sequenceNumber": self._next_seq(),
                "worldRevision": self.world_revision,
                "sentAt": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                "payload": {
                    "commandId": cmd_id,
                    "taskId": "task-cancelled",
                    "terminalState": "cancelled",
                    "completedCount": 0,
                    "skippedCount": 0,
                    "failedCount": 0,
                    "finalWorldRevision": self.world_revision,
                    "effects": [],
                    "error": None,
                    "retryRecommended": False,
                    "playerActionRequired": False,
                },
            }
            await self._send_json(writer, result)

        elif mtype == "skill.pause":
            payload = msg.get("payload", {})
            self.paused_commands.append(payload)

        elif mtype == "skill.resume":
            payload = msg.get("payload", {})
            self.resumed_commands.append(payload)
