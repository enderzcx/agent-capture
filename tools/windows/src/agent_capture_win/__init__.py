"""agent_capture_win -- a Windows window-recording backend over obs-websocket 5.x.

Isolated implementation module.  It is not part of the main ``agent-capture``
repository; the main writer integrates it.

Public surface::

    from agent_capture_win import run
    result = run("preflight", {"profile": "agent-capture", ...})

Honest boundaries (enforced in code, see ``policy.py``):

* OBS ``window_capture`` matches windows by ``title`` / ``class`` / ``exe`` with
  a match priority.  There is **no HWND or PID input**, so no HWND pinning is
  claimed and a same-title window can take over a match.
* Audio from ``window_capture`` is application/process granularity, not
  exclusive to one window.
* Sample-level audio presence is not observable over obs-websocket; the backend
  reports ``settings_confirm_capture_enabled`` and never claims "there is sound".
"""

from __future__ import annotations

__version__ = "0.1.0"

from .api import COMMANDS, run
from .backend import Config, ObsBackend
from .errors import CaptureError

__all__ = ["run", "COMMANDS", "Config", "ObsBackend", "CaptureError", "__version__"]
