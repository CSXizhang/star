from datetime import UTC, datetime

import pytest

from stardew_ai_runtime.protocol import Envelope, ProtocolError


def valid_message() -> dict[str, object]:
    return {
        "protocolVersion": "0.1",
        "messageType": "runtime.hello",
        "messageId": "msg-1",
        "senderInstanceId": "runtime-1",
        "sequenceNumber": 0,
        "worldRevision": 0,
        "sentAt": "2026-08-24T00:00:00Z",
        "payload": {"runtimeVersion": "0.1.0"},
    }


def test_parse_envelope() -> None:
    envelope = Envelope.from_mapping(valid_message())

    assert envelope.protocol_version == "0.1"
    assert envelope.message_type == "runtime.hello"
    assert envelope.sent_at.utcoffset() is not None


@pytest.mark.parametrize("field", ["messageId", "payload", "worldRevision"])
def test_missing_required_field_is_rejected(field: str) -> None:
    message = valid_message()
    message.pop(field)

    with pytest.raises(ProtocolError, match="missing required fields"):
        Envelope.from_mapping(message)


def test_unknown_protocol_is_rejected() -> None:
    message = valid_message()
    message["protocolVersion"] = "1.0"

    with pytest.raises(ProtocolError, match="unsupported protocol"):
        Envelope.from_mapping(message)


@pytest.mark.parametrize("value", [True, False])
def test_bool_as_sequence_number_is_rejected(value: bool) -> None:
    message = valid_message()
    message["sequenceNumber"] = value

    with pytest.raises(ProtocolError, match="sequenceNumber must be a non-negative integer"):
        Envelope.from_mapping(message)


@pytest.mark.parametrize("value", [True, False])
def test_bool_as_world_revision_is_rejected(value: bool) -> None:
    message = valid_message()
    message["worldRevision"] = value

    with pytest.raises(ProtocolError, match="worldRevision must be a non-negative integer"):
        Envelope.from_mapping(message)


@pytest.mark.parametrize("value", [-1, -100])
def test_negative_int_fields_are_rejected(value: int) -> None:
    msg1 = valid_message()
    msg1["sequenceNumber"] = value
    with pytest.raises(ProtocolError, match="sequenceNumber must be a non-negative integer"):
        Envelope.from_mapping(msg1)

    msg2 = valid_message()
    msg2["worldRevision"] = value
    with pytest.raises(ProtocolError, match="worldRevision must be a non-negative integer"):
        Envelope.from_mapping(msg2)


@pytest.mark.parametrize("naive_str", [
    "2026-08-24T00:00:00",
    "2026-08-24 00:00:00",
    "2026-08-24T12:30:45.123",
])
def test_naive_datetime_is_rejected(naive_str: str) -> None:
    message = valid_message()
    message["sentAt"] = naive_str

    with pytest.raises(ProtocolError, match="must include timezone offset"):
        Envelope.from_mapping(message)


@pytest.mark.parametrize("invalid_str", [
    "not-a-date",
    "2026-99-99T99:99:99Z",
    12345,
    None,
])
def test_invalid_datetime_is_rejected(invalid_str: object) -> None:
    message = valid_message()
    message["sentAt"] = invalid_str

    with pytest.raises(ProtocolError, match="must be an RFC 3339 string"):
        Envelope.from_mapping(message)


def test_naive_expires_at_is_rejected() -> None:
    message = valid_message()
    message["expiresAt"] = "2026-08-24T00:00:10"

    with pytest.raises(ProtocolError, match="must include timezone offset"):
        Envelope.from_mapping(message)


def test_roundtrip_mapping() -> None:
    message = valid_message()
    message["saveId"] = "save-123"
    message["gameSessionId"] = "sess-456"
    message["expiresAt"] = "2026-08-24T00:00:10.000000Z"
    message["idempotencyKey"] = "idem-789"
    message["correlationId"] = "corr-000"

    env = Envelope.from_mapping(message)
    serialized = env.to_mapping()
    env2 = Envelope.from_mapping(serialized)

    assert env2.protocol_version == env.protocol_version
    assert env2.message_id == env.message_id
    assert env2.save_id == "save-123"
    assert env2.game_session_id == "sess-456"
    assert env2.idempotency_key == "idem-789"


def test_factory_methods() -> None:
    hello = Envelope.create_hello("runtime-1", session_token="tok-123")
    assert hello.message_type == "runtime.hello"
    assert hello.payload["sessionToken"] == "tok-123"
    assert hello.sequence_number == 0

    exec_env = Envelope.create_execute_water_zone(
        sender_instance_id="runtime-1",
        sequence_number=1,
        save_id="save-1",
        game_session_id="sess-1",
        world_revision=5,
        command_id="cmd-1",
        task_id="task-1",
        location_id="Farm",
        tiles=[{"x": 10, "y": 20}],
        idempotency_key="key-1",
        expires_at=datetime(2026, 8, 24, 0, 1, 0, tzinfo=UTC),
    )
    assert exec_env.message_type == "skill.execute"
    assert exec_env.payload["skillId"] == "water-zone"
    assert exec_env.payload["parameters"]["tiles"] == [{"x": 10, "y": 20}]

    cancel = Envelope.create_cancel(
        sender_instance_id="runtime-1",
        sequence_number=2,
        save_id="save-1",
        game_session_id="sess-1",
        world_revision=5,
        command_id="cmd-1",
        correlation_id=exec_env.message_id,
        reason="user cancel",
    )
    assert cancel.message_type == "skill.cancel"
    assert cancel.correlation_id == exec_env.message_id

    pause = Envelope.create_pause(
        sender_instance_id="runtime-1",
        sequence_number=3,
        save_id="save-1",
        game_session_id="sess-1",
        world_revision=5,
        command_id="cmd-1",
        correlation_id=exec_env.message_id,
        reason="menu opened",
    )
    assert pause.message_type == "skill.pause"

    resume = Envelope.create_resume(
        sender_instance_id="runtime-1",
        sequence_number=4,
        save_id="save-1",
        game_session_id="sess-1",
        world_revision=5,
        command_id="cmd-1",
        correlation_id=exec_env.message_id,
    )
    assert resume.message_type == "skill.resume"


def test_factory_farm_autonomy_skill_executes() -> None:
    expires = datetime(2026, 8, 24, 0, 1, 0, tzinfo=UTC)

    harvest = Envelope.create_execute_harvest_zone(
        sender_instance_id="runtime-1",
        sequence_number=10,
        save_id="save-1",
        game_session_id="sess-1",
        world_revision=7,
        command_id="cmd-h1",
        task_id="task-harvest-01",
        location_id="Farm",
        tiles=[{"x": 64, "y": 15}],
        idempotency_key="save-1:task-harvest-01:attempt-1",
        expires_at=expires,
    )
    assert harvest.message_type == "skill.execute"
    assert harvest.payload["commandId"] == "cmd-h1"
    assert harvest.payload["taskId"] == "task-harvest-01"
    assert harvest.payload["skillId"] == "harvest-zone"
    assert harvest.payload["skillVersion"] == "0.1"
    assert harvest.payload["expectedWorldRevision"] == 7
    assert harvest.payload["parameters"] == {
        "locationId": "Farm",
        "tiles": [{"x": 64, "y": 15}],
    }
    assert harvest.payload["budgets"] == {
        "maxGameMinutes": 60,
        "maxStamina": 50.0,
        "maxWater": 20,
    }
    assert harvest.payload["cancelPolicy"] == "safe-point"
    assert harvest.payload["policyDecisionId"] == "policy-allow"
    harvest_wire = harvest.to_mapping()
    assert harvest_wire["saveId"] == "save-1"
    assert harvest_wire["gameSessionId"] == "sess-1"
    assert harvest_wire["idempotencyKey"] == "save-1:task-harvest-01:attempt-1"
    assert harvest_wire["expiresAt"] == "2026-08-24T00:01:00.000000Z"

    deposit = Envelope.create_execute_deposit_chest(
        sender_instance_id="runtime-1",
        sequence_number=11,
        save_id="save-1",
        game_session_id="sess-1",
        world_revision=7,
        command_id="cmd-d1",
        task_id="task-deposit-01",
        location_id="Farm",
        chest_x=70,
        chest_y=12,
        item_ids=["(O)24", "(O)188"],
        idempotency_key="save-1:task-deposit-01:attempt-1",
        expires_at=expires,
    )
    assert deposit.message_type == "skill.execute"
    assert deposit.payload["skillId"] == "deposit-chest"
    assert deposit.payload["skillVersion"] == "0.1"
    assert deposit.payload["parameters"] == {
        "locationId": "Farm",
        "chestTile": {"x": 70, "y": 12},
        "itemIds": ["(O)24", "(O)188"],
    }
    deposit_wire = deposit.to_mapping()
    assert deposit_wire["idempotencyKey"] == "save-1:task-deposit-01:attempt-1"
    assert deposit_wire["expiresAt"] == "2026-08-24T00:01:00.000000Z"

    deposit_all = Envelope.create_execute_deposit_chest(
        sender_instance_id="runtime-1",
        sequence_number=12,
        save_id="save-1",
        game_session_id="sess-1",
        world_revision=7,
        command_id="cmd-d2",
        task_id="task-deposit-02",
        location_id="Farm",
        chest_x=70,
        chest_y=12,
        idempotency_key="save-1:task-deposit-02:attempt-1",
        expires_at=expires,
    )
    # Omitted item_ids means "all non-tool items": itemIds is absent on the wire.
    assert deposit_all.payload["parameters"] == {
        "locationId": "Farm",
        "chestTile": {"x": 70, "y": 12},
    }

    organize = Envelope.create_execute_organize_chest(
        sender_instance_id="runtime-1",
        sequence_number=13,
        save_id="save-1",
        game_session_id="sess-1",
        world_revision=7,
        command_id="cmd-o1",
        task_id="task-organize-01",
        location_id="Farm",
        chest_x=70,
        chest_y=12,
        idempotency_key="save-1:task-organize-01:attempt-1",
        expires_at=expires,
    )
    assert organize.message_type == "skill.execute"
    assert organize.payload["skillId"] == "organize-chest"
    assert organize.payload["skillVersion"] == "0.1"
    assert organize.payload["parameters"] == {
        "locationId": "Farm",
        "chestTile": {"x": 70, "y": 12},
    }
    organize_wire = organize.to_mapping()
    assert organize_wire["idempotencyKey"] == "save-1:task-organize-01:attempt-1"
    assert organize_wire["expiresAt"] == "2026-08-24T00:01:00.000000Z"


def test_create_execute_hoe_tiles_envelope() -> None:
    expires = datetime(2026, 8, 24, 0, 1, 0, tzinfo=UTC)
    hoe = Envelope.create_execute_hoe_tiles(
        sender_instance_id="runtime-1",
        sequence_number=14,
        save_id="save-1",
        game_session_id="sess-1",
        world_revision=8,
        command_id="cmd-h1",
        task_id="task-hoe-01",
        location_id="Farm",
        tiles=[{"x": 64, "y": 15}, {"x": 65, "y": 15}],
        idempotency_key="save-1:task-hoe-01:attempt-1",
        expires_at=expires,
    )
    assert hoe.message_type == "skill.execute"
    assert hoe.payload["skillId"] == "hoe-tiles"
    assert hoe.payload["skillVersion"] == "0.1"
    assert hoe.payload["parameters"] == {
        "locationId": "Farm",
        "tiles": [{"x": 64, "y": 15}, {"x": 65, "y": 15}],
    }
    hoe_wire = hoe.to_mapping()
    assert hoe_wire["idempotencyKey"] == "save-1:task-hoe-01:attempt-1"
    assert hoe_wire["expiresAt"] == "2026-08-24T00:01:00.000000Z"


def test_create_execute_plant_seeds_envelope() -> None:
    expires = datetime(2026, 8, 24, 0, 1, 0, tzinfo=UTC)
    plant = Envelope.create_execute_plant_seeds(
        sender_instance_id="runtime-1",
        sequence_number=15,
        save_id="save-1",
        game_session_id="sess-1",
        world_revision=9,
        command_id="cmd-p1",
        task_id="task-plant-01",
        location_id="Farm",
        tiles=[{"x": 64, "y": 15}],
        seed_item_id="(O)472",
        idempotency_key="save-1:task-plant-01:attempt-1",
        expires_at=expires,
    )
    assert plant.message_type == "skill.execute"
    assert plant.payload["skillId"] == "plant-seeds"
    assert plant.payload["skillVersion"] == "0.1"
    assert plant.payload["parameters"] == {
        "locationId": "Farm",
        "tiles": [{"x": 64, "y": 15}],
        "seedItemId": "(O)472",
    }
    plant_wire = plant.to_mapping()
    assert plant_wire["idempotencyKey"] == "save-1:task-plant-01:attempt-1"
    assert plant_wire["expiresAt"] == "2026-08-24T00:01:00.000000Z"


def test_create_execute_ship_items_envelope() -> None:
    expires = datetime(2026, 8, 24, 0, 1, 0, tzinfo=UTC)
    ship = Envelope.create_execute_ship_items(
        sender_instance_id="runtime-1",
        sequence_number=16,
        save_id="save-1",
        game_session_id="sess-1",
        world_revision=10,
        command_id="cmd-s1",
        task_id="task-ship-01",
        location_id="Farm",
        items=[{"itemId": "(O)24", "count": 2}],
        idempotency_key="save-1:task-ship-01:attempt-1",
        expires_at=expires,
    )
    assert ship.message_type == "skill.execute"
    assert ship.payload["skillId"] == "ship-items"
    assert ship.payload["skillVersion"] == "0.1"
    assert ship.payload["parameters"] == {
        "locationId": "Farm",
        "items": [{"itemId": "(O)24", "count": 2}],
    }
    assert ship.payload["budgets"] == {
        "maxGameMinutes": 60,
        "maxStamina": 50.0,
        "maxWater": 20,
    }
    ship_wire = ship.to_mapping()
    assert ship_wire["idempotencyKey"] == "save-1:task-ship-01:attempt-1"
    assert ship_wire["expiresAt"] == "2026-08-24T00:01:00.000000Z"


def test_create_execute_navigate_to_envelope() -> None:
    expires = datetime(2026, 8, 24, 0, 1, 0, tzinfo=UTC)
    nav = Envelope.create_execute_navigate_to(
        sender_instance_id="runtime-1",
        sequence_number=17,
        save_id="save-1",
        game_session_id="sess-1",
        world_revision=11,
        command_id="cmd-n1",
        task_id="task-nav-01",
        location_id="Town",
        tile={"x": 43, "y": 58},
        idempotency_key="save-1:task-nav-01:attempt-1",
        expires_at=expires,
    )
    assert nav.message_type == "skill.execute"
    assert nav.payload["skillId"] == "navigate-to"
    assert nav.payload["skillVersion"] == "0.1"
    assert nav.payload["parameters"] == {
        "locationId": "Town",
        "tile": {"x": 43, "y": 58},
    }
    assert nav.payload["budgets"] == {
        "maxGameMinutes": 120,
        "maxStamina": 50.0,
        "maxWater": 0,
    }
    nav_wire = nav.to_mapping()
    assert nav_wire["idempotencyKey"] == "save-1:task-nav-01:attempt-1"
    assert nav_wire["expiresAt"] == "2026-08-24T00:01:00.000000Z"


def test_create_execute_purchase_items_envelope() -> None:
    expires = datetime(2026, 8, 24, 0, 1, 0, tzinfo=UTC)
    purchase = Envelope.create_execute_purchase_items(
        sender_instance_id="runtime-1",
        sequence_number=18,
        save_id="save-1",
        game_session_id="sess-1",
        world_revision=12,
        command_id="cmd-p1",
        task_id="task-purchase-01",
        shop_id="SeedShop",
        location_id="SeedShop",
        items=[{"itemId": "(O)472", "count": 2}],
        budget_limit=100,
        idempotency_key="save-1:task-purchase-01:attempt-1",
        expires_at=expires,
    )
    assert purchase.message_type == "skill.execute"
    assert purchase.payload["skillId"] == "purchase-items"
    assert purchase.payload["skillVersion"] == "0.1"
    assert purchase.payload["parameters"] == {
        "shopId": "SeedShop",
        "locationId": "SeedShop",
        "items": [{"itemId": "(O)472", "count": 2}],
        "budgetLimit": 100,
    }
    assert purchase.payload["budgets"] == {
        "maxGameMinutes": 60,
        "maxStamina": 50.0,
        "maxWater": 0,
    }
    wire = purchase.to_mapping()
    assert wire["idempotencyKey"] == "save-1:task-purchase-01:attempt-1"
    assert wire["expiresAt"] == "2026-08-24T00:01:00.000000Z"


