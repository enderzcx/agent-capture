"""Persisted ownership state.

``start`` and ``stop`` normally run as separate CLI invocations, so the proof
that a running record output belongs to *this* task has to survive the process.
The store is deliberately paranoid:

* ``run_id`` is restricted to a safe basename (no separators, no ``.``/``..``).
* the file is created with ``O_EXCL`` so a concurrent ``start`` cannot clobber it.
* the file is created with ``O_NOFOLLOW`` and mode ``0600``.
* writes are atomic (temp file in the same directory, then ``os.replace``).
* a file whose contents do not match the requested ``run_id``, or that is not
  valid JSON, is an error -- never silently treated as "no state".
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from typing import Any, Dict, Optional

from .errors import CONFIG_INVALID, DEPENDENCY_MISSING, INTERNAL, OBS_BUSY, STALE_STATE, CaptureError

RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
SCHEMA_VERSION = 1


def validate_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not RUN_ID_PATTERN.match(run_id):
        raise CaptureError(
            CONFIG_INVALID,
            "run_id must match %s" % (RUN_ID_PATTERN.pattern,),
            {"run_id": run_id if isinstance(run_id, str) else type(run_id).__name__},
        )
    if run_id in (".", ".."):
        raise CaptureError(CONFIG_INVALID, "run_id must not be '.' or '..'", {"run_id": run_id})
    return run_id


def default_state_dir() -> str:
    return os.path.join(tempfile.gettempdir(), "agent-capture-win")


class OwnershipStore:
    """Reads/writes one ownership record per ``run_id``."""

    def __init__(self, state_dir: Optional[str] = None) -> None:
        self.state_dir = state_dir or default_state_dir()

    def path_for(self, run_id: str) -> str:
        return os.path.join(self.state_dir, "%s.json" % (validate_run_id(run_id),))

    def exists(self, run_id: str) -> bool:
        return os.path.exists(self.path_for(run_id))

    def create(self, run_id: str, record: Dict[str, Any]) -> str:
        """Create the record, failing if one already exists (no-clobber)."""
        validate_run_id(run_id)
        os.makedirs(self.state_dir, exist_ok=True)
        target = self.path_for(run_id)
        payload = dict(record)
        payload["run_id"] = run_id
        payload["schema_version"] = SCHEMA_VERSION

        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(target, flags, 0o600)
        except FileExistsError as exc:
            raise CaptureError(
                STALE_STATE,
                "a state file for run_id %r already exists; refusing to clobber it" % (run_id,),
                {"run_id": run_id, "path": target},
            ) from exc
        except OSError as exc:
            raise CaptureError(
                INTERNAL, "could not create the state file", {"path": target, "errno": exc.errno}
            ) from exc
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
        except Exception:
            try:
                os.unlink(target)
            except OSError:
                pass
            raise
        return target

    def write(self, run_id: str, record: Dict[str, Any]) -> str:
        """Atomically replace an existing record."""
        validate_run_id(run_id)
        os.makedirs(self.state_dir, exist_ok=True)
        target = self.path_for(run_id)
        payload = dict(record)
        payload["run_id"] = run_id
        payload["schema_version"] = SCHEMA_VERSION
        tmp = "%s.tmp.%d" % (target, os.getpid())
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(tmp, flags, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            os.replace(tmp, target)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return target

    def read(self, run_id: str) -> Dict[str, Any]:
        validate_run_id(run_id)
        target = self.path_for(run_id)
        if not os.path.exists(target):
            raise CaptureError(
                STALE_STATE,
                "no state file for run_id %r; this task did not start a recording under that id"
                % (run_id,),
                {"run_id": run_id, "path": target},
            )
        if os.path.islink(target):
            raise CaptureError(STALE_STATE, "the state file is a symlink; refusing to read it", {"path": target})
        try:
            with open(target, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise CaptureError(
                STALE_STATE,
                "the state file is unreadable or corrupt; refusing to guess",
                {"path": target, "error": str(exc)[:200]},
            ) from exc
        if not isinstance(payload, dict):
            raise CaptureError(STALE_STATE, "the state file is not an object", {"path": target})
        if payload.get("run_id") != run_id:
            raise CaptureError(
                STALE_STATE,
                "the state file belongs to a different run_id",
                {"expected": run_id, "found": payload.get("run_id")},
            )
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise CaptureError(
                STALE_STATE,
                "the state file has an unsupported schema_version",
                {"expected": SCHEMA_VERSION, "found": payload.get("schema_version")},
            )
        return payload

    def delete(self, run_id: str) -> None:
        target = self.path_for(run_id)
        try:
            os.unlink(target)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise CaptureError(INTERNAL, "could not remove the state file", {"path": target, "errno": exc.errno}) from exc

class InstanceLock:
    """Serialises mutating commands against one OBS instance.

    Uses an OS advisory lock (``fcntl.flock`` on POSIX, ``msvcrt.locking`` on
    Windows) on a lock file that is **never deleted** and whose contents are
    never interpreted.  Deliberately not a PID file: a PID-based scheme either
    has to guess whether a stale PID is dead (and can delete a live lock), or
    blocks forever after a crash.  An advisory lock is released by the kernel
    when the process dies, so a crash self-heals without anyone deleting
    anything, and there is no unlink race between two waiters.

    Only ``start`` and ``stop`` take it; read-only commands never do.
    """

    def __init__(self, state_dir: Optional[str] = None, name: str = "obs-instance.lock") -> None:
        self.state_dir = state_dir or default_state_dir()
        self.path = os.path.join(self.state_dir, name)
        self._fd: Optional[int] = None

    def _lock_backend(self):
        try:
            import fcntl  # type: ignore

            return ("fcntl", fcntl)
        except ImportError:
            pass
        try:
            import msvcrt  # type: ignore

            return ("msvcrt", msvcrt)
        except ImportError:
            pass
        return (None, None)

    def acquire(self, timeout: float = 10.0, poll: float = 0.1) -> "InstanceLock":
        backend, module = self._lock_backend()
        if backend is None:
            raise CaptureError(
                DEPENDENCY_MISSING,
                "no OS advisory locking primitive is available, so concurrent mutating commands "
                "cannot be serialised; refusing to run unserialised",
                {"state_dir": self.state_dir},
            )
        os.makedirs(self.state_dir, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(self.path, flags, 0o600)

        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                if backend == "fcntl":
                    module.flock(fd, module.LOCK_EX | module.LOCK_NB)
                else:  # msvcrt
                    module.locking(fd, module.LK_NBLCK, 1)
                self._fd = fd
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise CaptureError(
                        OBS_BUSY,
                        "another agent-capture command is already mutating this OBS instance",
                        {"lock_path": self.path, "timeout_s": timeout},
                    ) from None
                time.sleep(poll)

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        backend, module = self._lock_backend()
        try:
            if backend == "fcntl":
                module.flock(fd, module.LOCK_UN)
            elif backend == "msvcrt":
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    module.locking(fd, module.LK_UNLCK, 1)
                except OSError:
                    pass
        except OSError:
            pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    def __enter__(self) -> "InstanceLock":
        return self.acquire()

    def __exit__(self, *exc_info: Any) -> None:
        self.release()
