"""Explicit existing-session attachment through MiniMax Code's ACP client API.

No session/new, model override, replacement instructions, or workspace writes.
The original client must remain idle while this adapter owns game turns. ACP
does not provide a cross-client mutex; this limitation is part of the handshake.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .agent_backends import BackendProgress, BackendResult, KimiBackend, McodeBackend
from .mcode_session_identity import IdentityVerificationError, verify_session_identity


def binding_path(run_dir: Path) -> Path:
    return Path(run_dir) / "data/companion-connection.json"


def read_binding(run_dir: Path | None) -> dict[str, Any] | None:
    if run_dir is None:
        return None
    try:
        value = json.loads(binding_path(run_dir).read_text(encoding="utf-8"))
        if (value.get("adapter") == "mcode-acp" and value.get("providerSessionId")
                and Path(value.get("cwd", "")).is_absolute()
                and value.get("originalClientIdleAcknowledged") is True):
            return value
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return None


def write_binding(run_dir: Path, value: dict[str, Any]) -> None:
    path = binding_path(run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


class AcpError(RuntimeError):
    pass


class AcpClient:
    """Small JSON-lines ACP client; history replay is deliberately discarded."""

    def __init__(self, binding: dict[str, Any], active_task: Any, *, timeout: float = 600,
                 progress=None):
        self.binding, self.active_task = binding, active_task
        self.timeout, self.progress = timeout, progress
        self.sequence = 0
        self.events: queue.Queue = queue.Queue()
        self.prompting = False
        self.reply = ""
        self.permission_required = False
        executable = McodeBackend(Path(binding["cwd"]), command=binding.get("command", "mcode"))._executable()
        prefix = [executable]
        if Path(executable).suffix.lower() in {".cmd", ".ps1"}:
            import shutil
            entry = Path(executable).parent / "node_modules/@minimax-ai/code/cli.js"
            node = shutil.which("node")
            if not node or not entry.is_file():
                raise AcpError("MiniMax Code 的 Node.js 启动入口不可用。")
            prefix = [node, str(entry)]
        env = dict(os.environ)
        # Preserve a non-default original memory/account environment explicitly.
        if binding.get("dataDir"):
            env["MINIMAX_DATA_DIR"] = binding["dataDir"]
        self.process = subprocess.Popen([*prefix, "acp"], cwd=binding["cwd"], env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace")
        self.process.stardew_acp_client = self
        active_task.process = self.process
        self.write_lock = threading.Lock()
        self.cancel_sent = False
        for stream, channel in ((self.process.stdout, "stdout"), (self.process.stderr, "stderr")):
            threading.Thread(target=self._read, args=(stream, channel), daemon=True).start()

    def _read(self, stream, channel):
        try:
            for line in stream:
                if channel == "stdout":
                    try:
                        self.events.put(json.loads(line))
                    except ValueError:
                        pass
                # Never copy client stderr or private history into game logs.
        finally:
            if channel == "stdout":
                self.events.put(None)

    def send(self, value):
        with self.write_lock:
            self.process.stdin.write(json.dumps({"jsonrpc": "2.0", **value}, ensure_ascii=False) + "\n")
            self.process.stdin.flush()

    def cancel(self):
        if not self.cancel_sent and self.process.poll() is None:
            self.cancel_sent = True
            try:
                self.send({"method": "session/cancel", "params": {"sessionId": self.binding["providerSessionId"]}})
            except (OSError, ValueError):
                pass

    def request(self, method, params):
        self.sequence += 1
        request_id = self.sequence
        self.send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if self.active_task.cancelled:
                self.cancel()
                raise AcpError("游戏回合已取消；原会话保留。")
            try:
                event = self.events.get(timeout=0.1)
            except queue.Empty:
                continue
            if event is None:
                raise AcpError("原会话连接已关闭；请重新连接原伙伴。")
            if event.get("id") == request_id and "method" not in event:
                if event.get("error"):
                    error = event["error"]
                    raise AcpError(str(error.get("message") or "ACP 请求失败"))
                return event.get("result") or {}
            if event.get("method") == "session/request_permission" and "id" in event:
                # Do not broaden the original client's permission mode. A request
                # requiring interactive approval must be resolved in that client.
                self.send({"id": event["id"], "result": {"outcome": {"outcome": "cancelled"}}})
                self.permission_required = True
                continue
            if "id" in event and "method" in event:
                self.send({"id": event["id"], "error": {"code": -32601, "message": "Client capability unavailable"}})
                continue
            if event.get("method") != "session/update" or not self.prompting:
                continue
            body = event.get("params") or {}
            if body.get("sessionId") != self.binding["providerSessionId"]:
                continue
            update = body.get("update") or {}
            kind = update.get("sessionUpdate")
            if kind in {"agent_message_chunk", "tool_call", "tool_call_update"}:
                self.active_task.provider_input_delivered = True
                deadline = time.monotonic() + self.timeout
            if kind == "agent_message_chunk" and update.get("content", {}).get("type") == "text":
                self.reply += update["content"].get("text", "")
            elif kind in {"tool_call", "tool_call_update"} and self.progress:
                self.progress(BackendProgress("mcode", "tool_started" if kind == "tool_call" else "tool_completed",
                                               "原伙伴正在处理游戏任务", update.get("title")))
        self.cancel()
        raise AcpError("原会话长时间没有响应；保留绑定，等待重新连接。")

    def initialize(self):
        result = self.request("initialize", {"protocolVersion": 1,
            "clientCapabilities": {}, "clientInfo": {"name": "stardew-companion", "version": "1"}})
        if not result.get("agentCapabilities", {}).get("loadSession"):
            raise AcpError("此客户端不支持恢复已有会话。")

    def sessions(self):
        self.initialize()
        result = self.request("session/list", {"cwd": self.binding["cwd"]})
        rows = list(result.get("sessions") or [])
        seen = set()
        while result.get("nextCursor") and result["nextCursor"] not in seen:
            seen.add(result["nextCursor"])
            result = self.request("session/list", {"cwd": self.binding["cwd"], "cursor": result["nextCursor"]})
            rows.extend(result.get("sessions") or [])
        return rows

    def load(self, mcp_servers=None):
        self.initialize()
        # session/load validates cwd against the provider's persisted workspace.
        # It replays history; request() ignores every update until prompt starts.
        self.request("session/load", {"sessionId": self.binding["providerSessionId"],
            "cwd": self.binding["cwd"], "mcpServers": mcp_servers or []})

    def close(self):
        self.cancel()
        if self.process.poll() is None:
            try:
                self.process.stdin.close()
                self.process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                KimiBackend.terminate(self.process)
        self.active_task.process = None


class AcpSessionDriver:
    name = "mcode"

    def __init__(self, run_dir: Path, binding: dict[str, Any], progress=None, context=None):
        self.run_dir, self.progress = Path(run_dir), progress
        self.binding, self.context = binding, context or {}

    @staticmethod
    def terminate(process):
        client = getattr(process, "stardew_acp_client", None)
        if client:
            client.cancel()
        else:
            KimiBackend.terminate(process)

    def _mcp(self):
        python = self.run_dir / "runtime/python/python.exe"
        if not python.is_file():
            python = Path(sys.executable)
        env = dict(self.context)
        env.update(STARDEW_MCP_SURFACE="light", STARDEW_MCP_WIRE_STYLE="mcode",
                   STARDEW_EXTERNAL_CODEX="0", STARDEW_TURN_CONTEXT="", STARDEW_MCP_FULL="0",
                   PYTHONPATH=str(Path(__file__).resolve().parents[1]))
        return [{"name": "stardew-companion", "command": str(python),
            "args": ["-B", "-m", "stardew_ai_runtime.mcp_server", "--run-dir", str(self.run_dir), "--surface", "light"],
            "env": [{"name": key, "value": value} for key, value in env.items()]}]

    def run(self, active_task, session_id, prompt):
        binding = self.binding
        if not binding or not binding.get("enabled", True):
            return BackendResult(False, "等待原伙伴主动连接；未启动替代会话。", session_id,
                error={"code": "EXTERNAL_CONNECTION_WAITING", "message": "No attached original session"}, provider="mcode").as_dict()
        expected = binding["providerSessionId"]
        if session_id and session_id != expected:
            return BackendResult(False, "伙伴绑定已改变，请重新连接。", session_id,
                error={"code": "EXTERNAL_SESSION_CHANGED", "message": "Binding changed before turn"}, provider="mcode").as_dict()
        client = None
        start = time.monotonic()
        active_task.provider_input_delivered = False
        try:
            verify_session_identity(binding)
            client = AcpClient(binding, active_task, progress=self.progress)
            client.load(self._mcp())
            client.prompting = True
            result = client.request("session/prompt", {"sessionId": expected,
                "prompt": [{"type": "text", "text": prompt}]})
            if client.permission_required:
                return BackendResult(False, "原客户端需要工具授权；请在原客户端批准游戏工具后重新连接。", expected,
                    error={"code": "EXTERNAL_PERMISSION_REQUIRED", "message": "Original client approval required"},
                    provider="mcode", duration=time.monotonic() - start).as_dict()
            success = result.get("stopReason") == "end_turn"
            if success:
                active_task.provider_input_delivered = True
            return BackendResult(success, client.reply, expected,
                                 error=None if success else {"code": "EXTERNAL_TURN_INTERRUPTED", "message": str(result.get("stopReason"))}, provider="mcode",
                                 duration=time.monotonic() - start).as_dict()
        except IdentityVerificationError as ex:
            return BackendResult(False, "原会话的角色或环境无法核验；已停止连接，请在原客户端修复后重连。", expected,
                error={"code": "EXTERNAL_IDENTITY_UNVERIFIED", "message": str(ex)}, provider="mcode",
                duration=time.monotonic() - start).as_dict()
        except (AcpError, OSError) as ex:
            return BackendResult(False, "原伙伴连接未完成，保留原会话；请检查原客户端连接。", expected,
                error={"code": "EXTERNAL_CONNECTION_FAILED", "message": str(ex)}, provider="mcode",
                duration=time.monotonic() - start).as_dict()
        finally:
            if client:
                client.close()


def call_adapter(binding, route, payload=None, *, timeout=3):
    """Only a loopback endpoint written by the explicitly launched adapter."""
    port = binding.get("port")
    if type(port) is not int or not 1 <= port <= 65535 or not binding.get("token"):
        raise AcpError("原客户端适配器尚未启动。")
    request = urllib.request.Request(f"http://127.0.0.1:{port}/{route}",
        data=json.dumps(payload or {}, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + binding["token"]})
    # Local traffic must not go through an inherited HTTP proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        result = json.load(response)
    if result.get("bindingId") != binding.get("bindingId"):
        raise AcpError("原伙伴连接标识不匹配。")
    return result


class RemoteTurn:
    """Cancellation handle, not an owned provider process."""
    def __init__(self, binding, request_id):
        self.binding, self.request_id, self.done = binding, request_id, False

    def poll(self):
        return 0 if self.done else None

    def cancel(self):
        try:
            call_adapter(self.binding, "cancel", {"requestId": self.request_id})
        except (OSError, ValueError, AcpError):
            pass

    kill = cancel


class ExternalSessionBackend:
    """Game-side transport only. It never launches or reconfigures an AI client."""
    name = "mcode"

    def __init__(self, run_dir, progress=None):
        self.run_dir, self.progress = Path(run_dir), progress

    @staticmethod
    def terminate(handle):
        handle.cancel()

    def run(self, active_task, session_id, prompt):
        binding = read_binding(self.run_dir)
        if not binding or not binding.get("enabled", True):
            return BackendResult(False, "等待原伙伴主动连接。", session_id,
                error={"code": "EXTERNAL_CONNECTION_WAITING", "message": "No original adapter"}, provider="mcode").as_dict()
        expected = binding["providerSessionId"]
        if session_id != expected:
            return BackendResult(False, "原伙伴绑定发生改变，请重新连接。", session_id,
                error={"code": "EXTERNAL_SESSION_CHANGED", "message": "Binding changed"}, provider="mcode").as_dict()
        handle = RemoteTurn(binding, active_task.request_id)
        active_task.process = handle
        events: queue.Queue = queue.Queue()
        allowed = {"STARDEW_LIFE_MODE", "STARDEW_LIFE_TURN_ID", "STARDEW_LIFE_SAVE_ID",
                   "STARDEW_LIFE_PROPOSAL_ID", "STARDEW_DECISION_TOKEN"}
        payload = {"requestId": active_task.request_id, "sessionId": expected, "prompt": prompt,
                   "context": {key: os.environ[key] for key in allowed if key in os.environ}}
        def deliver():
            try:
                events.put(call_adapter(binding, "turn", payload, timeout=86400))
            except (OSError, ValueError, AcpError) as ex:
                events.put(ex)
        threading.Thread(target=deliver, daemon=True).start()
        try:
            while True:
                if active_task.cancelled:
                    handle.cancel()
                    return BackendResult(False, "游戏回合已取消；原会话保留。", expected,
                        error={"code": "CANCELLED", "message": "Game turn cancelled"}, provider="mcode").as_dict()
                try:
                    message = events.get(timeout=0.15)
                except queue.Empty:
                    continue
                if isinstance(message, Exception):
                    return BackendResult(False, "原伙伴已断开；请从原客户端重新连接。", expected,
                        error={"code": "EXTERNAL_CONNECTION_FAILED", "message": str(message)}, provider="mcode").as_dict()
                active_task.provider_input_delivered = bool(message.get("inputDelivered"))
                return message["result"]
        finally:
            handle.done = True
            active_task.process = None


class AdapterServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, run_dir, binding):
        self.run_dir, self.binding = Path(run_dir), binding
        lock_path = self.run_dir / "data/external-adapter.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.owner_lock = lock_path.open("a+b")
        self.owner_lock.seek(0)
        if not self.owner_lock.read(1):
            self.owner_lock.write(b"0")
            self.owner_lock.flush()
        self.owner_lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.owner_lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.owner_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as ex:
            self.owner_lock.close()
            raise AcpError("此游戏已经有客户端适配器；请先断开旧连接。") from ex
        self.turn_lock = threading.Lock()
        self.active = None
        self.failure = None
        self.receipt_path = self.run_dir / "data/external-delivery.json"
        try:
            self.receipts = json.loads(self.receipt_path.read_text(encoding="utf-8"))
            if not isinstance(self.receipts, dict):
                self.receipts = {}
        except (OSError, ValueError):
            self.receipts = {}
        try:
            super().__init__(("127.0.0.1", 0), AdapterHandler)
        except OSError:
            self.owner_lock.close()
            raise
        self.binding.update(port=self.server_port, token=uuid.uuid4().hex + uuid.uuid4().hex)

    def cancel_active(self):
        task = self.active
        if task:
            task.cancelled = True
            if task.process:
                AcpSessionDriver.terminate(task.process)

    def server_close(self):
        self.cancel_active()
        if self.turn_lock.acquire(timeout=8):
            self.turn_lock.release()
        elif self.active and self.active.process:
            # Only the ACP child launched by this adapter belongs to us.
            KimiBackend.terminate(self.active.process)
        super().server_close()
        self.owner_lock.close()

    def save_receipt(self, key, value):
        self.receipts[key] = value
        # These are message delivery receipts, never queued game actions. Keep
        # unresolved deliveries even across restart; they must not be replayed.
        completed = [k for k, v in self.receipts.items() if v.get("state") == "completed"]
        for old in completed[:-64]:
            del self.receipts[old]
        self.receipt_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.receipt_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.receipts, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.receipt_path)


class AdapterHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        server = self.server
        if self.headers.get("Authorization") != "Bearer " + server.binding["token"]:
            self.send_error(403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > 4_000_000:
                self.send_error(413)
                return
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError):
            self.send_error(400)
            return
        response = {"bindingId": server.binding["bindingId"]}
        if self.path == "/health":
            response.update(status="error" if server.failure else "connected", error=server.failure,
                            activeRequest=server.active.request_id if server.active else None)
        elif self.path == "/cancel":
            if server.active and body.get("requestId") == server.active.request_id:
                server.cancel_active()
            response["cancelled"] = True
        elif self.path == "/disconnect":
            server.cancel_active()
            response["disconnected"] = True
            threading.Thread(target=server.shutdown, daemon=True).start()
        elif self.path == "/turn":
            if body.get("sessionId") != server.binding["providerSessionId"] or not server.turn_lock.acquire(False):
                self.send_error(409)
                return
            try:
                key = server.binding["providerSessionId"] + ":" + str(body.get("requestId", ""))
                digest = hashlib.sha256(str(body.get("prompt", "")).encode()).hexdigest()
                previous = server.receipts.get(key)
                if previous:
                    if previous.get("digest") != digest:
                        response["result"] = BackendResult(False, "同一消息编号收到不同内容，请重新确认要求。",
                            server.binding["providerSessionId"], error={"code": "EXTERNAL_DELIVERY_CONFLICT", "message": "Request ID reused"}, provider="mcode").as_dict()
                    elif previous.get("state") == "completed":
                        response.update(previous["response"])
                    else:
                        response["result"] = BackendResult(False, "上次投递结果未确认；请先在原会话核对游戏状态，未自动重放。",
                            server.binding["providerSessionId"], error={"code": "EXTERNAL_DELIVERY_UNKNOWN", "message": "Interrupted delivery requires reconciliation"}, provider="mcode").as_dict()
                elif server.failure:
                    response["result"] = BackendResult(False, "原伙伴连接需要修复，请重新运行连接命令。",
                        server.binding["providerSessionId"], error={"code": server.failure, "message": "Reconnect required"}, provider="mcode").as_dict()
                else:
                    server.save_receipt(key, {"digest": digest, "state": "pending"})
                    task = SimpleNamespace(request_id=str(body["requestId"]), process=None, cancelled=False,
                                           provider_input_delivered=False)
                    server.active = task
                    allowed = {"STARDEW_LIFE_MODE", "STARDEW_LIFE_TURN_ID", "STARDEW_LIFE_SAVE_ID",
                               "STARDEW_LIFE_PROPOSAL_ID", "STARDEW_DECISION_TOKEN"}
                    context = {k: str(v) for k, v in body.get("context", {}).items() if k in allowed}
                    driver = AcpSessionDriver(server.run_dir, server.binding, context=context)
                    response["result"] = driver.run(task, server.binding["providerSessionId"], str(body["prompt"]))
                    response["inputDelivered"] = task.provider_input_delivered
                    if response["result"].get("error") and not task.cancelled:
                        server.failure = response["result"]["error"]
                    server.save_receipt(key, {"digest": digest, "state": "completed",
                        "response": {k: v for k, v in response.items() if k != "bindingId"}})
            finally:
                server.active = None
                server.turn_lock.release()
        else:
            self.send_error(404)
            return
        raw = json.dumps(response, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (OSError, BrokenPipeError):
            if self.path == "/turn":
                server.cancel_active()


def main(argv=None):
    # Windows launchers and redirected client tools may default to cp1252.
    # A status message must not terminate an otherwise verified attachment.
    for stream in (sys.stdout, sys.stderr):
        if callable(getattr(stream, "reconfigure", None)):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Connect an existing MiniMax Code conversation without creating a new one.")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--session", help="Existing session ID supplied by the original client")
    parser.add_argument("--cwd", type=Path, help="Original session workspace")
    parser.add_argument("--name", default="原来的伙伴")
    parser.add_argument("--command", default="mcode")
    parser.add_argument("--data-dir", type=Path, help="Original MINIMAX_DATA_DIR if non-default")
    parser.add_argument("--original-client-idle", action="store_true",
                        help="Confirm the original client will stay idle while the game adapter runs this session")
    parser.add_argument("--disconnect", action="store_true")
    parser.add_argument("--list", action="store_true", help="List existing conversations in the original workspace; no model call")
    args = parser.parse_args(argv)
    if args.disconnect:
        binding = read_binding(args.run_dir)
        if binding:
            try:
                call_adapter(binding, "disconnect")
            except (OSError, ValueError, AcpError):
                pass
            write_binding(args.run_dir, {**binding, "enabled": False})
        print("游戏连接已断开；原客户端与原会话保留。")
        return 0
    if not args.cwd or not args.cwd.is_dir():
        parser.error("需要已有原会话的 --cwd 工作目录。")
    if not args.list and (not args.session or not args.original_client_idle):
        parser.error("连接需要原 session、原 cwd，并用 --original-client-idle 确认原窗口在游戏期间保持闲置。")
    binding = {"version": 1, "adapter": "mcode-acp", "clientName": "MiniMax Code",
        "providerSessionId": args.session, "cwd": str(args.cwd.resolve()), "conversationName": args.name,
        "command": args.command, "enabled": True, "originalClientIdleAcknowledged": True,
        "bindingId": uuid.uuid4().hex}
    data_dir = args.data_dir or os.environ.get("MINIMAX_DATA_DIR")
    if data_dir:
        binding["dataDir"] = str(Path(data_dir).resolve())
    task = SimpleNamespace(process=None, cancelled=False)
    client = None
    server = None
    ready = False
    try:
        existing = read_binding(args.run_dir)
        if existing and existing.get("enabled", True) and not args.list:
            try:
                call_adapter(existing, "health")
            except (OSError, ValueError, AcpError):
                pass
            else:
                raise AcpError("已有伙伴适配器正在连接；请先 --disconnect 再明确更换。")
        if not args.list:
            server = AdapterServer(args.run_dir, binding)
            binding["identity"] = verify_session_identity(binding)
        client = AcpClient(binding, task, timeout=30)
        if args.list:
            print(json.dumps(client.sessions(), ensure_ascii=False, indent=2))
            return 0
        client.load()  # verifies the real provider session and cwd; never prompts a model
        write_binding(args.run_dir, binding)
        ready = True
    except (AcpError, OSError, IdentityVerificationError) as ex:
        print("未连接：" + str(ex), file=sys.stderr)
        return 1
    finally:
        if client:
            client.close()
        if server and not ready:
            server.server_close()
    print(f"已验证原会话 {args.session}（{args.name}），原工作目录：{binding['cwd']}。")
    print("适配器正在等待游戏消息。请在游戏中选择“连接已有 AI 伙伴”；原窗口保持闲置，Ctrl+C断开。", flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.cancel_active()
        server.server_close()
        current = read_binding(args.run_dir)
        if current and current.get("bindingId") == binding["bindingId"]:
            write_binding(args.run_dir, {**binding, "enabled": False})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
