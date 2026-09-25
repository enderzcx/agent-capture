"""Scripted doubles: a fake transport and a small simulated OBS instance.

Two levels, on purpose:

* ``FakeTransport`` — scripted responses per request type.  Used to drive the
  policy/backend through every refusal path deterministically.
* ``FakeObs`` — a small model of a real OBS session (version, profile, scenes,
  inputs, special inputs, record output) that produces those responses.  Keeps
  the negative tests readable instead of hand-writing dozens of dicts.
* ``FakeSocket`` — a frame-level double for ``ObsWebSocketTransport`` so the
  real parsing code (auth string, op validation, requestId matching) is
  exercised too, including malformed frames.

None of this proves that a real capture works; it proves the protocol and the
policy behave as specified.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .errors import CaptureError, INTERNAL
from .transport import ObsRpcError, Transport

DEFAULT_SOURCE_NAME = "agent-capture-window-capture"
DEFAULT_SCENE = "agent-capture-window"
DEFAULT_PROFILE = "agent-capture"
DEFAULT_COLLECTION = "agent-capture"
DEFAULT_RECORD_DIR = "D:\\agent-capture\\recordings"
WINDOW_STRING = "Slay the Spire 2:UnityWndClass:SlayTheSpire2.exe"

DEFAULT_AVAILABLE_REQUESTS = [
    "GetVersion",
    "GetStats",
    "GetRecordStatus",
    "StartRecord",
    "StopRecord",
    "GetRecordDirectory",
    "SetRecordDirectory",
    "GetStreamStatus",
    "GetVirtualCamStatus",
    "GetReplayBufferStatus",
    "GetProfileList",
    "GetSceneCollectionList",
    "GetSceneList",
    "GetCurrentProgramScene",
    "GetSceneItemList",
    "GetInputList",
    "GetInputKindList",
    "GetInputSettings",
    "GetInputDefaultSettings",
    "GetInputMute",
    "GetInputVolume",
    "GetInputAudioTracks",
    "GetSpecialInputs",
    "GetSourceActive",
    "GetSourceScreenshot",
    "GetInputPropertiesListPropertyItems",
    "SetInputSettings",
    "CreateInput",
]


# --------------------------------------------------------------------------- #
# Frame-level double for the real transport
# --------------------------------------------------------------------------- #
class FakeSocket:
    """Minimal stand-in for ``websocket.WebSocket`` at the frame level."""

    def __init__(self, frames: Sequence[Any], *, close_frame: Any = None) -> None:
        self.frames: List[Any] = list(frames)
        self.close_frame = close_frame
        self.sent: List[Dict[str, Any]] = []
        self.closed = False
        self.timeout: Optional[float] = None

    def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    def settimeout(self, value: float) -> None:
        self.timeout = value

    def recv_data_frame(self, control_frame: bool = False) -> Tuple[int, Any]:
        if self.frames:
            return self.frames.pop(0)
        if self.close_frame is not None:
            frame = self.close_frame
            self.close_frame = None
            return (0x8, frame)
        return (0x8, None)

    def pong(self, data: Any) -> None:  # pragma: no cover - not exercised
        pass

    def close(self) -> None:
        self.closed = True


class ClosePayload:
    """Mimics the ``Close`` object carried by a websocket close frame."""

    def __init__(self, code: int, reason: str = "") -> None:
        self.code = code
        self.reason = reason


class CloseFrame:
    def __init__(self, code: int, reason: str = "") -> None:
        self.data = ClosePayload(code, reason)


def text_frame(payload: Any) -> Tuple[int, str]:
    return (0x1, payload if isinstance(payload, str) else json.dumps(payload))


def hello_frame(
    *,
    obs_version: str = "31.1.1",
    ws_version: str = "5.5.2",
    rpc_version: int = 1,
    authentication: Optional[Dict[str, str]] = None,
) -> Tuple[int, str]:
    data: Dict[str, Any] = {
        "obsStudioVersion": obs_version,
        "obsWebSocketVersion": ws_version,
        "rpcVersion": rpc_version,
    }
    if authentication is not None:
        data["authentication"] = authentication
    return text_frame({"op": 0, "d": data})


def identified_frame(negotiated: int = 1) -> Tuple[int, str]:
    return text_frame({"op": 2, "d": {"negotiatedRpcVersion": negotiated}})


def request_response(
    request_id: str,
    *,
    ok: bool = True,
    code: int = 100,
    comment: str = "",
    response_data: Optional[Dict[str, Any]] = None,
) -> Tuple[int, str]:
    data: Dict[str, Any] = {
        "requestType": "X",
        "requestId": request_id,
        "requestStatus": {"result": ok, "code": code},
    }
    if comment:
        data["requestStatus"]["comment"] = comment
    if response_data is not None:
        data["responseData"] = response_data
    return text_frame({"op": 7, "d": data})


# --------------------------------------------------------------------------- #
# Scripted transport
# --------------------------------------------------------------------------- #
class FakeTransport(Transport):
    """Scripted transport; records every call it receives."""

    def __init__(
        self,
        responses: Optional[Dict[str, Any]] = None,
        *,
        obs_version: str = "31.1.1",
        obs_websocket_version: str = "5.5.2",
        rpc_version: int = 1,
        available_requests: Optional[Sequence[str]] = None,
        hello: Optional[Dict[str, Any]] = None,
        unknown_request_error: bool = True,
    ) -> None:
        self.responses: Dict[str, Any] = dict(responses or {})
        self.calls: List[Tuple[str, Dict[str, Any]]] = []
        self.connected = False
        self.closed = False
        self._obs_version = obs_version
        self._obs_websocket_version = obs_websocket_version
        self._rpc_version = rpc_version
        self._available_requests = list(available_requests) if available_requests is not None else None
        self.hello = hello or {
            "obsStudioVersion": obs_version,
            "obsWebSocketVersion": obs_websocket_version,
            "rpcVersion": rpc_version,
        }
        self._unknown_request_error = unknown_request_error

    # -- Transport ----------------------------------------------------------
    def connect(self) -> None:
        self.connected = True

    def call(self, request_type: str, request_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self.calls.append((request_type, dict(request_data or {})))
        if request_type not in self.responses:
            if self._unknown_request_error:
                raise ObsRpcError(request_type, 204, "Unknown request type")
            return {}
        entry = self.responses[request_type]
        if callable(entry):
            entry = entry(request_data)
        if isinstance(entry, list):
            if not entry:
                raise ObsRpcError(request_type, 205, "script exhausted")
            entry = entry.pop(0)
        if isinstance(entry, ObsRpcError):
            raise entry
        if isinstance(entry, CaptureError):
            raise entry
        if entry is None:
            return {}
        return dict(entry)

    def close(self) -> None:
        self.closed = True

    # -- capabilities -------------------------------------------------------
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

    # -- helpers ------------------------------------------------------------
    def calls_of(self, request_type: str) -> List[Dict[str, Any]]:
        return [data for name, data in self.calls if name == request_type]

    def called(self, request_type: str) -> bool:
        return any(name == request_type for name, _ in self.calls)

    def set(self, request_type: str, response: Any) -> None:
        self.responses[request_type] = response


# --------------------------------------------------------------------------- #
# Simulated OBS instance
# --------------------------------------------------------------------------- #
class FakeObs:
    """A small model of one OBS session, good enough for the refusal paths."""

    def __init__(
        self,
        *,
        obs_version: str = "31.1.1",
        ws_version: str = "5.5.2",
        rpc_version: int = 1,
        profile: str = DEFAULT_PROFILE,
        scene_collection: str = DEFAULT_COLLECTION,
        scene: str = DEFAULT_SCENE,
        source_name: str = DEFAULT_SOURCE_NAME,
        source_exists: bool = True,
        source_kind: str = "window_capture",
        window_string: str = WINDOW_STRING,
        source_settings: Optional[Dict[str, Any]] = None,
        default_settings: Optional[Dict[str, Any]] = None,
        capture_audio_supported: bool = True,
        extra_scene_items: Optional[Sequence[Dict[str, Any]]] = None,
        special_inputs: Optional[Dict[str, Any]] = None,
        input_kinds: Optional[Sequence[str]] = None,
        streaming: bool = False,
        recording: bool = False,
        replay_buffer: bool = False,
        virtual_cam: bool = False,
        record_directory: str = DEFAULT_RECORD_DIR,
        record_bytes: int = 0,
        total_frames: int = 0,
        advance_bytes: int = 4096,
        advance_frames: int = 30,
        advance_on_poll: bool = True,
        source_muted: bool = False,
        source_volume_db: float = 0.0,
        audio_tracks: Optional[Dict[str, Any]] = None,
        available_requests: Optional[Sequence[str]] = None,
        window_items: Optional[Sequence[Dict[str, Any]]] = None,
        window_items_supported: bool = True,
        fail: Optional[Dict[str, Tuple[int, str]]] = None,
        stop_record_output_path: Optional[str] = None,
        start_record_ok: bool = True,
        omit_fields: Optional[Dict[str, Sequence[str]]] = None,
        mistype_fields: Optional[Dict[str, Tuple[str, Any]]] = None,
        screenshot_supported: bool = True,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self.obs_version = obs_version
        self.ws_version = ws_version
        self.rpc_version = rpc_version
        self.profile = profile
        self.scene_collection = scene_collection
        self.scene = scene
        self.source_name = source_name
        self.source_exists = source_exists
        self.source_kind = source_kind
        self.window_string = window_string
        self.capture_audio_supported = capture_audio_supported
        self.extra_scene_items = list(extra_scene_items or [])
        # Default models a *properly prepared* dedicated profile: OBS's global
        # Desktop Audio / Mic inputs are disabled (null).  Leaving them
        # configured is exactly what the isolation check must refuse, so tests
        # that want the busy case pass them in explicitly.
        self.special_inputs = dict(
            special_inputs
            if special_inputs is not None
            else {"desktop1": None, "desktop2": None, "desktop3": None,
                  "mic1": None, "mic2": None, "mic3": None, "mic4": None}
        )
        self.input_kinds = list(input_kinds or ["window_capture", "game_capture", "monitor_capture", "wasapi_output_capture", "wasapi_input_capture"])
        self.streaming = streaming
        self.recording = recording
        self.replay_buffer = replay_buffer
        self.virtual_cam = virtual_cam
        self.record_directory = record_directory
        self.record_bytes = record_bytes
        self.total_frames = total_frames
        self.advance_bytes = advance_bytes
        self.advance_frames = advance_frames
        self.advance_on_poll = advance_on_poll
        self.source_muted = source_muted
        self.source_volume_db = source_volume_db
        self.audio_tracks = dict(audio_tracks or {"track1": True, "track2": False, "track3": False, "track4": False, "track5": False, "track6": False})
        self.available_requests = list(available_requests) if available_requests is not None else list(DEFAULT_AVAILABLE_REQUESTS)
        self.window_items_supported = window_items_supported
        self.fail = dict(fail or {})
        self.start_record_ok = start_record_ok
        self.stop_record_output_path = stop_record_output_path
        self.requests_seen: List[str] = []
        self.mutations: List[str] = []
        #: request type -> fields to drop, to simulate a broken/mis-versioned server
        self.omit_fields = {k: set(v) for k, v in (omit_fields or {}).items()}
        #: "Request.field" -> replacement value, to simulate a wrong type
        self.mistype_fields = dict(mistype_fields or {})
        self.screenshot_supported = screenshot_supported
        self._clock = clock or time.time
        #: When the currently running record output was started (OBS resets both
        #: ``outputDuration`` and ``outputBytes`` for a new output).
        self.recording_started_at: Optional[float] = None

        # Exactly the defaults window-capture.c sets: method, force_sdr,
        # client_area.  Nothing else -- notably `capture_audio` has NO default in
        # the official source, which is why a defaults-based whitelist would drop
        # the one key that makes a recording audible.
        defaults: Dict[str, Any] = {
            "method": 0,          # METHOD_AUTO
            "force_sdr": False,
            "client_area": True,
        }
        self.default_settings = dict(default_settings if default_settings is not None else defaults)

        # The source's own stored settings.  Default models an operator who has
        # already pointed the capture at a window and enabled its audio; pass
        # source_settings={} to model a source that has never been configured.
        if source_settings is not None:
            self.source_settings = dict(source_settings)
        else:
            self.source_settings = {"window": window_string, "priority": 2, "capture_audio": True}
        if not capture_audio_supported:
            self.default_settings.pop("capture_audio", None)
            self.source_settings.pop("capture_audio", None)

        self.window_items = list(
            window_items
            if window_items is not None
            else [{"itemName": "[SlayTheSpire2.exe]: Slay the Spire 2", "itemValue": window_string, "itemEnabled": True}]
        )

    # -- model helpers ------------------------------------------------------
    def merged_settings(self) -> Dict[str, Any]:
        merged = dict(self.default_settings)
        merged.update(self.source_settings)
        return merged

    def current_window_string(self) -> str:
        return str(self.merged_settings().get("window") or self.window_string)

    def _scene_items(self) -> List[Dict[str, Any]]:
        items = []
        if self.source_exists:
            items.append(
                {
                    "sceneItemId": 1,
                    "sourceName": self.source_name,
                    "sceneItemEnabled": True,
                    "inputKind": self.source_kind,
                }
            )
        items.extend(self.extra_scene_items)
        return items

    def _maybe_fail(self, request_type: str) -> None:
        if request_type in self.fail:
            code, comment = self.fail[request_type]
            raise ObsRpcError(request_type, code, comment)

    def _respond(self, request_type: str, request_data: Dict[str, Any]) -> Dict[str, Any]:
        self.requests_seen.append(request_type)
        self._maybe_fail(request_type)
        return self._mangle(request_type, self._dispatch(request_type, request_data))

    def _mangle(self, request_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
        for field in self.omit_fields.get(request_type, ()):  # simulate a missing field
            data.pop(field, None)
        for key, value in self.mistype_fields.items():
            owner, _, field = key.partition(".")
            if owner == request_type:
                data[field] = value
        return data

    def _dispatch(self, request_type: str, request_data: Dict[str, Any]) -> Dict[str, Any]:

        if request_type == "GetVersion":
            return {
                "obsVersion": self.obs_version,
                "obsWebSocketVersion": self.ws_version,
                "rpcVersion": self.rpc_version,
                "availableRequests": list(self.available_requests),
                "supportedImageFormats": ["png", "jpg"],
                "platform": "windows",
                "platformDescription": "Windows 11 (10.0)",
            }
        if request_type == "GetStats":
            return {
                "activeFps": 60.0,
                "availableDiskSpace": 500_000_000_000,
                "outputTotalFrames": self.total_frames,
                "outputSkippedFrames": 0,
                "renderTotalFrames": self.total_frames,
                "renderSkippedFrames": 0,
            }
        if request_type == "GetStreamStatus":
            return {"outputActive": self.streaming, "outputReconnecting": False, "outputTimecode": "00:00:00.000", "outputDuration": 0, "outputCongestion": 0.0, "outputBytes": 0, "outputSkippedFrames": 0, "outputTotalFrames": 0}
        if request_type == "GetVirtualCamStatus":
            return {"outputActive": self.virtual_cam}
        if request_type == "GetReplayBufferStatus":
            return {"outputActive": self.replay_buffer}
        if request_type == "GetRecordStatus":
            if self.advance_on_poll and self.recording:
                self.record_bytes += self.advance_bytes
                self.total_frames += self.advance_frames
            if self.recording and self.recording_started_at is None:
                self.recording_started_at = self._clock()
            # A stopped output reports zeroes, exactly like OBS.
            duration = (
                max(0.0, (self._clock() - self.recording_started_at) * 1000.0)
                if self.recording and self.recording_started_at is not None
                else 0.0
            )
            return {
                "outputActive": self.recording,
                "outputPaused": False,
                "outputTimecode": "00:00:10.000",
                "outputDuration": duration,
                "outputBytes": self.record_bytes if self.recording else 0,
            }
        if request_type == "StartRecord":
            if not self.start_record_ok:
                raise ObsRpcError("StartRecord", 500, "Output is already running")
            if self.recording:
                raise ObsRpcError("StartRecord", 500, "Output is already running")
            self.mutations.append("StartRecord")
            self.recording = True
            # A new output starts from zero on both counters.
            self.record_bytes = 0
            self.recording_started_at = self._clock()
            return {"outputActive": True}
        if request_type == "StopRecord":
            if not self.recording:
                raise ObsRpcError("StopRecord", 501, "Output is not running")
            self.mutations.append("StopRecord")
            self.recording = False
            self.recording_started_at = None
            path = self.stop_record_output_path or (self.record_directory.rstrip("\\/") + "\\2026-09-25 18-00-00.mkv")
            return {"outputPath": path}
        if request_type == "GetRecordDirectory":
            return {"recordDirectory": self.record_directory}
        if request_type == "SetRecordDirectory":
            self.mutations.append("SetRecordDirectory")
            self.record_directory = str(request_data.get("recordDirectory") or self.record_directory)
            return {}
        if request_type == "GetProfileList":
            return {"currentProfileName": self.profile, "profiles": [self.profile]}
        if request_type == "GetSceneCollectionList":
            return {"currentSceneCollectionName": self.scene_collection, "sceneCollections": [self.scene_collection]}
        if request_type == "GetSceneList":
            return {
                "currentProgramSceneName": self.scene,
                "currentProgramSceneUuid": "scene-uuid",
                "currentPreviewSceneName": None,
                "currentPreviewSceneUuid": None,
                "scenes": [{"sceneName": self.scene, "sceneUuid": "scene-uuid", "sceneIndex": 0}],
            }
        if request_type == "GetCurrentProgramScene":
            return {"currentProgramSceneName": self.scene, "currentProgramSceneUuid": "scene-uuid"}
        if request_type == "GetSceneItemList":
            return {"sceneItems": self._scene_items()}
        if request_type == "GetInputList":
            if not self.source_exists:
                return {"inputs": []}
            return {"inputs": [{"inputName": self.source_name, "inputKind": self.source_kind, "unversionedInputKind": self.source_kind}]}
        if request_type == "GetInputKindList":
            return {"inputKinds": list(self.input_kinds)}
        if request_type == "GetInputSettings":
            if not self.source_exists or request_data.get("inputName") != self.source_name:
                raise ObsRpcError("GetInputSettings", 600, "No source was found by the name of `%s`." % (request_data.get("inputName"),))
            return {"inputSettings": dict(self.source_settings), "inputKind": self.source_kind}
        if request_type == "GetInputDefaultSettings":
            # Only window_capture has a modelled default set; other kinds return
            # an empty object, which is what a real OBS does for a kind whose
            # defaults are all implicit.
            if request_data.get("inputKind") == "window_capture":
                return {"defaultInputSettings": dict(self.default_settings)}
            return {"defaultInputSettings": {}}
        if request_type == "GetInputMute":
            return {"inputMuted": self.source_muted}
        if request_type == "GetInputVolume":
            return {"inputVolumeMul": 1.0, "inputVolumeDb": self.source_volume_db}
        if request_type == "GetInputAudioTracks":
            return {"inputAudioTracks": dict(self.audio_tracks)}
        if request_type == "GetSpecialInputs":
            return dict(self.special_inputs)
        if request_type == "GetSourceScreenshot":
            if not self.screenshot_supported:
                raise ObsRpcError("GetSourceScreenshot", 204, "Unknown request type")
            return {"imageData": "iVBORw0KGgoAAAANSUhEUg==", "imageFormat": "png"}
        if request_type == "GetSourceActive":
            return {"videoActive": True, "videoShowing": True}
        if request_type == "GetInputPropertiesListPropertyItems":
            if request_data.get("propertyName") != "window":
                raise ObsRpcError("GetInputPropertiesListPropertyItems", 600, "No property found")
            if not self.window_items_supported:
                raise ObsRpcError("GetInputPropertiesListPropertyItems", 606, "Resource is not configurable")
            return {"propertyItems": [dict(item) for item in self.window_items]}
        if request_type == "CreateInput":
            self.mutations.append("CreateInput")
            if request_data.get("inputName") == self.source_name:
                self.source_exists = True
                self.source_kind = str(request_data.get("inputKind") or self.source_kind)
                settings = request_data.get("inputSettings")
                if isinstance(settings, dict):
                    self.source_settings.update(settings)
            return {"inputUuid": "input-uuid", "sceneItemId": 1}
        if request_type == "SetInputSettings":
            self.mutations.append("SetInputSettings")
            if request_data.get("inputName") != self.source_name or not self.source_exists:
                raise ObsRpcError("SetInputSettings", 600, "No source was found by that name")
            settings = request_data.get("inputSettings")
            if isinstance(settings, dict):
                self.source_settings.update(settings)
            return {}
        if request_type == "SetCurrentProgramScene":
            self.mutations.append(request_type)
            self.scene = str(request_data.get("sceneName") or self.scene)
            return {}
        raise ObsRpcError(request_type, 204, "Unknown request type")

    def transport(self, **kwargs: Any) -> FakeTransport:
        responses: Dict[str, Any] = {}
        for name in set(self.available_requests) | {
            "GetVersion",
            "GetStats",
            "GetRecordStatus",
            "StartRecord",
            "StopRecord",
            "GetRecordDirectory",
            "GetInputPropertiesListPropertyItems",
        }:
            responses[name] = (lambda rt: (lambda data: self._respond(rt, data)))(name)
        transport = FakeTransport(
            responses,
            obs_version=self.obs_version,
            obs_websocket_version=self.ws_version,
            rpc_version=self.rpc_version,
            available_requests=self.available_requests,
            **kwargs,
        )
        return transport
