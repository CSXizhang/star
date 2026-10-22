from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from stardew_ai_runtime.protocol import Envelope, ProtocolError, validate_native_action_parameters
from stardew_ai_runtime.websocket_client import WebSocketClient, WebSocketError


class TransportClientError(WebSocketError):
    """Raised for high-level transport client failures."""


class TransportClient:
    """Production WebSocket client connecting to the Companion Mod transport server."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        session_token: str | None = None,
        instance_id: str | None = None,
    ):
        self.host = host
        self.port = port
        self.session_token = session_token
        self.instance_id = instance_id or f"runtime-{uuid.uuid4().hex[:8]}"

        self._ws: WebSocketClient | None = None
        self._sequence_number = 0
        self.world_revision = 0
        self.save_id: str | None = None
        self.game_session_id: str | None = None
        self.mod_welcome: Envelope | None = None
        self.latest_snapshot: Envelope | None = None
        self._completed_results: dict[str, Envelope] = {}
        self._native_revision_session: tuple[str, str] | None = None
        self._native_command_revisions: dict[str, int] = {}
        # Player-mode welcome facts; every field keeps its safe default when
        # the mod does not publish it (companion mode is unchanged).
        self.actor_mode: str = "companion"
        self.actor_name: str | None = None
        self.actor_multiplayer_id: int | None = None
        self.authority: str | None = None
        self.supports_thinking_pause: bool = False

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and not self._ws._closed

    def _next_sequence_number(self) -> int:
        seq = self._sequence_number
        self._sequence_number += 1
        return seq

    async def connect(self, timeout: float = 5.0) -> None:
        if self._ws is not None:
            return

        headers: dict[str, str] = {}
        if self.session_token:
            headers["Authorization"] = f"Bearer {self.session_token}"

        path = "/"

        try:
            self._ws = await WebSocketClient.connect(
                host=self.host,
                port=self.port,
                path=path,
                headers=headers,
                timeout=timeout,
            )
        except (WebSocketError, OSError) as ex:
            raise TransportClientError(
                f"Failed to connect to ws://{self.host}:{self.port}{path}: {ex}"
            ) from ex

    async def send_envelope(self, envelope: Envelope) -> None:
        if self._ws is None:
            raise TransportClientError("Not connected to transport server")
        payload_json = json.dumps(envelope.to_mapping(), ensure_ascii=False)
        await self._ws.send_text(payload_json)

    async def receive_envelope(self, timeout: float | None = 5.0) -> Envelope:
        if self._ws is None:
            raise TransportClientError("Not connected to transport server")
        text = await self._ws.receive_text(timeout=timeout)
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ProtocolError("Received non-object JSON from transport server")
        return Envelope.from_mapping(data)

    async def handshake(
        self,
        runtime_version: str = "0.1.0",
        features: Mapping[str, bool] | None = None,
        timeout: float = 5.0,
    ) -> Envelope:
        """Executes the initial runtime.hello -> mod.welcome handshake."""
        hello = Envelope.create_hello(
            sender_instance_id=self.instance_id,
            sequence_number=self._next_sequence_number(),
            runtime_version=runtime_version,
            session_token=self.session_token,
            features=features,
        )
        await self.send_envelope(hello)

        welcome = await self.receive_envelope(timeout=timeout)
        if welcome.message_type == "protocol.error":
            code = welcome.payload.get("code", "UNKNOWN")
            msg = welcome.payload.get("message", "Handshake rejected")
            raise TransportClientError(f"Server rejected handshake [{code}]: {msg}")

        if welcome.message_type != "mod.welcome":
            raise TransportClientError(
                f"Expected mod.welcome, received {welcome.message_type}"
            )

        self.mod_welcome = welcome
        self._apply_welcome_actor_fields(welcome)
        self.save_id = welcome.save_id
        self.game_session_id = welcome.game_session_id
        self.world_revision = welcome.world_revision
        return welcome

    async def wait_for_snapshot(self, timeout: float = 5.0) -> Envelope:
        """Awaits the world.snapshot following handshake or request."""
        start_time = datetime.now(UTC)
        remaining = timeout
        while remaining > 0:
            try:
                envelope = await self.receive_envelope(timeout=remaining)
            except TimeoutError:
                break
            if envelope.message_type == "world.snapshot":
                self.latest_snapshot = envelope
                self.world_revision = envelope.world_revision
                return envelope
            elif envelope.message_type == "skill.result":
                res_cmd = envelope.payload.get("commandId")
                if res_cmd:
                    self._completed_results[res_cmd] = envelope
                self.world_revision = envelope.world_revision
            elapsed = (datetime.now(UTC) - start_time).total_seconds()
            remaining = timeout - elapsed
        raise TimeoutError("Timed out waiting for world.snapshot")

    async def execute_water_zone(
        self,
        location_id: str,
        tiles: list[dict[str, int]],
        command_id: str | None = None,
        task_id: str | None = None,
        idempotency_key: str | None = None,
        expires_seconds: float = 30.0,
        max_game_minutes: int = 0,
        max_stamina: float = 50.0,
        max_water: int = 20,
        cancel_policy: str = "safe-point",
        world_revision: int | None = None,
        include_empty_tiles: bool | None = None,
    ) -> str:
        """Dispatches a skill.execute command targeting a water-zone."""
        if not self.save_id or not self.game_session_id:
            raise TransportClientError("Handshake must be completed before executing skills")

        cmd_id = command_id or f"cmd-{uuid.uuid4().hex[:8]}"
        tsk_id = task_id or f"task-{uuid.uuid4().hex[:8]}"
        idem_key = idempotency_key or f"{self.save_id}:{tsk_id}:attempt-1"
        expires_at = datetime.now(UTC) + timedelta(seconds=expires_seconds)
        target_revision = self.world_revision if world_revision is None else world_revision

        execute_envelope = Envelope.create_execute_water_zone(
            sender_instance_id=self.instance_id,
            sequence_number=self._next_sequence_number(),
            save_id=self.save_id,
            game_session_id=self.game_session_id,
            world_revision=target_revision,
            command_id=cmd_id,
            task_id=tsk_id,
            location_id=location_id,
            tiles=tiles,
            idempotency_key=idem_key,
            expires_at=expires_at,
            max_game_minutes=max_game_minutes,
            max_stamina=max_stamina,
            max_water=max_water,
            cancel_policy=cancel_policy,
            include_empty_tiles=include_empty_tiles,
        )

        await self.send_envelope(execute_envelope)
        return cmd_id

    async def execute_harvest_zone(
        self,
        location_id: str,
        tiles: list[dict[str, int]],
        command_id: str | None = None,
        task_id: str | None = None,
        idempotency_key: str | None = None,
        expires_seconds: float = 30.0,
        max_game_minutes: int = 0,
        max_stamina: float = 50.0,
        max_water: int = 20,
        cancel_policy: str = "safe-point",
        world_revision: int | None = None,
    ) -> str:
        """Dispatches a skill.execute command targeting a harvest-zone."""
        if not self.save_id or not self.game_session_id:
            raise TransportClientError("Handshake must be completed before executing skills")

        cmd_id = command_id or f"cmd-{uuid.uuid4().hex[:8]}"
        tsk_id = task_id or f"task-{uuid.uuid4().hex[:8]}"
        idem_key = idempotency_key or f"{self.save_id}:{tsk_id}:attempt-1"
        expires_at = datetime.now(UTC) + timedelta(seconds=expires_seconds)
        target_revision = self.world_revision if world_revision is None else world_revision

        execute_envelope = Envelope.create_execute_harvest_zone(
            sender_instance_id=self.instance_id,
            sequence_number=self._next_sequence_number(),
            save_id=self.save_id,
            game_session_id=self.game_session_id,
            world_revision=target_revision,
            command_id=cmd_id,
            task_id=tsk_id,
            location_id=location_id,
            tiles=tiles,
            idempotency_key=idem_key,
            expires_at=expires_at,
            max_game_minutes=max_game_minutes,
            max_stamina=max_stamina,
            max_water=max_water,
            cancel_policy=cancel_policy,
        )

        await self.send_envelope(execute_envelope)
        return cmd_id

    async def execute_hoe_tiles(
        self,
        location_id: str,
        tiles: list[dict[str, int]],
        command_id: str | None = None,
        task_id: str | None = None,
        idempotency_key: str | None = None,
        expires_seconds: float = 30.0,
        max_game_minutes: int = 0,
        max_stamina: float = 50.0,
        max_water: int = 20,
        cancel_policy: str = "safe-point",
        world_revision: int | None = None,
    ) -> str:
        """Dispatches a skill.execute command targeting hoe-tiles."""
        if not self.save_id or not self.game_session_id:
            raise TransportClientError("Handshake must be completed before executing skills")

        cmd_id = command_id or f"cmd-{uuid.uuid4().hex[:8]}"
        tsk_id = task_id or f"task-{uuid.uuid4().hex[:8]}"
        idem_key = idempotency_key or f"{self.save_id}:{tsk_id}:attempt-1"
        expires_at = datetime.now(UTC) + timedelta(seconds=expires_seconds)
        target_revision = self.world_revision if world_revision is None else world_revision

        execute_envelope = Envelope.create_execute_hoe_tiles(
            sender_instance_id=self.instance_id,
            sequence_number=self._next_sequence_number(),
            save_id=self.save_id,
            game_session_id=self.game_session_id,
            world_revision=target_revision,
            command_id=cmd_id,
            task_id=tsk_id,
            location_id=location_id,
            tiles=tiles,
            idempotency_key=idem_key,
            expires_at=expires_at,
            max_game_minutes=max_game_minutes,
            max_stamina=max_stamina,
            max_water=max_water,
            cancel_policy=cancel_policy,
        )

        await self.send_envelope(execute_envelope)
        return cmd_id

    async def execute_plant_seeds(
        self,
        location_id: str,
        tiles: list[dict[str, int]],
        seed_item_id: str,
        command_id: str | None = None,
        task_id: str | None = None,
        idempotency_key: str | None = None,
        expires_seconds: float = 30.0,
        max_game_minutes: int = 0,
        max_stamina: float = 50.0,
        max_water: int = 20,
        cancel_policy: str = "safe-point",
        world_revision: int | None = None,
    ) -> str:
        """Dispatches a skill.execute command targeting plant-seeds."""
        if not self.save_id or not self.game_session_id:
            raise TransportClientError("Handshake must be completed before executing skills")

        cmd_id = command_id or f"cmd-{uuid.uuid4().hex[:8]}"
        tsk_id = task_id or f"task-{uuid.uuid4().hex[:8]}"
        idem_key = idempotency_key or f"{self.save_id}:{tsk_id}:attempt-1"
        expires_at = datetime.now(UTC) + timedelta(seconds=expires_seconds)
        target_revision = self.world_revision if world_revision is None else world_revision

        execute_envelope = Envelope.create_execute_plant_seeds(
            sender_instance_id=self.instance_id,
            sequence_number=self._next_sequence_number(),
            save_id=self.save_id,
            game_session_id=self.game_session_id,
            world_revision=target_revision,
            command_id=cmd_id,
            task_id=tsk_id,
            location_id=location_id,
            tiles=tiles,
            seed_item_id=seed_item_id,
            idempotency_key=idem_key,
            expires_at=expires_at,
            max_game_minutes=max_game_minutes,
            max_stamina=max_stamina,
            max_water=max_water,
            cancel_policy=cancel_policy,
        )

        await self.send_envelope(execute_envelope)
        return cmd_id

    async def execute_native_action(
        self,
        skill_id: str,
        parameters: dict[str, Any],
        command_id: str | None = None,
        task_id: str | None = None,
        idempotency_key: str | None = None,
        expires_seconds: float = 30.0,
        max_game_minutes: int = 0,
        max_stamina: float = 50.0,
        max_water: int = 20,
        cancel_policy: str = "safe-point",
        world_revision: int | None = None,
    ) -> str:
        """Dispatches one allow-listed native farming/husbandry skill.execute command."""
        if not self.save_id or not self.game_session_id:
            raise TransportClientError("Handshake must be completed before executing skills")

        validate_native_action_parameters(skill_id, parameters)

        cmd_id = command_id or f"cmd-{uuid.uuid4().hex[:8]}"
        tsk_id = task_id or f"task-{uuid.uuid4().hex[:8]}"
        idem_key = idempotency_key or f"{self.save_id}:{tsk_id}:attempt-1"
        expires_at = datetime.now(UTC) + timedelta(seconds=expires_seconds)
        # expectedWorldRevision participates in the Mod's semantic fingerprint.
        # Re-dispatch of one native command must retain its original revision;
        # a fresh command uses current state. Never evict a used command in-session.
        session = (self.save_id, self.game_session_id)
        if self._native_revision_session != session:
            self._native_command_revisions.clear()
            self._native_revision_session = session
        if cmd_id not in self._native_command_revisions:
            if len(self._native_command_revisions) >= 256:
                raise TransportClientError("Native command replay cache is full for this game session")
            self._native_command_revisions[cmd_id] = self.world_revision if world_revision is None else world_revision
        target_revision = self._native_command_revisions[cmd_id] if world_revision is None else world_revision

        execute_envelope = Envelope.create_execute_native_action(
            sender_instance_id=self.instance_id,
            sequence_number=self._next_sequence_number(),
            save_id=self.save_id,
            game_session_id=self.game_session_id,
            world_revision=target_revision,
            command_id=cmd_id,
            task_id=tsk_id,
            skill_id=skill_id,
            parameters=parameters,
            idempotency_key=idem_key,
            expires_at=expires_at,
            max_game_minutes=max_game_minutes,
            max_stamina=max_stamina,
            max_water=max_water,
            cancel_policy=cancel_policy,
        )

        await self.send_envelope(execute_envelope)
        return cmd_id

    async def execute_deposit_chest(
        self,
        location_id: str,
        chest_x: int,
        chest_y: int,
        item_ids: list[str] | None = None,
        command_id: str | None = None,
        task_id: str | None = None,
        idempotency_key: str | None = None,
        expires_seconds: float = 30.0,
        max_game_minutes: int = 0,
        max_stamina: float = 50.0,
        max_water: int = 20,
        cancel_policy: str = "safe-point",
        world_revision: int | None = None,
        items: list[dict[str, Any]] | None = None,
    ) -> str:
        """Dispatches a skill.execute command targeting a deposit-chest."""
        if not self.save_id or not self.game_session_id:
            raise TransportClientError("Handshake must be completed before executing skills")

        cmd_id = command_id or f"cmd-{uuid.uuid4().hex[:8]}"
        tsk_id = task_id or f"task-{uuid.uuid4().hex[:8]}"
        idem_key = idempotency_key or f"{self.save_id}:{tsk_id}:attempt-1"
        expires_at = datetime.now(UTC) + timedelta(seconds=expires_seconds)
        target_revision = self.world_revision if world_revision is None else world_revision

        execute_envelope = Envelope.create_execute_deposit_chest(
            sender_instance_id=self.instance_id,
            sequence_number=self._next_sequence_number(),
            save_id=self.save_id,
            game_session_id=self.game_session_id,
            world_revision=target_revision,
            command_id=cmd_id,
            task_id=tsk_id,
            location_id=location_id,
            chest_x=chest_x,
            chest_y=chest_y,
            item_ids=item_ids,
            items=items,
            idempotency_key=idem_key,
            expires_at=expires_at,
            max_game_minutes=max_game_minutes,
            max_stamina=max_stamina,
            max_water=max_water,
            cancel_policy=cancel_policy,
        )

        await self.send_envelope(execute_envelope)
        return cmd_id

    async def execute_organize_chest(
        self,
        location_id: str,
        chest_x: int,
        chest_y: int,
        command_id: str | None = None,
        task_id: str | None = None,
        idempotency_key: str | None = None,
        expires_seconds: float = 30.0,
        max_game_minutes: int = 0,
        max_stamina: float = 50.0,
        max_water: int = 20,
        cancel_policy: str = "safe-point",
        world_revision: int | None = None,
    ) -> str:
        """Dispatches a skill.execute command targeting an organize-chest."""
        if not self.save_id or not self.game_session_id:
            raise TransportClientError("Handshake must be completed before executing skills")

        cmd_id = command_id or f"cmd-{uuid.uuid4().hex[:8]}"
        tsk_id = task_id or f"task-{uuid.uuid4().hex[:8]}"
        idem_key = idempotency_key or f"{self.save_id}:{tsk_id}:attempt-1"
        expires_at = datetime.now(UTC) + timedelta(seconds=expires_seconds)
        target_revision = self.world_revision if world_revision is None else world_revision

        execute_envelope = Envelope.create_execute_organize_chest(
            sender_instance_id=self.instance_id,
            sequence_number=self._next_sequence_number(),
            save_id=self.save_id,
            game_session_id=self.game_session_id,
            world_revision=target_revision,
            command_id=cmd_id,
            task_id=tsk_id,
            location_id=location_id,
            chest_x=chest_x,
            chest_y=chest_y,
            idempotency_key=idem_key,
            expires_at=expires_at,
            max_game_minutes=max_game_minutes,
            max_stamina=max_stamina,
            max_water=max_water,
            cancel_policy=cancel_policy,
        )

        await self.send_envelope(execute_envelope)
        return cmd_id

    async def execute_withdraw_chest(
        self,
        location_id: str,
        chest_x: int,
        chest_y: int,
        items: list[dict[str, Any]],
        command_id: str | None = None,
        task_id: str | None = None,
        idempotency_key: str | None = None,
        expires_seconds: float = 60.0,
        max_game_minutes: int = 0,
        max_stamina: float = 50.0,
        max_water: int = 20,
        cancel_policy: str = "safe-point",
        world_revision: int | None = None,
    ) -> str:
        """Dispatches a skill.execute command targeting a withdraw-chest."""
        if not self.save_id or not self.game_session_id:
            raise TransportClientError("Handshake must be completed before executing skills")

        cmd_id = command_id or f"cmd-{uuid.uuid4().hex[:8]}"
        tsk_id = task_id or f"task-{uuid.uuid4().hex[:8]}"
        idem_key = idempotency_key or f"{self.save_id}:{tsk_id}:attempt-1"
        expires_at = datetime.now(UTC) + timedelta(seconds=expires_seconds)
        target_revision = self.world_revision if world_revision is None else world_revision

        execute_envelope = Envelope.create_execute_withdraw_chest(
            sender_instance_id=self.instance_id,
            sequence_number=self._next_sequence_number(),
            save_id=self.save_id,
            game_session_id=self.game_session_id,
            world_revision=target_revision,
            command_id=cmd_id,
            task_id=tsk_id,
            location_id=location_id,
            chest_x=chest_x,
            chest_y=chest_y,
            items=items,
            idempotency_key=idem_key,
            expires_at=expires_at,
            max_game_minutes=max_game_minutes,
            max_stamina=max_stamina,
            max_water=max_water,
            cancel_policy=cancel_policy,
        )

        await self.send_envelope(execute_envelope)
        return cmd_id

    async def execute_ship_items(
        self,
        location_id: str = "Farm",
        items: list[dict[str, Any]] | None = None,
        command_id: str | None = None,
        task_id: str | None = None,
        idempotency_key: str | None = None,
        expires_seconds: float = 30.0,
        max_game_minutes: int = 0,
        max_stamina: float = 50.0,
        max_water: int = 20,
        cancel_policy: str = "safe-point",
        world_revision: int | None = None,
    ) -> str:
        """Dispatches a skill.execute command targeting ship-items."""
        if not self.save_id or not self.game_session_id:
            raise TransportClientError("Handshake must be completed before executing skills")

        cmd_id = command_id or f"cmd-{uuid.uuid4().hex[:8]}"
        tsk_id = task_id or f"task-{uuid.uuid4().hex[:8]}"
        idem_key = idempotency_key or f"{self.save_id}:{tsk_id}:attempt-1"
        expires_at = datetime.now(UTC) + timedelta(seconds=expires_seconds)
        target_revision = self.world_revision if world_revision is None else world_revision

        execute_envelope = Envelope.create_execute_ship_items(
            sender_instance_id=self.instance_id,
            sequence_number=self._next_sequence_number(),
            save_id=self.save_id,
            game_session_id=self.game_session_id,
            world_revision=target_revision,
            command_id=cmd_id,
            task_id=tsk_id,
            location_id=location_id,
            items=items or [],
            idempotency_key=idem_key,
            expires_at=expires_at,
            max_game_minutes=max_game_minutes,
            max_stamina=max_stamina,
            max_water=max_water,
            cancel_policy=cancel_policy,
        )

        await self.send_envelope(execute_envelope)
        return cmd_id

    async def execute_purchase_items(
        self,
        shop_id: str = "SeedShop",
        location_id: str = "SeedShop",
        items: list[dict[str, Any]] | None = None,
        budget_limit: int = 100,
        command_id: str | None = None,
        task_id: str | None = None,
        idempotency_key: str | None = None,
        expires_seconds: float = 30.0,
        max_game_minutes: int = 0,
        max_stamina: float = 50.0,
        max_water: int = 0,
        cancel_policy: str = "safe-point",
        world_revision: int | None = None,
    ) -> str:
        """Dispatches a skill.execute command targeting purchase-items."""
        if not self.save_id or not self.game_session_id:
            raise TransportClientError("Handshake must be completed before executing skills")

        cmd_id = command_id or f"cmd-{uuid.uuid4().hex[:8]}"
        tsk_id = task_id or f"task-{uuid.uuid4().hex[:8]}"
        idem_key = idempotency_key or f"{self.save_id}:{tsk_id}:attempt-1"
        expires_at = datetime.now(UTC) + timedelta(seconds=expires_seconds)
        target_revision = self.world_revision if world_revision is None else world_revision

        execute_envelope = Envelope.create_execute_purchase_items(
            sender_instance_id=self.instance_id,
            sequence_number=self._next_sequence_number(),
            save_id=self.save_id,
            game_session_id=self.game_session_id,
            world_revision=target_revision,
            command_id=cmd_id,
            task_id=tsk_id,
            shop_id=shop_id,
            location_id=location_id,
            items=items or [],
            budget_limit=budget_limit,
            idempotency_key=idem_key,
            expires_at=expires_at,
            max_game_minutes=max_game_minutes,
            max_stamina=max_stamina,
            max_water=max_water,
            cancel_policy=cancel_policy,
        )

        await self.send_envelope(execute_envelope)
        return cmd_id

    async def execute_navigate_to(
        self,
        location_id: str,
        tile: dict[str, int],
        command_id: str | None = None,
        task_id: str | None = None,
        idempotency_key: str | None = None,
        expires_seconds: float = 60.0,
        max_game_minutes: int = 0,
        max_stamina: float = 50.0,
        max_water: int = 0,
        cancel_policy: str = "safe-point",
        world_revision: int | None = None,
    ) -> str:
        """Dispatches a skill.execute command targeting navigate-to."""
        if not self.save_id or not self.game_session_id:
            raise TransportClientError("Handshake must be completed before executing skills")

        cmd_id = command_id or f"cmd-{uuid.uuid4().hex[:8]}"
        tsk_id = task_id or f"task-{uuid.uuid4().hex[:8]}"
        idem_key = idempotency_key or f"{self.save_id}:{tsk_id}:attempt-1"
        expires_at = datetime.now(UTC) + timedelta(seconds=expires_seconds)
        target_revision = self.world_revision if world_revision is None else world_revision

        execute_envelope = Envelope.create_execute_navigate_to(
            sender_instance_id=self.instance_id,
            sequence_number=self._next_sequence_number(),
            save_id=self.save_id,
            game_session_id=self.game_session_id,
            world_revision=target_revision,
            command_id=cmd_id,
            task_id=tsk_id,
            location_id=location_id,
            tile=tile,
            idempotency_key=idem_key,
            expires_at=expires_at,
            max_game_minutes=max_game_minutes,
            max_stamina=max_stamina,
            max_water=max_water,
            cancel_policy=cancel_policy,
        )

        await self.send_envelope(execute_envelope)
        return cmd_id

    async def execute_open_shop(self, location_id: str, shop_id: str,
                                command_id: str | None = None, task_id: str | None = None,
                                idempotency_key: str | None = None, expires_seconds: float = 30.0,
                                world_revision: int | None = None) -> str:
        """Dispatches a skill.execute command targeting open-shop."""
        if not self.save_id or not self.game_session_id:
            raise TransportClientError("Handshake must be completed before executing skills")
        cmd_id = command_id or f"cmd-{uuid.uuid4().hex[:8]}"
        tsk_id = task_id or f"task-{uuid.uuid4().hex[:8]}"
        envelope = Envelope.create_execute_skill(
            sender_instance_id=self.instance_id,
            sequence_number=self._next_sequence_number(),
            save_id=self.save_id,
            game_session_id=self.game_session_id,
            world_revision=self.world_revision if world_revision is None else world_revision,
            command_id=cmd_id,
            task_id=tsk_id,
            skill_id="open-shop",
            parameters={"locationId": location_id, "shopId": shop_id},
            idempotency_key=idempotency_key or f"{self.save_id}:{tsk_id}:attempt-1",
            expires_at=datetime.now(UTC) + timedelta(seconds=expires_seconds),
        )
        await self.send_envelope(envelope)
        return cmd_id

    async def execute_open_chest(self, location_id: str, chest_x: int, chest_y: int,
                                 command_id: str | None = None, task_id: str | None = None,
                                 idempotency_key: str | None = None, expires_seconds: float = 30.0,
                                 max_game_minutes: int = 0, world_revision: int | None = None) -> str:
        """Dispatches a skill.execute command targeting open-chest."""
        if not self.save_id or not self.game_session_id:
            raise TransportClientError("Handshake must be completed before executing skills")
        cmd_id = command_id or f"cmd-{uuid.uuid4().hex[:8]}"
        tsk_id = task_id or f"task-{uuid.uuid4().hex[:8]}"
        envelope = Envelope.create_execute_skill(
            sender_instance_id=self.instance_id,
            sequence_number=self._next_sequence_number(),
            save_id=self.save_id,
            game_session_id=self.game_session_id,
            world_revision=self.world_revision if world_revision is None else world_revision,
            command_id=cmd_id,
            task_id=tsk_id,
            skill_id="open-chest",
            parameters={"locationId": location_id, "chestTile": {"x": chest_x, "y": chest_y}},
            idempotency_key=idempotency_key or f"{self.save_id}:{tsk_id}:attempt-1",
            expires_at=datetime.now(UTC) + timedelta(seconds=expires_seconds),
            max_game_minutes=max_game_minutes,
        )
        await self.send_envelope(envelope)
        return cmd_id

    def _native_ui_location(self) -> str:
        payload = self.latest_snapshot.payload if self.latest_snapshot else {}
        world = payload.get("world") or {}
        return str(world.get("currentLocation") or "current-native-ui")

    async def execute_native_advance(self, allow_skip: bool = False,
                                     command_id: str | None = None, task_id: str | None = None,
                                     idempotency_key: str | None = None, expires_seconds: float = 30.0,
                                     world_revision: int | None = None) -> str:
        """Dispatches the native-advance UI continuation (skip cutscene/advance dialogue/dismiss menu)."""
        if not self.save_id or not self.game_session_id:
            raise TransportClientError("Handshake must be completed before executing skills")
        cmd_id = command_id or f"cmd-{uuid.uuid4().hex[:8]}"
        tsk_id = task_id or f"task-{uuid.uuid4().hex[:8]}"
        envelope = Envelope.create_execute_skill(
            sender_instance_id=self.instance_id,
            sequence_number=self._next_sequence_number(),
            save_id=self.save_id,
            game_session_id=self.game_session_id,
            world_revision=self.world_revision if world_revision is None else world_revision,
            command_id=cmd_id,
            task_id=tsk_id,
            skill_id="native-advance",
            parameters={"locationId": self._native_ui_location(), "allowSkip": allow_skip is True},
            idempotency_key=idempotency_key or f"{self.save_id}:{tsk_id}:attempt-1",
            expires_at=datetime.now(UTC) + timedelta(seconds=expires_seconds),
        )
        await self.send_envelope(envelope)
        return cmd_id

    async def execute_native_answer(self, choice_index: int,
                                    command_id: str | None = None, task_id: str | None = None,
                                    idempotency_key: str | None = None, expires_seconds: float = 30.0,
                                    world_revision: int | None = None) -> str:
        """Dispatches the native-answer dialogue choice (explicit choiceIndex)."""
        if not self.save_id or not self.game_session_id:
            raise TransportClientError("Handshake must be completed before executing skills")
        if isinstance(choice_index, bool) or not isinstance(choice_index, int) or choice_index < 0:
            raise TransportClientError("choice_index must be a non-negative integer")
        cmd_id = command_id or f"cmd-{uuid.uuid4().hex[:8]}"
        tsk_id = task_id or f"task-{uuid.uuid4().hex[:8]}"
        envelope = Envelope.create_execute_skill(
            sender_instance_id=self.instance_id,
            sequence_number=self._next_sequence_number(),
            save_id=self.save_id,
            game_session_id=self.game_session_id,
            world_revision=self.world_revision if world_revision is None else world_revision,
            command_id=cmd_id,
            task_id=tsk_id,
            skill_id="native-answer",
            parameters={"locationId": self._native_ui_location(), "choiceIndex": choice_index},
            idempotency_key=idempotency_key or f"{self.save_id}:{tsk_id}:attempt-1",
            expires_at=datetime.now(UTC) + timedelta(seconds=expires_seconds),
        )
        await self.send_envelope(envelope)
        return cmd_id

    async def execute_sleep(
        self,
        command_id: str | None = None,
        task_id: str | None = None,
        idempotency_key: str | None = None,
        expires_seconds: float = 300.0,
        max_game_minutes: int = 0,
        max_stamina: float = 0.0,
        max_water: int = 0,
        cancel_policy: str = "safe-point",
        world_revision: int | None = None,
    ) -> str:
        """Dispatches the player-mode native sleep skill (empty parameters)."""
        if not self.save_id or not self.game_session_id:
            raise TransportClientError("Handshake must be completed before executing skills")

        cmd_id = command_id or f"cmd-{uuid.uuid4().hex[:8]}"
        tsk_id = task_id or f"task-{uuid.uuid4().hex[:8]}"
        idem_key = idempotency_key or f"{self.save_id}:{tsk_id}:attempt-1"
        expires_at = datetime.now(UTC) + timedelta(seconds=expires_seconds)
        target_revision = self.world_revision if world_revision is None else world_revision

        execute_envelope = Envelope.create_execute_sleep(
            sender_instance_id=self.instance_id,
            sequence_number=self._next_sequence_number(),
            save_id=self.save_id,
            game_session_id=self.game_session_id,
            world_revision=target_revision,
            command_id=cmd_id,
            task_id=tsk_id,
            idempotency_key=idem_key,
            expires_at=expires_at,
            max_game_minutes=max_game_minutes,
            max_stamina=max_stamina,
            max_water=max_water,
            cancel_policy=cancel_policy,
        )

        await self.send_envelope(execute_envelope)
        return cmd_id

    async def cancel_skill(
        self,
        command_id: str,
        correlation_id: str,
        reason: str = "Cancelled by client",
        requested_by_player: bool = True,
        cancel_policy: str = "safe-point",
    ) -> None:
        """Sends a skill.cancel command."""
        if not self.save_id or not self.game_session_id:
            raise TransportClientError("Cannot cancel before handshake")

        cancel = Envelope.create_cancel(
            sender_instance_id=self.instance_id,
            sequence_number=self._next_sequence_number(),
            save_id=self.save_id,
            game_session_id=self.game_session_id,
            world_revision=self.world_revision,
            command_id=command_id,
            correlation_id=correlation_id,
            reason=reason,
            requested_by_player=requested_by_player,
            cancel_policy=cancel_policy,
        )
        await self.send_envelope(cancel)

    async def pause_skill(
        self,
        command_id: str,
        correlation_id: str,
        reason: str = "Client requested pause",
    ) -> None:
        """Sends a skill.pause command."""
        if not self.save_id or not self.game_session_id:
            raise TransportClientError("Cannot pause before handshake")

        pause = Envelope.create_pause(
            sender_instance_id=self.instance_id,
            sequence_number=self._next_sequence_number(),
            save_id=self.save_id,
            game_session_id=self.game_session_id,
            world_revision=self.world_revision,
            command_id=command_id,
            correlation_id=correlation_id,
            reason=reason,
        )
        await self.send_envelope(pause)

    async def resume_skill(
        self,
        command_id: str,
        correlation_id: str,
    ) -> None:
        """Sends a skill.resume command."""
        if not self.save_id or not self.game_session_id:
            raise TransportClientError("Cannot resume before handshake")

        resume = Envelope.create_resume(
            sender_instance_id=self.instance_id,
            sequence_number=self._next_sequence_number(),
            save_id=self.save_id,
            game_session_id=self.game_session_id,
            world_revision=self.world_revision,
            command_id=command_id,
            correlation_id=correlation_id,
        )
        await self.send_envelope(resume)

    async def request_command_result(self, command_id: str) -> None:
        """Read the current native session's cached receipt; never redispatch work."""
        if not self.save_id or not self.game_session_id:
            raise TransportClientError("Cannot reconcile before handshake")
        await self.send_envelope(Envelope.create_result_query(
            self.instance_id, self._next_sequence_number(), self.save_id,
            self.game_session_id, self.world_revision, command_id,
        ))

    def _apply_welcome_actor_fields(self, welcome: Envelope) -> None:
        """Store optional player-mode welcome fields with companion-safe defaults."""
        payload = welcome.payload if isinstance(welcome.payload, Mapping) else {}
        self.actor_mode = "player" if payload.get("actorMode") == "player" else "companion"
        actor_name = payload.get("actorName")
        self.actor_name = str(actor_name) if actor_name is not None else None
        actor_mid = payload.get("actorMultiplayerId")
        self.actor_multiplayer_id = (
            actor_mid if isinstance(actor_mid, int) and not isinstance(actor_mid, bool) else None
        )
        authority = payload.get("authority")
        self.authority = str(authority) if authority in {"human", "agent"} else None
        self.supports_thinking_pause = payload.get("supportsThinkingPause") is True

    async def send_agent_thinking(
        self,
        state: str,
        lease_id: str,
        request_id: str | None = None,
    ) -> None:
        """Sends agent.thinking on the chat channel (player-mode thinking pause)."""
        envelope = Envelope.create_agent_thinking(
            sender_instance_id=self.instance_id,
            request_id=request_id or f"thinking-{uuid.uuid4().hex[:8]}",
            state=state,
            lease_id=lease_id,
            save_id=self.save_id, game_session_id=self.game_session_id,
        )
        await self.send_envelope(envelope)

    async def send_player_control(
        self,
        action: str,
        request_id: str | None = None,
    ) -> None:
        """Sends player.control claim/release on the chat channel."""
        envelope = Envelope.create_player_control(
            sender_instance_id=self.instance_id,
            request_id=request_id or f"player-control-{uuid.uuid4().hex[:8]}",
            action=action,
            save_id=self.save_id, game_session_id=self.game_session_id,
        )
        await self.send_envelope(envelope)

    def get_cached_result(self, command_id: str) -> Envelope | None:
        """Returns and clears cached skill.result envelope for command_id if already received."""
        return self._completed_results.pop(command_id, None)

    async def wait_for_result(self, command_id: str, timeout: float = 10.0) -> Envelope:
        """Waits for terminal skill.result matching the target command ID."""
        if command_id in self._completed_results:
            env = self._completed_results.pop(command_id)
            self.world_revision = env.world_revision
            return env

        start_time = datetime.now(UTC)
        remaining = timeout

        while remaining > 0:
            try:
                envelope = await self.receive_envelope(timeout=remaining)
            except TimeoutError:
                break

            if envelope.message_type == "skill.result":
                res_cmd = envelope.payload.get("commandId")
                self.world_revision = envelope.world_revision
                if res_cmd == command_id:
                    return envelope
                if res_cmd:
                    self._completed_results[res_cmd] = envelope
            elif (envelope.message_type == "skill.result.status"
                  and envelope.payload.get("commandId") == command_id
                  and envelope.payload.get("status") == "unknown"):
                raise TimeoutError(f"Native session has no cached result for command {command_id}")
            elif envelope.message_type == "protocol.error":
                code = envelope.payload.get("code", "UNKNOWN")
                msg = envelope.payload.get("message", "Error")
                raise TransportClientError(f"Protocol error [{code}]: {msg}")
            elif envelope.message_type == "world.snapshot":
                self.latest_snapshot = envelope
                self.world_revision = envelope.world_revision

            elapsed = (datetime.now(UTC) - start_time).total_seconds()
            remaining = timeout - elapsed

        raise TimeoutError(f"Timed out waiting for result of command {command_id} after {timeout}s")

    async def close(self) -> None:
        if self._ws:
            try:
                await self._ws.close()
            finally:
                self._ws = None
