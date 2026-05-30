import os
import re
from pathlib import Path


PROJECT_ROOTS = [
    r"C:\xampp\htdocs",
    r"D:\_2026",
]


class ProjectService:
    def __init__(self):
        self.projects: list[dict] = []
        self.custom_paths: list[str] = []
        self.active_project: dict | None = None

    def scan(self) -> list[dict]:
        found: list[dict] = []
        seen: set[str] = set()

        for root in PROJECT_ROOTS:
            root_path = Path(root)
            if not root_path.exists():
                continue

            for child in root_path.iterdir():
                if child.is_dir():
                    project = make_project(child, root)
                    found.append(project)
                    seen.add(normalize_path(project["path"]))

        for custom_path in self.custom_paths:
            path = Path(custom_path)
            normalized = normalize_path(str(path))
            if path.is_dir() and normalized not in seen:
                found.append(make_project(path, "custom"))
                seen.add(normalized)

        self.projects = found

        if self.active_project:
            active = self.get_by_path(self.active_project["path"])
            self.active_project = active or self.active_project

        return self.projects

    def add_custom(self, path: str) -> dict | None:
        clean_path = str(Path(path).expanduser())
        if not Path(clean_path).is_dir():
            return None

        if normalize_path(clean_path) not in {normalize_path(p) for p in self.custom_paths}:
            self.custom_paths.append(clean_path)

        self.scan()
        return self.get_by_path(clean_path)

    def run_command(self, command: str) -> dict:
        match = re.fullmatch(r'\s*mkdir\s+(?:"([^"]+)"|(.+?))\s*', command, re.IGNORECASE)
        if not match:
            raise ValueError('Command belum didukung. Gunakan: mkdir "D:\\path\\folder-baru"')

        raw_path = match.group(1) or match.group(2)
        path = Path(raw_path.strip()).expanduser()
        if not path.name:
            raise ValueError("Nama folder wajib diisi")

        path.mkdir(parents=True, exist_ok=True)
        project = self.add_custom(str(path))
        if not project:
            raise ValueError("Folder gagal dibuat atau tidak dapat dibuka")

        return project

    def get_by_path(self, path: str) -> dict | None:
        normalized = normalize_path(path)
        return next((p for p in self.projects if normalize_path(p["path"]) == normalized), None)

    def set_active(self, path: str) -> dict | None:
        project = self.get_by_path(path)
        if project is None and Path(path).is_dir():
            project = self.add_custom(path)

        self.active_project = project
        return project

    def choose_default(self, configured_path: str | None = None) -> dict | None:
        self.scan()

        if configured_path:
            configured = self.get_by_path(configured_path)
            if configured:
                self.active_project = configured
                return configured

            if Path(configured_path).is_dir():
                self.active_project = make_project(Path(configured_path), "default")
                return self.active_project

        if self.projects:
            self.active_project = self.projects[0]
            return self.active_project

        return None


def make_project(path: Path, root: str) -> dict:
    return {
        "name": path.name,
        "path": str(path),
        "root": root,
        "type": detect_project_type(str(path)),
    }


def detect_project_type(path: str) -> str:
    if os.path.exists(os.path.join(path, "artisan")):
        return "Laravel"
    if os.path.exists(os.path.join(path, "package.json")):
        return "Node.js"
    if os.path.exists(os.path.join(path, "composer.json")):
        return "PHP/Composer"
    if os.path.exists(os.path.join(path, "index.php")):
        return "PHP"
    return "Unknown"


def normalize_path(path: str) -> str:
    return os.path.normcase(os.path.abspath(os.path.expanduser(path)))
