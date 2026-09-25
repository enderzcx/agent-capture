"""End-to-end protocol test over a real WebSocket connection.

``test_transport.py`` drives the real transport through a fake *socket*.
This file goes one step further: it stands up a real WebSocket server (the
``websockets`` library, a full RFC6455 implementation) that speaks the
obs-websocket 5 handshake and request protocol, and points the real
``ObsWebSocketTransport`` at it over loopback TCP.

That means the framing, the HTTP upgrade, the subprotocol negotiation, the
JSON opcodes, the authentication string and the request/response correlation
are all exercised for real -- only OBS itself is simulated.

It still proves nothing about capturing a window.  No Windows host and no OBS
were involved; the envelope level stays ``mock``.
"""

from __future__ import annotations

import json
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

websockets_sync_server = pytest.importorskip("websockets.sync.server")

from agent_capture_win.errors import AUTH_FAILED, BAD_RESPONSE, CaptureError, RPC_UNSUPPORTED  # noqa: E402
from agent_capture_win.fake import DEFAULT_RECORD_DIR, FakeObs  # noqa: E402
from agent_capture_win.transport import ObsRpcError, ObsWebSocketTransport, build_auth_string  # noqa: E402

SUBPROTOCOL = "obswebsocket.json"


class ObsServer:
    """A real WebSocket server that answers like obs-websocket 5.x."""

    def __init__(
        self,
        obs: FakeObs,
        *,
        password: str | None = None,
        corrupt_frame: bool = False,
        malformed_hello: bool = False,
    ) -> None:
        self.obs = obs
        self.password = password
        self.corrupt_frame = corrupt_frame
        self.malformed_hello = malformed_hello
        self.identify_seen: dict = {}
        self.requests: list[str] = []
        self._server = websockets_sync_server.serve(
            self._handle, "127.0.0.1", 0, subprotocols=[SUBPROTOCOL]
        )
        self.port = self._server.socket.getsockname()[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    # -- lifecycle ---------------------------------------------------------
    def stop(self) -> None:
        self._server.shutdown()
        self._thread.join(timeout=5)

    # -- protocol ----------------------------------------------------------
    def _handle(self, connection) -> None:
        if self.malformed_hello:
            connection.send("{this is not json")
            connection.close()
            return

        hello: dict = {
            "obsStudioVersion": self.obs.obs_version,
            "obsWebSocketVersion": self.obs.ws_version,
            "rpcVersion": self.obs.rpc_version,
        }
        if self.password is not None:
            hello["authentication"] = {"challenge": "test-challenge", "salt": "test-salt"}
        connection.send(json.dumps({"op": 0, "d": hello}))

        raw = connection.recv()
        identify = json.loads(raw)
        self.identify_seen = identify
        if identify.get("op") != 1:
            connection.close(code=4007, reason="not identified")
            return

        if self.password is not None:
            expected = build_auth_string(self.password, "test-salt", "test-challenge")
            if identify["d"].get("authentication") != expected:
                connection.close(code=4009, reason="authentication failed")
                return

        connection.send(json.dumps({"op": 2, "d": {"negotiatedRpcVersion": 1}}))

        while True:
            try:
                raw = connection.recv()
            except Exception:
                return
            if raw is None:
                return
            message = json.loads(raw)
            if message.get("op") != 6:
                connection.close(code=4006, reason="unknown opcode")
                return
            request_type = message["d"]["requestType"]
            request_id = message["d"]["requestId"]
            self.requests.append(request_type)

            if self.corrupt_frame:
                connection.send("}{ not json at all")
                continue

            try:
                data = self.obs._respond(request_type, message["d"].get("requestData") or {})
                status = {"result": True, "code": 100}
            except ObsRpcError as exc:
                data = None
                status = {"result": False, "code": exc.code, "comment": exc.comment}

            payload: dict = {"requestType": request_type, "requestId": request_id, "requestStatus": status}
            if data is not None:
                payload["responseData"] = data
            connection.send(json.dumps({"op": 7, "d": payload}))


@pytest.fixture
def server_factory():
    servers: list[ObsServer] = []

    def make(obs: FakeObs | None = None, **kwargs) -> ObsServer:
        server = ObsServer(obs or FakeObs(), **kwargs)
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.stop()


def connect(server: ObsServer, **kwargs) -> ObsWebSocketTransport:
    transport = ObsWebSocketTransport(host="127.0.0.1", port=server.port, **kwargs)
    transport.connect()
    return transport


# --------------------------------------------------------------------------- #
# happy path
# --------------------------------------------------------------------------- #
def test_real_websocket_handshake_and_requests(server_factory):
    server = server_factory()
    transport = connect(server)
    try:
        assert transport.rpc_version == 1
        assert transport.obs_websocket_version == "5.5.2"
        version = transport.call("GetVersion")
        assert version["platform"] == "windows"
        assert "GetRecordStatus" in transport.available_requests
        assert transport.call("GetRecordStatus")["outputActive"] is False
        assert server.identify_seen["d"]["rpcVersion"] == 1
        assert server.identify_seen["d"]["eventSubscriptions"] == 0
    finally:
        transport.close()


def test_real_websocket_authentication_succeeds_with_the_right_password(server_factory):
    server = server_factory(password="s3cret")
    transport = connect(server, password="s3cret")
    try:
        assert transport.call("GetVersion")["obsVersion"] == "31.1.1"
        assert server.identify_seen["d"]["authentication"]
    finally:
        transport.close()


def test_real_websocket_authentication_failure_is_reported_as_auth_failed(server_factory):
    server = server_factory(password="s3cret")
    with pytest.raises(CaptureError) as excinfo:
        connect(server, password="wrong-password")
    assert excinfo.value.category == AUTH_FAILED
    assert excinfo.value.detail["close_code"] == 4009


def test_real_websocket_requires_a_password_when_the_server_demands_one(server_factory):
    server = server_factory(password="s3cret")
    with pytest.raises(CaptureError) as excinfo:
        connect(server, password=None)
    assert excinfo.value.category == "CREDENTIAL_MISSING"


def test_real_websocket_malformed_frame_is_bad_response(server_factory):
    server = server_factory(malformed_hello=True)
    with pytest.raises(CaptureError) as excinfo:
        connect(server)
    assert excinfo.value.category == BAD_RESPONSE


def test_real_websocket_corrupt_response_frame_is_bad_response(server_factory):
    server = server_factory(corrupt_frame=True)
    # A malformed frame is fatal wherever it appears, including the capability
    # discovery that connect() performs, so the refusal may come from either.
    with pytest.raises(CaptureError) as excinfo:
        transport = connect(server)
        transport.call("GetVersion")
    assert excinfo.value.category == BAD_RESPONSE


def test_real_websocket_unknown_request_is_reported_as_unsupported(server_factory):
    server = server_factory()
    transport = connect(server)
    try:
        with pytest.raises(ObsRpcError) as excinfo:
            transport.call("DefinitelyNotARequest")
        assert excinfo.value.code == 204
    finally:
        transport.close()


# --------------------------------------------------------------------------- #
# the whole backend against a real socket
# --------------------------------------------------------------------------- #
def test_full_backend_lifecycle_over_a_real_socket(server_factory, tmp_path):
    from agent_capture_win import run

    obs = FakeObs()
    server = server_factory(obs)
    state_dir = str(tmp_path / "state")

    def config(**overrides):
        base = {
            "host": "127.0.0.1",
            "port": server.port,
            "profile": "agent-capture",
            "scene_collection": "agent-capture",
            "scene": "agent-capture-window",
            "target": {
                "title": "Slay the Spire 2",
                "class": "UnityWndClass",
                "exe": "SlayTheSpire2.exe",
                "priority": "exe",
            },
            "record_dir": DEFAULT_RECORD_DIR,
            "run_id": "e2e-001",
            "state_dir": state_dir,
        }
        base.update(overrides)
        return base

    probe = lambda path: {"exists": True, "size_bytes": 8192, "mtime": __import__("time").time()}

    preflight = run("preflight", config(), file_probe=probe, dir_probe=lambda p: [])
    assert preflight["ok"] is True, preflight.get("error")
    assert preflight["verification_level"] == "live_connected"

    started = run("start", config(), file_probe=probe, dir_probe=lambda p: [])
    assert started["ok"] is True, started.get("error")
    assert started["recording"]["evidence"] == "output_bytes_advanced"
    # A real OBS connection plus write progress is NOT "record verified": the
    # artifact has not been checked yet.
    assert started["verification_level"] == "live_connected"
    assert started["recording"]["picture_verified"] is False

    token = started["ownership"]["session_token"]
    status = run("status", config(session_token=token), file_probe=probe, dir_probe=lambda p: [])
    assert status["ok"] is True, status.get("error")

    stopped = run("stop", config(session_token=token), file_probe=probe, dir_probe=lambda p: [])
    assert stopped["ok"] is True, stopped.get("error")
    assert stopped["output"]["verified"] is True
    assert stopped["recording"]["artifact_verified"] is True
    assert stopped["recording"]["picture_verified"] is False
    # The file was verified on disk -- but only the *file*, never the picture.
    assert stopped["verification_level"] == "live_output_file_verified"
    assert stopped["verification"]["artifact"] == "file_verified"
    assert stopped["verification"]["picture"] == "unverified"
    assert stopped["verification"]["audio"] == "settings_only"
    assert "NOT CLAIMED" in stopped["verification"]["claims"]["picture"]
    assert "StartRecord" in server.requests and "StopRecord" in server.requests


def test_backend_over_real_socket_refuses_a_busy_obs(server_factory, tmp_path):
    from agent_capture_win import run

    server = server_factory(FakeObs(streaming=True))
    result = run(
        "preflight",
        {"port": server.port, "profile": "agent-capture", "scene_collection": "agent-capture", "scene": "agent-capture-window"},
    )
    assert result["ok"] is False
    assert result["error"]["category"] == "OBS_BUSY"
