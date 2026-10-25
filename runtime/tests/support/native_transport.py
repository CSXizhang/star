from unittest.mock import AsyncMock, MagicMock

from stardew_ai_runtime.protocol import Envelope


def _snapshot_env(payload_extra: dict | None = None) -> Envelope:
    payload = {
        "capturedRevision": 5,
        "companion": {
            "locationId": "Farm",
            "tileX": 64,
            "tileY": 15,
            "facingDirection": 2,
            "stamina": 250.0,
            "maxStamina": 270.0,
            "waterCanLevel": 3,
            "maxWaterCanLevel": 40,
            "hasWateringCan": True,
            "activity": "idle",
        },
        "world": {
            "currentLocation": "Farm",
            "timeOfDay": 610,
            "season": "spring",
            "dayOfMonth": 1,
            "isRaining": False,
        },
    }
    if payload_extra:
        payload.update(payload_extra)
    return Envelope.from_mapping(
        {
            "protocolVersion": "0.1",
            "messageType": "world.snapshot",
            "messageId": "snap-native-1",
            "saveId": "mock-save-id-001",
            "gameSessionId": "mock-session-001",
            "senderInstanceId": "mod-instance-001",
            "sequenceNumber": 1,
            "worldRevision": 5,
            "sentAt": "2026-09-16T00:00:00Z",
            "payload": payload,
        }
    )


def _client(snapshot: Envelope | None = None) -> MagicMock:
    client = MagicMock()
    client.is_connected = True
    client.save_id = "mock-save-id-001"
    client.game_session_id = "mock-session-001"
    client.world_revision = 5
    client.connect = AsyncMock()
    client.handshake = AsyncMock()
    client.close = AsyncMock()
    env = snapshot or _snapshot_env()
    client.latest_snapshot = env
    client.wait_for_snapshot = AsyncMock(return_value=env)
    client.execute_native_action = AsyncMock(return_value="cmd-native-1")
    client.wait_for_result = AsyncMock(
        return_value=Envelope.from_mapping(
            {
                "protocolVersion": "0.1",
                "messageType": "skill.result",
                "messageId": "res-1",
                "correlationId": "msg-exec-1",
                "saveId": "mock-save-id-001",
                "gameSessionId": "mock-session-001",
                "senderInstanceId": "mod-instance-001",
                "sequenceNumber": 1,
                "worldRevision": 6,
                "sentAt": "2026-09-16T00:00:01Z",
                "payload": {
                    "commandId": "cmd-native-1",
                    "taskId": "task-native-1",
                    "terminalState": "succeeded",
                    "completedCount": 1,
                    "skippedCount": 0,
                    "failedCount": 0,
                    "finalWorldRevision": 6,
                    "effects": [{"state": "fertilized"}],
                    "skillId": "apply-fertilizer",
                },
            }
        )
    )
    return client
