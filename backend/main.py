import asyncio
import hmac
import os
import tempfile
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

import config
from services import db
from services.chat_service import ChatManager, ChatSession, sdk_available
from services.project_service import ProjectService
from services.push_service import PushService
from services.session_manager import Session, SessionManager, engines_available
from services.telegram_service import TelegramService
from services.whisper_service import WhisperService


BASE_DIR = Path(__file__).resolve().parent
FRONTEND_FILE = BASE_DIR.parent / "frontend" / "index.html"

app = FastAPI(title="Jarvis Agent")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

projects = ProjectService()
sessions = SessionManager()
chats = ChatManager()
whisper = WhisperService()
telegram = TelegramService(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID, config.MACHINE_NAME)
push = PushService(config.MACHINE_NAME)


# ---------- Auth ----------

bearer_scheme = HTTPBearer(auto_error=False)


def require_auth(credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme)):
    if not config.AUTH_TOKEN:
        raise HTTPException(status_code=500, detail="JARVIS_AUTH_TOKEN belum diset di backend/.env")
    if not credentials or not hmac.compare_digest(credentials.credentials, config.AUTH_TOKEN):
        raise HTTPException(status_code=401, detail="Token salah atau tidak ada")


def ws_token_valid(token: str) -> bool:
    return bool(config.AUTH_TOKEN) and hmac.compare_digest(token, config.AUTH_TOKEN)


api = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])


# ---------- Payload models ----------

class PathPayload(BaseModel):
    path: str


class TextPayload(BaseModel):
    text: str


class CreateSessionPayload(BaseModel):
    engine: str
    path: str
    cols: int = 80
    rows: int = 24


class InputPayload(BaseModel):
    text: str = ""
    enter: bool = True


class KeyPayload(BaseModel):
    key: str


class ResizePayload(BaseModel):
    cols: int
    rows: int


class CreateChatPayload(BaseModel):
    path: str = ""
    resume_id: str = ""


class PermissionPayload(BaseModel):
    request_id: str
    decision: str  # allow | deny | allow_always
    message: str = ""


class AutoAllowPayload(BaseModel):
    readonly: bool | None = None
    edits: bool | None = None
    bash: bool | None = None
    all: bool | None = None


class GitPayload(BaseModel):
    path: str
    cmd: str  # status | diff | log | commit | push | pull
    message: str = ""


class SubscriptionPayload(BaseModel):
    subscription: dict


class PushPermissionPayload(BaseModel):
    chat_id: str
    request_id: str
    decision: str
    nonce: str


# ---------- Lifecycle ----------

@app.on_event("startup")
async def startup():
    projects.scan()
    sessions.on_approval = handle_approval
    sessions.on_exit = handle_session_exit
    chats.on_permission = handle_chat_permission
    chats.on_turn_done = handle_chat_turn_done
    telegram.input_handler = handle_telegram_input
    telegram.permission_handler = handle_telegram_permission
    telegram.status_provider = build_status_text
    await telegram.start()

    if config.WHISPER_ENABLED:
        asyncio.create_task(load_whisper_background())


@app.on_event("shutdown")
async def shutdown():
    await chats.shutdown()
    await sessions.shutdown()
    await telegram.stop()


async def load_whisper_background():
    try:
        await asyncio.to_thread(whisper.load, config.WHISPER_MODEL_SIZE, config.WHISPER_MODEL_PATH)
        print("[JARVIS] Whisper ready.")
    except Exception as exc:
        print(f"[JARVIS] Whisper gagal load: {exc}")


# ---------- Telegram wiring ----------

async def handle_approval(session: Session, excerpt: str):
    await telegram.notify_approval(session.id, session.engine, f"{session.project_name} — {session.project_path}", excerpt)
    await push.notify(
        f"🔔 {session.engine} butuh konfirmasi",
        f"{session.project_name}: {excerpt[-200:]}",
        data={"kind": "open"},
        tag=f"term-{session.id}",
    )


async def handle_session_exit(session: Session):
    text = f"🏁 Sesi {session.engine} di '{session.project_name}' berakhir (exit code {session.exit_code})."
    await telegram.notify(text)
    await push.notify("🏁 Sesi berakhir", text, data={"kind": "open"}, tag=f"term-{session.id}")


def permission_nonce(chat_id: str, request_id: str) -> str:
    return hmac.new(
        config.AUTH_TOKEN.encode(), f"{chat_id}:{request_id}".encode(), "sha256"
    ).hexdigest()[:20]


async def handle_telegram_input(session_id: str, data: str, is_named_key: bool) -> bool:
    session = sessions.get(session_id)
    if not session:
        return False
    if is_named_key:
        return session.write_key(data)
    return session.write(data)


async def handle_chat_permission(chat: ChatSession, request_event: dict):
    detail = permission_detail(request_event["tool"], request_event.get("input") or {})
    await telegram.notify_chat_permission(
        chat.id, request_event["id"], request_event["tool"], detail,
        f"{chat.project_name} — {chat.project_path}",
    )
    await push.notify(
        f"🔐 Izinkan {request_event['tool']}?",
        f"{chat.project_name}: {detail[:200]}",
        data={
            "kind": "chat_permission",
            "chat_id": chat.id,
            "request_id": request_event["id"],
            "nonce": permission_nonce(chat.id, request_event["id"]),
        },
        actions=[
            {"action": "allow", "title": "✅ Izinkan"},
            {"action": "deny", "title": "❌ Tolak"},
        ],
        tag=f"perm-{chat.id}-{request_event['id']}",
    )


async def handle_chat_turn_done(chat: ChatSession, result_event: dict):
    if result_event.get("is_error"):
        text = f"⚠️ Chat Claude di '{chat.project_name}' error ({result_event.get('subtype')})."
        await telegram.notify(text)
        await push.notify("⚠️ Task error", text, data={"kind": "open"}, tag=f"chat-{chat.id}")
    elif (result_event.get("duration_ms") or 0) > 45_000:
        cost = result_event.get("cost_usd")
        cost_text = f" · ${cost:.2f}" if cost else ""
        text = f"✅ Task Claude di '{chat.project_name}' selesai{cost_text}."
        await telegram.notify(text)
        await push.notify("✅ Task selesai", text, data={"kind": "open"}, tag=f"chat-{chat.id}")


async def handle_telegram_permission(chat_session_id: str, request_id: str, decision: str) -> bool:
    chat = chats.get(chat_session_id)
    if not chat:
        return False
    return chat.resolve_permission(request_id, decision)


def permission_detail(tool: str, input_data: dict) -> str:
    if tool == "Bash":
        return f"$ {input_data.get('command', '')}"
    if tool in ("Write", "Edit", "MultiEdit", "Read", "NotebookEdit"):
        detail = input_data.get("file_path", "")
        if tool == "Edit":
            detail += f"\n--- lama ---\n{str(input_data.get('old_string', ''))[:200]}"
            detail += f"\n+++ baru +++\n{str(input_data.get('new_string', ''))[:200]}"
        elif tool == "Write":
            detail += f"\n{str(input_data.get('content', ''))[:250]}"
        return detail
    pairs = ", ".join(f"{k}={str(v)[:80]}" for k, v in list(input_data.items())[:5])
    return pairs or tool


def build_status_text() -> str:
    lines = [f"🖥 {config.MACHINE_NAME} — online"]
    active = sessions.list_info() + chats.list_info()
    if not active:
        lines.append("Tidak ada sesi.")
    for info in active:
        kind = info.get("engine") or "chat"
        lines.append(f"• [{info['id']}] {kind} @ {info['project']} — {info['status']}")
    return "\n".join(lines)


# ---------- Frontend (tanpa auth; token dimasukkan user di app) ----------

@app.get("/")
async def index():
    if not FRONTEND_FILE.exists():
        raise HTTPException(status_code=404, detail="frontend/index.html tidak ditemukan")
    return FileResponse(FRONTEND_FILE)


NO_CACHE = {"Cache-Control": "no-cache, must-revalidate"}


@app.get("/manifest.json")
async def manifest():
    return FileResponse(FRONTEND_FILE.parent / "manifest.json",
                        media_type="application/manifest+json", headers=NO_CACHE)


@app.get("/sw.js")
async def service_worker():
    return FileResponse(FRONTEND_FILE.parent / "sw.js", media_type="text/javascript", headers=NO_CACHE)


@app.get("/icon.svg")
async def icon():
    return FileResponse(FRONTEND_FILE.parent / "icon.svg", media_type="image/svg+xml")


@app.get("/icon-192.png")
async def icon_192():
    return FileResponse(FRONTEND_FILE.parent / "icon-192.png", media_type="image/png", headers=NO_CACHE)


@app.get("/icon-512.png")
async def icon_512():
    return FileResponse(FRONTEND_FILE.parent / "icon-512.png", media_type="image/png", headers=NO_CACHE)


# ---------- API ----------

@api.get("/status")
async def status():
    chat_ok, chat_error = sdk_available()
    engines = engines_available()
    engines["chat"] = {"available": chat_ok and engines.get("claude", {}).get("available", False)}
    if not engines["chat"]["available"]:
        engines["chat"]["error"] = chat_error or engines.get("claude", {}).get("error")
    return {
        "backend": "online",
        "machine": config.MACHINE_NAME,
        "platform": "windows" if config.IS_WINDOWS else "linux",
        "engines": engines,
        "chats": chats.list_info(),
        "whisper": {
            "enabled": config.WHISPER_ENABLED,
            "loaded": whisper.is_loaded,
            "loading": whisper.is_loading,
            "model_size": whisper.model_size,
            "error": whisper.last_error,
        },
        "sessions": sessions.list_info(),
        "active_project": projects.active_project,
    }


@api.get("/projects")
async def list_projects():
    return {"projects": projects.projects, "active_project": projects.active_project}


@api.post("/projects/refresh")
async def refresh_projects():
    return {"projects": projects.scan(), "active_project": projects.active_project}


@api.post("/projects/custom")
async def add_custom_project(payload: PathPayload):
    project = projects.add_custom(payload.path)
    if not project:
        raise HTTPException(status_code=400, detail="Path tidak valid atau bukan folder")
    return {"project": project, "projects": projects.projects}


@api.post("/projects/command")
async def run_project_command(payload: TextPayload):
    try:
        project = projects.run_command(payload.text)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"project": project, "projects": projects.projects}


@api.get("/sessions")
async def list_sessions():
    return {"sessions": sessions.list_info()}


@api.post("/sessions")
async def create_session(payload: CreateSessionPayload):
    project = projects.set_active(payload.path)
    if not project:
        raise HTTPException(status_code=404, detail="Project tidak ditemukan")

    try:
        session = await sessions.create(payload.engine, project["path"], payload.cols, payload.rows)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Gagal start sesi: {exc}") from exc

    return {"session": session.info()}


@api.post("/sessions/{session_id}/input")
async def session_input(session_id: str, payload: InputPayload):
    session = sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Sesi tidak ditemukan")
    data = payload.text + ("\r" if payload.enter else "")
    if not data:
        raise HTTPException(status_code=400, detail="Input kosong")
    if not session.write(data):
        raise HTTPException(status_code=409, detail="Sesi sudah tidak berjalan")
    return {"ok": True}


@api.post("/sessions/{session_id}/key")
async def session_key(session_id: str, payload: KeyPayload):
    session = sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Sesi tidak ditemukan")
    if not session.write_key(payload.key):
        raise HTTPException(status_code=409, detail="Key tidak dikenal atau sesi sudah berhenti")
    return {"ok": True}


@api.post("/sessions/{session_id}/resize")
async def session_resize(session_id: str, payload: ResizePayload):
    session = sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Sesi tidak ditemukan")
    session.resize(payload.cols, payload.rows)
    return {"ok": True}


@api.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    session = sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Sesi tidak ditemukan")
    await session.kill()
    sessions.remove(session_id)
    return {"ok": True}


# ---------- Chat (Claude Agent SDK) ----------

@api.get("/chat")
async def list_chats():
    active = chats.list_info()
    active_ids = {c["id"] for c in active}
    recent = [r for r in db.recent_chat_sessions(20) if r["id"] not in active_ids]
    return {"active": active, "recent": recent}


@api.post("/chat")
async def create_chat(payload: CreateChatPayload):
    try:
        if payload.resume_id:
            chat = await chats.resume(payload.resume_id)
        else:
            project = projects.set_active(payload.path)
            if not project:
                raise HTTPException(status_code=404, detail="Project tidak ditemukan")
            chat = await chats.create(project["path"])
    except HTTPException:
        raise
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Gagal start chat: {exc}") from exc
    return {"chat": chat.info()}


@api.post("/chat/{chat_id}/prompt")
async def chat_prompt(chat_id: str, payload: TextPayload):
    chat = chats.get(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Sesi chat tidak ditemukan")
    sent = await chat.send_prompt(payload.text)
    if not sent:
        raise HTTPException(status_code=409, detail="Chat sedang memproses atau teks kosong")
    return {"ok": True}


@api.post("/chat/{chat_id}/permission")
async def chat_permission(chat_id: str, payload: PermissionPayload):
    chat = chats.get(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Sesi chat tidak ditemukan")
    if not chat.resolve_permission(payload.request_id, payload.decision, payload.message):
        raise HTTPException(status_code=409, detail="Permission request sudah tidak aktif")
    return {"ok": True}


@api.post("/chat/{chat_id}/interrupt")
async def chat_interrupt(chat_id: str):
    chat = chats.get(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Sesi chat tidak ditemukan")
    await chat.interrupt()
    return {"ok": True}


@api.post("/chat/{chat_id}/auto-allow")
async def chat_auto_allow(chat_id: str, payload: AutoAllowPayload):
    chat = chats.get(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Sesi chat tidak ditemukan")
    chat.set_auto_allow(payload.model_dump(exclude_none=True))
    return {"ok": True, "auto_allow": chat.auto_allow}


@api.delete("/chat/{chat_id}")
async def close_chat(chat_id: str, delete_history: bool = False):
    closed = await chats.close(chat_id, delete_history=delete_history)
    if not closed:
        raise HTTPException(status_code=404, detail="Sesi chat tidak ditemukan")
    return {"ok": True}


# ---------- Web Push ----------

@api.get("/push/key")
async def push_key():
    return {"key": push.public_key}


@api.post("/push/subscribe")
async def push_subscribe(payload: SubscriptionPayload):
    if not payload.subscription.get("endpoint"):
        raise HTTPException(status_code=400, detail="Subscription tidak valid")
    db.save_push_subscription(payload.subscription)
    return {"ok": True, "total": len(db.list_push_subscriptions())}


@api.post("/push/unsubscribe")
async def push_unsubscribe(payload: SubscriptionPayload):
    db.delete_push_subscription(payload.subscription.get("endpoint", ""))
    return {"ok": True}


@api.post("/push/test")
async def push_test():
    await push.notify("🔔 Tes notifikasi", "Web Push dari Jarvis aktif!", data={"kind": "open"}, tag="test")
    return {"ok": True, "subscribers": len(db.list_push_subscriptions())}


@app.post("/api/push/permission")
async def push_permission(payload: PushPermissionPayload):
    """Dipanggil dari tombol notifikasi (service worker) — divalidasi nonce, bukan Bearer."""
    expected = permission_nonce(payload.chat_id, payload.request_id)
    if not hmac.compare_digest(payload.nonce, expected):
        raise HTTPException(status_code=401, detail="Nonce salah")
    chat = chats.get(payload.chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Sesi chat tidak ditemukan")
    if payload.decision not in ("allow", "deny", "allow_always"):
        raise HTTPException(status_code=400, detail="Decision tidak valid")
    if not chat.resolve_permission(payload.request_id, payload.decision):
        raise HTTPException(status_code=409, detail="Permission request sudah tidak aktif")
    return {"ok": True}


# ---------- Git ----------

GIT_COMMANDS = {
    "status": ["git", "status", "--short", "--branch"],
    "diff": ["git", "diff", "HEAD"],
    "log": ["git", "log", "--oneline", "-15"],
    "push": ["git", "push"],
    "pull": ["git", "pull"],
}


@api.post("/git")
async def git_command(payload: GitPayload):
    if not Path(payload.path).is_dir():
        raise HTTPException(status_code=400, detail="Path tidak valid")

    if payload.cmd == "commit":
        if not payload.message.strip():
            raise HTTPException(status_code=400, detail="Pesan commit wajib diisi")
        add_out = await run_git(payload.path, ["git", "add", "-A"])
        commit_out = await run_git(payload.path, ["git", "commit", "-m", payload.message.strip()])
        return {"output": (add_out + "\n" + commit_out).strip()}

    command = GIT_COMMANDS.get(payload.cmd)
    if not command:
        raise HTTPException(status_code=400, detail=f"Perintah git tidak didukung: {payload.cmd}")
    output = await run_git(payload.path, command)
    return {"output": output.strip() or "(tidak ada output)"}


async def run_git(cwd: str, command: list[str]) -> str:
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await asyncio.wait_for(process.communicate(), timeout=60)
    return stdout.decode("utf-8", errors="replace")[:100_000]


# ---------- Upload gambar (lampiran prompt) ----------

UPLOAD_DIR_NAME = ".jarvis-uploads"
UPLOAD_MAX_BYTES = 10 * 1024 * 1024
UPLOAD_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
UPLOAD_MAX_AGE = 7 * 24 * 3600


@api.post("/upload")
async def upload_image(file: UploadFile = File(...), path: str = Form(...)):
    import time as _time
    import uuid as _uuid

    project = Path(path)
    if not project.is_dir():
        raise HTTPException(status_code=400, detail="Path project tidak valid")

    ext = Path(file.filename or "img.jpg").suffix.lower() or ".jpg"
    if ext not in UPLOAD_EXTS:
        raise HTTPException(status_code=400, detail=f"Tipe file tidak didukung: {ext}")

    uploads = project / UPLOAD_DIR_NAME
    uploads.mkdir(exist_ok=True)
    # folder self-ignoring: tidak menyentuh .gitignore project
    marker = uploads / ".gitignore"
    if not marker.exists():
        marker.write_text("*\n")

    # bersihkan upload lama
    now = _time.time()
    for old in uploads.glob("img-*"):
        try:
            if now - old.stat().st_mtime > UPLOAD_MAX_AGE:
                old.unlink()
        except OSError:
            pass

    name = f"img-{_time.strftime('%Y%m%d-%H%M%S')}-{_uuid.uuid4().hex[:4]}{ext}"
    target = uploads / name
    written = 0
    with open(target, "wb") as handle:
        while chunk := await file.read(1024 * 1024):
            written += len(chunk)
            if written > UPLOAD_MAX_BYTES:
                handle.close()
                target.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail="Gambar terlalu besar (maks 10MB)")
            handle.write(chunk)

    return {"path": f"{UPLOAD_DIR_NAME}/{name}", "abs": str(target)}


@app.get("/api/uploads")
async def serve_upload(p: str, token: str = Query("")):
    """Serve thumbnail lampiran untuk <img> (auth via token query, dibatasi folder uploads)."""
    if not ws_token_valid(token):
        raise HTTPException(status_code=401, detail="Token salah")
    target = Path(p).resolve()
    if UPLOAD_DIR_NAME not in target.parts or not target.is_file():
        raise HTTPException(status_code=404, detail="File tidak ditemukan")
    return FileResponse(target)


@api.post("/transcribe")
async def transcribe_audio(file: UploadFile = File(...)):
    ensure_whisper_ready()
    audio_path = await save_upload(file)
    try:
        text = await asyncio.to_thread(whisper.transcribe, audio_path)
    except Exception as exc:
        if Path(audio_path).exists():
            Path(audio_path).unlink()
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"text": text}


app.include_router(api)


# ---------- WebSocket per sesi ----------

@app.websocket("/ws/session/{session_id}")
async def websocket_session(
    websocket: WebSocket,
    session_id: str,
    token: str = Query(""),
    since: int = Query(0),
):
    if not ws_token_valid(token):
        await websocket.close(code=4401)
        return

    session = sessions.get(session_id)
    if not session:
        await websocket.close(code=4404)
        return

    await websocket.accept()

    backlog = session.output_since(since)
    await websocket.send_json({"t": "o", "d": backlog, "s": session.seq})
    if session.status == "exited":
        await websocket.send_json({"t": "exit", "code": session.exit_code})

    async def forward(frame: dict):
        await websocket.send_json(frame)

    session.subscribe(forward)
    try:
        while True:
            message = await websocket.receive_json()
            kind = message.get("t")
            if kind == "i":
                session.write(str(message.get("d", "")))
            elif kind == "key":
                session.write_key(str(message.get("k", "")))
            elif kind == "resize":
                session.resize(int(message.get("cols", 80)), int(message.get("rows", 24)))
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        session.unsubscribe(forward)


@app.websocket("/ws/chat/{chat_id}")
async def websocket_chat(
    websocket: WebSocket,
    chat_id: str,
    token: str = Query(""),
    since: int = Query(0),
):
    if not ws_token_valid(token):
        await websocket.close(code=4401)
        return

    chat = chats.get(chat_id)
    if not chat:
        await websocket.close(code=4404)
        return

    await websocket.accept()

    await websocket.send_json({"t": "hello", "info": chat.info()})
    for event in chat.events_since(since):
        await websocket.send_json(event)

    async def forward(event: dict):
        await websocket.send_json(event)

    chat.subscribe(forward)
    try:
        while True:
            message = await websocket.receive_json()
            kind = message.get("t")
            if kind == "prompt":
                await chat.send_prompt(str(message.get("text", "")))
            elif kind == "permission":
                chat.resolve_permission(
                    str(message.get("id", "")),
                    str(message.get("decision", "")),
                    str(message.get("message", "")),
                )
            elif kind == "interrupt":
                await chat.interrupt()
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        chat.unsubscribe(forward)


# ---------- Helpers ----------

async def save_upload(file: UploadFile) -> str:
    suffix = Path(file.filename or "audio.webm").suffix or ".webm"
    fd, audio_path = tempfile.mkstemp(prefix="jarvis-audio-", suffix=suffix)
    os.close(fd)

    with open(audio_path, "wb") as handle:
        while chunk := await file.read(1024 * 1024):
            handle.write(chunk)

    return audio_path


def ensure_whisper_ready():
    if not config.WHISPER_ENABLED:
        raise HTTPException(status_code=503, detail="Whisper dinonaktifkan di mesin ini (WHISPER_ENABLED=false).")
    if whisper.is_loaded:
        return
    if whisper.is_loading:
        raise HTTPException(status_code=503, detail="Whisper masih loading model. Coba lagi sebentar.")
    raise HTTPException(status_code=503, detail=whisper.last_error or "Whisper belum siap.")
