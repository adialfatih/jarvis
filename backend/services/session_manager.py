import asyncio
import hashlib
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path


IS_WINDOWS = sys.platform == "win32"

# Frame = dict yang dikirim apa adanya ke WebSocket client:
#   {"t": "o", "d": <output>, "s": <seq>}  — output PTY
#   {"t": "exit", "code": <int|None>}      — proses berakhir
FrameCallback = Callable[[dict], Awaitable[None]]

MAX_BUFFER_CHARS = 300_000
APPROVAL_IDLE_SECONDS = 1.5

# Tombol/keystroke bernama yang boleh dikirim client & Telegram
NAMED_KEYS = {
    "enter": "\r",
    "esc": "\x1b",
    "tab": "\t",
    "shift_tab": "\x1b[Z",
    "up": "\x1b[A",
    "down": "\x1b[B",
    "right": "\x1b[C",
    "left": "\x1b[D",
    "ctrl_c": "\x03",
    "backspace": "\x7f",
}

ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"            # CSI (warna, kursor, dsb.)
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"   # OSC (judul window, dsb.)
    r"|\x1b[@-_]"                            # escape 2-byte lain
)

APPROVAL_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r"\(y/n\)",
        r"\[y/n\]",
        r"\(yes/no\)",
        r"\by/N\b",
        r"do you want",
        r"would you like",
        r"proceed\?",
        r"continue\?",
        r"allow .{0,80}\?",
        r"permission",
        r"\b1\.\s*yes\b",
        r"❯\s*1\b",
        r"press enter to continue",
        r"trust the files",
        r"waiting for (?:your )?approval",
    ]
]

ENGINE_DEFAULTS: dict[str, list[str]] = {
    "claude": ["claude"],
    "codex": ["codex"],
    "shell": ["cmd"] if IS_WINDOWS else [os.environ.get("SHELL", "/bin/bash")],
}


class PtyHandle:
    """Adapter PTY lintas platform: ptyprocess (Linux/mac) & pywinpty (Windows)."""

    def __init__(self, argv: list[str], cwd: str, cols: int, rows: int):
        env = dict(os.environ)
        env.setdefault("LANG", "C.UTF-8")
        env["TERM"] = "xterm-256color"
        env["COLORTERM"] = "truecolor"

        if IS_WINDOWS:
            from winpty import PtyProcess as WinPtyProcess

            cmdline = subprocess.list2cmdline(argv)
            self._proc = WinPtyProcess.spawn(cmdline, cwd=cwd, dimensions=(rows, cols), env=env)
        else:
            from ptyprocess import PtyProcessUnicode

            self._proc = PtyProcessUnicode.spawn(argv, cwd=cwd, dimensions=(rows, cols), env=env)

    def read(self) -> str:
        """Blocking read; return "" saat EOF/proses mati."""
        try:
            return self._proc.read(4096) or ""
        except (EOFError, OSError, ConnectionError):
            return ""

    def write(self, data: str):
        self._proc.write(data)

    def resize(self, cols: int, rows: int):
        self._proc.setwinsize(rows, cols)

    def isalive(self) -> bool:
        try:
            return self._proc.isalive()
        except Exception:
            return False

    def terminate(self, force: bool = False):
        try:
            self._proc.terminate(force=force)
        except Exception:
            pass

    @property
    def exitstatus(self) -> int | None:
        return getattr(self._proc, "exitstatus", None)


class Session:
    def __init__(self, engine: str, project_path: str, argv: list[str], cols: int = 80, rows: int = 24):
        self.id = uuid.uuid4().hex[:8]
        self.engine = engine
        self.project_path = project_path
        self.project_name = Path(project_path).name
        self.argv = argv
        self.cols = cols
        self.rows = rows
        self.status = "starting"  # starting | running | exited
        self.exit_code: int | None = None
        self.created_at = time.time()
        self.last_output_at = 0.0

        self.on_approval: Callable[["Session", str], Awaitable[None]] | None = None
        self.on_exit: Callable[["Session"], Awaitable[None]] | None = None

        self._handle: PtyHandle | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subscribers: list[FrameCallback] = []
        self._buffer: list[str] = []
        self._buffer_len = 0
        self._buffer_start_seq = 0
        self._seq = 0
        self._idle_timer: asyncio.TimerHandle | None = None
        self._last_prompt_hash: str | None = None

    @property
    def seq(self) -> int:
        return self._seq

    def info(self) -> dict:
        return {
            "id": self.id,
            "engine": self.engine,
            "project": self.project_name,
            "path": self.project_path,
            "status": self.status,
            "exit_code": self.exit_code,
            "created_at": self.created_at,
            "seq": self._seq,
        }

    async def start(self):
        self._loop = asyncio.get_running_loop()
        self._handle = await asyncio.to_thread(PtyHandle, self.argv, self.project_path, self.cols, self.rows)
        self.status = "running"
        threading.Thread(target=self._read_loop, name=f"pty-{self.id}", daemon=True).start()

    def _read_loop(self):
        assert self._handle and self._loop
        while True:
            data = self._handle.read()
            if not data:
                if self._handle.isalive():
                    time.sleep(0.05)
                    continue
                break
            self._loop.call_soon_threadsafe(self._on_output, data)
        self._loop.call_soon_threadsafe(self._on_eof)

    def _on_output(self, data: str):
        self._seq += len(data)
        self._buffer.append(data)
        self._buffer_len += len(data)
        self._trim_buffer()
        self.last_output_at = time.time()
        self._broadcast({"t": "o", "d": data, "s": self._seq})

        if self._idle_timer:
            self._idle_timer.cancel()
        if self.status == "running":
            self._idle_timer = self._loop.call_later(APPROVAL_IDLE_SECONDS, self._check_approval)

    def _trim_buffer(self):
        while self._buffer_len > MAX_BUFFER_CHARS and self._buffer:
            dropped = self._buffer.pop(0)
            self._buffer_len -= len(dropped)
            self._buffer_start_seq += len(dropped)

    def _on_eof(self):
        if self._idle_timer:
            self._idle_timer.cancel()
            self._idle_timer = None
        self.status = "exited"
        self.exit_code = self._handle.exitstatus if self._handle else None
        self._broadcast({"t": "exit", "code": self.exit_code})
        if self.on_exit:
            asyncio.ensure_future(self.on_exit(self))

    def _check_approval(self):
        tail_clean = ANSI_RE.sub("", self.tail_output(2000))
        if not any(pattern.search(tail_clean) for pattern in APPROVAL_PATTERNS):
            return

        lines = [line.rstrip() for line in tail_clean.splitlines() if line.strip()]
        excerpt = "\n".join(lines[-12:])[-700:]
        prompt_hash = hashlib.md5(excerpt.encode()).hexdigest()
        if prompt_hash == self._last_prompt_hash:
            return
        self._last_prompt_hash = prompt_hash

        if self.on_approval:
            asyncio.ensure_future(self.on_approval(self, excerpt))

    def tail_output(self, chars: int) -> str:
        return "".join(self._buffer)[-chars:]

    def output_since(self, seq: int) -> str:
        joined = "".join(self._buffer)
        offset = seq - self._buffer_start_seq
        if offset <= 0:
            return joined
        if offset >= len(joined):
            return ""
        return joined[offset:]

    def write(self, data: str) -> bool:
        if not self._handle or self.status != "running":
            return False
        self._handle.write(data)
        return True

    def write_key(self, key: str) -> bool:
        sequence = NAMED_KEYS.get(key)
        if sequence is None:
            return False
        return self.write(sequence)

    def resize(self, cols: int, rows: int):
        self.cols, self.rows = cols, rows
        if self._handle and self.status == "running":
            try:
                self._handle.resize(cols, rows)
            except Exception:
                pass

    async def kill(self):
        if not self._handle:
            return
        self._handle.terminate(force=False)
        for _ in range(20):
            if not self._handle.isalive():
                return
            await asyncio.sleep(0.1)
        self._handle.terminate(force=True)

    def subscribe(self, callback: FrameCallback):
        self._subscribers.append(callback)

    def unsubscribe(self, callback: FrameCallback):
        if callback in self._subscribers:
            self._subscribers.remove(callback)

    def _broadcast(self, frame: dict):
        for callback in list(self._subscribers):
            asyncio.ensure_future(self._safe_send(callback, frame))

    async def _safe_send(self, callback: FrameCallback, frame: dict):
        try:
            await callback(frame)
        except Exception:
            self.unsubscribe(callback)


class SessionManager:
    def __init__(self):
        self.sessions: dict[str, Session] = {}
        self.on_approval: Callable[[Session, str], Awaitable[None]] | None = None
        self.on_exit: Callable[[Session], Awaitable[None]] | None = None

    async def create(self, engine: str, project_path: str, cols: int = 80, rows: int = 24) -> Session:
        argv = resolve_engine_argv(engine)
        session = Session(engine, project_path, argv, cols, rows)
        session.on_approval = self.on_approval
        session.on_exit = self.on_exit
        await session.start()
        self.sessions[session.id] = session
        return session

    def get(self, session_id: str) -> Session | None:
        return self.sessions.get(session_id)

    def list_info(self) -> list[dict]:
        return [s.info() for s in sorted(self.sessions.values(), key=lambda s: s.created_at)]

    async def kill(self, session_id: str) -> bool:
        session = self.sessions.get(session_id)
        if not session:
            return False
        await session.kill()
        return True

    def remove(self, session_id: str):
        self.sessions.pop(session_id, None)

    async def shutdown(self):
        await asyncio.gather(*(s.kill() for s in self.sessions.values()), return_exceptions=True)


def resolve_engine_argv(engine: str) -> list[str]:
    override = os.getenv(f"ENGINE_{engine.upper()}_CMD", "").strip()
    if override:
        argv = shlex.split(override)
    else:
        default = ENGINE_DEFAULTS.get(engine)
        if not default:
            raise ValueError(f"Engine tidak dikenal: {engine}")
        argv = list(default)

    executable = which_engine(argv[0])
    if not executable:
        raise FileNotFoundError(
            f"Command '{argv[0]}' untuk engine '{engine}' tidak ditemukan di PATH. "
            f"Set ENGINE_{engine.upper()}_CMD di .env jika lokasinya custom."
        )
    return [executable, *argv[1:]]


def which_engine(name: str) -> str | None:
    if IS_WINDOWS:
        return shutil.which(f"{name}.cmd") or shutil.which(f"{name}.exe") or shutil.which(name)
    return shutil.which(name)


def engines_available() -> dict[str, dict]:
    result = {}
    for engine in ENGINE_DEFAULTS:
        try:
            argv = resolve_engine_argv(engine)
            result[engine] = {"available": True, "command": argv[0]}
        except (FileNotFoundError, ValueError) as exc:
            result[engine] = {"available": False, "error": str(exc)}
    return result
