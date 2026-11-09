"""Shared provider lifetime and game-message delivery for every turn source."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import TYPE_CHECKING, Any

from .agy_process import terminate_agy_process
from .protocol import Envelope

if TYPE_CHECKING:
    from .chat_bridge import ActiveChatTask, WebSocketClient

logger = logging.getLogger(__name__)


def execute_provider(bridge, active_task, conversation_id, prompt, token):
    """Run the selected adapter in the existing turn, then record delivery facts."""
    from .context_usage import record_context_usage

    keys = ("STARDEW_DECISION_TOKEN", "STARDEW_MCP_SURFACE", "STARDEW_LIFE_TURN_ID", "STARDEW_LIFE_SAVE_ID")
    previous = {key: os.environ.get(key) for key in keys}
    os.environ["STARDEW_MCP_SURFACE"] = "light"
    os.environ["STARDEW_LIFE_TURN_ID"] = active_task.request_id
    os.environ["STARDEW_LIFE_SAVE_ID"] = active_task.save_id or ""
    if active_task.execution_deferred:
        os.environ.pop("STARDEW_DECISION_TOKEN", None)
    else:
        os.environ["STARDEW_DECISION_TOKEN"] = token
    try:
        backend = bridge._get_backend()
        if hasattr(backend, "progress"):
            backend.progress = getattr(bridge, "_backend_progress_callback", None)
        native_session = getattr(bridge, "_player_session_id", None)
        key, sent, pending = bridge._prepare_prompt_delivery(active_task, conversation_id, prompt, backend)
        result = backend.run(active_task, conversation_id, sent)
        if conversation_id and result.get("conversation_id") not in {None, conversation_id}:
            active_task.provider_input_delivered = False
            result.update(actual_provider_session_id=result.get("conversation_id"),
                          success=False, error="PROVIDER_SESSION_CHANGED",
                          response="后端返回了不同会话；原绑定保留，请检查客户端。", conversation_id=conversation_id)
        delivered_session = result.get("conversation_id") or conversation_id
        confirmed = bool(delivered_session and (getattr(active_task, "provider_input_delivered", False)
                         or (not active_task.cancelled and not getattr(active_task, "provider_interrupted", False)
                             and not result.get("error"))))
        if confirmed:
            bridge._acknowledge_prompt_delivery(key, pending, active_task.save_id, str(delivered_session))
            if bridge.actor_mode == "player":
                bridge._work_reload_context(active_task.save_id, str(delivered_session), delivered=True,
                                            expected_game_session_id=native_session)
        # Explicit provider events only; a small usage counter is not proof of
        # compaction. Native mcode compaction also invalidates this shared file.
        if result.get("context_compacted") is True and delivered_session:
            bridge._prompt_delivery.invalidate_session(key, str(delivered_session))
        record_context_usage(bridge.run_dir, active_task, bridge.backend_name,
                             conversation_id, result, pending, confirmed)
        return result
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class TurnExecutionMixin:
    """Uses the bridge's existing owner, task and stores; never creates a session."""

    async def _join_interrupted_provider(self, active_task: ActiveChatTask, future: asyncio.Future, *, steering=False) -> None:
        """Keep the model slot until its executor has restored turn-local state."""
        if steering:
            active_task.provider_interrupted = True
        else:
            active_task.cancelled = True
            active_task.abort_reason = active_task.abort_reason or "Chat channel closed"
        proc = active_task.process
        if proc is not None and proc.poll() is None:
            try:
                terminate = getattr(self._backend, "terminate", None)
                if self.backend_name == "agy":
                    terminate_agy_process(proc)
                elif callable(terminate):
                    terminate(proc)
                else:
                    proc.kill()
            except Exception:
                logger.warning("Could not stop interrupted provider", exc_info=True)
        while True:
            try:
                active_task.interrupted_result = await asyncio.shield(future)
                break
            except asyncio.CancelledError:
                # Repeated cancel/disconnect requests still cannot abandon an
                # executor which owns process-global turn environment variables.
                if future.cancelled():
                    break
            except Exception:
                logger.debug("Interrupted provider exited with an error", exc_info=True)
                break

    async def _settle_turn_messages(self, active_task, result):
        if not self.run_dir:
            return result
        from .turn_messages import exchange
        delivered = exchange(self.run_dir / "data" / "turn-messages.json",
                             active_task.request_id, active_task.save_id)
        ids = {message["requestId"] for message in delivered}
        # Tool-boundary delivery is confirmed independently of the turn's outcome.
        if self._work_store is not None:
            self._work_store.mark_midflight_consumed(active_task.save_id, list(ids))
        for item in list(self._life_queue):
            if item["request_id"] not in ids or item["save_id"] != active_task.save_id:
                continue
            self._life_queue.remove(item)
            await self._send_reply(item.get("ws") or self._chat_ws, Envelope.create_life_chat_reply(
                self.instance_id, item["request_id"], item["save_id"], status="completed",
                reply_text="这条补充已送达当前回合。",
                profile_revision=self._profile_revision(item["save_id"]),
                memory_revision=self._memory_revision(item["save_id"])))
        return result

    async def _await_work_provider(
        self, active_task: ActiveChatTask, conversation_id: str | None, prompt: str,
    ) -> dict[str, Any]:
        return await self._await_provider(active_task, self._execute_turn, conversation_id, prompt)

    async def _await_life_provider(
        self, active_task: ActiveChatTask, conversation_id: str | None,
        prompt: str, mode: str | None = None,
    ) -> dict[str, Any]:
        args = (conversation_id, prompt) if mode is None else (conversation_id, prompt, mode)
        return await self._await_provider(active_task, self._execute_life_turn, *args)

    async def _await_provider(self, active_task: ActiveChatTask, execute, *args) -> dict[str, Any]:
        """Join the owned executor before releasing turn state, even on cancellation."""
        from .snapshot_cache import cache_lock
        from .turn_messages import exchange
        path = self.run_dir / "data" / "turn-messages.json" if self.run_dir else None
        lease_path = self.run_dir / "data" / "life-turn.json" if self.run_dir else None
        if lease_path:
            try:
                lease = json.loads(lease_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                lease = {}
            if (lease.get("requestId"), lease.get("saveId")) != (active_task.request_id, active_task.save_id):
                self._life_turn_lease(active_task.request_id, active_task.save_id, player_text=active_task.prompt)
        def messages(**kwargs):
            return exchange(path, active_task.request_id, active_task.save_id, **kwargs) if path else []
        generation = self._life_control_generation.get(active_task.save_id, 0)
        native_session = getattr(self, "_player_session_id", None)
        previous_usage = {}
        supplement_ids = []
        resumed_events = set()
        future = None
        try:
            async with self._thinking_pause(active_task.request_id, active_task.save_id or None):
                while True:
                    active_task.provider_interrupted = False
                    active_task.provider_input_delivered = False
                    future = asyncio.get_running_loop().run_in_executor(None, execute, active_task, *args)
                    while not future.done():
                        await asyncio.wait({future}, timeout=0.1)
                        await self._apply_life_intent(self._chat_ws, active_task.request_id, active_task.save_id or "")
                        changed = getattr(active_task, "world_change_at", None)
                        companion = (self._latest_snapshot_payload or {}).get("companion") or {}
                        if (changed is not None and time.monotonic() - changed >= getattr(self, "world_steering_timeout_seconds", 30)
                                and not companion.get("activeCommandId") and messages(pending_world=True)):
                            await self._join_interrupted_provider(active_task, future, steering=True)
                            break
                    result = await asyncio.shield(future)
                    if result.get("success"):
                        messages(ack_world=True)
                    if supplement_ids and (active_task.provider_input_delivered or result.get("success")):
                        messages(acknowledge_ids=supplement_ids)
                    pending = messages(pending_world=True) if active_task.world_change_at is not None else []
                    allowed = (not active_task.cancelled and getattr(self, "control_owner", None) != "human"
                               and self._life_control_generation.get(active_task.save_id, 0) == generation
                               and getattr(self, "_player_session_id", None) == native_session
                               and not (self._work_store and self._work_store.state(active_task.save_id).paused))
                    session = args[0] or result.get("conversation_id") or active_task.provider_conversation_id
                    if (not pending or not allowed or not session
                            or not (result.get("success") or active_task.provider_interrupted or result.get("error") == "TIMEOUT")
                            or not any(m["requestId"] not in resumed_events for m in pending)
                            or result.get("error") == "PROVIDER_SESSION_CHANGED"):
                        if previous_usage:
                            result["usage"] = {**(result.get("usage") or {}), **{
                                k: v + (result.get("usage") or {}).get(k, 0) for k, v in previous_usage.items()}}
                        return result
                    # The old executor has exited. Reconcile once before resuming
                    # the SAME session; never infer zero output from a lost receipt.
                    observation = await self._observe_world_continuation(active_task)
                    if active_task.cancelled or self._life_control_generation.get(active_task.save_id, 0) != generation:
                        return result
                    for k, v in (result.get("usage") or {}).items():
                        if k.endswith("tokens") and type(v) is int:
                            previous_usage[k] = previous_usage.get(k, 0) + v
                    supplement = "世界变化补充（继续原会话和原目标，先判断当前状态，不重放旧动作）：\n" + json.dumps(
                        {"events": pending, "observation": observation}, ensure_ascii=False)
                    args = (session, supplement, *args[2:])
                    supplement_ids = [m["requestId"] for m in pending]
                    resumed_events.update(supplement_ids)
                    # Keep unconfirmed events available at the next tool boundary.
                    active_task.world_change_at = time.monotonic()
        except asyncio.CancelledError:
            if future is not None:
                await self._join_interrupted_provider(active_task, future)
            raise
        except Exception:
            # Mailbox/control failures cannot abandon a thread which still owns
            # a provider process and the process-global turn environment.
            if future is not None and not future.done():
                await self._join_interrupted_provider(active_task, future)
            raise
        finally:
            await self._settle_turn_messages(active_task, {})
            if lease_path:
                with cache_lock(lease_path):
                    lease = json.loads(lease_path.read_text(encoding="utf-8"))
                    if lease.get("requestId") == active_task.request_id:
                        lease["requestId"] = None
                        lease_path.write_text(json.dumps(lease, ensure_ascii=False), encoding="utf-8")

    async def _observe_world_continuation(self, active_task):
        from .scheduler import CompanionScheduler
        scheduler = CompanionScheduler(run_dir=self.run_dir)
        try:
            status = await scheduler.get_status()
            if status.get("saveId") != active_task.save_id:
                active_task.cancelled = True
                return {"status": "save_changed"}
            command = getattr(active_task, "last_native_command_id", None)
            receipt = await scheduler.reconcile_command(command) if command else None
            return {"status": status, "lastAction": receipt,
                    "receiptKnown": receipt is not None, "instruction": "Missing receipt is unknown, not zero output."}
        finally:
            await scheduler.close()

    async def _send_reply(self, ws: WebSocketClient | None, reply: Envelope) -> bool:
        if reply.message_type in {"chat.reply", "life.chat.reply"} and reply.payload.get("status") in {
            "completed", "failed", "cancelled", "interrupted", "selected", "decision-completed", "partial", "unknown",
        }:
            await self._push_usage_state(ws, str(reply.payload.get("saveId") or ""))
        if reply.message_type in {"chat.reply", "life.chat.reply", "autonomy.state"} and ws is not None and reply.payload.get("saveId"):
            save_id = str(reply.payload["saveId"])
            await self._push_work_state(ws, save_id)
        if ws is None:
            return False
        try:
            json_text = json.dumps(reply.to_mapping(), ensure_ascii=False)
            await ws.send_text(json_text)
            return True
        except Exception as ex:
            logger.warning("Failed to send chat reply to WebSocket: %s", ex)
            return False
