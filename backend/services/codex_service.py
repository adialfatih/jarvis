import asyncio
import contextlib
import os
import shlex
import shutil
from collections.abc import Awaitable, Callable


OutputCallback = Callable[[str], Awaitable[None]]


class CodexService:
    def __init__(self):
        self.process: asyncio.subprocess.Process | None = None
        self.output_callbacks: list[OutputCallback] = []
        self.is_running = False
        self.current_project: str | None = None
        self.last_error: str | None = None
        self.is_busy = False
        self._read_task: asyncio.Task | None = None
        self._restart_requested = False
        self._lock = asyncio.Lock()
        self._job_lock = asyncio.Lock()

    async def start(self, working_dir: str):
        async with self._lock:
            await self._start_unlocked(working_dir)

    async def _start_unlocked(self, working_dir: str):
        self.current_project = working_dir
        self.last_error = None
        self.is_running = True

        if not shutil.which("codex.cmd") and not shutil.which("codex.exe") and not shutil.which("codex"):
            self.is_running = False
            self.last_error = "Codex CLI tidak ditemukan. Jalankan setup.bat atau install @openai/codex."
            await self._broadcast(f"[JARVIS] {self.last_error}\n")
            return

        await self._broadcast(f"[JARVIS] Codex exec ready in: {working_dir}\n")

    async def stop(self):
        async with self._lock:
            await self._stop_unlocked()

    async def _stop_unlocked(self):
        self._restart_requested = True
        self.is_running = False

        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=3)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()

        if self._read_task and self._read_task is not asyncio.current_task():
            self._read_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._read_task

        self.process = None
        self._read_task = None
        self.is_busy = False
        self._restart_requested = False

    async def switch_project(self, new_working_dir: str):
        async with self._lock:
            await self._broadcast(f"[JARVIS] Switching to project: {new_working_dir}\n")
            await self._stop_unlocked()
            await asyncio.sleep(1)
            await self._start_unlocked(new_working_dir)

    async def restart(self):
        if not self.current_project:
            await self._broadcast("[JARVIS] Tidak ada project aktif untuk restart Codex.\n")
            return
        await self._broadcast(f"[JARVIS] Codex exec ready in: {self.current_project}\n")

    async def _read_output(self):
        process = self.process
        if not process or not process.stdout:
            return

        try:
            while True:
                line = await process.stdout.readline()
                if line:
                    await self._broadcast(line.decode("utf-8", errors="replace"))
                    continue
                break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error = str(exc)
            await self._broadcast(f"[JARVIS] Error membaca output Codex: {exc}\n")
        finally:
            ended_project = self.current_project
            self.is_running = False

            if not self._restart_requested:
                await self._broadcast("[JARVIS] Codex process ended. Auto-restarting...\n")
                if ended_project:
                    await asyncio.sleep(2)
                    async with self._lock:
                        if not self.is_running and self.current_project == ended_project:
                            await self._start_unlocked(ended_project)

    async def _broadcast(self, text: str):
        dead_callbacks: list[OutputCallback] = []
        for callback in list(self.output_callbacks):
            try:
                await callback(text)
            except Exception:
                dead_callbacks.append(callback)

        for callback in dead_callbacks:
            self.remove_output_callback(callback)

    async def send_input(self, text: str) -> bool:
        if not text.strip():
            return False

        if not self.current_project or not self.is_running:
            return False

        if self._job_lock.locked():
            await self._broadcast("[JARVIS] Codex masih memproses perintah sebelumnya.\n")
            return False

        async with self._job_lock:
            self.is_busy = True
            await self._broadcast(f"\n[JARVIS] Running Codex exec in: {self.current_project}\n")
            await self._broadcast(f"[JARVIS] Prompt: {text.rstrip()}\n\n")

            command = resolve_codex_command() + resolve_codex_exec_args() + [text.rstrip()]

            try:
                self.process = await asyncio.create_subprocess_exec(
                    *command,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=self.current_project,
                )

                if self.process.stdout:
                    while True:
                        line = await self.process.stdout.readline()
                        if not line:
                            break
                        await self._broadcast(line.decode("utf-8", errors="replace"))

                return_code = await self.process.wait()
                await self._broadcast(f"\n[JARVIS] Codex exec selesai. Exit code: {return_code}\n")
                self.last_error = None if return_code == 0 else f"Codex exec exit code {return_code}"
                return return_code == 0
            except FileNotFoundError:
                self.last_error = "Codex CLI tidak ditemukan."
                await self._broadcast(f"[JARVIS] {self.last_error}\n")
                return False
            except Exception as exc:
                self.last_error = str(exc)
                await self._broadcast(f"[JARVIS] Gagal menjalankan Codex exec: {exc}\n")
                return False
            finally:
                self.process = None
                self.is_busy = False

        return False

    def add_output_callback(self, callback: OutputCallback):
        self.output_callbacks.append(callback)

    def remove_output_callback(self, callback: OutputCallback):
        if callback in self.output_callbacks:
            self.output_callbacks.remove(callback)


def resolve_codex_command() -> list[str]:
    configured = os.getenv("CODEX_COMMAND")
    if configured:
        return shlex.split(configured)

    executable = shutil.which("codex.cmd") or shutil.which("codex.exe") or shutil.which("codex")
    if not executable:
        return ["codex"]

    return [executable]


def resolve_codex_exec_args() -> list[str]:
    configured = os.getenv("CODEX_EXEC_ARGS")
    if configured:
        return shlex.split(configured)
    return ["exec", "--skip-git-repo-check", "--color", "always"]
