"""Real obs-websocket 5.x transport.

RFC6455 framing is delegated to ``websocket-client``; this module only
implements the obs-websocket JSON protocol on top of it (Hello / Identify /
Identified / Request / RequestResponse) plus the official authentication
string algorithm.

Deliberate properties:

* **RPC only.**  No policy, no retries, no interpretation of what a request
  *means*.  Policy lives in ``policy.py`` so it can be tested against scripted
  responses without a socket.
* **Loopback by default.**  A non-loopback endpoint is refused unless the
  caller explicitly opts in, so a mistyped host cannot ship OBS credentials to
  a remote machine.
* **The user's proxy settings are honoured, not bypassed.**  The proxy
  environment is left untouched; the loopback host is simply added to
  ``http_no_proxy`` so local OBS traffic is never tunnelled through a proxy
  (and so OBS credentials never reach a proxy).
* **Unknown is never a pass.**  A malformed frame, a mismatched request id, a
  missing ``requestStatus`` or an unexpected opcode raise ``BAD_RESPONSE``
  rather than being ignored.
* **No automatic retry of a mutating request.**  ``StartRecord`` /
  ``StopRecord`` are never re-sent by this layer.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import logging
import os
import uuid
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .errors import (
    AUTH_FAILED,
    BAD_RESPONSE,
    CONNECT_FAILED,
    CREDENTIAL_MISSING,
    DEPENDENCY_MISSING,
    ENDPOINT_NOT_LOOPBACK,
    INTERNAL,
    RPC_UNSUPPORTED,
    CaptureError,
)

log = logging.getLogger("agent_capture_win.transport")

# --- WebSocketOpCode (official protocol) -------------------------------------
OP_HELLO = 0
OP_IDENTIFY = 1
OP_IDENTIFIED = 2
OP_REIDENTIFY = 3
OP_EVENT = 5
OP_REQUEST = 6
OP_REQUEST_RESPONSE = 7

# --- WebSocketCloseCode (official protocol) ----------------------------------
CLOSE_CODES = {
    4000: "UnknownReason",
    4002: "MessageDecodeError",
    4003: "MissingDataField",
    4004: "InvalidDataFieldType",
    4005: "InvalidDataFieldValue",
    4006: "UnknownOpCode",
    4007: "NotIdentified",
    4008: "AlreadyIdentified",
    4009: "AuthenticationFailed",
    4010: "UnsupportedRpcVersion",
    4011: "SessionInvalidated",
    4012: "UnsupportedFeature",
}

# --- RequestStatus (official protocol) ---------------------------------------
REQUEST_STATUS = {
    10: "NoError",
    100: "Success",
    203: "MissingRequestType",
    204: "UnknownRequestType",
    205: "GenericError",
    206: "UnsupportedRequestBatchExecutionType",
    207: "NotReady",
    300: "MissingRequestField",
    301: "MissingRequestData",
    400: "InvalidRequestField",
    401: "InvalidRequestFieldType",
    402: "RequestFieldOutOfRange",
    403: "RequestFieldEmpty",
    404: "TooManyRequestFields",
    500: "OutputRunning",
    501: "OutputNotRunning",
    502: "OutputPaused",
    503: "OutputNotPaused",
    504: "OutputDisabled",
    505: "StudioModeActive",
    506: "StudioModeNotActive",
    600: "ResourceNotFound",
    601: "ResourceAlreadyExists",
    602: "InvalidResourceType",
    603: "NotEnoughResources",
    604: "InvalidResourceState",
    605: "InvalidInputKind",
    606: "ResourceNotConfigurable",
    607: "InvalidFilterKind",
    700: "ResourceCreationFailed",
    701: "ResourceActionFailed",
    702: "RequestProcessingFailed",
    703: "CannotAct",
}

#: Close codes that mean "the credentials or the account are not usable".
_AUTH_CLOSE_CODES = (4009,)
#: Close codes that mean "this client/server pair cannot talk protocol".
_PROTOCOL_CLOSE_CODES = (4010, 4012, 4006)

LOOPBACK_HOSTNAMES = ("localhost", "localhost.localdomain")


# --------------------------------------------------------------------------- #
# Pure helpers (unit tested without any socket)
# --------------------------------------------------------------------------- #
def build_auth_string(password: str, salt: str, challenge: str) -> str:
    """Official obs-websocket authentication string.

    ``base64(sha256(base64(sha256(password + salt)) + challenge))``
    """
    secret = base64.b64encode(hashlib.sha256((password + salt).encode("utf-8")).digest())
    auth = base64.b64encode(hashlib.sha256(secret + challenge.encode("utf-8")).digest())
    return auth.decode("ascii")


def is_loopback_host(host: str) -> bool:
    """True for ``127.0.0.0/8``, ``::1`` and the literal name ``localhost``."""
    candidate = (host or "").strip().strip("[]")
    if not candidate:
        return False
    if candidate.lower() in LOOPBACK_HOSTNAMES:
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def parse_version(text: str) -> Tuple[int, ...]:
    """Parse ``"5.3.0"`` / ``"5.3.0-rc1"`` into a comparable tuple."""
    if not text:
        return ()
    cleaned = str(text).strip()
    for separator in ("-", "+"):
        cleaned = cleaned.split(separator)[0]
    parts: List[int] = []
    for chunk in cleaned.split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def version_at_least(text: str, minimum: Tuple[int, ...]) -> bool:
    got = parse_version(text)
    if not got:
        return False
    width = max(len(got), len(minimum))
    padded_got = got + (0,) * (width - len(got))
    padded_min = minimum + (0,) * (width - len(minimum))
    return padded_got >= padded_min


def _parse_close_payload(payload: Any) -> Tuple[Optional[int], str]:
    """Extract ``(code, reason)`` from a WebSocket close payload.

    ``websocket-client`` hands back the *raw* close body (a 2-byte big-endian
    status code followed by a UTF-8 reason) rather than a parsed object, so the
    code has to be decoded here.  Objects that already expose ``code``/``reason``
    are accepted too, which keeps the frame doubles simple.
    """
    if payload is None:
        return None, ""
    if hasattr(payload, "code"):
        code = getattr(payload, "code", None)
        reason = getattr(payload, "reason", "") or ""
        if isinstance(reason, (bytes, bytearray)):
            reason = reason.decode("utf-8", "replace")
        return code, str(reason)
    if isinstance(payload, (bytes, bytearray)):
        raw = bytes(payload)
        if len(raw) >= 2:
            return int.from_bytes(raw[:2], "big"), raw[2:].decode("utf-8", "replace")
        return None, raw.decode("utf-8", "replace")
    if isinstance(payload, str):
        return None, payload
    return None, ""


class ObsRpcError(Exception):
    """A request the server answered with ``requestStatus.ok == false``."""
    def __init__(self, request_type: str, code: int, comment: str, data: Any = None) -> None:
        super().__init__("%s failed: %s (%s)" % (request_type, comment, code))
        self.request_type = request_type
        self.code = code
        self.comment = comment
        self.data = data

    @property
    def status_name(self) -> str:
        return REQUEST_STATUS.get(self.code, "Unknown(%s)" % (self.code,))

    def to_detail(self) -> Dict[str, Any]:
        return {
            "request_type": self.request_type,
            "obs_code": self.code,
            "obs_status": self.status_name,
            "comment": self.comment,
        }


# --------------------------------------------------------------------------- #
# Transport interface
# --------------------------------------------------------------------------- #
class Transport(ABC):
    """Minimal RPC surface the backend depends on."""

    #: Populated by ``connect()``.
    hello: Dict[str, Any]

    @abstractmethod
    def connect(self) -> None:
        ...

    @abstractmethod
    def call(self, request_type: str, request_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Send a request and return ``responseData`` (``{}`` when absent)."""

    def try_call(
        self, request_type: str, request_data: Optional[Dict[str, Any]] = None
    ) -> Tuple[bool, Any]:
        """Call without raising; returns ``(ok, responseData | ObsRpcError)``."""
        try:
            return True, self.call(request_type, request_data)
        except ObsRpcError as exc:
            return False, exc

    @abstractmethod
    def close(self) -> None:
        ...

    @property
    def available_requests(self) -> Optional[Sequence[str]]:
        return None

    @property
    def obs_version(self) -> str:
        return ""

    @property
    def obs_websocket_version(self) -> str:
        return ""

    @property
    def rpc_version(self) -> Optional[int]:
        return None


def resolve_password(
    password_env: Optional[str] = None,
    password_file: Optional[str] = None,
) -> Optional[str]:
    """Read the password from the named env var or a restricted file.

    The value is never logged and never echoed back to a caller.  A world
    readable password file is refused rather than silently accepted.
    """
    if password_env:
        value = os.environ.get(password_env)
        if value is None:
            raise CaptureError(
                CREDENTIAL_MISSING,
                "environment variable %r is not set" % (password_env,),
                {"password_env": password_env},
            )
        return value
    if password_file:
        try:
            mode = os.stat(password_file).st_mode
        except OSError as exc:
            raise CaptureError(
                CREDENTIAL_MISSING,
                "password file is not readable",
                {"password_file": password_file, "errno": exc.errno},
            ) from exc
        if os.name != "nt" and (mode & 0o077):
            raise CaptureError(
                CREDENTIAL_MISSING,
                "password file permissions are too open (expected 0600)",
                {"password_file": password_file, "mode": oct(mode & 0o777)},
            )
        with open(password_file, "r", encoding="utf-8") as handle:
            return handle.read().strip()
    return None


# --------------------------------------------------------------------------- #
# Real transport
# --------------------------------------------------------------------------- #
class ObsWebSocketTransport(Transport):
    """obs-websocket 5.x client over a real WebSocket connection."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 4455,
        password: Optional[str] = None,
        *,
        allow_non_loopback: bool = False,
        connect_timeout: float = 6.0,
        request_timeout: float = 8.0,
        url_path: str = "/",
        subprotocol: str = "obswebsocket.json",
        _socket_factory: Any = None,
    ) -> None:
        self.host = host
        self.port = int(port)
        self._password = password
        self.allow_non_loopback = bool(allow_non_loopback)
        self.connect_timeout = float(connect_timeout)
        self.request_timeout = float(request_timeout)
        self.url_path = url_path or "/"
        self.subprotocol = subprotocol
        self._socket_factory = _socket_factory
        self._ws: Any = None
        self.hello: Dict[str, Any] = {}
        self._identified = False
        self._available_requests: Optional[List[str]] = None
        self._obs_version = ""
        self._obs_websocket_version = ""
        self._rpc_version: Optional[int] = None

    # -- connection ---------------------------------------------------------
    def _require_websocket_module(self) -> Any:
        if self._socket_factory is not None:
            return None
        try:
            import websocket  # type: ignore
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise CaptureError(
                DEPENDENCY_MISSING,
                "the 'websocket-client' package is required for the real transport",
                {"package": "websocket-client", "install": "pip install websocket-client"},
            ) from exc
        return websocket

    def _validate_endpoint(self) -> None:
        if is_loopback_host(self.host):
            return
        if self.allow_non_loopback:
            log.warning("connecting to non-loopback obs-websocket host %s (explicitly allowed)", self.host)
            return
        raise CaptureError(
            ENDPOINT_NOT_LOOPBACK,
            "refusing to send OBS credentials to non-loopback host %r" % (self.host,),
            {"host": self.host, "hint": "set allow_non_loopback=true only for a host you control"},
        )

    def connect(self) -> None:
        self._validate_endpoint()
        websocket = self._require_websocket_module()

        url = "ws://%s:%d%s" % (
            ("[%s]" % self.host) if ":" in self.host and not self.host.startswith("[") else self.host,
            self.port,
            self.url_path,
        )
        options: Dict[str, Any] = {
            "timeout": self.connect_timeout,
            "subprotocols": [self.subprotocol],
            # Do not invent an Origin header for a local service.
            "suppress_origin": True,
            # Honour the user's proxy configuration but never tunnel loopback.
            "http_no_proxy": ["127.0.0.1", "localhost", "::1", self.host],
        }
        try:
            if self._socket_factory is not None:
                self._ws = self._socket_factory(url, **options)
            else:
                self._ws = websocket.create_connection(url, **options)
        except CaptureError:
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced as a category
            raise CaptureError(
                CONNECT_FAILED,
                "could not connect to obs-websocket at %s:%d (%s)" % (self.host, self.port, type(exc).__name__),
                {"host": self.host, "port": self.port, "error": str(exc)[:300]},
            ) from exc

        try:
            self._ws.settimeout(self.request_timeout)
        except Exception:  # noqa: BLE001 - optional on fake sockets
            pass
        self._handshake()

    def _handshake(self) -> None:
        message = self._receive()
        op = message.get("op")
        data = message.get("d")
        if op != OP_HELLO:
            raise CaptureError(
                BAD_RESPONSE,
                "expected a Hello (op 0) from obs-websocket, got op %r" % (op,),
                {"op": op},
            )
        if not isinstance(data, dict):
            raise CaptureError(BAD_RESPONSE, "Hello payload is not an object")
        self.hello = dict(data)

        rpc_version = data.get("rpcVersion")
        if not isinstance(rpc_version, int) or isinstance(rpc_version, bool):
            raise CaptureError(BAD_RESPONSE, "Hello is missing an integer rpcVersion", {"hello": data})
        if rpc_version < 1:
            raise CaptureError(
                RPC_UNSUPPORTED,
                "obs-websocket advertises rpcVersion %r, this client needs >= 1" % (rpc_version,),
            )

        self._obs_version = str(data.get("obsStudioVersion") or "")
        self._obs_websocket_version = str(data.get("obsWebSocketVersion") or "")

        identify: Dict[str, Any] = {"rpcVersion": 1, "eventSubscriptions": 0}
        authentication = data.get("authentication")
        if authentication is not None:
            if not isinstance(authentication, dict):
                raise CaptureError(BAD_RESPONSE, "Hello.authentication is not an object")
            challenge = authentication.get("challenge")
            salt = authentication.get("salt")
            if not isinstance(challenge, str) or not isinstance(salt, str):
                raise CaptureError(
                    BAD_RESPONSE, "Hello.authentication is missing challenge/salt"
                )
            if self._password is None:
                raise CaptureError(
                    CREDENTIAL_MISSING,
                    "obs-websocket requires a password but none was supplied",
                    {"password_env_hint": "set the configured env var, or disable auth in OBS"},
                )
            identify["authentication"] = build_auth_string(self._password, salt, challenge)

        self._send({"op": OP_IDENTIFY, "d": identify})

        reply = self._receive()
        if reply.get("op") != OP_IDENTIFIED:
            raise CaptureError(
                BAD_RESPONSE,
                "expected Identified (op 2), got op %r" % (reply.get("op"),),
                {"op": reply.get("op")},
            )
        payload = reply.get("d")
        if not isinstance(payload, dict):
            raise CaptureError(BAD_RESPONSE, "Identified payload is not an object")
        negotiated = payload.get("negotiatedRpcVersion")
        if not isinstance(negotiated, int) or isinstance(negotiated, bool):
            raise CaptureError(
                BAD_RESPONSE, "Identified is missing an integer negotiatedRpcVersion", {"payload": payload}
            )
        self._rpc_version = negotiated
        self._identified = True

        # Capability discovery.  A failure here is not fatal: callers that need
        # a specific request re-check before using it.
        ok, data_or_error = self.try_call("GetVersion")
        if ok and isinstance(data_or_error, dict):
            requests = data_or_error.get("availableRequests")
            if isinstance(requests, list):
                self._available_requests = [str(item) for item in requests]
            if data_or_error.get("obsVersion"):
                self._obs_version = str(data_or_error["obsVersion"])
            if data_or_error.get("obsWebSocketVersion"):
                self._obs_websocket_version = str(data_or_error["obsWebSocketVersion"])

    # -- messaging ----------------------------------------------------------
    def _send(self, payload: Dict[str, Any]) -> None:
        if self._ws is None:
            raise CaptureError(INTERNAL, "transport is not connected")
        try:
            self._ws.send(json.dumps(payload))
        except Exception as exc:  # noqa: BLE001
            raise CaptureError(
                CONNECT_FAILED, "sending to obs-websocket failed (%s)" % (type(exc).__name__,), {"error": str(exc)[:300]}
            ) from exc

    def _receive(self) -> Dict[str, Any]:
        if self._ws is None:
            raise CaptureError(INTERNAL, "transport is not connected")
        try:
            result = self._ws.recv_data_frame(control_frame=True)
        except CaptureError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise self._classify_socket_error(exc) from exc

        opcode, value = result if isinstance(result, tuple) and len(result) == 2 else (None, None)
        # ``recv_data_frame(control_frame=True)`` returns ``(opcode, Frame)`` for
        # every frame type, so the payload always lives on ``Frame.data``.
        # Doubles that return the payload directly are tolerated as well.
        payload = getattr(value, "data", value)

        if opcode == 0x8:  # ABNF.OPCODE_CLOSE
            raise self._close_error(value)
        if opcode == 0x9:  # ping
            try:
                self._ws.pong(payload)
            except Exception:  # noqa: BLE001 - best effort
                pass
            return self._receive()
        if opcode == 0xA:  # pong
            return self._receive()
        if opcode != 0x1:
            raise CaptureError(
                BAD_RESPONSE,
                "expected a text frame from obs-websocket, got opcode %r" % (opcode,),
                {"opcode": opcode},
            )

        if isinstance(payload, (bytes, bytearray)):
            try:
                payload = payload.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise CaptureError(BAD_RESPONSE, "obs-websocket frame is not valid UTF-8") from exc
        if not isinstance(payload, str):
            raise CaptureError(
                BAD_RESPONSE, "obs-websocket frame is not text", {"type": type(payload).__name__}
            )

        try:
            message = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise CaptureError(
                BAD_RESPONSE, "obs-websocket sent malformed JSON", {"error": str(exc)[:200]}
            ) from exc
        if not isinstance(message, dict) or "op" not in message:
            raise CaptureError(BAD_RESPONSE, "obs-websocket message is not an object with an 'op'")
        if not isinstance(message.get("op"), int) or isinstance(message.get("op"), bool):
            raise CaptureError(BAD_RESPONSE, "obs-websocket message has a non-integer 'op'")
        return message

    def _close_error(self, frame: Any) -> CaptureError:
        code, reason = _parse_close_payload(getattr(frame, "data", frame))
        name = CLOSE_CODES.get(code, "Close(%s)" % (code,))
        detail = {"close_code": code, "close_name": name, "reason": str(reason)[:200]}
        if code in _AUTH_CLOSE_CODES:
            return CaptureError(AUTH_FAILED, "obs-websocket rejected the credentials (%s)" % (name,), detail)
        if code in _PROTOCOL_CLOSE_CODES:
            return CaptureError(RPC_UNSUPPORTED, "obs-websocket closed the session (%s)" % (name,), detail)
        if not self._identified:
            return CaptureError(
                CONNECT_FAILED, "obs-websocket closed the connection during handshake (%s)" % (name,), detail
            )
        return CaptureError(CONNECT_FAILED, "obs-websocket closed the connection (%s)" % (name,), detail)

    def _classify_socket_error(self, exc: Exception) -> CaptureError:
        name = type(exc).__name__
        detail = {"error": str(exc)[:300], "exception": name}
        if name == "WebSocketTimeoutException":
            return CaptureError(CONNECT_FAILED, "obs-websocket did not answer in time", detail)
        if not self._identified:
            return CaptureError(CONNECT_FAILED, "obs-websocket handshake failed (%s)" % (name,), detail)
        return CaptureError(CONNECT_FAILED, "obs-websocket connection lost (%s)" % (name,), detail)

    # -- requests -----------------------------------------------------------
    def call(self, request_type: str, request_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if not self._identified:
            raise CaptureError(INTERNAL, "call() before connect()")
        request_id = str(uuid.uuid4())
        payload: Dict[str, Any] = {"op": OP_REQUEST, "d": {"requestType": request_type, "requestId": request_id}}
        if request_data:
            payload["d"]["requestData"] = request_data
        self._send(payload)

        while True:
            message = self._receive()
            op = message.get("op")
            if op == OP_EVENT:
                continue
            if op != OP_REQUEST_RESPONSE:
                raise CaptureError(
                    BAD_RESPONSE,
                    "expected RequestResponse (op 7) for %s, got op %r" % (request_type, op),
                    {"request_type": request_type, "op": op},
                )
            data = message.get("d")
            if not isinstance(data, dict):
                raise CaptureError(BAD_RESPONSE, "RequestResponse payload is not an object")
            if data.get("requestId") != request_id:
                raise CaptureError(
                    BAD_RESPONSE,
                    "RequestResponse carries a different requestId than the one sent",
                    {"expected": request_id, "got": data.get("requestId")},
                )
            status = data.get("requestStatus")
            if not isinstance(status, dict) or not isinstance(status.get("result"), bool):
                raise CaptureError(
                    BAD_RESPONSE,
                    "RequestResponse has no boolean requestStatus.result",
                    {"request_type": request_type, "request_status": status},
                )
            if status["result"] is False:
                raise ObsRpcError(
                    request_type,
                    int(status.get("code") or 0),
                    str(status.get("comment") or ""),
                    data.get("responseData"),
                )
            response_data = data.get("responseData")
            if response_data is None:
                return {}
            if not isinstance(response_data, dict):
                raise CaptureError(
                    BAD_RESPONSE,
                    "RequestResponse.responseData is not an object",
                    {"request_type": request_type, "type": type(response_data).__name__},
                )
            return response_data

    def close(self) -> None:
        ws, self._ws = self._ws, None
        self._identified = False
        if ws is None:
            return
        try:
            ws.close()
        except Exception:  # noqa: BLE001 - closing must never raise
            pass

    # -- capability surface -------------------------------------------------
    @property
    def available_requests(self) -> Optional[Sequence[str]]:
        return self._available_requests

    @property
    def obs_version(self) -> str:
        return self._obs_version

    @property
    def obs_websocket_version(self) -> str:
        return self._obs_websocket_version

    @property
    def rpc_version(self) -> Optional[int]:
        return self._rpc_version

    def __enter__(self) -> "ObsWebSocketTransport":
        self.connect()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()
