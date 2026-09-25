# agent-capture-win

A Windows **window-recording backend** driven over [obs-websocket 5.x](https://github.com/obsproject/obs-websocket).

It records one explicitly chosen window plus that application's own audio by
driving a dedicated OBS profile / scene collection / scene / source. It is a
library plus a small CLI; it owns no UI and no daemon.

```bash
pip install .            # runtime dependency: websocket-client
pip install .[test]      # plus pytest + websockets for the test suite
```

## What it will not do

These are refusals in the code, not documentation promises:

- **No whole-screen fallback.** If the requested window cannot be matched, the
  call fails; it never degrades to a display capture.
- **No global desktop audio, no microphone.** If OBS still has a global Desktop
  Audio or Mic input configured, the call is refused -- whether or not that
  input appears in the scene's source list (it usually does not).
- **No automatic re-matching.** Only the explicit `title` / `class` / `exe`
  selector is used, with an explicit match priority.
- **No taking over an OBS in use.** Streaming, recording, the replay buffer or
  the virtual camera being active is a refusal.
- **No "success" without evidence.** A successful `StartRecord` is not proof
  that anything was captured; see *Verification layers* below.
- **No HWND pinning.** OBS's `window_capture` matches by window title, class and
  executable and re-enumerates on every re-hook. There is no HWND or PID input
  anywhere in its settings, so a window with the same title/class/exe can take
  over a match. That is stated in the output rather than papered over.
- **Audio is application/process granularity**, never exclusive to one window.

## Usage

```python
from agent_capture_win import run

result = run("preflight", {
    "host": "127.0.0.1", "port": 4455,
    "password_env": "AGENT_CAPTURE_OBS_PASSWORD",
    "profile": "agent-capture",
    "scene_collection": "agent-capture",
    "scene": "agent-capture-window",
    "target": {"title": "Example Game", "class": "UnityWndClass",
               "exe": "ExampleGame.exe", "priority": "exe"},
    "record_dir": "D:\\captures",
    "run_id": "take-001",
})
```

Commands: `capabilities`, `targets`, `preflight`, `start`, `status`, `stop`.
Every command returns one JSON-shaped dict carrying `backend`, `target`,
`actual_source`, `recording`, `output`, `error.category` and the verification
block. All parameters come from that one config object; nothing is read from
machine-wide configuration.

```bash
python -m agent_capture_win preflight --config cfg.json
echo '{}' | python -m agent_capture_win capabilities --config -
```

Exit codes: `0` ok, `2` refused/failed (the JSON explains why), `3` usage error.

## Verification layers

A single "verified" flag invites reading "bytes were written" as "the recording
is good", so the layers are reported separately and never imply one another:

| Layer | What it can say |
|---|---|
| `protocol` | `mock` (scripted/local server) or `live_connected` (a real obs-websocket) |
| `artifact` | `file_verified`: the output exists, is non-empty, is inside the dedicated directory and did not overwrite a pre-existing file |
| `picture` | always `unverified` -- nothing here proves *which* window was captured, nor that the picture moves |
| `audio` | `settings_only`: the source is configured to capture its own application audio. obs-websocket exposes no audio samples, so "there is sound" is not claimed |

`verification_level` only reaches `live_output_file_verified` after a real
recording was stopped *and* its file was checked on disk. Rising `outputBytes`
alone never promotes it.

## Testing

```bash
python -m pytest tests -q
```

The suite runs anywhere -- no Windows machine and no OBS required. It covers
three levels: pure policy, scripted transport responses, and a real WebSocket
server (the `websockets` library) speaking the obs-websocket protocol, which the
real transport is pointed at over loopback.

**No Windows machine was available while this was written, so no real capture
was performed.** The tests prove protocol handling and refusal logic; they do
not prove that a window or its audio was ever recorded.

## Official references

This module was written against the official sources, not third-party write-ups:

- [obs-websocket protocol](https://github.com/obsproject/obs-websocket/blob/master/docs/generated/protocol.md)
- [`plugins/win-capture/window-capture.c`](https://github.com/obsproject/obs-studio/blob/master/plugins/win-capture/window-capture.c) -- the real setting keys and defaults
- [`libobs/util/windows/window-helpers.c`](https://github.com/obsproject/obs-studio/blob/master/libobs/util/windows/window-helpers.c) -- the `title:class:exe` window string, the match-priority enum and the scoring loop

Their source is **not** vendored here; see [`LICENSE-NOTES.md`](LICENSE-NOTES.md).
