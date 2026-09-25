"""Transport tests: official auth algorithm, endpoint safety, and real frame parsing.

The frame-level tests drive the *real* ``ObsWebSocketTransport`` (not the fake
transport) through a fake socket, so the parsing and validation code that would
run against a live OBS is what gets exercised here.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from agent_capture_win.errors import (  # noqa: E402
    AUTH_FAILED,
    BAD_RESPONSE,
    CONNECT_FAILED,
    CREDENTIAL_MISSING,
    ENDPOINT_NOT_LOOPBACK,
    RPC_UNSUPPORTED,
    CaptureError,
)
from agent_capture_win.fake import (  # noqa: E402
    CloseFrame,
    FakeSocket,
    hello_frame,
    identified_frame,
    request_response,
    text_frame,
)
from agent_capture_win.transport import (  # noqa: E402
    ObsRpcError,
    ObsWebSocketTransport,
    build_auth_string,
    is_loopback_host,
    parse_version,
    resolve_password,
    version_at_least,
)


# --------------------------------------------------------------------------- #
# pure helpers
# --------------------------------------------------------------------------- #
def test_auth_string_matches_official_algorithm():
    # Independently computed vector:
    #   secret = b64(sha256("secret" + "salt123"))
    #          = JlNehE1ySMNAV1WZ9o08tgy1Xxca9j4SDKUC2uTAoRQ=
    #   auth   = b64(sha256(secret + "challenge456"))
    assert build_auth_string("secret", "salt123", "challenge456") == "xgzgHJ5CaCNvrzkqxH6D2xMsV17ODXfIyB12Cj4aV1o="


@pytest.mark.parametrize(
    "host,expected",
    [
        ("127.0.0.1", True),
        ("127.5.5.5", True),
        ("::1", True),
        ("[::1]", True),
        ("localhost", True),
        ("LOCALHOST", True),
        ("0.0.0.0", False),
        ("10.0.0.5", False),
        ("203.0.113.7", False),
        ("obs.example.com", False),
        ("", False),
    ],
)
def test_is_loopback_host(host, expected):
    assert is_loopback_host(host) is expected


def test_parse_version_and_comparison():
    assert parse_version("5.3.0") == (5, 3, 0)
    assert parse_version("5.5.2-rc1") == (5, 5, 2)
    assert parse_version("") == ()
    assert version_at_least("5.3.0", (5, 3, 0)) is True
    assert version_at_least("5.2.9", (5, 3, 0)) is False
    assert version_at_least("5.10.0", (5, 3, 0)) is True
    assert version_at_least("", (5, 3, 0)) is False


def test_resolve_password_env_missing_is_explicit():
    with pytest.raises(CaptureError) as excinfo:
        resolve_password("AGENT_CAPTURE_DEFINITELY_UNSET_VAR", None)
    assert excinfo.value.category == CREDENTIAL_MISSING
    assert "AGENT_CAPTURE_DEFINITELY_UNSET_VAR" in excinfo.value.message


def test_resolve_password_env_present(monkeypatch):
    monkeypatch.setenv("AGENT_CAPTURE_TEST_PW", "hunter2")
    assert resolve_password("AGENT_CAPTURE_TEST_PW", None) == "hunter2"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_resolve_password_refuses_world_readable_file():
    with tempfile.NamedTemporaryFile("w", delete=False) as handle:
        handle.write("hunter2\n")
        path = handle.name
    try:
        os.chmod(path, 0o644)
        with pytest.raises(CaptureError) as excinfo:
            resolve_password(None, path)
        assert excinfo.value.category == CREDENTIAL_MISSING
        assert "too open" in excinfo.value.message
        os.chmod(path, 0o600)
        assert resolve_password(None, path) == "hunter2"
    finally:
        os.unlink(path)


def test_resolve_password_missing_file():
    with pytest.raises(CaptureError) as excinfo:
        resolve_password(None, "/nonexistent/password/file")
    assert excinfo.value.category == CREDENTIAL_MISSING


# --------------------------------------------------------------------------- #
# connection policy
# --------------------------------------------------------------------------- #
def test_non_loopback_endpoint_is_refused_before_connecting():
    transport = ObsWebSocketTransport(host="203.0.113.7", port=4455, password="x")
    with pytest.raises(CaptureError) as excinfo:
        transport.connect()
    assert excinfo.value.category == ENDPOINT_NOT_LOOPBACK
    assert "203.0.113.7" in excinfo.value.message


def test_non_loopback_allowed_only_when_opted_in():
    sock = FakeSocket([hello_frame(), identified_frame(), request_response("x", response_data={"obsVersion": "31"})])
    transport = ObsWebSocketTransport(
        host="203.0.113.7", port=4455, allow_non_loopback=True, _socket_factory=lambda url, **kw: sock
    )
    # The handshake will fail because requestId does not match, which proves the
    # endpoint gate was passed rather than blocking.
    with pytest.raises(CaptureError) as excinfo:
        transport.connect()
    assert excinfo.value.category == BAD_RESPONSE


# --------------------------------------------------------------------------- #
# handshake
# --------------------------------------------------------------------------- #
def _handshake_socket(auth=None, **hello_kwargs):
    frames = [hello_frame(authentication=auth, **hello_kwargs), identified_frame()]
    frames.append(request_response("PLACEHOLDER", response_data={"obsVersion": "31.1.1", "availableRequests": ["GetVersion"]}))
    return FakeSocket(frames)


def test_handshake_without_auth_identifies_and_reads_capabilities():
    # The transport generates its own requestId, so the double echoes it back.
    class EchoSocket(FakeSocket):
        def recv_data_frame(self, control_frame=False):
            if self.frames:
                op, value = self.frames.pop(0)
                if isinstance(value, str) and "PLACEHOLDER" in value:
                    value = value.replace("PLACEHOLDER", self.sent[-1]["d"]["requestId"])
                return (op, value)
            return (0x8, None)

    sock = EchoSocket(
        [
            hello_frame(),
            identified_frame(),
            request_response("PLACEHOLDER", response_data={"obsVersion": "31.1.1", "availableRequests": ["GetVersion", "GetRecordStatus"]}),
        ]
    )
    transport = ObsWebSocketTransport(_socket_factory=lambda url, **kw: sock)
    transport.connect()
    assert transport.obs_websocket_version == "5.5.2"
    assert transport.obs_version == "31.1.1"
    assert transport.rpc_version == 1
    assert set(transport.available_requests) == {"GetVersion", "GetRecordStatus"}
    identify = sock.sent[0]
    assert identify["op"] == 1
    assert identify["d"]["rpcVersion"] == 1
    assert "authentication" not in identify["d"]
    transport.close()


def test_handshake_with_auth_sends_the_official_auth_string():
    class EchoSocket(FakeSocket):
        def recv_data_frame(self, control_frame=False):
            if self.frames:
                op, value = self.frames.pop(0)
                if isinstance(value, str) and "PLACEHOLDER" in value:
                    value = value.replace("PLACEHOLDER", self.sent[-1]["d"]["requestId"])
                return (op, value)
            return (0x8, None)

    sock = EchoSocket(
        [
            hello_frame(authentication={"challenge": "challenge456", "salt": "salt123"}),
            identified_frame(),
            request_response("PLACEHOLDER", response_data={"obsVersion": "31.1.1"}),
        ]
    )
    transport = ObsWebSocketTransport(password="secret", _socket_factory=lambda url, **kw: sock)
    transport.connect()
    assert sock.sent[0]["d"]["authentication"] == "xgzgHJ5CaCNvrzkqxH6D2xMsV17ODXfIyB12Cj4aV1o="
    transport.close()


def test_auth_required_without_password_fails_closed():
    sock = FakeSocket([hello_frame(authentication={"challenge": "c", "salt": "s"})])
    transport = ObsWebSocketTransport(_socket_factory=lambda url, **kw: sock)
    with pytest.raises(CaptureError) as excinfo:
        transport.connect()
    assert excinfo.value.category == CREDENTIAL_MISSING


def test_server_close_4009_is_reported_as_auth_failure():
    sock = FakeSocket([hello_frame(authentication={"challenge": "c", "salt": "s"})], close_frame=CloseFrame(4009, "auth failed"))
    transport = ObsWebSocketTransport(password="wrong", _socket_factory=lambda url, **kw: sock)
    with pytest.raises(CaptureError) as excinfo:
        transport.connect()
    assert excinfo.value.category == AUTH_FAILED
    assert excinfo.value.detail["close_code"] == 4009


def test_server_close_4010_is_reported_as_protocol_mismatch():
    sock = FakeSocket([], close_frame=CloseFrame(4010, "bad rpc version"))
    transport = ObsWebSocketTransport(_socket_factory=lambda url, **kw: sock)
    with pytest.raises(CaptureError) as excinfo:
        transport.connect()
    assert excinfo.value.category == RPC_UNSUPPORTED


@pytest.mark.parametrize(
    "frames,expected_category",
    [
        ([text_frame({"op": 6, "d": {}})], BAD_RESPONSE),  # not a Hello
        ([text_frame({"op": 0, "d": {"obsWebSocketVersion": "5.5.2"}})], BAD_RESPONSE),  # no rpcVersion
        ([text_frame({"op": 0, "d": {"rpcVersion": 0}})], RPC_UNSUPPORTED),
        ([(0x1, "{not json")], BAD_RESPONSE),
        ([(0x2, b"\x00\x01")], BAD_RESPONSE),  # binary frame
        ([text_frame({"op": "zero", "d": {}})], BAD_RESPONSE),
    ],
)
def test_malformed_handshake_frames_never_pass(frames, expected_category):
    sock = FakeSocket(frames)
    transport = ObsWebSocketTransport(_socket_factory=lambda url, **kw: sock)
    with pytest.raises(CaptureError) as excinfo:
        transport.connect()
    assert excinfo.value.category == expected_category


def _connected_socket(response_frames):
    class EchoSocket(FakeSocket):
        def recv_data_frame(self, control_frame=False):
            if self.frames:
                op, value = self.frames.pop(0)
                if isinstance(value, str) and "PLACEHOLDER" in value:
                    value = value.replace("PLACEHOLDER", self.sent[-1]["d"]["requestId"])
                return (op, value)
            return (0x8, None)

    return EchoSocket([hello_frame(), identified_frame(), request_response("PLACEHOLDER", response_data={})] + list(response_frames))


# --------------------------------------------------------------------------- #
# request/response validation
# --------------------------------------------------------------------------- #
def test_request_response_with_mismatched_request_id_is_bad_response():
    sock = _connected_socket([request_response("someone-elses-id", response_data={"x": 1})])
    transport = ObsWebSocketTransport(_socket_factory=lambda url, **kw: sock)
    transport.connect()
    with pytest.raises(CaptureError) as excinfo:
        transport.call("GetVersion")
    assert excinfo.value.category == BAD_RESPONSE
    assert "requestId" in excinfo.value.message


def test_request_response_without_boolean_result_is_bad_response():
    frame = text_frame({"op": 7, "d": {"requestId": "PLACEHOLDER", "requestStatus": {"code": 100}}})
    sock = _connected_socket([frame])
    transport = ObsWebSocketTransport(_socket_factory=lambda url, **kw: sock)
    transport.connect()
    with pytest.raises(CaptureError) as excinfo:
        transport.call("GetVersion")
    assert excinfo.value.category == BAD_RESPONSE


def test_request_response_with_non_object_data_is_bad_response():
    frame = text_frame(
        {"op": 7, "d": {"requestId": "PLACEHOLDER", "requestStatus": {"result": True, "code": 100}, "responseData": [1, 2]}}
    )
    sock = _connected_socket([frame])
    transport = ObsWebSocketTransport(_socket_factory=lambda url, **kw: sock)
    transport.connect()
    with pytest.raises(CaptureError) as excinfo:
        transport.call("GetVersion")
    assert excinfo.value.category == BAD_RESPONSE


def test_unexpected_opcode_while_waiting_for_response_is_bad_response():
    sock = _connected_socket([text_frame({"op": 2, "d": {"negotiatedRpcVersion": 1}})])
    transport = ObsWebSocketTransport(_socket_factory=lambda url, **kw: sock)
    transport.connect()
    with pytest.raises(CaptureError) as excinfo:
        transport.call("GetVersion")
    assert excinfo.value.category == BAD_RESPONSE


def test_events_are_skipped_while_waiting_for_a_response():
    sock = _connected_socket(
        [
            text_frame({"op": 5, "d": {"eventType": "RecordStateChanged", "eventIntent": 1}}),
            request_response("PLACEHOLDER", response_data={"outputActive": False}),
        ]
    )
    transport = ObsWebSocketTransport(_socket_factory=lambda url, **kw: sock)
    transport.connect()
    assert transport.call("GetRecordStatus") == {"outputActive": False}


def test_failed_request_status_raises_obs_rpc_error_with_code():
    frame = text_frame(
        {
            "op": 7,
            "d": {
                "requestId": "PLACEHOLDER",
                "requestStatus": {"result": False, "code": 600, "comment": "No source was found"},
            },
        }
    )
    # Two identical failure frames: one for call(), one for try_call().
    sock = _connected_socket([frame, frame])
    transport = ObsWebSocketTransport(_socket_factory=lambda url, **kw: sock)
    transport.connect()
    with pytest.raises(ObsRpcError) as excinfo:
        transport.call("GetInputSettings", {"inputName": "nope"})
    assert excinfo.value.code == 600
    assert excinfo.value.status_name == "ResourceNotFound"
    ok, error = transport.try_call("GetInputSettings", {"inputName": "nope"})
    assert ok is False and error.code == 600


def test_connection_loss_is_reported_as_connect_failed():
    sock = _connected_socket([])  # no further frames -> close
    transport = ObsWebSocketTransport(_socket_factory=lambda url, **kw: sock)
    transport.connect()
    with pytest.raises(CaptureError) as excinfo:
        transport.call("GetVersion")
    assert excinfo.value.category == CONNECT_FAILED
