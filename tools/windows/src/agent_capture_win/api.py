"""Public entry point: ``run(command, config) -> dict``.

Kept in its own module so ``agent_capture_win`` (the package) stays importable
without pulling in argparse or the CLI.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from .backend import LEVEL_MOCK, ObsBackend, Config
from .errors import CONFIG_INVALID, CaptureError, error_dict
from .transport import Transport

COMMANDS = ("capabilities", "targets", "preflight", "start", "status", "stop")


def run(
    command: str,
    config: Optional[Dict[str, Any]] = None,
    *,
    transport: Optional[Transport] = None,
    transport_factory: Optional[Callable[[Config], Transport]] = None,
    verification_level: Optional[str] = None,
    file_probe: Optional[Callable[[str], Dict[str, Any]]] = None,
    dir_probe: Optional[Callable[[str], Any]] = None,
    clock: Optional[Callable[[], float]] = None,
    sleep: Optional[Callable[[float], None]] = None,
) -> Dict[str, Any]:
    """Execute one backend command and always return a JSON-shaped object.

    ``transport`` / ``file_probe`` / ``dir_probe`` exist so tests can drive the
    full policy path without a Windows machine or a real OBS.
    """
    if command not in COMMANDS:
        return {
            "schema": "agent-capture-win/1",
            "backend": "obs-websocket",
            "command": command,
            "ok": False,
            "verification_level": verification_level or "none",
            "error": error_dict(CONFIG_INVALID, "unknown command %r" % (command,), {"valid": list(COMMANDS)}),
        }

    try:
        parsed = config if isinstance(config, Config) else Config.from_dict(config)
    except CaptureError as exc:
        return {
            "schema": "agent-capture-win/1",
            "backend": "obs-websocket",
            "command": command,
            "ok": False,
            "verification_level": verification_level or "none",
            "error": exc.to_dict(),
        }

    kwargs: Dict[str, Any] = {}
    if transport is not None:
        kwargs["transport"] = transport
    if transport_factory is not None:
        kwargs["transport_factory"] = transport_factory
    if verification_level is not None:
        kwargs["verification_level"] = verification_level
    elif transport is not None:
        kwargs["verification_level"] = LEVEL_MOCK
    if file_probe is not None:
        kwargs["file_probe"] = file_probe
    if dir_probe is not None:
        kwargs["dir_probe"] = dir_probe
    if clock is not None:
        kwargs["clock"] = clock
    if sleep is not None:
        kwargs["sleep"] = sleep

    backend = ObsBackend(parsed, **kwargs)
    handler = getattr(backend, command)
    try:
        result = handler()
    except CaptureError as exc:  # defensive: handlers already convert
        envelope = backend._envelope(command)
        result = backend._fail(envelope, exc)
    # The instance is the single source of truth for how far verification got;
    # the envelope is stamped from it last, so a value captured when the envelope
    # was first built can never be reported as the outcome.
    if isinstance(result, dict):
        result["verification_level"] = backend.verification_level
        result["verification"] = backend.verification
    return result
