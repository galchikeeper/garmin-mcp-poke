"""Private, atomic state shared by processes on one persistent filesystem."""
import fcntl
import json
import math
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path


class StorageError(RuntimeError):
    pass


def private_directory(path):
    path = Path(path).expanduser().absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise StorageError("State directory must not contain symlinks")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def atomic_json(path, value):
    """Never expose partially written state or group/world-readable tokens."""
    path = Path(path)
    if path.is_symlink():
        raise StorageError("State files must not be symlinks")
    fd, temporary = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, encoding="utf-8") as stream:
        return json.load(stream)


class StateStore:
    def __init__(self, directory=None):
        configured = directory or os.getenv("GARMIN_STATE_DIR")
        if os.getenv("RENDER") and not configured:
            raise StorageError("Render requires GARMIN_STATE_DIR on a persistent disk")
        self.directory = private_directory(configured or "~/.garmin-mcp")
        if os.getenv("RENDER") and not any(
            os.path.ismount(p) for p in (self.directory, *self.directory.parents)
            if p != Path(p.anchor)
        ):
            raise StorageError("GARMIN_STATE_DIR is not on a mounted persistent disk")
        self.tokens = self.directory / "garmin_tokens.json"
        self.guard = self.directory / "request_guard.json"
        self.lock_path = self.directory / ".request.lock"

    @contextmanager
    def locked(self):
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def read_guard(self):
        try:
            data = read_json(self.guard)
        except FileNotFoundError:
            return {"blocked_until": 0.0, "rate_limit_count": 0, "auth_required": False}
        except (OSError, ValueError) as exc:
            raise StorageError("Cannot read request guard; requests stopped") from exc
        if not isinstance(data, dict):
            raise StorageError("Invalid request guard; requests stopped")
        deadline = data.get("blocked_until")
        count = data.get("rate_limit_count")
        if (isinstance(deadline, bool) or not isinstance(deadline, (float, int))
                or not math.isfinite(deadline) or deadline < 0
                or isinstance(count, bool) or not isinstance(count, int) or count < 0
                or not isinstance(data.get("auth_required"), bool)):
            raise StorageError("Invalid request guard; requests stopped")
        return data

    def save_guard(self, data):
        try:
            atomic_json(self.guard, data)
        except (OSError, ValueError) as exc:
            raise StorageError("Cannot persist request guard; requests stopped") from exc
