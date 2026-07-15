import os
import platform
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

IS_WINDOWS = platform.system() == "Windows"

AUTH_TOKEN = os.getenv("JARVIS_AUTH_TOKEN", "").strip()
MACHINE_NAME = (
    os.getenv("JARVIS_MACHINE_NAME", "").strip()
    or platform.node()
    or ("Windows" if IS_WINDOWS else "Linux")
)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

WHISPER_ENABLED = os.getenv("WHISPER_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL_SIZE", "small")
WHISPER_MODEL_PATH = os.getenv("WHISPER_MODEL_PATH", "").strip() or str(Path.home() / "whisper-models")


def project_roots() -> list[str]:
    raw = os.getenv("PROJECT_ROOTS", "")
    roots = [part.strip() for part in raw.split(";") if part.strip()]
    if roots:
        return roots
    if IS_WINDOWS:
        return [r"C:\xampp\htdocs", r"D:\_2026"]
    return [str(Path.home() / "_2026")]
