from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


class ProtocolError(ValueError):
    """Raised when an incoming envelope violates the baseline contract."""


# Explicit allow-list of the agricultural/husbandry skills and the exact parameter
# keys each one accepts. This is not a generic execution entry: every key is fixed
# here, schedule, transport, MCP schema and the C# handler all validate the same
# contract, and an unknown skill/parameter is rejected instead of being forwarded.
NATIVE_ACTION_SKILLS: dict[str, frozenset[str]] = {
    "refill-watering-can": frozenset({"locationId", "tiles"}),
    "apply-fertilizer": frozenset({"locationId", "tiles", "fertilizerItemId"}),
    "clear-debris": frozenset({"locationId", "tiles"}),
    "pickup-items": frozenset({"locationId", "tiles"}),
    "chop-tree": frozenset({"locationId", "tiles"}),
    "insert-machine": frozenset({"locationId", "tile", "itemId", "itemCount"}),
    "collect-machine": frozenset({"locationId", "tiles"}),
    "pet-animal": frozenset({"locationId", "tiles", "animalName"}),
    "feed-animals": frozenset({"locationId", "buildingName"}),
    "toggle-animal-door": frozenset({"locationId", "tiles"}),
    "collect-animal-produce": frozenset({"locationId", "tiles", "animalName"}),
}

NATIVE_ACTION_TASK_PREFIXES: dict[str, str] = {
    "refill-watering-can": "task-refill-",
    "apply-fertilizer": "task-fert-",
    "clear-debris": "task-clear-",
    "pickup-items": "task-pickup-",
    "chop-tree": "task-chop-",
    "insert-machine": "task-mins-",
    "collect-machine": "task-mcol-",
    "pet-animal": "task-pet-",
    "feed-animals": "task-feed-",
    "toggle-animal-door": "task-door-",
    "collect-animal-produce": "task-aprod-",
}


def validate_native_action_parameters(skill_id: str, parameters: Mapping[str, Any]) -> None:
    """Reject unknown native skills and parameter keys before any dispatch."""
    allowed = NATIVE_ACTION_SKILLS.get(skill_id)
    if allowed is None:
        raise ProtocolError(f"unsupported native action skill '{skill_id}'")
    unknown = set(parameters) - set(allowed)
    if unknown:
        raise ProtocolError(
            f"unsupported parameter(s) for '{skill_id}': {', '.join(sorted(unknown))}"
        )
    if "locationId" not in parameters:
        raise ProtocolError(f"'locationId' is required for '{skill_id}'")


@dataclass(frozen=True, slots=True)
class Envelope:
    protocol_version: str
    message_type: str
    message_id: str
    sender_instance_id: str
    sequence_number: int
    world_revision: int
    sent_at: datetime
    payload: Mapping[str, Any]
    save_id: str | None = None
    game_session_id: str | None = None
    correlation_id: str | None = None
    expires_at: datetime | None = None
    idempotency_key: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> Envelope:
        required = (
            "protocolVersion",
            "messageType",
            "messageId",
            "senderInstanceId",
            "sequenceNumber",
            "worldRevision",
            "sentAt",
            "payload",
        )
        missing = [field for field in required if field not in value]
        if missing:
            raise ProtocolError(f"missing required fields: {', '.join(missing)}")
        if value["protocolVersion"] != "0.1":
            raise ProtocolError("unsupported protocol version")
        seq = value["sequenceNumber"]
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            raise ProtocolError("sequenceNumber must be a non-negative integer (booleans rejected)")
        rev = value["worldRevision"]
        if isinstance(rev, bool) or not isinstance(rev, int) or rev < 0:
            raise ProtocolError("worldRevision must be a non-negative integer (booleans rejected)")
        if not isinstance(value["payload"], Mapping):
            raise ProtocolError("payload must be an object")

        return cls(
            protocol_version=value["protocolVersion"],
            message_type=value["messageType"],
            message_id=value["messageId"],
            sender_instance_id=value["senderInstanceId"],
            sequence_number=seq,
            world_revision=rev,
            sent_at=_parse_datetime(value["sentAt"], "sentAt"),
            payload=value["payload"],
            save_id=value.get("saveId"),
            game_session_id=value.get("gameSessionId"),
            correlation_id=value.get("correlationId"),
            expires_at=_parse_optional_datetime(value.get("expiresAt"), "expiresAt"),
            idempotency_key=value.get("idempotencyKey"),
        )

    def to_mapping(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "protocolVersion": self.protocol_version,
            "messageType": self.message_type,
            "messageId": self.message_id,
            "senderInstanceId": self.sender_instance_id,
            "sequenceNumber": self.sequence_number,
            "worldRevision": self.world_revision,
            "sentAt": _format_datetime(self.sent_at),
            "payload": dict(self.payload),
        }
        if self.correlation_id is not None:
            result["correlationId"] = self.correlation_id
        if self.save_id is not None:
            result["saveId"] = self.save_id
        if self.game_session_id is not None:
            result["gameSessionId"] = self.game_session_id
        if self.expires_at is not None:
            result["expiresAt"] = _format_datetime(self.expires_at)
        if self.idempotency_key is not None:
            result["idempotencyKey"] = self.idempotency_key
        return result

    @classmethod
    def create_hello(
        cls,
        sender_instance_id: str,
        sequence_number: int = 0,
        runtime_version: str = "0.1.0",
        session_token: str | None = None,
        features: Mapping[str, bool] | None = None,
    ) -> Envelope:
        return cls(
            protocol_version="0.1",
            message_type="runtime.hello",
            message_id=f"msg-hello-{uuid.uuid4().hex[:8]}",
            sender_instance_id=sender_instance_id,
            sequence_number=sequence_number,
            world_revision=0,
            sent_at=datetime.now(UTC),
            payload={
                "supportedProtocolVersions": ["0.1"],
                "runtimeVersion": runtime_version,
                "sessionToken": session_token,
                "features": dict(features or {
                    "planner": False,
                    "memory": False,
                    "voice": False,
                    "mcp": False,
                }),
            },
        )

    @classmethod
    def create_execute_native_action(
        cls,
        sender_instance_id: str,
        sequence_number: int,
        save_id: str,
        game_session_id: str,
        world_revision: int,
        command_id: str,
        task_id: str,
        skill_id: str,
        parameters: Mapping[str, Any],
        idempotency_key: str,
        expires_at: datetime,
        max_game_minutes: int = 60,
        max_stamina: float = 50.0,
        max_water: int = 20,
        cancel_policy: str = "safe-point",
        policy_decision_id: str = "policy-allow",
    ) -> Envelope:
        """Builds skill.execute for one explicitly allow-listed native action."""
        validate_native_action_parameters(skill_id, parameters)
        return cls(
            protocol_version="0.1",
            message_type="skill.execute",
            message_id=f"msg-exec-{uuid.uuid4().hex[:8]}",
            sender_instance_id=sender_instance_id,
            sequence_number=sequence_number,
            world_revision=world_revision,
            sent_at=datetime.now(UTC),
            save_id=save_id,
            game_session_id=game_session_id,
            idempotency_key=idempotency_key,
            expires_at=expires_at,
            payload={
                "commandId": command_id,
                "taskId": task_id,
                "skillId": skill_id,
                "skillVersion": "0.1",
                "expectedWorldRevision": world_revision,
                "parameters": dict(parameters),
                "budgets": {
                    "maxGameMinutes": max_game_minutes,
                    "maxStamina": max_stamina,
                    "maxWater": max_water,
                },
                "cancelPolicy": cancel_policy,
                "policyDecisionId": policy_decision_id,
            },
        )

    @classmethod
    def create_execute_water_zone(
        cls,
        sender_instance_id: str,
        sequence_number: int,
        save_id: str,
        game_session_id: str,
        world_revision: int,
        command_id: str,
        task_id: str,
        location_id: str,
        tiles: list[dict[str, int]],
        idempotency_key: str,
        expires_at: datetime,
        max_game_minutes: int = 60,
        max_stamina: float = 50.0,
        max_water: int = 20,
        cancel_policy: str = "safe-point",
        policy_decision_id: str = "policy-allow",
    ) -> Envelope:
        return cls(
            protocol_version="0.1",
            message_type="skill.execute",
            message_id=f"msg-exec-{uuid.uuid4().hex[:8]}",
            sender_instance_id=sender_instance_id,
            sequence_number=sequence_number,
            world_revision=world_revision,
            sent_at=datetime.now(UTC),
            save_id=save_id,
            game_session_id=game_session_id,
            idempotency_key=idempotency_key,
            expires_at=expires_at,
            payload={
                "commandId": command_id,
                "taskId": task_id,
                "skillId": "water-zone",
                "skillVersion": "0.1",
                "expectedWorldRevision": world_revision,
                "parameters": {
                    "locationId": location_id,
                    "tiles": tiles,
                },
                "budgets": {
                    "maxGameMinutes": max_game_minutes,
                    "maxStamina": max_stamina,
                    "maxWater": max_water,
                },
                "cancelPolicy": cancel_policy,
                "policyDecisionId": policy_decision_id,
            },
        )

    @classmethod
    def create_execute_harvest_zone(
        cls,
        sender_instance_id: str,
        sequence_number: int,
        save_id: str,
        game_session_id: str,
        world_revision: int,
        command_id: str,
        task_id: str,
        location_id: str,
        tiles: list[dict[str, int]],
        idempotency_key: str,
        expires_at: datetime,
        max_game_minutes: int = 60,
        max_stamina: float = 50.0,
        max_water: int = 20,
        cancel_policy: str = "safe-point",
        policy_decision_id: str = "policy-allow",
    ) -> Envelope:
        return cls(
            protocol_version="0.1",
            message_type="skill.execute",
            message_id=f"msg-exec-{uuid.uuid4().hex[:8]}",
            sender_instance_id=sender_instance_id,
            sequence_number=sequence_number,
            world_revision=world_revision,
            sent_at=datetime.now(UTC),
            save_id=save_id,
            game_session_id=game_session_id,
            idempotency_key=idempotency_key,
            expires_at=expires_at,
            payload={
                "commandId": command_id,
                "taskId": task_id,
                "skillId": "harvest-zone",
                "skillVersion": "0.1",
                "expectedWorldRevision": world_revision,
                "parameters": {
                    "locationId": location_id,
                    "tiles": tiles,
                },
                "budgets": {
                    "maxGameMinutes": max_game_minutes,
                    "maxStamina": max_stamina,
                    "maxWater": max_water,
                },
                "cancelPolicy": cancel_policy,
                "policyDecisionId": policy_decision_id,
            },
        )

    @classmethod
    def create_execute_hoe_tiles(
        cls,
        sender_instance_id: str,
        sequence_number: int,
        save_id: str,
        game_session_id: str,
        world_revision: int,
        command_id: str,
        task_id: str,
        location_id: str,
        tiles: list[dict[str, int]],
        idempotency_key: str,
        expires_at: datetime,
        max_game_minutes: int = 60,
        max_stamina: float = 50.0,
        max_water: int = 20,
        cancel_policy: str = "safe-point",
        policy_decision_id: str = "policy-allow",
    ) -> Envelope:
        return cls(
            protocol_version="0.1",
            message_type="skill.execute",
            message_id=f"msg-exec-{uuid.uuid4().hex[:8]}",
            sender_instance_id=sender_instance_id,
            sequence_number=sequence_number,
            world_revision=world_revision,
            sent_at=datetime.now(UTC),
            save_id=save_id,
            game_session_id=game_session_id,
            idempotency_key=idempotency_key,
            expires_at=expires_at,
            payload={
                "commandId": command_id,
                "taskId": task_id,
                "skillId": "hoe-tiles",
                "skillVersion": "0.1",
                "expectedWorldRevision": world_revision,
                "parameters": {
                    "locationId": location_id,
                    "tiles": tiles,
                },
                "budgets": {
                    "maxGameMinutes": max_game_minutes,
                    "maxStamina": max_stamina,
                    "maxWater": max_water,
                },
                "cancelPolicy": cancel_policy,
                "policyDecisionId": policy_decision_id,
            },
        )

    @classmethod
    def create_execute_plant_seeds(
        cls,
        sender_instance_id: str,
        sequence_number: int,
        save_id: str,
        game_session_id: str,
        world_revision: int,
        command_id: str,
        task_id: str,
        location_id: str,
        tiles: list[dict[str, int]],
        seed_item_id: str,
        idempotency_key: str,
        expires_at: datetime,
        max_game_minutes: int = 60,
        max_stamina: float = 50.0,
        max_water: int = 20,
        cancel_policy: str = "safe-point",
        policy_decision_id: str = "policy-allow",
    ) -> Envelope:
        return cls(
            protocol_version="0.1",
            message_type="skill.execute",
            message_id=f"msg-exec-{uuid.uuid4().hex[:8]}",
            sender_instance_id=sender_instance_id,
            sequence_number=sequence_number,
            world_revision=world_revision,
            sent_at=datetime.now(UTC),
            save_id=save_id,
            game_session_id=game_session_id,
            idempotency_key=idempotency_key,
            expires_at=expires_at,
            payload={
                "commandId": command_id,
                "taskId": task_id,
                "skillId": "plant-seeds",
                "skillVersion": "0.1",
                "expectedWorldRevision": world_revision,
                "parameters": {
                    "locationId": location_id,
                    "tiles": tiles,
                    "seedItemId": seed_item_id,
                },
                "budgets": {
                    "maxGameMinutes": max_game_minutes,
                    "maxStamina": max_stamina,
                    "maxWater": max_water,
                },
                "cancelPolicy": cancel_policy,
                "policyDecisionId": policy_decision_id,
            },
        )

    @classmethod
    def create_execute_deposit_chest(
        cls,
        sender_instance_id: str,
        sequence_number: int,
        save_id: str,
        game_session_id: str,
        world_revision: int,
        command_id: str,
        task_id: str,
        location_id: str,
        chest_x: int,
        chest_y: int,
        idempotency_key: str,
        expires_at: datetime,
        item_ids: list[str] | None = None,
        max_game_minutes: int = 60,
        max_stamina: float = 50.0,
        max_water: int = 20,
        cancel_policy: str = "safe-point",
        policy_decision_id: str = "policy-allow",
    ) -> Envelope:
        parameters: dict[str, Any] = {
            "locationId": location_id,
            "chestTile": {"x": chest_x, "y": chest_y},
        }
        if item_ids is not None:
            parameters["itemIds"] = item_ids
        return cls(
            protocol_version="0.1",
            message_type="skill.execute",
            message_id=f"msg-exec-{uuid.uuid4().hex[:8]}",
            sender_instance_id=sender_instance_id,
            sequence_number=sequence_number,
            world_revision=world_revision,
            sent_at=datetime.now(UTC),
            save_id=save_id,
            game_session_id=game_session_id,
            idempotency_key=idempotency_key,
            expires_at=expires_at,
            payload={
                "commandId": command_id,
                "taskId": task_id,
                "skillId": "deposit-chest",
                "skillVersion": "0.1",
                "expectedWorldRevision": world_revision,
                "parameters": parameters,
                "budgets": {
                    "maxGameMinutes": max_game_minutes,
                    "maxStamina": max_stamina,
                    "maxWater": max_water,
                },
                "cancelPolicy": cancel_policy,
                "policyDecisionId": policy_decision_id,
            },
        )

    @classmethod
    def create_execute_organize_chest(
        cls,
        sender_instance_id: str,
        sequence_number: int,
        save_id: str,
        game_session_id: str,
        world_revision: int,
        command_id: str,
        task_id: str,
        location_id: str,
        chest_x: int,
        chest_y: int,
        idempotency_key: str,
        expires_at: datetime,
        max_game_minutes: int = 60,
        max_stamina: float = 50.0,
        max_water: int = 20,
        cancel_policy: str = "safe-point",
        policy_decision_id: str = "policy-allow",
    ) -> Envelope:
        return cls(
            protocol_version="0.1",
            message_type="skill.execute",
            message_id=f"msg-exec-{uuid.uuid4().hex[:8]}",
            sender_instance_id=sender_instance_id,
            sequence_number=sequence_number,
            world_revision=world_revision,
            sent_at=datetime.now(UTC),
            save_id=save_id,
            game_session_id=game_session_id,
            idempotency_key=idempotency_key,
            expires_at=expires_at,
            payload={
                "commandId": command_id,
                "taskId": task_id,
                "skillId": "organize-chest",
                "skillVersion": "0.1",
                "expectedWorldRevision": world_revision,
                "parameters": {
                    "locationId": location_id,
                    "chestTile": {"x": chest_x, "y": chest_y},
                },
                "budgets": {
                    "maxGameMinutes": max_game_minutes,
                    "maxStamina": max_stamina,
                    "maxWater": max_water,
                },
                "cancelPolicy": cancel_policy,
                "policyDecisionId": policy_decision_id,
            },
        )

    @classmethod
    def create_execute_withdraw_chest(
        cls,
        sender_instance_id: str,
        sequence_number: int,
        save_id: str,
        game_session_id: str,
        world_revision: int,
        command_id: str,
        task_id: str,
        location_id: str,
        chest_x: int,
        chest_y: int,
        items: list[dict[str, Any]],
        idempotency_key: str,
        expires_at: datetime,
        max_game_minutes: int = 60,
        max_stamina: float = 50.0,
        max_water: int = 20,
        cancel_policy: str = "safe-point",
        policy_decision_id: str = "policy-allow",
    ) -> Envelope:
        parameters: dict[str, Any] = {
            "locationId": location_id,
            "chestTile": {"x": chest_x, "y": chest_y},
            "items": items,
        }
        return cls(
            protocol_version="0.1",
            message_type="skill.execute",
            message_id=f"msg-exec-{uuid.uuid4().hex[:8]}",
            sender_instance_id=sender_instance_id,
            sequence_number=sequence_number,
            world_revision=world_revision,
            sent_at=datetime.now(UTC),
            save_id=save_id,
            game_session_id=game_session_id,
            idempotency_key=idempotency_key,
            expires_at=expires_at,
            payload={
                "commandId": command_id,
                "taskId": task_id,
                "skillId": "withdraw-chest",
                "skillVersion": "0.1",
                "expectedWorldRevision": world_revision,
                "parameters": parameters,
                "budgets": {
                    "maxGameMinutes": max_game_minutes,
                    "maxStamina": max_stamina,
                    "maxWater": max_water,
                },
                "cancelPolicy": cancel_policy,
                "policyDecisionId": policy_decision_id,
            },
        )

    @classmethod
    def create_execute_ship_items(
        cls,
        sender_instance_id: str,
        sequence_number: int,
        save_id: str,
        game_session_id: str,
        world_revision: int,
        command_id: str,
        task_id: str,
        location_id: str,
        items: list[dict[str, Any]],
        idempotency_key: str,
        expires_at: datetime,
        max_game_minutes: int = 60,
        max_stamina: float = 50.0,
        max_water: int = 20,
        cancel_policy: str = "safe-point",
        policy_decision_id: str = "policy-allow",
    ) -> Envelope:
        return cls(
            protocol_version="0.1",
            message_type="skill.execute",
            message_id=f"msg-exec-{uuid.uuid4().hex[:8]}",
            sender_instance_id=sender_instance_id,
            sequence_number=sequence_number,
            world_revision=world_revision,
            sent_at=datetime.now(UTC),
            save_id=save_id,
            game_session_id=game_session_id,
            idempotency_key=idempotency_key,
            expires_at=expires_at,
            payload={
                "commandId": command_id,
                "taskId": task_id,
                "skillId": "ship-items",
                "skillVersion": "0.1",
                "expectedWorldRevision": world_revision,
                "parameters": {
                    "locationId": location_id,
                    "items": items,
                },
                "budgets": {
                    "maxGameMinutes": max_game_minutes,
                    "maxStamina": max_stamina,
                    "maxWater": max_water,
                },
                "cancelPolicy": cancel_policy,
                "policyDecisionId": policy_decision_id,
            },
        )

    @classmethod
    def create_execute_purchase_items(
        cls,
        sender_instance_id: str,
        sequence_number: int,
        save_id: str,
        game_session_id: str,
        world_revision: int,
        command_id: str,
        task_id: str,
        shop_id: str,
        location_id: str,
        items: list[dict[str, Any]],
        budget_limit: int,
        idempotency_key: str,
        expires_at: datetime,
        max_game_minutes: int = 60,
        max_stamina: float = 50.0,
        max_water: int = 0,
        cancel_policy: str = "safe-point",
        policy_decision_id: str = "policy-allow",
    ) -> Envelope:
        return cls(
            protocol_version="0.1",
            message_type="skill.execute",
            message_id=f"msg-exec-{uuid.uuid4().hex[:8]}",
            sender_instance_id=sender_instance_id,
            sequence_number=sequence_number,
            world_revision=world_revision,
            sent_at=datetime.now(UTC),
            save_id=save_id,
            game_session_id=game_session_id,
            idempotency_key=idempotency_key,
            expires_at=expires_at,
            payload={
                "commandId": command_id,
                "taskId": task_id,
                "skillId": "purchase-items",
                "skillVersion": "0.1",
                "expectedWorldRevision": world_revision,
                "parameters": {
                    "shopId": shop_id,
                    "locationId": location_id,
                    "items": items,
                    "budgetLimit": budget_limit,
                },
                "budgets": {
                    "maxGameMinutes": max_game_minutes,
                    "maxStamina": max_stamina,
                    "maxWater": max_water,
                },
                "cancelPolicy": cancel_policy,
                "policyDecisionId": policy_decision_id,
            },
        )

    @classmethod
    def create_execute_navigate_to(
        cls,
        sender_instance_id: str,
        sequence_number: int,
        save_id: str,
        game_session_id: str,
        world_revision: int,
        command_id: str,
        task_id: str,
        location_id: str,
        tile: dict[str, int],
        idempotency_key: str,
        expires_at: datetime,
        max_game_minutes: int = 120,
        max_stamina: float = 50.0,
        max_water: int = 0,
        cancel_policy: str = "safe-point",
        policy_decision_id: str = "policy-allow",
    ) -> Envelope:
        return cls(
            protocol_version="0.1",
            message_type="skill.execute",
            message_id=f"msg-exec-{uuid.uuid4().hex[:8]}",
            sender_instance_id=sender_instance_id,
            sequence_number=sequence_number,
            world_revision=world_revision,
            sent_at=datetime.now(UTC),
            save_id=save_id,
            game_session_id=game_session_id,
            idempotency_key=idempotency_key,
            expires_at=expires_at,
            payload={
                "commandId": command_id,
                "taskId": task_id,
                "skillId": "navigate-to",
                "skillVersion": "0.1",
                "expectedWorldRevision": world_revision,
                "parameters": {
                    "locationId": location_id,
                    "tile": tile,
                },
                "budgets": {
                    "maxGameMinutes": max_game_minutes,
                    "maxStamina": max_stamina,
                    "maxWater": max_water,
                },
                "cancelPolicy": cancel_policy,
                "policyDecisionId": policy_decision_id,
            },
        )

    @classmethod
    def create_cancel(
        cls,
        sender_instance_id: str,
        sequence_number: int,
        save_id: str,
        game_session_id: str,
        world_revision: int,
        command_id: str,
        correlation_id: str,
        reason: str,
        requested_by_player: bool = True,
        cancel_policy: str = "safe-point",
    ) -> Envelope:
        return cls(
            protocol_version="0.1",
            message_type="skill.cancel",
            message_id=f"msg-cancel-{uuid.uuid4().hex[:8]}",
            correlation_id=correlation_id,
            sender_instance_id=sender_instance_id,
            sequence_number=sequence_number,
            world_revision=world_revision,
            sent_at=datetime.now(UTC),
            save_id=save_id,
            game_session_id=game_session_id,
            payload={
                "commandId": command_id,
                "reason": reason,
                "requestedByPlayer": requested_by_player,
                "cancelPolicy": cancel_policy,
            },
        )

    @classmethod
    def create_pause(
        cls,
        sender_instance_id: str,
        sequence_number: int,
        save_id: str,
        game_session_id: str,
        world_revision: int,
        command_id: str,
        correlation_id: str,
        reason: str,
    ) -> Envelope:
        return cls(
            protocol_version="0.1",
            message_type="skill.pause",
            message_id=f"msg-pause-{uuid.uuid4().hex[:8]}",
            correlation_id=correlation_id,
            sender_instance_id=sender_instance_id,
            sequence_number=sequence_number,
            world_revision=world_revision,
            sent_at=datetime.now(UTC),
            save_id=save_id,
            game_session_id=game_session_id,
            payload={
                "commandId": command_id,
                "reason": reason,
            },
        )

    @classmethod
    def create_resume(
        cls,
        sender_instance_id: str,
        sequence_number: int,
        save_id: str,
        game_session_id: str,
        world_revision: int,
        command_id: str,
        correlation_id: str,
    ) -> Envelope:
        return cls(
            protocol_version="0.1",
            message_type="skill.resume",
            message_id=f"msg-resume-{uuid.uuid4().hex[:8]}",
            correlation_id=correlation_id,
            sender_instance_id=sender_instance_id,
            sequence_number=sequence_number,
            world_revision=world_revision,
            sent_at=datetime.now(UTC),
            save_id=save_id,
            game_session_id=game_session_id,
            payload={
                "commandId": command_id,
            },
        )

    @classmethod
    def create_chat_submit(
        cls,
        sender_instance_id: str,
        request_id: str,
        text: str,
        source: str = "text",
        save_id: str | None = None,
        game_session_id: str | None = None,
    ) -> Envelope:
        return cls(
            protocol_version="0.1",
            message_type="chat.submit",
            message_id=f"msg-chat-{uuid.uuid4().hex[:8]}",
            sender_instance_id=sender_instance_id,
            sequence_number=0,
            world_revision=0,
            sent_at=datetime.now(UTC),
            save_id=save_id,
            game_session_id=game_session_id,
            payload={
                "requestId": request_id,
                "text": text,
                "source": source,
                "saveId": save_id,
            },
        )

    @classmethod
    def create_chat_reply(
        cls,
        sender_instance_id: str,
        request_id: str,
        status: str,
        reply_text: str,
        save_id: str | None = None,
        tokens_used: int | None = None,
        prompt_tokens: int | None = None,
        output_tokens: int | None = None,
        cached_tokens: int | None = None,
        cache_read_tokens: int | None = None,
        cache_write_tokens: int | None = None,
        model_calls: int | None = None,
        conversation_id: str | None = None,
        error: str | None = None,
        usage_source: str | None = None,
        provider: str | None = None,
        command_id: str | None = None,
        command_complete: bool | None = None,
    ) -> Envelope:
        payload: dict[str, Any] = {
            "requestId": request_id,
            "status": status,
            "replyText": reply_text,
        }
        if tokens_used is not None:
            payload["tokensUsed"] = tokens_used
        if prompt_tokens is not None:
            payload["promptTokens"] = prompt_tokens
        if output_tokens is not None:
            payload["outputTokens"] = output_tokens
        if cached_tokens is not None:
            payload["cachedTokens"] = cached_tokens
        # Explicit usage split: input (promptTokens), cache read, cache write,
        # output, total (tokensUsed) and the number of model calls. Unknown fields
        # stay absent so the UI shows "unknown" instead of a fabricated zero.
        if cache_read_tokens is not None:
            payload["cacheReadTokens"] = cache_read_tokens
        if cache_write_tokens is not None:
            payload["cacheWriteTokens"] = cache_write_tokens
        if model_calls is not None:
            payload["modelCalls"] = model_calls
        if save_id is not None:
            # Payload-level save partition so a stale reply can never overwrite
            # another save's visible F8 state.
            payload["saveId"] = save_id
        if conversation_id is not None:
            payload["conversationId"] = conversation_id
        if error is not None:
            payload["error"] = error
        if usage_source is not None:
            payload["usageSource"] = usage_source
        if provider is not None:
            payload["provider"] = provider
        if command_id is not None:
            payload["commandId"] = command_id
        if command_complete is not None:
            payload["commandComplete"] = command_complete
        return cls(
            protocol_version="0.1",
            message_type="chat.reply",
            message_id=f"msg-reply-{uuid.uuid4().hex[:8]}",
            sender_instance_id=sender_instance_id,
            sequence_number=0,
            world_revision=0,
            sent_at=datetime.now(UTC),
            save_id=save_id,
            payload=payload,
        )

    @classmethod
    def create_chat_cancel(
        cls,
        sender_instance_id: str,
        request_id: str | None = None,
        reason: str = "player_cancelled",
        save_id: str | None = None,
    ) -> Envelope:
        payload: dict[str, Any] = {
            "reason": reason,
        }
        if request_id is not None:
            payload["requestId"] = request_id
        return cls(
            protocol_version="0.1",
            message_type="chat.cancel",
            message_id=f"msg-cancel-{uuid.uuid4().hex[:8]}",
            sender_instance_id=sender_instance_id,
            sequence_number=0,
            world_revision=0,
            sent_at=datetime.now(UTC),
            save_id=save_id,
            payload=payload,
        )

    @classmethod
    def create_autonomy_control(
        cls, sender_instance_id: str, request_id: str, save_id: str,
        action: str, **parameters: Any
    ) -> Envelope:
        """Control the persisted free-mode state; never dispatches an agent."""
        return cls(
            protocol_version="0.1", message_type="autonomy.control",
            message_id=f"msg-autonomy-control-{uuid.uuid4().hex[:8]}",
            sender_instance_id=sender_instance_id, sequence_number=0,
            world_revision=0, sent_at=datetime.now(UTC), save_id=save_id,
            payload={"requestId": request_id, "saveId": save_id,
                     "action": action, "parameters": parameters},
        )

    @classmethod
    def create_autonomy_state(
        cls, sender_instance_id: str, request_id: str, save_id: str,
        state: Mapping[str, Any], status: str = "confirmed", reason: str | None = None
    ) -> Envelope:
        payload = {"requestId": request_id, "saveId": save_id, **dict(state), "status": status}
        if reason is not None:
            payload["reason"] = reason
        return cls(
            protocol_version="0.1", message_type="autonomy.state",
            message_id=f"msg-autonomy-state-{uuid.uuid4().hex[:8]}",
            sender_instance_id=sender_instance_id, sequence_number=0,
            world_revision=0, sent_at=datetime.now(UTC), save_id=save_id,
            payload=payload,
        )

    @classmethod
    def create_life_chat_reply(
        cls,
        sender_instance_id: str,
        request_id: str,
        save_id: str,
        status: str,
        profile_revision: int,
        memory_revision: int,
        reply_text: str | None = None,
        queue_position: int | None = None,
        error: str | None = None,
    ) -> Envelope:
        """life.chat.reply: no token/session fields (contract §1.2)."""
        payload: dict[str, Any] = {
            "requestId": request_id,
            "saveId": save_id,
            "status": status,
            "profileRevision": profile_revision,
            "memoryRevision": memory_revision,
        }
        if reply_text is not None:
            payload["replyText"] = reply_text
        if queue_position is not None:
            payload["queuePosition"] = queue_position
        if error is not None:
            payload["error"] = error
        return cls(
            protocol_version="0.1", message_type="life.chat.reply",
            message_id=f"msg-life-reply-{uuid.uuid4().hex[:8]}",
            sender_instance_id=sender_instance_id, sequence_number=0,
            world_revision=0, sent_at=datetime.now(UTC), save_id=save_id,
            payload=payload,
        )

    @classmethod
    def create_life_profile_state(
        cls,
        sender_instance_id: str,
        request_id: str,
        save_id: str,
        profile: Mapping[str, Any] | None,
        profile_revision: int,
        status: str = "confirmed",
        reason: str | None = None,
        work: Mapping[str, Any] | None = None,
    ) -> Envelope:
        """life.profile.state: response to get/set (contract §1.3/§1.4)."""
        payload: dict[str, Any] = {
            "requestId": request_id,
            "saveId": save_id,
            "profile": dict(profile) if profile is not None else None,
            "profileRevision": profile_revision,
            "status": status,
        }
        if work is not None:
            payload["work"] = dict(work)
        if reason is not None:
            payload["reason"] = reason
        return cls(
            protocol_version="0.1", message_type="life.profile.state",
            message_id=f"msg-life-profile-{uuid.uuid4().hex[:8]}",
            sender_instance_id=sender_instance_id, sequence_number=0,
            world_revision=0, sent_at=datetime.now(UTC), save_id=save_id,
            payload=payload,
        )

    @classmethod
    def create_life_memory_state(
        cls,
        sender_instance_id: str,
        request_id: str,
        save_id: str,
        entries: list[Any],
        memory_revision: int,
        status: str = "confirmed",
        reason: str | None = None,
        entry: Mapping[str, Any] | None = None,
    ) -> Envelope:
        """life.memory.state: response to list/edit (contract §1.5/§1.6)."""
        payload: dict[str, Any] = {
            "requestId": request_id,
            "saveId": save_id,
            "memoryRevision": memory_revision,
            "status": status,
        }
        if entries is not None:
            # Strip the internal dedup field; the wire field set is §1.5.
            payload["entries"] = [
                {k: v for k, v in e.items() if k != "commandId"}
                if isinstance(e, dict) else e
                for e in entries
            ]
        if entry is not None:
            wire_entry = dict(entry)
            wire_entry.pop("commandId", None)
            payload["entry"] = wire_entry
        if reason is not None:
            payload["reason"] = reason
        return cls(
            protocol_version="0.1", message_type="life.memory.state",
            message_id=f"msg-life-memory-{uuid.uuid4().hex[:8]}",
            sender_instance_id=sender_instance_id, sequence_number=0,
            world_revision=0, sent_at=datetime.now(UTC), save_id=save_id,
            payload=payload,
        )

    @classmethod
    def create_life_care(
        cls,
        sender_instance_id: str,
        save_id: str,
        kind: str,
        event_key: str,
        text: str,
        game_date: str,
    ) -> Envelope:
        """life.care: proactive companion care message (contract §1.7)."""
        payload: dict[str, Any] = {
            "saveId": save_id,
            "kind": kind,
            "eventKey": event_key,
            "text": text,
            "gameDate": game_date,
        }
        return cls(
            protocol_version="0.1", message_type="life.care",
            message_id=f"msg-life-care-{uuid.uuid4().hex[:8]}",
            sender_instance_id=sender_instance_id, sequence_number=0,
            world_revision=0, sent_at=datetime.now(UTC), save_id=save_id,
            payload=payload,
        )


@dataclass(frozen=True, slots=True)
class ChatSubmitPayload:
    request_id: str
    text: str
    source: str = "text"
    save_id: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ChatSubmitPayload:
        return cls(
            request_id=str(value.get("requestId", "")),
            text=str(value.get("text", "")),
            source=str(value.get("source", "text")),
            save_id=value.get("saveId"),
        )

    def to_mapping(self) -> dict[str, Any]:
        res: dict[str, Any] = {
            "requestId": self.request_id,
            "text": self.text,
            "source": self.source,
        }
        if self.save_id is not None:
            res["saveId"] = self.save_id
        return res


@dataclass(frozen=True, slots=True)
class ChatCancelPayload:
    request_id: str | None = None
    reason: str = "player_cancelled"

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ChatCancelPayload:
        return cls(
            request_id=value.get("requestId"),
            reason=str(value.get("reason", "player_cancelled")),
        )

    def to_mapping(self) -> dict[str, Any]:
        res: dict[str, Any] = {
            "reason": self.reason,
        }
        if self.request_id is not None:
            res["requestId"] = self.request_id
        return res


@dataclass(frozen=True, slots=True)
class ChatReplyPayload:
    request_id: str
    status: str
    reply_text: str
    tokens_used: int | None = None
    prompt_tokens: int | None = None
    output_tokens: int | None = None
    cached_tokens: int | None = None
    conversation_id: str | None = None
    error: str | None = None
    usage_source: str | None = None
    command_id: str | None = None
    command_complete: bool | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ChatReplyPayload:
        return cls(
            request_id=str(value.get("requestId", "")),
            status=str(value.get("status", "completed")),
            reply_text=str(value.get("replyText", "")),
            tokens_used=value.get("tokensUsed"),
            prompt_tokens=value.get("promptTokens"),
            output_tokens=value.get("outputTokens"),
            cached_tokens=value.get("cachedTokens"),
            conversation_id=value.get("conversationId"),
            error=value.get("error"),
            usage_source=value.get("usageSource"),
            command_id=value.get("commandId"),
            command_complete=value.get("commandComplete"),
        )

    def to_mapping(self) -> dict[str, Any]:
        res: dict[str, Any] = {
            "requestId": self.request_id,
            "status": self.status,
            "replyText": self.reply_text,
        }
        if self.tokens_used is not None:
            res["tokensUsed"] = self.tokens_used
        if self.prompt_tokens is not None:
            res["promptTokens"] = self.prompt_tokens
        if self.output_tokens is not None:
            res["outputTokens"] = self.output_tokens
        if self.cached_tokens is not None:
            res["cachedTokens"] = self.cached_tokens
        if self.conversation_id is not None:
            res["conversationId"] = self.conversation_id
        if self.error is not None:
            res["error"] = self.error
        if self.usage_source is not None:
            res["usageSource"] = self.usage_source
        if self.command_id is not None:
            res["commandId"] = self.command_id
        if self.command_complete is not None:
            res["commandComplete"] = self.command_complete
        return res


_LIFE_CHAT_MODES = frozenset({"chat", "plan"})
_LIFE_CHAT_STATUSES = frozenset({"processing", "queued", "completed", "failed"})
_LIFE_MEMORY_KINDS = frozenset({"preference", "agreement", "event"})
_LIFE_MEMORY_OPS = frozenset({"add", "correct", "delete"})
_LIFE_CARE_KINDS = frozenset({"morning", "work-done", "evening"})
_LIFE_PLAY_STYLES = frozenset({"earn", "workhorse", "community", "decor"})
_LIFE_PERSONALITIES = frozenset({"gentle", "lively", "calm", "tsundere"})
_LIFE_CARE_FREQUENCIES = frozenset({"quiet", "moderate", "chatty"})


@dataclass(frozen=True, slots=True)
class LifeChatSubmitPayload:
    """life.chat.submit payload (C#->Py): contract §1.1"""

    request_id: str
    save_id: str
    mode: str
    text: str
    source: str = "life-menu"

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> LifeChatSubmitPayload:
        request_id = str(value.get("requestId", ""))
        save_id = str(value.get("saveId", ""))
        mode = str(value.get("mode", "chat"))
        text = str(value.get("text", ""))
        source = str(value.get("source", "life-menu"))
        if not request_id:
            raise ProtocolError("life.chat.submit: requestId is required")
        if not save_id:
            raise ProtocolError("life.chat.submit: saveId is required")
        if mode not in _LIFE_CHAT_MODES:
            raise ProtocolError(f"life.chat.submit: invalid mode '{mode}'")
        if not text or not (1 <= len(text) <= 500):
            raise ProtocolError("life.chat.submit: text must be 1..500 chars")
        return cls(request_id=request_id, save_id=save_id, mode=mode, text=text, source=source)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "requestId": self.request_id,
            "saveId": self.save_id,
            "mode": self.mode,
            "text": self.text,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class LifeChatReplyPayload:
    """life.chat.reply payload (Py->C#): contract §1.2 (no token/session fields)"""

    request_id: str
    save_id: str
    status: str
    profile_revision: int
    memory_revision: int
    reply_text: str | None = None
    queue_position: int | None = None
    error: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> LifeChatReplyPayload:
        return cls(
            request_id=str(value.get("requestId", "")),
            save_id=str(value.get("saveId", "")),
            status=str(value.get("status", "completed")),
            profile_revision=int(value.get("profileRevision", 0)),
            memory_revision=int(value.get("memoryRevision", 0)),
            reply_text=value.get("replyText"),
            queue_position=value.get("queuePosition"),
            error=value.get("error"),
        )

    def to_mapping(self) -> dict[str, Any]:
        res: dict[str, Any] = {
            "requestId": self.request_id,
            "saveId": self.save_id,
            "status": self.status,
            "profileRevision": self.profile_revision,
            "memoryRevision": self.memory_revision,
        }
        if self.reply_text is not None:
            res["replyText"] = self.reply_text
        if self.queue_position is not None:
            res["queuePosition"] = self.queue_position
        if self.error is not None:
            res["error"] = self.error
        return res


@dataclass(frozen=True, slots=True)
class LifeProfileGetPayload:
    """life.profile.get payload (C#->Py): contract §1.3"""

    request_id: str
    save_id: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> LifeProfileGetPayload:
        request_id = str(value.get("requestId", ""))
        save_id = str(value.get("saveId", ""))
        if not request_id:
            raise ProtocolError("life.profile.get: requestId is required")
        if not save_id:
            raise ProtocolError("life.profile.get: saveId is required")
        return cls(request_id=request_id, save_id=save_id)

    def to_mapping(self) -> dict[str, Any]:
        return {"requestId": self.request_id, "saveId": self.save_id}


@dataclass(frozen=True, slots=True)
class LifeProfileSetPayload:
    """life.profile.set payload (C#->Py): contract §1.4"""

    request_id: str
    save_id: str
    expected_revision: int
    patch: Mapping[str, Any]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> LifeProfileSetPayload:
        request_id = str(value.get("requestId", ""))
        save_id = str(value.get("saveId", ""))
        if not request_id:
            raise ProtocolError("life.profile.set: requestId is required")
        if not save_id:
            raise ProtocolError("life.profile.set: saveId is required")
        expected_revision = value.get("expectedRevision")
        if expected_revision is None or isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
            raise ProtocolError("life.profile.set: expectedRevision must be an integer")
        patch = value.get("patch", {})
        if not isinstance(patch, Mapping):
            raise ProtocolError("life.profile.set: patch must be an object")
        if "playStyle" in patch and patch["playStyle"] not in _LIFE_PLAY_STYLES:
            raise ProtocolError(f"life.profile.set: invalid playStyle '{patch['playStyle']}'")
        if "personality" in patch and patch["personality"] not in _LIFE_PERSONALITIES:
            raise ProtocolError(f"life.profile.set: invalid personality '{patch['personality']}'")
        if "careFrequency" in patch and patch["careFrequency"] not in _LIFE_CARE_FREQUENCIES:
            raise ProtocolError(f"life.profile.set: invalid careFrequency '{patch['careFrequency']}'")
        if "companionName" in patch:
            name = str(patch["companionName"]).strip()
            if not (1 <= len(name) <= 12):
                raise ProtocolError("life.profile.set: companionName must be 1..12 chars")
        return cls(
            request_id=request_id,
            save_id=save_id,
            expected_revision=expected_revision,
            patch=patch,
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "requestId": self.request_id,
            "saveId": self.save_id,
            "expectedRevision": self.expected_revision,
            "patch": dict(self.patch),
        }


@dataclass(frozen=True, slots=True)
class LifeMemoryListPayload:
    """life.memory.list payload (C#->Py): contract §1.5"""

    request_id: str
    save_id: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> LifeMemoryListPayload:
        request_id = str(value.get("requestId", ""))
        save_id = str(value.get("saveId", ""))
        if not request_id:
            raise ProtocolError("life.memory.list: requestId is required")
        if not save_id:
            raise ProtocolError("life.memory.list: saveId is required")
        return cls(request_id=request_id, save_id=save_id)

    def to_mapping(self) -> dict[str, Any]:
        return {"requestId": self.request_id, "saveId": self.save_id}


@dataclass(frozen=True, slots=True)
class LifeMemoryEditPayload:
    """life.memory.edit payload (C#->Py): contract §1.6"""

    request_id: str
    save_id: str
    expected_revision: int
    op: str
    entry_id: str | None = None
    kind: str | None = None
    text: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> LifeMemoryEditPayload:
        request_id = str(value.get("requestId", ""))
        save_id = str(value.get("saveId", ""))
        if not request_id:
            raise ProtocolError("life.memory.edit: requestId is required")
        if not save_id:
            raise ProtocolError("life.memory.edit: saveId is required")
        expected_revision = value.get("expectedRevision")
        if expected_revision is None or isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
            raise ProtocolError("life.memory.edit: expectedRevision must be an integer")
        op = str(value.get("op", ""))
        if op not in _LIFE_MEMORY_OPS:
            raise ProtocolError(f"life.memory.edit: invalid op '{op}'")
        kind = value.get("kind")
        if kind is not None and kind not in _LIFE_MEMORY_KINDS:
            raise ProtocolError(f"life.memory.edit: invalid kind '{kind}'")
        if op == "add" and kind == "event":
            raise ProtocolError("life.memory.edit: op=add cannot accept kind=event")
        text = value.get("text")
        if text is not None:
            if not isinstance(text, str) or not (1 <= len(text) <= 200):
                raise ProtocolError("life.memory.edit: text must be 1..200 chars")
        entry_id = value.get("id")
        return cls(
            request_id=request_id,
            save_id=save_id,
            expected_revision=expected_revision,
            op=op,
            entry_id=entry_id,
            kind=kind,
            text=text,
        )

    def to_mapping(self) -> dict[str, Any]:
        res: dict[str, Any] = {
            "requestId": self.request_id,
            "saveId": self.save_id,
            "expectedRevision": self.expected_revision,
            "op": self.op,
        }
        if self.entry_id is not None:
            res["id"] = self.entry_id
        if self.kind is not None:
            res["kind"] = self.kind
        if self.text is not None:
            res["text"] = self.text
        return res


def _parse_datetime(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise ProtocolError(f"{field} must be an RFC 3339 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ProtocolError(f"{field} must be an RFC 3339 string") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProtocolError(f"{field} must include timezone offset (e.g. 'Z' or '+00:00')")
    return parsed


def _parse_optional_datetime(value: Any, field: str) -> datetime | None:
    if value is None:
        return None
    return _parse_datetime(value, field)


def _format_datetime(dt: datetime) -> str:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ProtocolError("Cannot format naive datetime")
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
