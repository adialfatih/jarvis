"""Sesi chat terstruktur untuk Claude Code via Claude Agent SDK.

Berbeda dengan sesi PTY (terminal mentah), sesi chat memberi event JSON
terstruktur: teks assistant, kartu tool, dan permission request yang bisa
dijawab Approve/Deny/Always dari HP atau Telegram.
"""

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

from . import db


EventCallback = Callable[[dict], Awaitable[None]]

READ_ONLY_TOOLS = {"Read", "Glob", "Grep", "WebSearch", "WebFetch", "TodoWrite", "NotebookRead"}
EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}

MAX_TEXT_CHARS = 30_000


def sdk_available() -> tuple[bool, str | None]:
    try:
        import claude_agent_sdk  # noqa: F401

        return True, None
    except ImportError as exc:
        return False, f"claude-agent-sdk belum terinstall: {exc}"


class ChatSession:
    def __init__(self, session_id: str, project_path: str, resume_claude_id: str | None = None):
        self.id = session_id
        self.project_path = project_path
        self.project_name = Path(project_path).name
        self.claude_session_id = resume_claude_id
        self.status = "starting"  # starting | idle | running | waiting_permission | error | closed
        self.created_at = time.time()
        self.last_prompt: str | None = None
        self.events: list[dict] = []
        self.auto_allow = {"readonly": True, "edits": False, "bash": False, "all": False}
        self._always_allowed_tools: set[str] = set()
        self._pending_permissions: dict[str, asyncio.Future] = {}
        self._subscribers: list[EventCallback] = []
        self._client = None
        self._turn_task: asyncio.Task | None = None
        self._turn_started_at = 0.0

        # di-set oleh ChatManager/main
        self.on_permission: Callable[["ChatSession", dict], Awaitable[None]] | None = None
        self.on_turn_done: Callable[["ChatSession", dict], Awaitable[None]] | None = None

    # ---------- lifecycle ----------

    async def start(self):
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

        options = ClaudeAgentOptions(
            cwd=self.project_path,
            permission_mode="default",
            can_use_tool=self._can_use_tool,
            resume=self.claude_session_id,
        )
        self._client = ClaudeSDKClient(options=options)
        await self._client.connect()
        self._set_status("idle")
        self._persist()

    async def close(self):
        if self._turn_task and not self._turn_task.done():
            self._turn_task.cancel()
        for future in self._pending_permissions.values():
            if not future.done():
                future.set_result(("deny", "Sesi ditutup"))
        if self._client:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None
        self._set_status("closed")
        self._persist()

    # ---------- prompt & turn ----------

    async def send_prompt(self, text: str) -> bool:
        text = text.strip()
        if not text or not self._client:
            return False
        if self.status in ("running", "waiting_permission"):
            return False

        self.last_prompt = text[:300]
        self._emit({"t": "user", "text": text})
        self._set_status("running")
        self._turn_started_at = time.time()
        self._persist()

        await self._client.query(text)
        self._turn_task = asyncio.create_task(self._consume_turn())
        return True

    async def _consume_turn(self):
        try:
            async for message in self._client.receive_response():
                await self._handle_message(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._emit({"t": "error", "message": str(exc)})
            self._set_status("error")
            self._persist()

    async def _handle_message(self, message):
        from claude_agent_sdk import (
            AssistantMessage,
            ResultMessage,
            TextBlock,
            ThinkingBlock,
            ToolResultBlock,
            ToolUseBlock,
            UserMessage,
        )

        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    self._emit({"t": "text", "text": block.text[:MAX_TEXT_CHARS]})
                elif isinstance(block, ThinkingBlock):
                    if block.thinking.strip():
                        self._emit({"t": "thinking", "text": block.thinking[:2000]})
                elif isinstance(block, ToolUseBlock):
                    self._emit({
                        "t": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": _truncate_values(block.input),
                    })
        elif isinstance(message, UserMessage):
            content = message.content
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, ToolResultBlock):
                        self._emit({
                            "t": "tool_result",
                            "tool_use_id": block.tool_use_id,
                            "content": _stringify_result(block.content)[:8000],
                            "is_error": bool(block.is_error),
                        })
        elif isinstance(message, ResultMessage):
            self.claude_session_id = message.session_id
            result_event = {
                "t": "result",
                "subtype": message.subtype,
                "is_error": message.is_error,
                "duration_ms": message.duration_ms,
                "cost_usd": message.total_cost_usd,
                "num_turns": message.num_turns,
            }
            self._emit(result_event)
            self._set_status("error" if message.is_error else "idle")
            self._persist()
            if self.on_turn_done:
                await self.on_turn_done(self, result_event)

    async def interrupt(self):
        if self._client and self.status in ("running", "waiting_permission"):
            for request_id, future in list(self._pending_permissions.items()):
                if not future.done():
                    future.set_result(("deny", "Diinterupsi oleh user"))
            try:
                await self._client.interrupt()
            except Exception as exc:
                self._emit({"t": "error", "message": f"Interrupt gagal: {exc}"})

    # ---------- permission ----------

    async def _can_use_tool(self, tool_name: str, input_data: dict, context):
        from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

        if self._is_auto_allowed(tool_name):
            return PermissionResultAllow()

        request_id = uuid.uuid4().hex[:8]
        future = asyncio.get_running_loop().create_future()
        self._pending_permissions[request_id] = future

        request_event = {
            "t": "permission_request",
            "id": request_id,
            "tool": tool_name,
            "input": _truncate_values(input_data),
        }
        self._emit(request_event)
        self._set_status("waiting_permission")

        if self.on_permission:
            try:
                await self.on_permission(self, request_event)
            except Exception:
                pass

        try:
            decision, message = await future
        finally:
            self._pending_permissions.pop(request_id, None)

        self._emit({"t": "permission_result", "id": request_id, "decision": decision})
        if self.status == "waiting_permission":
            self._set_status("running")

        if decision == "allow_always":
            self._always_allowed_tools.add(tool_name)
            return PermissionResultAllow()
        if decision == "allow":
            return PermissionResultAllow()
        return PermissionResultDeny(message=message or "Ditolak oleh user via Jarvis", interrupt=False)

    def resolve_permission(self, request_id: str, decision: str, message: str = "") -> bool:
        future = self._pending_permissions.get(request_id)
        if not future or future.done():
            return False
        if decision not in ("allow", "deny", "allow_always"):
            return False
        future.set_result((decision, message))
        return True

    def pending_permission_ids(self) -> list[str]:
        return list(self._pending_permissions.keys())

    def _is_auto_allowed(self, tool_name: str) -> bool:
        if tool_name in self._always_allowed_tools:
            return True
        if self.auto_allow.get("all"):
            return True
        if self.auto_allow.get("readonly") and tool_name in READ_ONLY_TOOLS:
            return True
        if self.auto_allow.get("edits") and tool_name in EDIT_TOOLS:
            return True
        if self.auto_allow.get("bash") and tool_name == "Bash":
            return True
        return False

    def set_auto_allow(self, flags: dict):
        for key in ("readonly", "edits", "bash", "all"):
            if key in flags:
                self.auto_allow[key] = bool(flags[key])
        self._emit({"t": "auto_allow", "flags": dict(self.auto_allow)})

    # ---------- events / subscribers ----------

    @property
    def seq(self) -> int:
        return len(self.events)

    def events_since(self, since: int) -> list[dict]:
        return self.events[since:]

    def _emit(self, event: dict):
        event = {**event, "i": len(self.events), "ts": time.time()}
        self.events.append(event)
        try:
            db.append_chat_event(self.id, event["i"], event)
        except Exception:
            pass
        for callback in list(self._subscribers):
            asyncio.ensure_future(self._safe_send(callback, event))

    def _set_status(self, status: str):
        self.status = status
        self._emit({"t": "status", "status": status})

    async def _safe_send(self, callback: EventCallback, event: dict):
        try:
            await callback(event)
        except Exception:
            self.unsubscribe(callback)

    def subscribe(self, callback: EventCallback):
        self._subscribers.append(callback)

    def unsubscribe(self, callback: EventCallback):
        if callback in self._subscribers:
            self._subscribers.remove(callback)

    # ---------- info / persistence ----------

    def info(self) -> dict:
        return {
            "id": self.id,
            "kind": "chat",
            "project": self.project_name,
            "path": self.project_path,
            "status": self.status,
            "claude_session_id": self.claude_session_id,
            "last_prompt": self.last_prompt,
            "auto_allow": dict(self.auto_allow),
            "pending_permissions": self.pending_permission_ids(),
            "created_at": self.created_at,
            "seq": self.seq,
        }

    def _persist(self):
        try:
            db.upsert_chat_session({
                "id": self.id,
                "project_path": self.project_path,
                "project_name": self.project_name,
                "claude_session_id": self.claude_session_id,
                "status": self.status,
                "last_prompt": self.last_prompt,
                "created_at": self.created_at,
            })
        except Exception:
            pass


class ChatManager:
    def __init__(self):
        self.sessions: dict[str, ChatSession] = {}
        self.on_permission = None
        self.on_turn_done = None

    async def create(self, project_path: str) -> ChatSession:
        available, error = sdk_available()
        if not available:
            raise RuntimeError(error)
        session = ChatSession(uuid.uuid4().hex[:8], project_path)
        self._wire(session)
        await session.start()
        self.sessions[session.id] = session
        return session

    async def resume(self, local_id: str) -> ChatSession:
        if local_id in self.sessions and self.sessions[local_id].status not in ("closed", "error"):
            return self.sessions[local_id]

        available, error = sdk_available()
        if not available:
            raise RuntimeError(error)

        stored = db.get_chat_session(local_id)
        if not stored:
            raise KeyError("Sesi chat tidak ditemukan di riwayat")
        if not Path(stored["project_path"]).is_dir():
            raise RuntimeError(f"Folder project sudah tidak ada: {stored['project_path']}")

        session = ChatSession(local_id, stored["project_path"], stored["claude_session_id"])
        session.events = db.load_chat_events(local_id)
        session.last_prompt = stored["last_prompt"]
        session.created_at = stored["created_at"]
        self._wire(session)
        await session.start()
        self.sessions[local_id] = session
        return session

    def _wire(self, session: ChatSession):
        session.on_permission = self.on_permission
        session.on_turn_done = self.on_turn_done

    def get(self, session_id: str) -> ChatSession | None:
        return self.sessions.get(session_id)

    def list_info(self) -> list[dict]:
        return [
            s.info()
            for s in sorted(self.sessions.values(), key=lambda s: s.created_at)
            if s.status != "closed"
        ]

    async def close(self, session_id: str, delete_history: bool = False) -> bool:
        session = self.sessions.pop(session_id, None)
        if session:
            await session.close()
        if delete_history:
            db.delete_chat_session(session_id)
        return session is not None or delete_history

    async def shutdown(self):
        await asyncio.gather(*(s.close() for s in self.sessions.values()), return_exceptions=True)


def _truncate_values(data: dict, limit: int = 20_000) -> dict:
    cleaned = {}
    for key, value in (data or {}).items():
        if isinstance(value, str) and len(value) > limit:
            cleaned[key] = value[:limit] + f"\n… [terpotong, total {len(value)} karakter]"
        else:
            cleaned[key] = value
    return cleaned


def _stringify_result(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            text = getattr(block, "text", None)
            if text is None and isinstance(block, dict):
                text = block.get("text")
            if text:
                parts.append(str(text))
        return "\n".join(parts)
    return "" if content is None else str(content)
