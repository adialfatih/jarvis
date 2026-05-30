import asyncio
import os
import tempfile
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from services.codex_service import CodexService
from services.project_service import ProjectService
from services.whisper_service import WhisperService


BASE_DIR = Path(__file__).resolve().parent
FRONTEND_FILE = BASE_DIR.parent / "frontend" / "index.html"

load_dotenv(BASE_DIR / ".env")

app = FastAPI(title="Jarvis Codex Remote")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

projects = ProjectService()
codex = CodexService()
whisper = WhisperService()


class PathPayload(BaseModel):
    path: str


class TextPayload(BaseModel):
    text: str


@app.on_event("startup")
async def startup():
    projects.scan()

    whisper_model_size = os.getenv("WHISPER_MODEL_SIZE", "small")
    whisper_model_path = os.getenv("WHISPER_MODEL_PATH", r"D:\whisper-models")
    asyncio.create_task(load_whisper_background(whisper_model_size, whisper_model_path))

    default_project = projects.choose_default(os.getenv("CODEX_DEFAULT_PROJECT"))
    if default_project:
        await codex.start(default_project["path"])
    else:
        await codex._broadcast("[JARVIS] Tidak ada project ditemukan. Tambahkan path manual dari HP.\n")


@app.on_event("shutdown")
async def shutdown():
    await codex.stop()


async def load_whisper_background(model_size: str, model_path: str):
    await codex._broadcast(f"[JARVIS] Loading Whisper model '{model_size}' di background...\n")
    try:
        await asyncio.to_thread(whisper.load, model_size, model_path)
        await codex._broadcast("[JARVIS] Whisper ready.\n")
    except Exception as exc:
        print(f"[JARVIS] Whisper load failed: {exc}")
        await codex._broadcast(f"[JARVIS] Whisper gagal load: {exc}\n")


@app.get("/")
async def index():
    if not FRONTEND_FILE.exists():
        raise HTTPException(status_code=404, detail="frontend/index.html tidak ditemukan")
    return FileResponse(FRONTEND_FILE)


@app.get("/api/status")
async def status():
    return {
        "backend": "online",
        "whisper": {
            "loaded": whisper.is_loaded,
            "loading": whisper.is_loading,
            "model_size": whisper.model_size,
            "error": whisper.last_error,
        },
        "codex": {
            "running": codex.is_running,
            "busy": codex.is_busy,
            "project": codex.current_project,
            "error": codex.last_error,
        },
        "active_project": projects.active_project,
    }


@app.get("/api/projects")
async def list_projects():
    return {"projects": projects.projects, "active_project": projects.active_project}


@app.post("/api/projects/refresh")
async def refresh_projects():
    return {"projects": projects.scan(), "active_project": projects.active_project}


@app.post("/api/projects/custom")
async def add_custom_project(payload: PathPayload):
    project = projects.add_custom(payload.path)
    if not project:
        raise HTTPException(status_code=400, detail="Path tidak valid atau bukan folder")
    return {"project": project, "projects": projects.projects}


@app.post("/api/switch-project")
async def switch_project(payload: PathPayload):
    project = projects.set_active(payload.path)
    if not project:
        raise HTTPException(status_code=404, detail="Project tidak ditemukan")

    await codex.switch_project(project["path"])
    return {"ok": True, "active_project": project}


@app.post("/api/send")
async def send_to_codex(payload: TextPayload):
    sent = await codex.send_input(payload.text)
    if not sent:
        raise HTTPException(status_code=409, detail="Codex belum running atau teks kosong")
    return {"ok": True}


@app.post("/api/transcribe")
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


@app.post("/api/transcribe-and-send")
async def transcribe_and_send(file: UploadFile = File(...)):
    ensure_whisper_ready()
    audio_path = await save_upload(file)
    try:
        text = await asyncio.to_thread(whisper.transcribe, audio_path)
    except Exception as exc:
        if Path(audio_path).exists():
            Path(audio_path).unlink()
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    sent = await codex.send_input(text)
    return {"text": text, "sent": sent}


@app.post("/api/restart-codex")
async def restart_codex():
    await codex.restart()
    return {"ok": True}


@app.websocket("/ws/output")
async def websocket_output(websocket: WebSocket):
    await websocket.accept()

    async def send(text: str):
        await websocket.send_text(text)

    codex.add_output_callback(send)
    await send("[JARVIS] Connected to Jarvis output stream.\n")

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        codex.remove_output_callback(send)


async def save_upload(file: UploadFile) -> str:
    suffix = Path(file.filename or "audio.webm").suffix or ".webm"
    fd, audio_path = tempfile.mkstemp(prefix="jarvis-audio-", suffix=suffix)
    os.close(fd)

    with open(audio_path, "wb") as handle:
        while chunk := await file.read(1024 * 1024):
            handle.write(chunk)

    return audio_path


def ensure_whisper_ready():
    if whisper.is_loaded:
        return
    if whisper.is_loading:
        raise HTTPException(status_code=503, detail="Whisper masih loading/download model. Coba lagi setelah status ready.")
    raise HTTPException(status_code=503, detail=whisper.last_error or "Whisper belum siap.")
