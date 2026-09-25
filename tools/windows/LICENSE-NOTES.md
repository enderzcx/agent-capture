# Licensing notes for integrators

Two things this repository deliberately does **not** contain, and what to do
instead.

## 1. OBS Project source code is not vendored

The implementation was written by reading the official OBS sources, but none of
that C code is copied or redistributed here. OBS Studio and obs-websocket are
licensed under the **GNU General Public License v2.0** (OBS Studio; with some
parts under other compatible licenses). Vendoring their source into a
permissively licensed repository would be a licensing problem, so this
repository only *links* to them:

| Referenced source | Why | Licence |
|---|---|---|
| [`obsproject/obs-websocket` `docs/generated/protocol.md`](https://github.com/obsproject/obs-websocket/blob/master/docs/generated/protocol.md) | request/response fields, auth algorithm, `RequestStatus` and `WebSocketCloseCode` enums | GPL-2.0 |
| [`obsproject/obs-studio` `plugins/win-capture/window-capture.c`](https://github.com/obsproject/obs-studio/blob/master/plugins/win-capture/window-capture.c) | the `window_capture` setting keys and their defaults | GPL-2.0 |
| [`obsproject/obs-studio` `libobs/util/windows/window-helpers.c`](https://github.com/obsproject/obs-studio/blob/master/libobs/util/windows/window-helpers.c) and `.h` | the `title:class:exe` window string, the `window_priority` enum values, the `ms_find_window` scoring loop | GPL-2.0 |

Facts taken from those sources (a setting key name, an enum's numeric values, a
format string) are used as facts; no code was copied. If you re-derive anything
from them, cite the upstream file and keep the GPL in mind.

## 2. Runtime dependencies

| Package | Used for | Licence | Notes |
|---|---|---|---|
| [`websocket-client`](https://github.com/websocket-client/websocket-client) | RFC6455 framing for the real transport (`websocket-client>=1.6`) | **Apache-2.0** | Only runtime dependency. The obs-websocket JSON protocol on top of it is this project's own code. |
| [`websockets`](https://github.com/python-websockets/websockets) | test-only: a real WebSocket **server** so the real transport is exercised over loopback | **BSD-3-Clause** | `[test]` extra only; never imported at runtime. |
| [`pytest`](https://github.com/pytest-dev/pytest) | test runner | **MIT** | `[test]` extra only. |

Verified against the installed distributions' metadata
(`websocket-client 1.9.2`, `websockets 17.1`, `pytest 9.1.1`). Re-check before
publishing, and add their notices to whatever your distribution requires.

## 3. What must not be published

The following stay in the private working directory and must not be copied into a
public repository:

- the handoff/evidence documents (they contain a private host name, a private
  network address and developer-absolute paths);
- the downloaded official sources used as references;
- any recording, log or screenshot produced while testing against a real machine.

The `public/` directory is the exact set of files that is safe to publish; it
contains nothing else, and nothing outside it should be copied.
