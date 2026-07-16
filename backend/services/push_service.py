"""Web Push (PWA) — kanal notifikasi kedua di samping Telegram.

Kunci VAPID dibuat otomatis saat pertama jalan dan disimpan di backend/.
Subscription browser disimpan di SQLite; subscription mati (404/410) dihapus
otomatis saat pengiriman gagal.
"""

import asyncio
import base64
import json
from pathlib import Path

from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


VAPID_KEY_FILE = Path(__file__).resolve().parent.parent / "vapid_private.pem"
VAPID_CLAIMS_SUB = "mailto:jarvis@localhost"


class PushService:
    def __init__(self, machine_name: str):
        self.machine = machine_name
        self._vapid = None
        self._public_key_b64: str | None = None

    def _ensure_keys(self):
        if self._vapid is not None:
            return
        from py_vapid import Vapid

        if VAPID_KEY_FILE.exists():
            self._vapid = Vapid.from_file(str(VAPID_KEY_FILE))
        else:
            self._vapid = Vapid()
            self._vapid.generate_keys()
            self._vapid.save_key(str(VAPID_KEY_FILE))
            print("[JARVIS] Kunci VAPID baru dibuat:", VAPID_KEY_FILE.name)

        raw = self._vapid.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
        self._public_key_b64 = base64.urlsafe_b64encode(raw).decode().rstrip("=")

    @property
    def public_key(self) -> str:
        self._ensure_keys()
        return self._public_key_b64

    async def notify(self, title: str, body: str, data: dict | None = None,
                     actions: list[dict] | None = None, tag: str = ""):
        """Kirim notifikasi ke semua subscription. Aman dipanggil walau belum ada subscriber."""
        from services import db

        subscriptions = db.list_push_subscriptions()
        if not subscriptions:
            return

        payload = json.dumps({
            "title": f"[{self.machine}] {title}",
            "body": body[:500],
            "tag": tag or None,
            "actions": actions or [],
            **(data or {}),
        }, ensure_ascii=False)

        self._ensure_keys()
        await asyncio.gather(
            *(asyncio.to_thread(self._send_one, sub, payload) for sub in subscriptions),
            return_exceptions=True,
        )

    def _send_one(self, subscription: dict, payload: str):
        from pywebpush import WebPushException, webpush

        try:
            webpush(
                subscription_info=subscription,
                data=payload,
                vapid_private_key=str(VAPID_KEY_FILE),
                vapid_claims={"sub": VAPID_CLAIMS_SUB},
                ttl=3600,
            )
        except WebPushException as exc:
            status = getattr(exc.response, "status_code", None)
            if status in (400, 403, 404, 410):
                from services import db

                db.delete_push_subscription(subscription.get("endpoint", ""))
                print(f"[JARVIS] Push subscription mati (HTTP {status}) — dihapus.")
            else:
                print(f"[JARVIS] Web push gagal: {exc}")
        except Exception as exc:
            print(f"[JARVIS] Web push error: {exc}")
