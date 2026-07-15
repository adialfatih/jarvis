import asyncio
import html
from collections.abc import Awaitable, Callable

import httpx


# key callback → keterangan tombol; keystroke aslinya di-resolve oleh session (NAMED_KEYS / literal)
APPROVAL_KEYBOARD = [
    [("1", "1"), ("2", "2"), ("3", "3")],
    [("✅ y", "y"), ("❌ n", "n")],
    [("⏎ Enter", "enter"), ("Esc", "esc"), ("↑", "up"), ("↓", "down")],
]

LITERAL_KEYS = {"1", "2", "3", "y", "n"}


class TelegramService:
    """Notifikasi + remote approve via inline button (long polling, 1 bot per mesin)."""

    def __init__(self, token: str, chat_id: str, machine_name: str):
        self.enabled = bool(token and chat_id)
        self.machine = machine_name
        self.chat_id = str(chat_id)
        self._base = f"https://api.telegram.org/bot{token}"
        self._client: httpx.AsyncClient | None = None
        self._poll_task: asyncio.Task | None = None
        self._offset = 0
        self._message_sessions: dict[int, str] = {}  # message_id notifikasi → session_id

        # di-set oleh main.py
        self.input_handler: Callable[[str, str, bool], Awaitable[bool]] | None = None  # (session_id, data, is_named_key)
        self.permission_handler: Callable[[str, str, str], Awaitable[bool]] | None = None  # (chat_id, request_id, decision)
        self.status_provider: Callable[[], str] | None = None

    async def start(self):
        if not self.enabled:
            print("[JARVIS] Telegram nonaktif (token/chat_id kosong).")
            return
        self._client = httpx.AsyncClient(timeout=70)
        self._poll_task = asyncio.create_task(self._poll_loop())
        print("[JARVIS] Telegram bot aktif.")

    async def stop(self):
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        if self._client:
            await self._client.aclose()

    async def notify(self, text: str):
        await self._call("sendMessage", chat_id=self.chat_id, text=f"[{self.machine}] {text}")

    async def notify_approval(self, session_id: str, engine: str, project: str, excerpt: str):
        text = (
            f"🔔 <b>[{html.escape(self.machine)}]</b> <b>{html.escape(engine)}</b> butuh konfirmasi\n"
            f"📁 <code>{html.escape(project)}</code>\n\n"
            f"<pre>{html.escape(excerpt[-700:])}</pre>\n"
            f"Tekan tombol, atau <i>reply</i> pesan ini untuk mengetik input manual."
        )
        keyboard = {
            "inline_keyboard": [
                [{"text": label, "callback_data": f"k|{session_id}|{key}"} for label, key in row]
                for row in APPROVAL_KEYBOARD
            ]
        }
        result = await self._call(
            "sendMessage", chat_id=self.chat_id, text=text, parse_mode="HTML", reply_markup=keyboard
        )
        if result and result.get("ok"):
            self._message_sessions[result["result"]["message_id"]] = session_id

    async def notify_chat_permission(self, chat_session_id: str, request_id: str,
                                     tool: str, detail: str, project: str):
        text = (
            f"🔐 <b>[{html.escape(self.machine)}]</b> Claude minta izin: <b>{html.escape(tool)}</b>\n"
            f"📁 <code>{html.escape(project)}</code>\n\n"
            f"<pre>{html.escape(detail[:700])}</pre>"
        )
        keyboard = {
            "inline_keyboard": [[
                {"text": "✅ Izinkan", "callback_data": f"p|{chat_session_id}|{request_id}|allow"},
                {"text": "❌ Tolak", "callback_data": f"p|{chat_session_id}|{request_id}|deny"},
                {"text": "♾ Selalu", "callback_data": f"p|{chat_session_id}|{request_id}|allow_always"},
            ]]
        }
        await self._call("sendMessage", chat_id=self.chat_id, text=text,
                         parse_mode="HTML", reply_markup=keyboard)

    async def _poll_loop(self):
        while True:
            try:
                response = await self._client.get(
                    f"{self._base}/getUpdates", params={"timeout": 50, "offset": self._offset}
                )
                payload = response.json()
                if not payload.get("ok"):
                    if payload.get("error_code") == 409:
                        print("[JARVIS] Telegram 409: bot ini dipakai agent lain. Gunakan 1 bot per mesin!")
                        await asyncio.sleep(30)
                    else:
                        await asyncio.sleep(5)
                    continue
                for update in payload.get("result", []):
                    self._offset = update["update_id"] + 1
                    try:
                        await self._handle_update(update)
                    except Exception as exc:
                        print(f"[JARVIS] Telegram update error: {exc}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[JARVIS] Telegram poll error: {exc}")
                await asyncio.sleep(5)

    async def _handle_update(self, update: dict):
        if "callback_query" in update:
            await self._handle_callback(update["callback_query"])
            return

        message = update.get("message")
        if not message or str(message.get("chat", {}).get("id")) != self.chat_id:
            return

        text = (message.get("text") or "").strip()
        if not text:
            return

        if text.startswith("/status"):
            status = self.status_provider() if self.status_provider else "Tidak ada info."
            await self._call("sendMessage", chat_id=self.chat_id, text=f"[{self.machine}]\n{status}")
            return

        reply = message.get("reply_to_message")
        if reply and reply.get("message_id") in self._message_sessions:
            session_id = self._message_sessions[reply["message_id"]]
            ok = await self._route_input(session_id, text + "\r", is_named_key=False)
            feedback = "✔ Terkirim ke sesi." if ok else "✖ Sesi sudah tidak aktif."
            await self._call("sendMessage", chat_id=self.chat_id, text=feedback,
                             reply_to_message_id=message["message_id"])
            return

        await self._call(
            "sendMessage",
            chat_id=self.chat_id,
            text=f"[{self.machine}] Perintah: /status — atau reply pesan notifikasi untuk kirim input ke sesi.",
        )

    async def _handle_callback(self, callback: dict):
        callback_id = callback.get("id")
        if str(callback.get("from", {}).get("id")) != self.chat_id:
            await self._call("answerCallbackQuery", callback_query_id=callback_id, text="Bukan untukmu.")
            return

        parts = (callback.get("data") or "").split("|")

        if len(parts) == 4 and parts[0] == "p":
            _, chat_session_id, request_id, decision = parts
            ok = False
            if self.permission_handler:
                ok = await self.permission_handler(chat_session_id, request_id, decision)
            label = {"allow": "✅ Diizinkan", "deny": "❌ Ditolak", "allow_always": "♾ Selalu diizinkan"}.get(decision, decision)
            feedback = label if ok else "✖ Request sudah tidak aktif"
            await self._call("answerCallbackQuery", callback_query_id=callback_id, text=feedback)
            return

        if len(parts) != 3 or parts[0] != "k":
            await self._call("answerCallbackQuery", callback_query_id=callback_id)
            return

        _, session_id, key = parts
        if key in LITERAL_KEYS:
            ok = await self._route_input(session_id, key, is_named_key=False)
        else:
            ok = await self._route_input(session_id, key, is_named_key=True)

        feedback = f"✔ '{key}' terkirim" if ok else "✖ Sesi sudah tidak aktif"
        await self._call("answerCallbackQuery", callback_query_id=callback_id, text=feedback)

    async def _route_input(self, session_id: str, data: str, is_named_key: bool) -> bool:
        if not self.input_handler:
            return False
        return await self.input_handler(session_id, data, is_named_key)

    async def _call(self, method: str, **params) -> dict | None:
        if not self.enabled or not self._client:
            return None
        try:
            response = await self._client.post(f"{self._base}/{method}", json=params)
            return response.json()
        except Exception as exc:
            print(f"[JARVIS] Telegram {method} gagal: {exc}")
            return None
