import json
import os
import platform
import secrets
import socket
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from link_protocol import KEY_BYTES, clean_name

KEY_FILE = "computer.key"
PHONES_FILE = "phones.json"
DAEMON_FILE = "daemon.json"
LOCK_FILE = "daemon.lock"
LOG_FILE = "daemon.log"
OUT_FILE = "daemon.out"
REPLACE_ATTEMPTS = 20
REPLACE_PAUSE_SECONDS = 0.05


def default_home() -> Path:
    override = os.environ.get("EVOCA_LINK_HOME")
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Evoca Link"
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))) / "Evoca Link"
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "evoca-link"


def computer_name() -> str:
    if sys.platform == "darwin":
        try:
            import subprocess
            named = subprocess.run(["scutil", "--get", "ComputerName"], capture_output=True, text=True, timeout=2).stdout.strip()
            if named:
                return clean_name(named)
        except (OSError, subprocess.SubprocessError):
            pass
    return clean_name(socket.gethostname().split(".")[0] or "Ordenador")


def os_name() -> str:
    return {"darwin": "macos", "win32": "windows"}.get(sys.platform, "linux" if sys.platform.startswith("linux") else "other")


def os_label() -> str:
    return {"macos": "macOS", "windows": "Windows", "linux": "Linux"}.get(os_name(), platform.system())


def write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
    try:
        os.write(descriptor, data)
    finally:
        os.close(descriptor)
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            # Windows refuses to replace a file another process has open for reading (the MCP server reading phones.json), so wait for it to close.
            if os.name != "nt" or attempt == REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(REPLACE_PAUSE_SECONDS)


@dataclass
class PairedPhone:
    id: str
    name: str
    paired_at: float
    last_seen: float | None = None
    app: str | None = None


@dataclass
class DaemonInfo:
    port: int
    token: str
    pid: int
    started_at: float


@dataclass
class LinkState:
    home: Path = field(default_factory=default_home)

    def ensure(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(self.home, 0o700)

    def secret_key(self) -> bytes:
        path = self.home / KEY_FILE
        if path.exists():
            data = path.read_bytes()
            if len(data) == KEY_BYTES:
                return data
            raise RuntimeError(f"{path} is damaged; remove it to create a new identity (paired iPhones will need to pair again)")
        key = secrets.token_bytes(KEY_BYTES)
        self.ensure()
        write_private(path, key)
        return key

    def phones(self) -> list[PairedPhone]:
        path = self.home / PHONES_FILE
        if not path.exists():
            return []
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise RuntimeError(f"{path} could not be read: {error}") from error
        return [PairedPhone(**row) for row in rows if isinstance(row, dict) and "id" in row]

    def save_phones(self, phones: list[PairedPhone]) -> None:
        self.ensure()
        write_private(self.home / PHONES_FILE, json.dumps([asdict(p) for p in phones], ensure_ascii=False, indent=2).encode("utf-8"))

    def phone(self, phone_id: str) -> PairedPhone | None:
        return next((p for p in self.phones() if p.id == phone_id), None)

    def add_phone(self, phone_id: str, name: str, app: str | None) -> PairedPhone:
        phones = [p for p in self.phones() if p.id != phone_id]
        added = PairedPhone(id=phone_id, name=clean_name(name) or "iPhone", paired_at=time.time(), app=app)
        self.save_phones(phones + [added])
        return added

    def touch_phone(self, phone_id: str, app: str | None) -> None:
        phones = self.phones()
        for phone in phones:
            if phone.id == phone_id:
                phone.last_seen = time.time()
                phone.app = app or phone.app
        self.save_phones(phones)

    def remove_phone(self, phone_id: str) -> bool:
        phones = self.phones()
        kept = [p for p in phones if p.id != phone_id]
        if len(kept) == len(phones):
            return False
        self.save_phones(kept)
        return True

    def find_phone(self, wanted: str) -> PairedPhone | None:
        folded = wanted.strip().lower()
        matches = [p for p in self.phones() if p.id.startswith(folded) or p.name.lower() == folded]
        return matches[0] if len(matches) == 1 else None

    def daemon_info(self) -> DaemonInfo | None:
        path = self.home / DAEMON_FILE
        try:
            return DaemonInfo(**json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            return None

    def write_daemon_info(self, info: DaemonInfo) -> None:
        self.ensure()
        write_private(self.home / DAEMON_FILE, json.dumps(asdict(info)).encode("utf-8"))

    def clear_daemon_info(self, pid: int) -> None:
        info = self.daemon_info()
        if info and info.pid == pid:
            try:
                (self.home / DAEMON_FILE).unlink()
            except OSError:
                pass

    @property
    def log_path(self) -> Path:
        return self.home / LOG_FILE

    @property
    def out_path(self) -> Path:
        return self.home / OUT_FILE

    @property
    def lock_path(self) -> Path:
        return self.home / LOCK_FILE


class SingleInstance:
    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self.handle.close()
            self.handle = None
            return False
