"""The Windows/OBS backend: capabilities / targets / preflight / start / status / stop.

One explicit config object in, one JSON-shaped envelope out.  No machine-wide
config is read; no global state is mutated beyond the dedicated OBS profile,
scene collection, scene and source that the caller names.

Honesty rules baked into the code (not just the docs):

* ``preflight`` is read-only: it never creates, changes or deletes an OBS
  object and never switches profile / scene collection.
* ``StartRecord`` returning successfully is **not** evidence of a picture.  The
  recording only counts once ``outputBytes`` (or the frame counter) has moved
  past the pre-start baseline; otherwise the call fails with ``NO_PROGRESS`` and
  the output this task started is cleaned up.
* ``StopRecord`` is only issued for an output this task provably owns: matching
  session token, non-stale state, and bytes beyond our own baseline.
* The returned ``outputPath`` must live inside the dedicated directory, must
  exist, must be non-empty and must not be a file that already existed before
  the recording started (the user's master copy).
* HWND pinning is not claimed, because OBS does not offer it.
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import policy, validate
from .errors import (
    AUDIO_NOT_CAPTURED,
    BAD_RESPONSE,
    CONFIG_INVALID,
    NO_PROGRESS,
    NOT_OWNER,
    OBS_BUSY,
    OUTPUT_PATH_INVALID,
    SCENE_MISMATCH,
    SOURCE_DRIFT,
    STALE_STATE,
    TARGET_NOT_FOUND,
    CaptureError,
    INTERNAL,
)
from .state import InstanceLock, OwnershipStore, validate_run_id
from .targets import (
    PRIORITY_EXE,
    SUPPORTED_MATCH_DIMENSIONS,
    UNSUPPORTED_MATCH_DIMENSIONS,
    WindowTarget,
    parse_window_string,
    redact_window_item,
)
from .transport import ObsRpcError, ObsWebSocketTransport, Transport, resolve_password

SCHEMA = "agent-capture-win/1"
BACKEND_NAME = "obs-websocket"
#: The id the main repo's platform registry uses for this backend
#: (``scripts/capture_targets.py`` -> ``BACKENDS["windows"].backend``).
BACKEND_ID = "windows-obs-websocket"

LEVEL_NONE = "none"
LEVEL_MOCK = "mock"
LEVEL_LIVE_CONNECTED = "live_connected"
#: Set only after a real recording was stopped *and* the file it produced was
#: verified to exist, be non-empty, live inside the dedicated directory and not
#: be a pre-existing master.  It deliberately does **not** claim that the
#: picture or the audio content is correct -- see the ``verification`` block.
#:
#: This is *not* the main CLI's ``verified_level``.  That one stays ``none`` on
#: Windows until there is real-machine evidence of a correct capture.
LEVEL_LIVE_OUTPUT_VERIFIED = "live_output_file_verified"

#: Independent verification layers.  They are reported separately on purpose:
#: a single ladder invites reading "bytes went up" as "the recording is good".
VERIFICATION_LAYERS = ("protocol", "artifact", "picture", "audio")

#: What an advanced byte counter actually proves.
EVIDENCE_SCOPE_WRITE_PROGRESS = "output_write_progress_only__not_picture_proof"

#: How long start/stop wait for the per-instance mutation lock.
INSTANCE_LOCK_TIMEOUT_S = 10.0

WINDOW_PROPERTY = "window"

#: Settings this backend writes when it prepares its own dedicated source.
DESIRED_SOURCE_SETTINGS = ("window", "priority", "method", "capture_audio", "cursor", "client_area", "compatibility", "force_sdr")

_CONFIG_FIELDS = {
    "host": str,
    "port": int,
    "password_env": (str, type(None)),
    "password_file": (str, type(None)),
    "allow_non_loopback": bool,
    "profile": (str, type(None)),
    "scene_collection": (str, type(None)),
    "scene": (str, type(None)),
    "require_dedicated_profile": bool,
    "source_name": str,
    "target": (dict, type(None)),
    "record_dir": (str, type(None)),
    "capture_audio": bool,
    "allow_desktop_audio": bool,
    "allow_mic": bool,
    "allow_partial_match": bool,
    "allow_streaming": bool,
    "create_source_if_missing": bool,
    "start_evidence_timeout_s": (int, float),
    "poll_interval_s": (int, float),
    "max_record_seconds": (int, float),
    "connect_timeout": (int, float),
    "request_timeout": (int, float),
    "run_id": (str, type(None)),
    "session_token": (str, type(None)),
    "state_dir": (str, type(None)),
    "reveal_window_details": bool,
    "verify_output_file": bool,
}


@dataclass
class Config:
    """Every knob the backend has.  Nothing is read from the machine."""

    host: str = "127.0.0.1"
    port: int = 4455
    #: Name of the env var holding the password.  ``None`` means "no password
    #: configured": the transport still fails closed if the server demands auth.
    password_env: Optional[str] = None
    password_file: Optional[str] = None
    allow_non_loopback: bool = False

    profile: Optional[str] = None
    scene_collection: Optional[str] = None
    scene: Optional[str] = None
    require_dedicated_profile: bool = True

    source_name: str = "agent-capture-window-capture"
    target: Optional[Dict[str, Any]] = None
    record_dir: Optional[str] = None

    capture_audio: bool = True
    allow_desktop_audio: bool = False
    allow_mic: bool = False
    allow_partial_match: bool = False
    allow_streaming: bool = False
    create_source_if_missing: bool = True

    start_evidence_timeout_s: float = 12.0
    poll_interval_s: float = 0.5
    max_record_seconds: float = 3600.0
    connect_timeout: float = 6.0
    request_timeout: float = 8.0

    run_id: Optional[str] = None
    session_token: Optional[str] = None
    state_dir: Optional[str] = None

    reveal_window_details: bool = False
    verify_output_file: bool = True

    @classmethod
    def from_dict(cls, raw: Optional[Dict[str, Any]]) -> "Config":
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise CaptureError(CONFIG_INVALID, "config must be an object", {"got": type(raw).__name__})
        unknown = sorted(set(raw) - set(_CONFIG_FIELDS))
        if unknown:
            raise CaptureError(
                CONFIG_INVALID,
                "unknown config field(s): %s" % (", ".join(unknown),),
                {"unknown": unknown, "known": sorted(_CONFIG_FIELDS)},
            )
        kwargs: Dict[str, Any] = {}
        for key, expected in _CONFIG_FIELDS.items():
            if key not in raw:
                continue
            value = raw[key]
            if isinstance(value, bool) and expected is int:
                raise CaptureError(CONFIG_INVALID, "%s must be an integer" % (key,))
            if not isinstance(value, expected):
                raise CaptureError(
                    CONFIG_INVALID,
                    "%s has the wrong type" % (key,),
                    {"expected": str(expected), "got": type(value).__name__},
                )
            kwargs[key] = value

        config = cls(**kwargs)
        if config.port <= 0 or config.port > 65535:
            raise CaptureError(CONFIG_INVALID, "port is out of range", {"port": config.port})
        for name in ("start_evidence_timeout_s", "max_record_seconds", "poll_interval_s", "connect_timeout", "request_timeout"):
            value = getattr(config, name)
            if value != value or value in (float("inf"), float("-inf")):
                raise CaptureError(CONFIG_INVALID, "%s must be finite" % (name,))
        if config.poll_interval_s <= 0:
            raise CaptureError(CONFIG_INVALID, "poll_interval_s must be > 0")
        if config.start_evidence_timeout_s < 0:
            raise CaptureError(CONFIG_INVALID, "start_evidence_timeout_s must be >= 0")
        if config.max_record_seconds <= 0:
            raise CaptureError(CONFIG_INVALID, "max_record_seconds must be > 0")
        if config.password_env and config.password_file:
            raise CaptureError(
                CONFIG_INVALID,
                "give either password_env or password_file, not both",
                {"password_env": config.password_env, "password_file": config.password_file},
            )
        if config.run_id is not None:
            validate_run_id(config.run_id)
        return config

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for key in _CONFIG_FIELDS:
            value = getattr(self, key)
            if key in ("password_env", "password_file"):
                # Names only; never the secret itself.
                out[key] = value
            elif key == "session_token":
                out[key] = "<redacted>" if value else None
            else:
                out[key] = value
        return out


@dataclass
class Inspection:
    """Everything a read-only pass learned about the OBS session."""

    capabilities: Optional[policy.Capabilities] = None
    activity: Optional[policy.ObsActivity] = None
    session: Dict[str, Any] = field(default_factory=dict)
    source: Dict[str, Any] = field(default_factory=dict)
    audio: Optional[policy.AudioIsolation] = None
    target: Optional[WindowTarget] = None
    window_items: Optional[List[Dict[str, Any]]] = None
    record_directory: Optional[str] = None
    record_status: Dict[str, Any] = field(default_factory=dict)
    stats: Dict[str, Any] = field(default_factory=dict)
    problems: List[CaptureError] = field(default_factory=list)
    #: Audio settings that start() will write before recording (informational).
    audio_not_configured_yet: List[str] = field(default_factory=list)

    def add_problem(self, error: CaptureError) -> None:
        self.problems.append(error)

    def collect(self, fn: Callable[[], Any]) -> Any:
        """Run a check; capture its refusal instead of aborting the pass."""
        try:
            return fn()
        except CaptureError as exc:
            self.add_problem(exc)
            return None

    @property
    def ok(self) -> bool:
        return not self.problems


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_file_probe(path: str) -> Dict[str, Any]:
    try:
        stat = os.stat(path)
    except OSError:
        return {"exists": False, "size_bytes": None, "mtime": None}
    return {"exists": True, "size_bytes": stat.st_size, "mtime": stat.st_mtime}


def _default_dir_probe(path: str) -> Optional[List[str]]:
    try:
        return sorted(os.listdir(path))
    except OSError:
        return None


class ObsBackend:
    """The OBS WebSocket backend."""

    backend = BACKEND_NAME

    def __init__(
        self,
        config: Optional[Config] = None,
        *,
        transport: Optional[Transport] = None,
        transport_factory: Optional[Callable[[Config], Transport]] = None,
        verification_level: Optional[str] = None,
        file_probe: Callable[[str], Dict[str, Any]] = _default_file_probe,
        dir_probe: Callable[[str], Optional[List[str]]] = _default_dir_probe,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        store: Optional[OwnershipStore] = None,
    ) -> None:
        self.config = config or Config()
        self._transport = transport
        self._transport_factory = transport_factory
        self._injected = transport is not None
        self._level = verification_level or (LEVEL_MOCK if self._injected else LEVEL_NONE)
        self._file_probe = file_probe
        self._dir_probe = dir_probe
        self._clock = clock
        self._sleep = sleep
        self._store = store or OwnershipStore(self.config.state_dir)
        self._session_token: Optional[str] = None
        self._settings_snapshot: Optional[Dict[str, Any]] = None
        self._baseline_bytes = 0
        self._baseline_frames = 0
        self._started_at = 0.0
        self._window_string = ""
        self._preexisting: List[str] = []

    # ------------------------------------------------------------------ #
    # plumbing
    # ------------------------------------------------------------------ #
    def _make_transport(self) -> Transport:
        if self._transport_factory is not None:
            return self._transport_factory(self.config)
        password = resolve_password(self.config.password_env, self.config.password_file)
        return ObsWebSocketTransport(
            host=self.config.host,
            port=self.config.port,
            password=password,
            allow_non_loopback=self.config.allow_non_loopback,
            connect_timeout=self.config.connect_timeout,
            request_timeout=self.config.request_timeout,
        )

    def connect(self) -> Transport:
        if self._transport is None:
            self._transport = self._make_transport()
            self._transport.connect()
            if not self._injected:
                self._level = LEVEL_LIVE_CONNECTED
        return self._transport

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()

    @property
    def verification_level(self) -> str:
        """The highest level this instance actually reached."""
        return self._level

    @property
    def verification(self) -> Dict[str, Any]:
        """Per-layer claims for the level this instance actually reached."""
        return self._verification()

    def _verification(self) -> Dict[str, Any]:
        """Per-layer claims, kept apart so none of them implies another."""
        protocol = "mock" if self._level == LEVEL_MOCK else (
            "live_connected" if self._level in (LEVEL_LIVE_CONNECTED, LEVEL_LIVE_OUTPUT_VERIFIED)
            else "not_connected"
        )
        artifact = "file_verified" if self._level == LEVEL_LIVE_OUTPUT_VERIFIED else "unverified"
        return {
            "protocol": protocol,
            "artifact": artifact,
            # Neither of these can be established over obs-websocket.
            "picture": "unverified",
            "audio": "settings_only" if self.config.capture_audio else "unverified",
            "claims": {
                "protocol": "obs-websocket requests and responses behaved as specified",
                "artifact": "the output file exists, is non-empty, is inside the dedicated "
                            "directory and did not overwrite a pre-existing file",
                "picture": "NOT CLAIMED: nothing here proves which window was captured, nor that "
                           "the picture moves",
                "audio": "NOT CLAIMED: only that the source is configured to capture its own "
                         "application audio; obs-websocket exposes no audio samples",
            },
        }

    def _call(self, request_type: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        transport = self.connect()
        try:
            return transport.call(request_type, data)
        except ObsRpcError as exc:
            raise policy.classify_rpc_error(exc) from exc

    def _try(self, request_type: str, data: Optional[Dict[str, Any]] = None) -> Tuple[bool, Any]:
        try:
            return True, self._call(request_type, data)
        except CaptureError as exc:
            return False, exc

    def _envelope(self, command: str) -> Dict[str, Any]:
        return {
            "schema": SCHEMA,
            "backend": self.backend,
            "backend_id": BACKEND_ID,
            "command": command,
            "ok": False,
            "verification_level": self._level,
            "verification": self._verification(),
            "obs": {},
            "target": {},
            "actual_source": None,
            "recording": {
                "active": False,
                "paused": False,
                "duration_ms": None,
                "bytes": None,
                "frames": None,
                "advanced": False,
                "evidence": "none",
            },
            "output": {"dir": self.config.record_dir, "path": None, "exists": None, "size_bytes": None, "verified": False},
            "error": None,
            "notes": [],
            "checked_at": _utc_now(),
        }

    def _fail(self, envelope: Dict[str, Any], error: CaptureError) -> Dict[str, Any]:
        envelope["ok"] = False
        envelope["error"] = error.to_dict()
        envelope["verification_level"] = self._level
        envelope["verification"] = self._verification()
        return envelope

    # ------------------------------------------------------------------ #
    # read-only inspection
    # ------------------------------------------------------------------ #
    def _capabilities(self) -> policy.Capabilities:
        version = self._call("GetVersion")
        kinds: Optional[List[str]] = None
        ok, data = self._try("GetInputKindList")
        if ok and isinstance(data, dict):
            raw_kinds = data.get("inputKinds")
            if isinstance(raw_kinds, list):
                kinds = [str(kind) for kind in raw_kinds]
        # availableRequests is a documented GetVersion response field.  Requiring
        # it means capability questions are answered from the server's own list
        # rather than from an assumption that everything is supported.
        requests = [
            validate.require_str({"v": entry}, "v", "GetVersion.availableRequests")
            for entry in validate.require_list(version, "availableRequests", "GetVersion")
        ]
        rpc_version = validate.optional_number(version, "rpcVersion", "GetVersion")
        return policy.capabilities_from(
            obs_version=validate.require_str(version, "obsVersion", "GetVersion"),
            obs_websocket_version=validate.require_str(version, "obsWebSocketVersion", "GetVersion"),
            rpc_version=int(rpc_version) if rpc_version is not None else None,
            available_requests=requests,
            input_kinds=kinds,
        )

    def _activity(self) -> policy.ObsActivity:
        """Read the four output states with strict field validation.

        A missing or mistyped ``outputActive`` is a broken response, not
        "idle": treating it as idle is how an in-use OBS gets taken over.
        """
        stream = self._call("GetStreamStatus")
        record = self._call("GetRecordStatus")
        replay = self._call("GetReplayBufferStatus")
        virtual_cam = self._call("GetVirtualCamStatus")
        return policy.ObsActivity(
            streaming=validate.require_bool(stream, "outputActive", "GetStreamStatus"),
            recording=validate.require_bool(record, "outputActive", "GetRecordStatus"),
            replay_buffer=validate.require_bool(replay, "outputActive", "GetReplayBufferStatus"),
            virtual_cam=validate.require_bool(virtual_cam, "outputActive", "GetVirtualCamStatus"),
        )

    def _record_status(self) -> Dict[str, Any]:
        """Validated ``GetRecordStatus``.

        ``outputActive``/``outputDuration``/``outputBytes`` are required and
        type-checked because every ownership and evidence decision rests on
        them.  ``outputPaused`` is informational, so it may be absent, but a
        mistyped value is still rejected.
        """
        data = self._call("GetRecordStatus")
        return {
            "outputActive": validate.require_bool(data, "outputActive", "GetRecordStatus"),
            "outputPaused": bool(validate.optional_bool(data, "outputPaused", "GetRecordStatus")),
            "outputDuration": validate.require_number(data, "outputDuration", "GetRecordStatus"),
            "outputBytes": validate.require_number(data, "outputBytes", "GetRecordStatus"),
        }

    def _stats_frames(self) -> Optional[int]:
        """``GetStats.outputTotalFrames`` -- informational only, never evidence."""
        ok, stats = self._try("GetStats")
        if not ok or not isinstance(stats, dict):
            return None
        value = validate.optional_number(stats, "outputTotalFrames", "GetStats")
        return int(value) if value is not None else None

    def _session(self) -> Dict[str, Any]:
        profiles = self._call("GetProfileList")
        collections = self._call("GetSceneCollectionList")
        scenes = self._call("GetSceneList")
        names: List[str] = []
        for item in validate.require_list(scenes, "scenes", "GetSceneList"):
            if not isinstance(item, dict):
                raise CaptureError(
                    BAD_RESPONSE, "GetSceneList returned a non-object scene entry", {"entry": repr(item)[:120]}
                )
            names.append(validate.require_str(item, "sceneName", "GetSceneList"))
        return {
            "profile": validate.require_str(profiles, "currentProfileName", "GetProfileList"),
            "scene_collection": validate.require_str(
                collections, "currentSceneCollectionName", "GetSceneCollectionList"
            ),
            # Documented as nullable when the canvas is not the main one.
            "program_scene": validate.optional_str(scenes, "currentProgramSceneName", "GetSceneList"),
            "scenes": names,
        }

    def _source_state(self) -> Dict[str, Any]:
        listing = self._call("GetInputList")
        inputs = [i for i in (listing.get("inputs") or []) if isinstance(i, dict)]
        match = next((i for i in inputs if i.get("inputName") == self.config.source_name), None)
        if match is None:
            return {"exists": False, "name": self.config.source_name}
        kind = str(match.get("inputKind") or "")
        settings_response = self._call("GetInputSettings", {"inputName": self.config.source_name})
        raw_settings = validate.require_dict(settings_response, "inputSettings", "GetInputSettings")
        defaults: Dict[str, Any] = {}
        if not kind or kind == policy.WINDOW_CAPTURE_KIND:
            # Only a window_capture source has settings this backend understands;
            # asking for another kind's defaults would be a pointless call.
            defaults_response = self._call("GetInputDefaultSettings", {"inputKind": policy.WINDOW_CAPTURE_KIND})
            defaults = validate.require_dict(
                defaults_response, "defaultInputSettings", "GetInputDefaultSettings"
            )
        effective = policy.merge_input_settings(
            raw_settings if isinstance(raw_settings, dict) else {},
            defaults if isinstance(defaults, dict) else {},
        )
        return {
            "exists": True,
            "name": self.config.source_name,
            "kind": kind or str(settings_response.get("inputKind") or ""),
            "settings_raw": raw_settings if isinstance(raw_settings, dict) else {},
            "settings_defaults": defaults if isinstance(defaults, dict) else {},
            "settings_effective": effective,
        }

    def _scene_items(self, scene: Optional[str]) -> List[Dict[str, Any]]:
        if not scene:
            return []
        response = self._call("GetSceneItemList", {"sceneName": scene})
        return [i for i in (response.get("sceneItems") or []) if isinstance(i, dict)]

    def _window_items(self) -> Optional[List[Dict[str, Any]]]:
        ok, data = self._try(
            "GetInputPropertiesListPropertyItems",
            {"inputName": self.config.source_name, "propertyName": WINDOW_PROPERTY},
        )
        if not ok or not isinstance(data, dict):
            return None
        items = validate.require_list(data, "propertyItems", "GetInputPropertiesListPropertyItems")
        return [i for i in items if isinstance(i, dict)]

    def _record_directory(self, capabilities: policy.Capabilities) -> Optional[str]:
        if not capabilities.supports_request("GetRecordDirectory"):
            return None
        ok, data = self._try("GetRecordDirectory")
        if not ok or not isinstance(data, dict):
            return None
        value = validate.optional_str(data, "recordDirectory", "GetRecordDirectory")
        return value or None

    def inspect(
        self,
        *,
        enforce: bool = True,
        for_start: bool = False,
        enforce_idle: bool = True,
        allow_recording: bool = False,
        allow_streaming: Optional[bool] = None,
    ) -> Inspection:
        """One read-only pass; never mutates OBS state.

        ``for_start`` suppresses only the "the dedicated source does not exist
        yet" problem, because ``start()`` is allowed to create it.  Every other
        refusal still applies.
        """
        inspection = Inspection()
        capabilities = self._capabilities()
        inspection.capabilities = capabilities
        inspection.activity = self._activity()
        inspection.session = self._session()

        if enforce_idle:
            # ``allow_recording`` is only ever passed by status(), and only for a
            # record output whose session token matches ours; the caller then
            # proves identity separately.  Streaming / replay / virtual camera are
            # always checked and are never exempted by it.
            inspection.collect(
                lambda: policy.check_idle(
                    inspection.activity,
                    allow_streaming=(
                        self.config.allow_streaming if allow_streaming is None else allow_streaming
                    ),
                    allow_recording=allow_recording,
                )
            )
        inspection.collect(
            lambda: policy.check_session_selection(
                profile=self.config.profile,
                scene_collection=self.config.scene_collection,
                scene=self.config.scene,
                current_profile=inspection.session.get("profile"),
                current_scene_collection=inspection.session.get("scene_collection"),
                current_scene=inspection.session.get("program_scene"),
                require_dedicated=self.config.require_dedicated_profile,
            )
        )

        inspection.source = self._source_state()
        if not inspection.source.get("exists"):
            if not for_start:
                inspection.add_problem(
                    CaptureError(
                        TARGET_NOT_FOUND,
                        "the dedicated capture source %r does not exist in this OBS session"
                        % (self.config.source_name,),
                        {"source_name": self.config.source_name, "hint": "start() can create it in a dedicated session"},
                    )
                )
        else:
            kind = str(inspection.source.get("kind") or "")
            if kind and kind != policy.WINDOW_CAPTURE_KIND:
                inspection.add_problem(
                    CaptureError(
                        TARGET_NOT_FOUND,
                        "source %r is a %r, not a window_capture; refusing to record a display or "
                        "game capture as a window" % (self.config.source_name, kind),
                        {"source_name": self.config.source_name, "kind": kind},
                    )
                )

        # Target semantics
        target_raw = self.config.target
        if target_raw is None and inspection.source.get("settings_effective"):
            title, window_class, exe = parse_window_string(
                str(inspection.source["settings_effective"].get("window") or "")
            )
            priority = inspection.source["settings_effective"].get("priority")
            target_raw = {
                "title": title,
                "class": window_class,
                "exe": exe,
                "priority": priority if isinstance(priority, int) else PRIORITY_EXE,
            }
        if target_raw is None:
            inspection.add_problem(CaptureError(TARGET_NOT_FOUND, "no target given and the source has no window set"))
        else:
            try:
                target = WindowTarget.from_config(target_raw)
                target.validate(allow_partial_match=self.config.allow_partial_match)
                inspection.target = target
            except CaptureError as exc:
                inspection.add_problem(exc)

        # Audio isolation (global audio / mic must not be mixed in)
        if inspection.session.get("program_scene"):
            items = self._scene_items(inspection.session["program_scene"])
            special = self._call("GetSpecialInputs")
            kinds_by_name: Dict[str, str] = {}
            for item in items:
                name = item.get("sourceName")
                if name and item.get("inputKind"):
                    kinds_by_name[str(name)] = str(item["inputKind"])
            inspection.audio = inspection.collect(
                lambda: policy.check_audio_isolation(
                    scene_items=items,
                    special_inputs=special,
                    input_kinds_by_name=kinds_by_name,
                    managed_source_name=self.config.source_name,
                    allow_desktop_audio=self.config.allow_desktop_audio,
                    allow_mic=self.config.allow_mic,
                )
            )

        # Audio settings of our own source
        if inspection.source.get("exists"):
            effective = inspection.source.get("settings_effective") or {}
            muted: Optional[bool] = None
            volume_db: Optional[float] = None
            tracks: Optional[Dict[str, Any]] = None
            ok, data = self._try("GetInputMute", {"inputName": self.config.source_name})
            if ok and isinstance(data, dict):
                muted = validate.require_bool(data, "inputMuted", "GetInputMute")
            ok, data = self._try("GetInputVolume", {"inputName": self.config.source_name})
            if ok and isinstance(data, dict):
                volume_db = validate.require_number(data, "inputVolumeDb", "GetInputVolume")
            ok, data = self._try("GetInputAudioTracks", {"inputName": self.config.source_name})
            if ok and isinstance(data, dict):
                tracks = validate.require_dict(data, "inputAudioTracks", "GetInputAudioTracks")

            # ``capture_audio`` deliberately has no default in window-capture.c,
            # so it is absent from GetInputDefaultSettings on every build.  Only
            # an explicit capability signal may be read as "unsupported".
            supports_capture_audio: Optional[bool] = None
            if self.config.capture_audio and inspection.capabilities.supports_window_capture() is False:
                supports_capture_audio = False

            ok_audio, problems, evidence = policy.check_audio_settings(
                effective_settings=effective,
                audio_requested=self.config.capture_audio,
                source_muted=muted,
                source_volume_db=volume_db,
                audio_tracks=tracks,
                capture_audio_supported=supports_capture_audio,
            )
            if inspection.audio is not None:
                inspection.audio.evidence_level = evidence
            if not ok_audio and for_start:
                # start() is about to write these settings itself and then
                # re-verify, so "not configured yet" must not block it.  A build
                # that cannot honour the key is still caught by the re-check.
                inspection.audio_not_configured_yet = problems
            elif not ok_audio:
                inspection.add_problem(
                    CaptureError(
                        AUDIO_NOT_CAPTURED,
                        "; ".join(problems),
                        {"audio": {"muted": muted, "volume_db": volume_db, "tracks": tracks}},
                    )
                )

            inspection.window_items = self._window_items()
            if inspection.target is not None:
                # Called even when the list is unavailable: check_target_present
                # turns "unknown" into a refusal instead of a silent pass.
                inspection.collect(
                    lambda: policy.check_target_present(
                        window_string=inspection.target.to_obs_window_string(),
                        property_items=inspection.window_items,
                    )
                )

        inspection.record_directory = self._record_directory(capabilities)
        # Validated, so a dropped/mistyped outputDuration or outputBytes is a
        # broken response rather than a zero that looks like "nothing recorded".
        inspection.record_status = self._record_status()
        ok, stats = self._try("GetStats")
        inspection.stats = stats if ok and isinstance(stats, dict) else {}

        if enforce and inspection.problems:
            raise inspection.problems[0]
        return inspection

    # ------------------------------------------------------------------ #
    # public commands
    # ------------------------------------------------------------------ #
    def capabilities(self) -> Dict[str, Any]:
        envelope = self._envelope("capabilities")
        try:
            capabilities = self._capabilities()
        except CaptureError as exc:
            return self._fail(envelope, exc)
        finally:
            self.close()

        envelope["obs"] = {
            "version": capabilities.obs_version,
            "websocket_version": capabilities.obs_websocket_version,
            "rpc_version": capabilities.rpc_version,
            "platform": "windows" if capabilities.obs_websocket_version else None,
        }
        envelope["capabilities"] = capabilities.to_dict()
        envelope["platform_capability"] = self._platform_capability()
        envelope["capabilities"].update(
            {
                "backend": BACKEND_NAME,
                "supported_match_dimensions": list(SUPPORTED_MATCH_DIMENSIONS),
                "unsupported_match_dimensions": list(UNSUPPORTED_MATCH_DIMENSIONS),
                "hwnd_pinning": False,
                "whole_screen_fallback": False,
                "audio_granularity": "process",
                "mic_supported": False,
                "desktop_audio_supported": False,
            }
        )
        envelope["ok"] = True
        envelope["verification_level"] = self._level
        return envelope

    def _platform_capability(self) -> Dict[str, Any]:
        """This backend's row in the main repo's platform registry.

        Mirrors the field names ``scripts/capture_targets.py`` uses so the main
        CLI can merge it without translation.  Two entries deliberately differ
        from that registry's current text:

        * ``video_granularities`` is ``["window"]`` only: this backend never
          captures at application granularity, so it does not claim ``"app"``.
        * ``audio_granularities`` is ``["none", "app"]`` because OBS application
          audio capture is process granularity, never window exclusive.
        """
        return {
            "backend": BACKEND_ID,
            "platform": "windows",
            "video_granularities": ["window"],
            "audio_granularities": ["none", "app"],
            "audio_per_window_os_capable": False,
            "verified_level": self._level,
            "verified_scope": (
                "协议与策略已在 macOS 上用真实 WebSocket 连接（模拟 OBS）验证；"
                "未在 Windows 实机验证（tailnet 内那台 Windows 无可用凭据）。"
                "mock 只证明协议/状态机，不证明能录到画面或声音。"
            ),
            "unimplemented": {
                "display": "本后端未实现整屏采集（明确不录全局桌面）。",
                "system": "本后端不录全局桌面音：会把与目标无关的声音混进来。",
                "window_audio": "本后端未实现窗口级音频（走 OBS window_capture 的 "
                                "application audio capture，粒度是应用/进程）。",
                "microphone": "本后端不添加麦克风源。",
            },
            "notes": [
                "不接管正在直播/录制/replay/virtualcam 的 OBS 实例：连接后先查，活跃即拒绝。",
                "不让 OBS 按同类窗口自动重匹配：只按显式 title/class/exe + priority 交给 OBS 语义；",
                "不承诺 HWND pinning：OBS 每次重挂钩都会重新枚举窗口，同名窗口替换无法彻底避免。",
                "Windows 的可执行名匹配不要套 macOS 的 bundle id 规则。",
                "音频是 app/process 粒度，不是窗口独占；obs-websocket 不提供音频采样，"
                "因此只能证明「已配置为采集该应用音频」，不能证明「有声音」。",
            ],
        }

    def targets(self) -> Dict[str, Any]:
        envelope = self._envelope("targets")
        try:
            source = self._source_state()
            window_items = self._window_items()
        except CaptureError as exc:
            return self._fail(envelope, exc)
        finally:
            self.close()

        reveal = self.config.reveal_window_details
        selected = ""
        if source.get("settings_effective"):
            selected = str(source["settings_effective"].get("window") or "")
            title, window_class, exe = parse_window_string(selected)
            priority = source["settings_effective"].get("priority")
            envelope["target"] = {
                "requested": self.config.target,
                "configured": {
                    "title": title,
                    "class": window_class,
                    "exe": exe,
                    "priority": priority if isinstance(priority, int) else None,
                },
                "match_semantics": "obs_window_priority",
                "supported_match_dimensions": list(SUPPORTED_MATCH_DIMENSIONS),
                "unsupported_match_dimensions": list(UNSUPPORTED_MATCH_DIMENSIONS),
                "hwnd_pinning": False,
                "match_risk": "same_title_class_or_exe_window_can_take_over__not_preventable_in_obs",
            }

        candidates: List[Dict[str, Any]] = []
        if window_items is not None:
            for item in window_items:
                value = str(item.get("itemValue") or "")
                candidates.append(redact_window_item(item, revealed=reveal or (value == selected and bool(selected))))

        envelope["targets"] = {
            "configured_window_string": selected if (reveal and selected) else None,
            "candidates": candidates,
            "candidate_count": len(candidates),
            "enumeration_available": window_items is not None,
            "redacted": not reveal,
            "note": (
                "candidate windows come from OBS's own window list property; titles and "
                "executables of unrelated windows are fingerprinted unless reveal_window_details=true"
                if window_items is not None
                else "OBS did not expose its window list property; candidates cannot be enumerated "
                "over obs-websocket in this session"
            ),
        }
        envelope["ok"] = True
        envelope["verification_level"] = self._level
        return envelope

    def preflight(self) -> Dict[str, Any]:
        envelope = self._envelope("preflight")
        try:
            inspection = self.inspect(enforce=False)
        except CaptureError as exc:
            return self._fail(envelope, exc)
        finally:
            self.close()

        # The destination has to be provable before anything records, so preflight
        # reports it too rather than leaving it to be discovered afterwards.
        inspection.collect(
            lambda: policy.check_record_directory(inspection.record_directory, self.config.record_dir)
        )

        self._fill(envelope, inspection)
        checks = {
            "idle": not any(
                getattr(inspection.activity, name, False)
                for name in ("streaming", "recording", "replay_buffer", "virtual_cam")
            ),
            "dedicated_session": bool(inspection.session.get("profile"))
            and (not self.config.profile or inspection.session.get("profile") == self.config.profile),
            "source_ready": bool(inspection.source.get("exists")),
            "audio_isolated": inspection.audio is not None and not inspection.audio.offending_global and not inspection.audio.offending_mic,
            # "unknown" is deliberately not True: when OBS cannot list windows the
            # target is unconfirmed, and that refuses the run (see check_target_present).
            "target_present": (
                "unknown"
                if inspection.window_items is None
                else (
                    False
                    if inspection.target is None
                    else any(
                        str(item.get("itemValue") or "") == inspection.target.to_obs_window_string()
                        for item in inspection.window_items
                    )
                )
            ),
            "record_directory_legal": inspection.record_directory is not None
            and policy.is_absolute_path(inspection.record_directory),
            "read_only": True,
        }
        envelope["checks"] = checks
        envelope["read_only"] = True
        if inspection.ok:
            envelope["ok"] = True
            envelope["verification_level"] = self._level
            envelope["notes"].append(
                "preflight performed no writes: no profile switch, no scene change, no source creation"
            )
        else:
            self._fail(envelope, inspection.problems[0])
            envelope["problems"] = [p.to_dict() for p in inspection.problems]
        return envelope

    def _fill(self, envelope: Dict[str, Any], inspection: Inspection) -> None:
        capabilities = inspection.capabilities
        if capabilities is not None:
            envelope["obs"] = {
                "version": capabilities.obs_version,
                "websocket_version": capabilities.obs_websocket_version,
                "rpc_version": capabilities.rpc_version,
                "platform": "windows",
            }
        if inspection.target is not None:
            envelope["target"] = {
                "requested": self.config.target or inspection.target.to_dict(),
                "match_semantics": "obs_window_priority",
                "window_string": inspection.target.to_obs_window_string(),
                "priority": inspection.target.priority_name,
                "hwnd_pinning": False,
                "match_risk": inspection.target.match_risk(),
            }
        if inspection.source.get("exists"):
            effective = inspection.source.get("settings_effective") or {}
            envelope["actual_source"] = {
                "name": inspection.source.get("name"),
                "kind": inspection.source.get("kind"),
                "scene": inspection.session.get("program_scene"),
                "settings_effective": {
                    key: effective.get(key)
                    for key in DESIRED_SOURCE_SETTINGS
                    if key in effective
                },
                "settings_source": "GetInputSettings overlaid on GetInputDefaultSettings",
                "audio_granularity": "process",
            }
        if inspection.audio is not None:
            envelope["audio"] = inspection.audio.to_dict()
        if inspection.record_directory is not None:
            envelope["output"]["dir"] = inspection.record_directory
        status = inspection.record_status or {}
        if status:
            envelope["recording"].update(
                {
                    "active": bool(status.get("outputActive")),
                    "paused": bool(status.get("outputPaused")),
                    "duration_ms": status.get("outputDuration"),
                    "bytes": status.get("outputBytes"),
                    "frames": inspection.stats.get("outputTotalFrames"),
                }
            )
        envelope["session"] = inspection.session
        if inspection.problems:
            envelope["problems"] = [p.to_dict() for p in inspection.problems]

    def start(self) -> Dict[str, Any]:
        envelope = self._envelope("start")
        try:
            return self._start(envelope)
        except CaptureError as exc:
            return self._fail(envelope, exc)
        finally:
            self.close()

    def _start(self, envelope: Dict[str, Any]) -> Dict[str, Any]:
        if not self.config.run_id:
            raise CaptureError(CONFIG_INVALID, "start requires run_id (used for owner-only stop)")

        inspection = self.inspect(enforce=False, for_start=True)
        if inspection.problems:
            raise inspection.problems[0]
        if inspection.activity and inspection.activity.recording:
            raise CaptureError(
                OBS_BUSY,
                "OBS is already recording; this task will not take over an output it did not start",
                {"activity": inspection.activity.to_dict()},
            )
        capabilities = inspection.capabilities
        if capabilities is None:
            raise CaptureError(INTERNAL, "the OBS session reported no capabilities")
        target = inspection.target
        if target is None:
            raise CaptureError(
                TARGET_NOT_FOUND,
                "no usable capture target: the source has no window set and no target was configured",
                {"source_name": self.config.source_name},
            )

        # --- the output directory must be provable *before* anything records --
        # Recording first and discovering afterwards that the destination is
        # unknown or outside the dedicated directory leaves an unverifiable take.
        intended_directory = policy.check_record_directory(
            self.config.record_dir or inspection.record_directory, self.config.record_dir
        )

        # Everything past this point can mutate OBS, so it is serialised against
        # any other agent-capture command on the same instance.
        lock = InstanceLock(self.config.state_dir)
        lock.acquire(timeout=INSTANCE_LOCK_TIMEOUT_S)
        try:
            return self._start_locked(
                envelope, inspection, capabilities, target, intended_directory
            )
        finally:
            lock.release()

    def _start_locked(
        self,
        envelope: Dict[str, Any],
        inspection: Inspection,
        capabilities: policy.Capabilities,
        target: WindowTarget,
        intended_directory: str,
    ) -> Dict[str, Any]:
        # Reserve the run before touching the output, so a second start with the
        # same run_id is refused here rather than after an orphaned recording.
        reservation = {
            "status": "starting",
            "session_token": uuid.uuid4().hex,
            "started_at": self._clock(),
            "record_directory": intended_directory,
            "source_name": self.config.source_name,
            "profile": self.config.profile,
            "scene_collection": self.config.scene_collection,
            "scene": self.config.scene,
            "obs_version": capabilities.obs_version,
            "obs_websocket_version": capabilities.obs_websocket_version,
        }
        state_path = self._store.create(self.config.run_id, reservation)
        reserved = True
        self._session_token = reservation["session_token"]
        self._started_at = reservation["started_at"]

        try:
            # Dedicated record directory: capability gated, never assumed.
            if self.config.record_dir:
                current = inspection.record_directory
                if current is None or not policy.same_directory(current, self.config.record_dir):
                    capabilities.require_request(
                        "SetRecordDirectory", policy.MIN_WS_VERSION_SET_RECORD_DIRECTORY
                    )
                    self._call("SetRecordDirectory", {"recordDirectory": self.config.record_dir})
                    refreshed_directory = self._record_directory(capabilities)
                    if refreshed_directory is None or not policy.same_directory(
                        refreshed_directory, self.config.record_dir
                    ):
                        raise CaptureError(
                            INTERNAL,
                            "OBS did not accept the requested record directory",
                            {"requested": self.config.record_dir, "current": refreshed_directory},
                        )

            return self._start_after_reservation(
                envelope, inspection, capabilities, target, intended_directory, state_path
            )
        except BaseException:
            # Never leave a reservation (or a stray output) behind on failure.
            if reserved:
                self._cleanup_reservation(envelope)
            raise

    def _start_after_reservation(
        self,
        envelope: Dict[str, Any],
        inspection: Inspection,
        capabilities: policy.Capabilities,
        target: WindowTarget,
        intended_directory: str,
        state_path: str,
    ) -> Dict[str, Any]:
        # Prepare our own source if needed (only inside a dedicated session).
        desired = {
            "window": target.to_obs_window_string(),
            "priority": target.priority,
            "method": 0,
            "capture_audio": bool(self.config.capture_audio),
            "cursor": False,
            "client_area": True,
            "compatibility": False,
            "force_sdr": False,
        }
        # These are exactly the settings window-capture.c reads, and they are all
        # sent explicitly.  They must NOT be filtered against
        # GetInputDefaultSettings: `capture_audio` has no default in the official
        # source, so a defaults-based whitelist would silently drop the one key
        # that makes the recording audible.
        if not inspection.source.get("exists"):
            if not self.config.create_source_if_missing:
                raise CaptureError(
                    TARGET_NOT_FOUND,
                    "the dedicated capture source does not exist and create_source_if_missing is false",
                    {"source_name": self.config.source_name},
                )
            if self.config.require_dedicated_profile and not self.config.scene:
                raise CaptureError(
                    CONFIG_INVALID,
                    "refusing to create a source without a dedicated scene configured",
                    {"scene": self.config.scene},
                )
            create_settings = dict(desired)
            self._call(
                "CreateInput",
                {
                    "sceneName": self.config.scene,
                    "inputName": self.config.source_name,
                    "inputKind": policy.WINDOW_CAPTURE_KIND,
                    "inputSettings": create_settings,
                    "sceneItemEnabled": True,
                },
            )
            envelope["notes"].append("created the dedicated window_capture source %r" % (self.config.source_name,))
            inspection.source = self._source_state()

        applied = dict(desired)
        self._call("SetInputSettings", {"inputName": self.config.source_name, "inputSettings": applied, "overlay": True})

        # Re-verify the prepared source with exactly the same rules preflight
        # applies: settings accepted, audio actually configured, target still in
        # OBS's window list, session still dedicated.
        refreshed = self.inspect(enforce=False)
        if refreshed.problems:
            raise refreshed.problems[0]
        effective = (refreshed.source or {}).get("settings_effective") or {}
        self._settings_snapshot = effective
        self._window_string = str(effective.get("window") or target.to_obs_window_string())

        # Pre-start baseline: we only ever stop an output that moved past this.
        before_status = self._record_status()
        self._baseline_bytes = int(before_status["outputBytes"])
        self._baseline_frames = self._stats_frames()

        preexisting: List[str] = []
        directory = self._record_directory(capabilities) or self.config.record_dir
        policy.check_record_directory(directory, self.config.record_dir)
        listed = self._dir_probe(directory)
        preexisting = list(listed or [])
        self._preexisting = preexisting
        if listed is None:
            envelope["notes"].append(
                "the record directory could not be listed, so pre-existing files could not be "
                "snapshotted; the file-age check remains as the master-file backstop"
            )

        self._call("StartRecord")

        # Evidence: a successful StartRecord is not a picture, and not even proof
        # that anything was written.  Wait for the *record output's* byte counter
        # to move past our baseline.  ``GetStats.outputTotalFrames`` is a global
        # output counter, so it is reported for information only and never used
        # as evidence that this recording has a picture.
        deadline = self._started_at + float(self.config.start_evidence_timeout_s)
        evidence = "none"
        last_status: Dict[str, Any] = {}
        while True:
            last_status = self._record_status()
            if int(last_status["outputBytes"]) > self._baseline_bytes:
                evidence = "output_bytes_advanced"
                break
            if self._clock() >= deadline:
                break
            self._sleep(float(self.config.poll_interval_s))

        if evidence == "none":
            cleanup = self._cleanup_owned_output()
            raise CaptureError(
                NO_PROGRESS,
                "StartRecord was accepted but the output wrote no bytes within %.1fs; the "
                "recording is not usable" % (float(self.config.start_evidence_timeout_s),),
                {
                    "baseline_bytes": self._baseline_bytes,
                    "output_bytes": last_status.get("outputBytes"),
                    "cleanup": cleanup,
                },
            )

        # A source screenshot is a *weak* signal that OBS is rendering something
        # for this source.  It is not proof of which window, and not proof of
        # motion, so the level is not raised on it.
        picture_probe = self._probe_source_picture()

        state = {
            "status": "recording",
            "session_token": self._session_token,
            "started_at": self._started_at,
            "baseline_bytes": self._baseline_bytes,
            "baseline_frames": self._baseline_frames,
            "last_seen_bytes": int(last_status["outputBytes"]),
            "last_seen_duration_ms": float(last_status["outputDuration"]),
            "last_seen_at": self._clock(),
            # Calibration sample: OBS's own duration reading plus the wall-clock
            # moment it was taken.  Later identity checks are measured against
            # this, which makes a restart detectable within seconds.
            "first_observed_duration_ms": float(last_status["outputDuration"]),
            "first_observed_at": self._clock(),
            "record_directory": directory,
            "preexisting_outputs": preexisting,
            "preexisting_snapshot_available": listed is not None,
            "max_record_seconds": float(self.config.max_record_seconds),
            "source_name": self.config.source_name,
            "target_window_string": self._window_string,
            "settings_snapshot": effective,
            "scene": self.config.scene,
            "profile": self.config.profile,
            "scene_collection": self.config.scene_collection,
            "obs_version": capabilities.obs_version,
            "obs_websocket_version": capabilities.obs_websocket_version,
            "capture_audio": bool(self.config.capture_audio),
        }
        self._store.write(self.config.run_id, state)

        envelope["ok"] = True
        envelope["verification_level"] = self._level
        self._fill(envelope, refreshed)
        envelope["recording"].update(
            {
                "active": bool(last_status["outputActive"]),
                "bytes": last_status["outputBytes"],
                "frames": self._stats_frames(),
                "advanced": True,
                "evidence": evidence,
                "evidence_scope": EVIDENCE_SCOPE_WRITE_PROGRESS,
                "picture_verified": False,
                "artifact_verified": False,
            }
        )
        envelope["picture_probe"] = picture_probe
        envelope["ownership"] = {
            "run_id": self.config.run_id,
            "session_token": self._session_token,
            "state_path": state_path,
            "baseline_bytes": self._baseline_bytes,
        }
        envelope["notes"].append(
            "evidence=%s means the record output is writing bytes; it is not proof that the "
            "target window's picture was captured. Picture content still needs a source "
            "screenshot or an actual look at the file." % (evidence,)
        )
        return envelope

    # ------------------------------------------------------------------ #
    # guarded cleanup
    # ------------------------------------------------------------------ #
    def _cleanup_owned_output(self) -> str:
        """Stop the output we started, but only if it is provably still ours."""
        try:
            identity = self._current_identity()
        except CaptureError as exc:
            return "not_stopped_identity_unconfirmed:%s" % (exc.category,)
        if not identity.get("verified"):
            return "not_stopped_identity_unconfirmed"
        ok, result = self._try("StopRecord")
        if ok:
            return "stopped_after_no_progress"
        return "stop_failed:%s" % (getattr(result, "category", "unknown"),)

    def _cleanup_reservation(self, envelope: Dict[str, Any]) -> None:
        try:
            self._store.delete(self.config.run_id)
        except CaptureError as exc:
            envelope["notes"].append("could not remove the run reservation: %s" % (exc.category,))

    def _current_identity(self) -> Dict[str, Any]:
        """Re-read the live session and check the active output is still ours."""
        state = self._store.read(self.config.run_id)
        live = self._record_status()
        capabilities = self._capabilities()
        return policy.verify_output_identity(
            state=state,
            live_record=live,
            session=self._session(),
            source=self._source_state(),
            record_directory=self._record_directory(capabilities),
            obs_version=capabilities.obs_version,
            obs_websocket_version=capabilities.obs_websocket_version,
            now=self._clock(),
        )

    def _probe_source_picture(self) -> Dict[str, Any]:
        """Optional, clearly-labelled weak signal from ``GetSourceScreenshot``."""
        if not self._capabilities().supports_request("GetSourceScreenshot"):
            return {
                "available": False,
                "reason": "GetSourceScreenshot is not advertised by this obs-websocket",
                "proves_picture": False,
            }
        ok, data = self._try(
            "GetSourceScreenshot",
            {"sourceName": self.config.source_name, "imageFormat": "png", "imageWidth": 64, "imageHeight": 64},
        )
        if not ok or not isinstance(data, dict):
            return {
                "available": False,
                "reason": "screenshot request failed: %s" % (getattr(data, "category", "unknown"),),
                "proves_picture": False,
            }
        image = data.get("imageData")
        size = len(image) if isinstance(image, str) else 0
        return {
            "available": True,
            "encoded_bytes": size,
            "nonempty": size > 0,
            "proves_picture": False,
            "reason": "OBS rendered a frame for this source; this does not prove which window it "
            "is, nor that the picture is moving",
        }

    def status(self) -> Dict[str, Any]:
        envelope = self._envelope("status")
        try:
            return self._status(envelope)
        except CaptureError as exc:
            return self._fail(envelope, exc)
        finally:
            self.close()

    def _status(self, envelope: Dict[str, Any]) -> Dict[str, Any]:
        state: Optional[Dict[str, Any]] = None
        if self.config.run_id and self._store.exists(self.config.run_id):
            state = self._store.read(self.config.run_id)

        # Exempt *only* a record output this task can prove is its own; streaming,
        # the replay buffer and the virtual camera are still checked.  A record
        # output that is not ours is reported as NOT_OWNER below, which is a more
        # precise refusal than OBS_BUSY.
        token_matches = bool(
            state is not None and state.get("session_token") == self.config.session_token
        )
        inspection = self.inspect(
            enforce=False,
            enforce_idle=True,
            allow_recording=token_matches,
            allow_streaming=self.config.allow_streaming,
        )
        self._fill(envelope, inspection)

        live = inspection.record_status or {}
        active = bool(live.get("outputActive"))
        current_bytes = int(live.get("outputBytes") or 0)
        current_duration = float(live.get("outputDuration") or 0.0)

        ownership: Dict[str, Any] = {"owned": False, "run_id": self.config.run_id}
        identity: Optional[Dict[str, Any]] = None
        if state is None:
            ownership["reason"] = "no state file for this run_id"
            if active:
                self._fail(
                    envelope,
                    CaptureError(
                        NOT_OWNER,
                        "an OBS record output is active but this task has no ownership record for it",
                        {"run_id": self.config.run_id, "output_bytes": current_bytes},
                    ),
                )
        else:
            ownership.update(
                {
                    "owned": token_matches,
                    "status": state.get("status"),
                    "started_at": state.get("started_at"),
                    "baseline_bytes": state.get("baseline_bytes"),
                    "target_window_string": state.get("target_window_string"),
                }
            )
            if active:
                ownership["advanced_bytes"] = current_bytes - int(state.get("baseline_bytes") or 0)

            if active and not token_matches:
                self._fail(
                    envelope,
                    CaptureError(
                        NOT_OWNER,
                        "the active recording was started by a different session token",
                        {"run_id": self.config.run_id},
                    ),
                )

            age = self._clock() - float(state.get("started_at") or 0)
            if envelope["error"] is None and age > float(
                state.get("max_record_seconds") or self.config.max_record_seconds
            ):
                self._fail(
                    envelope,
                    CaptureError(
                        STALE_STATE,
                        "the ownership record is older than max_record_seconds",
                        {"age_seconds": round(age, 3)},
                    ),
                )
            if not active and envelope["error"] is None:
                envelope["notes"].append(
                    "the record output is not active; it was stopped outside this task or never started"
                )

            # Remember what we last saw, so a later restart is detectable.
            if active and state.get("status") == "recording":
                try:
                    self._store.write(
                        self.config.run_id,
                        dict(state, last_seen_bytes=current_bytes, last_seen_duration_ms=current_duration,
                             last_seen_at=self._clock()),
                    )
                except CaptureError as exc:
                    envelope["notes"].append("could not record progress: %s" % (exc.category,))

            # Most specific refusal first: a re-pointed source or a changed scene
            # is a direct observation about *our* capture, so it is reported as
            # such rather than as a generic identity failure.
            changed = policy.detect_source_drift(
                state.get("settings_snapshot"), (inspection.source or {}).get("settings_effective")
            )
            if changed:
                envelope["source_drift"] = changed
                if envelope["error"] is None:
                    self._fail(
                        envelope,
                        CaptureError(
                            SOURCE_DRIFT,
                            "the capture source settings changed while recording (%s)" % (", ".join(changed),),
                            {"changed": changed},
                        ),
                    )

            if state.get("scene") and inspection.session.get("program_scene") != state.get("scene"):
                envelope["scene_changed"] = {
                    "expected": state.get("scene"),
                    "current": inspection.session.get("program_scene"),
                }
                if envelope["error"] is None:
                    self._fail(
                        envelope,
                        CaptureError(
                            SCENE_MISMATCH,
                            "the OBS program scene changed while recording",
                            envelope["scene_changed"],
                        ),
                    )

            # Identity, not just "a token matches": a stop-then-restart by
            # something else also has bytes past our baseline.
            if active and envelope["error"] is None:
                try:
                    identity = policy.verify_output_identity(
                        state=state,
                        live_record=live,
                        session=inspection.session,
                        source=inspection.source,
                        record_directory=inspection.record_directory,
                        obs_version=(inspection.capabilities.obs_version if inspection.capabilities else ""),
                        obs_websocket_version=(
                            inspection.capabilities.obs_websocket_version if inspection.capabilities else ""
                        ),
                        now=self._clock(),
                    )
                    ownership["identity_verified"] = True
                except CaptureError as exc:
                    identity = exc.detail
                    ownership["identity_verified"] = False
                    self._fail(envelope, exc)
                if identity is not None:
                    envelope["output_identity"] = identity

        envelope["ownership"] = ownership

        if envelope["error"] is None and inspection.problems:
            self._fail(envelope, inspection.problems[0])
        if envelope["error"] is None:
            envelope["ok"] = True
            advanced = bool(
                state is not None and current_bytes > int(state.get("baseline_bytes") or 0)
            )
            envelope["recording"]["advanced"] = advanced
            envelope["recording"]["evidence"] = "output_bytes_advanced" if advanced else "none"
            envelope["recording"]["evidence_scope"] = EVIDENCE_SCOPE_WRITE_PROGRESS
            envelope["recording"]["picture_verified"] = False
            envelope["recording"]["artifact_verified"] = False
        return envelope

    def stop(self) -> Dict[str, Any]:
        envelope = self._envelope("stop")
        try:
            return self._stop(envelope)
        except CaptureError as exc:
            return self._fail(envelope, exc)
        finally:
            self.close()

    def _stop(self, envelope: Dict[str, Any]) -> Dict[str, Any]:
        if not self.config.run_id:
            raise CaptureError(CONFIG_INVALID, "stop requires run_id")
        if not self.config.session_token:
            raise CaptureError(
                NOT_OWNER,
                "stop requires the session_token returned by start(); refusing to stop an "
                "output this call cannot prove it owns",
                {"run_id": self.config.run_id},
            )
        state = self._store.read(self.config.run_id)
        if state.get("session_token") != self.config.session_token:
            raise CaptureError(
                NOT_OWNER,
                "the session_token does not match the one that started this recording",
                {"run_id": self.config.run_id},
            )

        started_at = float(state.get("started_at") or 0)
        age = self._clock() - started_at
        if age > float(state.get("max_record_seconds") or self.config.max_record_seconds):
            raise CaptureError(
                STALE_STATE,
                "the ownership record is stale; refusing to stop an expired state",
                {"age_seconds": round(age, 3), "max_record_seconds": state.get("max_record_seconds")},
            )

        # Serialise against other mutating commands, then prove the running
        # output is still ours before sending StopRecord.
        lock = InstanceLock(self.config.state_dir)
        lock.acquire(timeout=INSTANCE_LOCK_TIMEOUT_S)
        try:
            return self._stop_locked(envelope, state, started_at)
        finally:
            lock.release()

    def _stop_locked(
        self, envelope: Dict[str, Any], state: Dict[str, Any], started_at: float
    ) -> Dict[str, Any]:
        live = self._record_status()
        if not live["outputActive"]:
            raise CaptureError(
                STALE_STATE,
                "the record output is not running; nothing to stop",
                {"output_active": False},
            )
        baseline = int(state.get("baseline_bytes") or 0)
        current_bytes = int(live["outputBytes"])
        if current_bytes <= baseline:
            raise CaptureError(
                NOT_OWNER,
                "the running output has written no bytes beyond this task's baseline; it is not "
                "provably this task's recording",
                {"baseline_bytes": baseline, "current_bytes": current_bytes},
            )

        capabilities = self._capabilities()
        identity = policy.verify_output_identity(
            state=state,
            live_record=live,
            session=self._session(),
            source=self._source_state(),
            record_directory=self._record_directory(capabilities),
            obs_version=capabilities.obs_version,
            obs_websocket_version=capabilities.obs_websocket_version,
            now=self._clock(),
        )
        envelope["output_identity"] = identity

        response = self._call("StopRecord")
        output_path = validate.require_str(response, "outputPath", "StopRecord", allow_empty=False)

        directory = str(state.get("record_directory") or self.config.record_dir or "")
        preexisting = set(state.get("preexisting_outputs") or [])
        probe: Dict[str, Any] = {"exists": None, "size_bytes": None, "mtime": None}
        if self.config.verify_output_file:
            probe = self._file_probe(output_path)
        try:
            output = policy.check_output_path(
                output_path=output_path,
                record_directory=directory,
                preexisting_outputs=preexisting,
                started_at=started_at,
                exists=probe.get("exists") if self.config.verify_output_file else None,
                size_bytes=probe.get("size_bytes") if self.config.verify_output_file else None,
                mtime=probe.get("mtime") if self.config.verify_output_file else None,
            )
        except CaptureError as exc:
            envelope["output"] = {
                "path": output_path or None,
                "dir": directory or None,
                "exists": probe.get("exists"),
                "size_bytes": probe.get("size_bytes"),
                "verified": False,
            }
            envelope["recording"] = {
                "active": False,
                "paused": False,
                "duration_ms": live["outputDuration"],
                "bytes": current_bytes,
                "frames": self._stats_frames(),
                "advanced": True,
                "evidence": "output_bytes_advanced",
                "evidence_scope": EVIDENCE_SCOPE_WRITE_PROGRESS,
                "picture_verified": False,
                "artifact_verified": False,
            }
            self._fail(envelope, exc)
            envelope["notes"].append(
                "the recording was stopped, but the output could not be verified; do not treat this "
                "file as a good take"
            )
            return envelope

        envelope["output"] = output
        envelope["recording"] = {
            "active": False,
            "paused": False,
            "duration_ms": live["outputDuration"],
            "bytes": current_bytes,
            "frames": self._stats_frames(),
            "advanced": True,
            "evidence": "output_bytes_advanced",
            "evidence_scope": EVIDENCE_SCOPE_WRITE_PROGRESS,
            # The artifact exists and is inside the dedicated directory.  What is
            # *inside* it still has to be looked at.
            "picture_verified": False,
            "artifact_verified": True,
        }
        envelope["ownership"] = {
            "run_id": self.config.run_id,
            "session_token": self.config.session_token,
            "verified_owner": True,
            "identity_verified": True,
            "stopped_by": "this task",
        }

        if self._level == LEVEL_LIVE_CONNECTED:
            # Only now, with a real output that was proven ours and a file that
            # was checked on disk, does the level rise.  It still says nothing
            # about the picture or the audio content.
            self._level = LEVEL_LIVE_OUTPUT_VERIFIED

        self._store.delete(self.config.run_id)
        envelope["ok"] = True
        envelope["verification_level"] = self._level
        envelope["notes"].append(
            "verified: the output was proven to be this task's recording, and the file lives inside "
            "the dedicated directory without overwriting a pre-existing file. Picture content still "
            "requires a source screenshot or an actual look at the file."
        )
        return envelope
