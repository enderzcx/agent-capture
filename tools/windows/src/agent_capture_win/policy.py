"""Policy: what the backend refuses, what it verifies, what it refuses to claim.

This module is deliberately free of sockets so every rule can be tested against
scripted responses.  The transport does RPC; this module decides.

Rules implemented here, each traceable to a requirement:

* capability gating (never call a request the server did not advertise)
* idle guard (never take over an OBS that is already streaming/recording/etc.)
* dedicated profile / scene collection / scene enforcement
* global desktop audio and microphone isolation
* effective source settings = ``GetInputSettings`` overlaid on
  ``GetInputDefaultSettings`` (the former does **not** contain defaults)
* source drift detection between preflight and start/status
* start evidence (bytes/frames must actually advance)
* ownership + staleness for ``StopRecord``
* output path containment and master-file protection
* audio evidence, including the honest statement that sample-level audio
  presence is *not* observable over obs-websocket
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .errors import (
    GLOBAL_AUDIO_MIXED,
    MIC_MIXED,
    NOT_OWNER,
    OBS_BUSY,
    OUTPUT_EMPTY,
    OUTPUT_NOT_FOUND,
    OUTPUT_PATH_INVALID,
    PROFILE_MISMATCH,
    RPC_UNSUPPORTED,
    SCENE_COLLECTION_MISMATCH,
    SCENE_MISMATCH,
    SCENE_NOT_DEDICATED,
    SOURCE_DRIFT,
    STALE_STATE,
    TARGET_NOT_FOUND,
    TARGET_UNCONFIRMED,
    CaptureError,
)
from .transport import ObsRpcError, parse_version, version_at_least

#: obs-websocket version that introduced ``SetRecordDirectory`` (official docs).
MIN_WS_VERSION_SET_RECORD_DIRECTORY = (5, 3, 0)

#: ``GetRecordDirectory`` exists since 5.0.0; gated anyway because the server
#: may be older than the client and because availableRequests is authoritative.
MIN_WS_VERSION_GET_RECORD_DIRECTORY = (5, 0, 0)

#: The OBS input kind for a Windows window capture.
WINDOW_CAPTURE_KIND = "window_capture"

#: Scene item / input kinds that mean "global audio is being mixed in".
GLOBAL_AUDIO_KINDS = ("wasapi_output_capture", "wasapi_process_output_capture", "coreaudio_output_capture")

AUDIO_EVIDENCE_NONE = "none"
AUDIO_EVIDENCE_SETTINGS = "settings_confirm_capture_enabled"
AUDIO_EVIDENCE_UNOBSERVABLE = "sample_level_not_observable_over_obs_websocket"


# --------------------------------------------------------------------------- #
# Capabilities
# --------------------------------------------------------------------------- #
@dataclass
class Capabilities:
    obs_version: str = ""
    obs_websocket_version: str = ""
    rpc_version: Optional[int] = None
    available_requests: Optional[Sequence[str]] = None
    input_kinds: Optional[Sequence[str]] = None

    @property
    def request_list_known(self) -> bool:
        """Whether the server told us which requests it implements."""
        return self.available_requests is not None

    def supports_request(self, request_type: str) -> bool:
        """``availableRequests`` is authoritative, and its absence is not a pass.

        An unknown request list used to be treated as "everything is
        supported", which means an unadvertised request is attempted and the
        only thing standing between us and a surprise is the server's error.
        Unknown now means unsupported: the caller refuses and says why.
        """
        if self.available_requests is None:
            return False
        return request_type in set(self.available_requests)

    def require_request(self, request_type: str, min_ws_version: Optional[Tuple[int, ...]] = None) -> None:
        """Refuse up front instead of calling a request that cannot work."""
        if min_ws_version is not None and self.obs_websocket_version:
            if not version_at_least(self.obs_websocket_version, min_ws_version):
                raise CaptureError(
                    RPC_UNSUPPORTED,
                    "%s needs obs-websocket >= %s, server is %s"
                    % (request_type, ".".join(str(p) for p in min_ws_version), self.obs_websocket_version),
                    {
                        "request_type": request_type,
                        "required_version": ".".join(str(p) for p in min_ws_version),
                        "server_version": self.obs_websocket_version,
                    },
                )
        if not self.request_list_known:
            raise CaptureError(
                RPC_UNSUPPORTED,
                "the connected obs-websocket did not report which requests it supports, so %s "
                "cannot be confirmed and will not be attempted" % (request_type,),
                {
                    "request_type": request_type,
                    "obs_websocket_version": self.obs_websocket_version,
                    "request_list_known": False,
                },
            )
        if not self.supports_request(request_type):
            raise CaptureError(
                RPC_UNSUPPORTED,
                "the connected obs-websocket does not advertise %s" % (request_type,),
                {
                    "request_type": request_type,
                    "obs_websocket_version": self.obs_websocket_version,
                    "available_requests_count": len(self.available_requests or ()),
                },
            )

    def supports_window_capture(self) -> Optional[bool]:
        if self.input_kinds is None:
            return None
        return WINDOW_CAPTURE_KIND in set(self.input_kinds)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "obs_version": self.obs_version,
            "obs_websocket_version": self.obs_websocket_version,
            "rpc_version": self.rpc_version,
            "available_requests": sorted(self.available_requests) if self.available_requests else None,
            "window_capture_kind_available": self.supports_window_capture(),
            "get_record_directory_supported": self.supports_request("GetRecordDirectory")
            and version_at_least(self.obs_websocket_version, MIN_WS_VERSION_GET_RECORD_DIRECTORY)
            if self.obs_websocket_version
            else self.supports_request("GetRecordDirectory"),
            "set_record_directory_supported": self.supports_request("SetRecordDirectory")
            and version_at_least(self.obs_websocket_version, MIN_WS_VERSION_SET_RECORD_DIRECTORY)
            if self.obs_websocket_version
            else self.supports_request("SetRecordDirectory"),
        }


def capabilities_from(
    *,
    obs_version: str,
    obs_websocket_version: str,
    rpc_version: Optional[int],
    available_requests: Optional[Sequence[str]],
    input_kinds: Optional[Sequence[str]] = None,
) -> Capabilities:
    return Capabilities(
        obs_version=obs_version or "",
        obs_websocket_version=obs_websocket_version or "",
        rpc_version=rpc_version,
        available_requests=list(available_requests) if available_requests is not None else None,
        input_kinds=list(input_kinds) if input_kinds is not None else None,
    )


# --------------------------------------------------------------------------- #
# Idle guard
# --------------------------------------------------------------------------- #
@dataclass
class ObsActivity:
    streaming: bool = False
    recording: bool = False
    replay_buffer: bool = False
    virtual_cam: bool = False

    def busy_reasons(self) -> List[str]:
        reasons = []
        if self.streaming:
            reasons.append("streaming")
        if self.recording:
            reasons.append("recording")
        if self.replay_buffer:
            reasons.append("replay_buffer")
        if self.virtual_cam:
            reasons.append("virtual_camera")
        return reasons

    def to_dict(self) -> Dict[str, Any]:
        return {
            "streaming": self.streaming,
            "recording": self.recording,
            "replay_buffer": self.replay_buffer,
            "virtual_cam": self.virtual_cam,
        }


def check_idle(
    activity: ObsActivity,
    *,
    allow_streaming: bool = False,
    allow_recording: bool = False,
) -> None:
    """Refuse to take over an OBS instance the user is already using.

    ``allow_recording`` exempts **only** the record output, and only for
    commands that then prove ownership of that exact output through
    :func:`verify_output_identity`.  Streaming, the replay buffer and the
    virtual camera are always checked independently and are never exempted by
    it, so a running recording cannot be used to smuggle the others through.
    """
    reasons = activity.busy_reasons()
    if allow_streaming:
        reasons = [r for r in reasons if r != "streaming"]
    if allow_recording:
        reasons = [r for r in reasons if r != "recording"]
    if reasons:
        raise CaptureError(
            OBS_BUSY,
            "refusing to touch an OBS instance that is already in use (%s)" % (", ".join(reasons),),
            {
                "busy": reasons,
                "activity": activity.to_dict(),
                "exempted": {
                    "streaming": bool(allow_streaming),
                    "recording": bool(allow_recording),
                },
            },
        )


# --------------------------------------------------------------------------- #
# Session selection (dedicated profile / collection / scene)
# --------------------------------------------------------------------------- #
def check_session_selection(
    *,
    profile: Optional[str],
    scene_collection: Optional[str],
    scene: Optional[str],
    current_profile: Optional[str],
    current_scene_collection: Optional[str],
    current_scene: Optional[str],
    require_dedicated: bool = True,
) -> None:
    if require_dedicated:
        missing = [
            name
            for name, value in (
                ("profile", profile),
                ("scene_collection", scene_collection),
                ("scene", scene),
            )
            if not value
        ]
        if missing:
            raise CaptureError(
                PROFILE_MISMATCH if "profile" in missing else SCENE_MISMATCH,
                "require_dedicated_profile=true but %s not configured; refusing to reuse "
                "whatever session the user currently has open" % (", ".join(missing),),
                {"missing": missing},
            )
    # A missing "current" value is *not* a pass.  Skipping the comparison when
    # OBS reports nothing is how a recording ends up running in whatever session
    # the user happened to have open.
    for label, expected, current, category in (
        ("profile", profile, current_profile, PROFILE_MISMATCH),
        ("scene collection", scene_collection, current_scene_collection, SCENE_COLLECTION_MISMATCH),
        ("program scene", scene, current_scene, SCENE_MISMATCH),
    ):
        if not expected:
            continue
        if not current:
            raise CaptureError(
                category,
                "OBS did not report its current %s, so it cannot be confirmed that this task's "
                "dedicated %s is in use" % (label, label),
                {"expected": expected, "current": current},
            )
        if expected != current:
            raise CaptureError(
                category,
                "OBS is on %s %r, this task requires %r" % (label, current, expected),
                {"expected": expected, "current": current},
            )


# --------------------------------------------------------------------------- #
# Audio isolation
# --------------------------------------------------------------------------- #
@dataclass
class AudioIsolation:
    offending_global: List[str] = field(default_factory=list)
    offending_mic: List[str] = field(default_factory=list)
    extra_enabled_sources: List[str] = field(default_factory=list)
    evidence_level: str = AUDIO_EVIDENCE_NONE

    def to_dict(self) -> Dict[str, Any]:
        return {
            "global_audio_sources_enabled": sorted(self.offending_global),
            "microphone_sources_enabled": sorted(self.offending_mic),
            "extra_enabled_sources": sorted(self.extra_enabled_sources),
            "evidence_level": self.evidence_level,
        }


def _enabled_scene_sources(scene_items: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for item in scene_items or ():
        if not isinstance(item, dict):
            continue
        if not item.get("sceneItemEnabled", True):
            continue
        name = item.get("sourceName")
        if name:
            out[str(name)] = item
    return out


def check_audio_isolation(
    *,
    scene_items: Sequence[Dict[str, Any]],
    special_inputs: Dict[str, Any],
    input_kinds_by_name: Dict[str, str],
    managed_source_name: str,
    allow_desktop_audio: bool = False,
    allow_mic: bool = False,
    recording_tracks: Optional[Dict[str, Any]] = None,
) -> AudioIsolation:
    """Refuse anything that would mix audio or picture into the recording.

    Two independent checks, because they catch different things:

    1. **Special inputs.**  ``GetSpecialInputs`` reports OBS's global Desktop
       Audio / Mic sources.  They routinely are **not** in the current scene's
       item list and still get recorded, so enumerating scene items alone misses
       them entirely.  A non-null name means the special input is configured, so
       the dedicated profile must have it disabled (set to "Disabled") before
       this backend will record.  No mute/track-based exemption is attempted:
       a wrongly-permissive exemption is worse than asking the operator to
       prepare the profile.

    2. **The scene must be dedicated.**  The only enabled scene item allowed is
       the single managed ``window_capture`` source.  Anything else that is
       enabled -- a nested scene, a group, a media source, another capture --
       would be recorded too, so it is refused.  This deliberately does not walk
       into nested scenes or groups: it refuses them instead of trying to prove
       they are empty.
    """
    isolation = AudioIsolation()

    desktop_names = {
        str((special_inputs or {}).get(key))
        for key in ("desktop1", "desktop2", "desktop3")
        if isinstance((special_inputs or {}).get(key), str) and (special_inputs or {}).get(key)
    }
    mic_names = {
        str((special_inputs or {}).get(key))
        for key in ("mic1", "mic2", "mic3", "mic4")
        if isinstance((special_inputs or {}).get(key), str) and (special_inputs or {}).get(key)
    }

    # --- 1. special inputs, whether or not they are scene items --------------
    if desktop_names and not allow_desktop_audio:
        raise CaptureError(
            GLOBAL_AUDIO_MIXED,
            "OBS still has a global Desktop Audio input configured (%s); it is recorded "
            "independently of the scene's source list. Disable it in the dedicated profile "
            "(Audio settings -> Global Audio -> Desktop Audio = Disabled) before recording."
            % (", ".join(sorted(desktop_names)),),
            {
                "special_inputs": {"desktop": sorted(desktop_names)},
                "why": "global desktop audio is not a scene item and would still be mixed in",
            },
        )
    if mic_names and not allow_mic:
        raise CaptureError(
            MIC_MIXED,
            "OBS still has a global microphone input configured (%s); it is recorded "
            "independently of the scene's source list. Disable it in the dedicated profile "
            "(Audio settings -> Global Audio -> Mic/Auxiliary = Disabled) before recording."
            % (", ".join(sorted(mic_names)),),
            {
                "special_inputs": {"mic": sorted(mic_names)},
                "why": "the global mic is not a scene item and would still be mixed in",
            },
        )

    # --- 2. the scene must contain only our managed source -------------------
    enabled = _enabled_scene_sources(scene_items)
    for name, item in sorted(enabled.items()):
        if name == managed_source_name:
            continue
        kind = str(input_kinds_by_name.get(name, "") or item.get("inputKind") or "")
        if name in desktop_names or kind in GLOBAL_AUDIO_KINDS:
            isolation.offending_global.append(name)
        elif name in mic_names or "input_capture" in kind or kind.endswith("_input"):
            isolation.offending_mic.append(name)
        else:
            isolation.extra_enabled_sources.append(name)

    if isolation.offending_global and not allow_desktop_audio:
        raise CaptureError(
            GLOBAL_AUDIO_MIXED,
            "the program scene has an enabled global-audio source (%s); this task records only "
            "the target application's audio" % (", ".join(sorted(isolation.offending_global)),),
            {"sources": sorted(isolation.offending_global), "allow_desktop_audio": False},
        )
    if isolation.offending_mic and not allow_mic:
        raise CaptureError(
            MIC_MIXED,
            "the program scene has an enabled microphone source (%s); this task does not record "
            "the microphone" % (", ".join(sorted(isolation.offending_mic)),),
            {"sources": sorted(isolation.offending_mic), "allow_mic": False},
        )
    if isolation.extra_enabled_sources:
        raise CaptureError(
            SCENE_NOT_DEDICATED,
            "the program scene has enabled sources besides the managed capture (%s); the scene is "
            "not dedicated, so those sources would be recorded as well"
            % (", ".join(sorted(isolation.extra_enabled_sources)),),
            {
                "extra_enabled_sources": sorted(isolation.extra_enabled_sources),
                "managed_source": managed_source_name,
                "note": "nested scenes and groups are refused rather than inspected for content",
            },
        )

    if recording_tracks is not None:
        isolation.evidence_level = AUDIO_EVIDENCE_SETTINGS
    return isolation


def merge_input_settings(
    input_settings: Optional[Dict[str, Any]],
    default_settings: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Effective settings = input over defaults.

    ``GetInputSettings`` explicitly does not include defaults, so the defaults
    from ``GetInputDefaultSettings`` must be overlaid or keys such as
    ``client_area`` / ``method`` would be invisible to every check.
    """
    merged: Dict[str, Any] = dict(default_settings or {})
    merged.update(input_settings or {})
    return merged


def check_audio_settings(
    *,
    effective_settings: Dict[str, Any],
    audio_requested: bool,
    source_muted: Optional[bool] = None,
    source_volume_db: Optional[float] = None,
    audio_tracks: Optional[Dict[str, Any]] = None,
    capture_audio_supported: Optional[bool] = None,
) -> Tuple[bool, List[str], str]:
    """Judge whether the source is *configured* to record its own audio.

    Returns ``(ok, problems, evidence_level)``.  The evidence level never
    exceeds ``settings_confirm_capture_enabled``: obs-websocket exposes no
    audio sample data, so sample-level silence cannot be proven or disproven
    from here, and the backend says so instead of implying success.
    """
    problems: List[str] = []
    if not audio_requested:
        return True, problems, AUDIO_EVIDENCE_NONE

    if capture_audio_supported is False:
        problems.append(
            "this OBS session has no window_capture application-audio capability "
            "(needs Windows 10 2004+ application audio capture); the recording would be silent"
        )
        return False, problems, AUDIO_EVIDENCE_NONE

    if not effective_settings.get("capture_audio"):
        if "capture_audio" in effective_settings:
            problems.append("window_capture 'capture_audio' is disabled; the recording would be silent")
        else:
            problems.append(
                "window_capture has no 'capture_audio' value in this session, which OBS treats as off "
                "(the setting has no default, and older builds have no application audio capture at all); "
                "the recording would be silent"
            )
        return False, problems, AUDIO_EVIDENCE_NONE

    if source_muted:
        problems.append("the capture source is muted")
    if source_volume_db is not None and source_volume_db <= -100.0:
        problems.append("the capture source volume is effectively zero (%.1f dB)" % (source_volume_db,))
    if audio_tracks is not None:
        track_one = bool(audio_tracks.get("track1", True))
        if not track_one:
            problems.append("audio track 1 is disabled for this source; the recording would be silent")

    if problems:
        return False, problems, AUDIO_EVIDENCE_SETTINGS
    return True, problems, AUDIO_EVIDENCE_SETTINGS


# --------------------------------------------------------------------------- #
# Source drift
# --------------------------------------------------------------------------- #
#: Settings whose change means "this is no longer the capture we preflighted".
DRIFT_RELEVANT_SETTINGS = ("window", "priority", "method", "capture_audio", "cursor", "client_area", "compatibility", "force_sdr")


def detect_source_drift(
    before: Optional[Dict[str, Any]],
    after: Optional[Dict[str, Any]],
    *,
    keys: Sequence[str] = DRIFT_RELEVANT_SETTINGS,
) -> List[str]:
    """Return the settings that changed (empty means no drift)."""
    if before is None or after is None:
        return []
    changed = []
    for key in keys:
        if before.get(key, "<absent>") != after.get(key, "<absent>"):
            changed.append(key)
    return changed


def require_no_drift(before: Optional[Dict[str, Any]], after: Optional[Dict[str, Any]]) -> None:
    changed = detect_source_drift(before, after)
    if changed:
        raise CaptureError(
            SOURCE_DRIFT,
            "the capture source settings changed since preflight (%s); the target window may "
            "have been replaced" % (", ".join(changed),),
            {"changed": changed},
        )


# --------------------------------------------------------------------------- #
# Target window presence
# --------------------------------------------------------------------------- #
def check_target_present(
    *,
    window_string: str,
    property_items: Optional[Sequence[Dict[str, Any]]],
) -> None:
    """Refuse unless the exact window string is in OBS's window list.

    ``property_items`` comes from
    ``GetInputPropertiesListPropertyItems(propertyName="window")``.  When the
    list is unavailable the answer is *unknown*, and unknown is not "found":
    this raises ``TARGET_UNCONFIRMED`` so the caller refuses instead of
    proceeding on an unverified match.
    """
    if property_items is None:
        raise CaptureError(
            TARGET_UNCONFIRMED,
            "OBS did not expose its window list, so the target window could not be confirmed; "
            "refusing to record against an unverified match",
            {"window_string": window_string, "enumeration_available": False},
        )
    values = {str(item.get("itemValue")) for item in property_items if isinstance(item, dict)}
    if window_string not in values:
        raise CaptureError(
            TARGET_NOT_FOUND,
            "the requested window is not in OBS's current window list; it was closed, renamed, "
            "or is not capturable",
            {"window_string": window_string, "candidates": len(values)},
        )


# --------------------------------------------------------------------------- #
# Ownership, staleness, output verification
# --------------------------------------------------------------------------- #
@dataclass
class RecordingOwnership:
    """Everything needed to prove a later ``StopRecord`` belongs to this task."""

    run_token: str
    session_token: str
    started_at: float
    baseline_bytes: int
    record_directory: str
    preexisting_outputs: Set[str] = field(default_factory=set)
    max_record_seconds: float = 3600.0
    last_seen_bytes: int = 0
    source_name: str = ""
    target_window_string: str = ""

    def is_stale(self, now: Optional[float] = None, *, running: bool = True) -> bool:
        now = time.time() if now is None else now
        if now - self.started_at > self.max_record_seconds:
            return True
        return not running

    def require_owner(self, session_token: str) -> None:
        if not session_token or session_token != self.session_token:
            raise CaptureError(
                NOT_OWNER,
                "this session does not own the active recording; refusing to stop it",
                {"expected_session": self.session_token, "given_session": session_token},
            )

    def require_fresh(self, now: Optional[float] = None, *, running: bool = True) -> None:
        if self.is_stale(now, running=running):
            raise CaptureError(
                STALE_STATE,
                "the recording state is stale; refusing to act on an expired state",
                {
                    "age_seconds": round((time.time() if now is None else now) - self.started_at, 3),
                    "max_record_seconds": self.max_record_seconds,
                    "output_active": running,
                },
            )

    def require_progress(self, current_bytes: int) -> None:
        if current_bytes <= self.baseline_bytes:
            raise CaptureError(
                NOT_OWNER,
                "the record output has not written any new bytes since this task started it; "
                "it is not this task's recording",
                {"baseline_bytes": self.baseline_bytes, "current_bytes": current_bytes},
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_token": self.run_token,
            "session_token": self.session_token,
            "started_at": self.started_at,
            "baseline_bytes": self.baseline_bytes,
            "record_directory": self.record_directory,
            "max_record_seconds": self.max_record_seconds,
            "source_name": self.source_name,
        }


def raw_path_segments(path: str) -> Tuple[str, Tuple[str, ...]]:
    """Split a path without resolving anything (used for traversal detection)."""
    return _split_path(path)


def has_traversal_segment(path: str) -> bool:
    """True when the path literally contains a ``..`` component."""
    _, parts = _split_path(path)
    return any(part == ".." for part in parts)


def _split_path(path: str) -> Tuple[str, Tuple[str, ...]]:
    raw = str(path or "").strip().replace("\\", "/")
    anchor = ""
    if raw.startswith("//"):
        parts = [p for p in raw[2:].split("/") if p]
        if len(parts) >= 2:
            anchor = "//%s/%s" % (parts[0].lower(), parts[1].lower())
            parts = parts[2:]
        else:
            anchor = "//" + (parts[0].lower() if parts else "")
            parts = []
    elif len(raw) >= 2 and raw[1] == ":":
        anchor = raw[0].lower() + ":"
        raw = raw[2:]
        parts = [p for p in raw.split("/") if p]
    else:
        parts = [p for p in raw.split("/") if p]
    return anchor, tuple(parts)


def normalize_for_containment(path: str) -> Tuple[str, Tuple[str, ...]]:
    """Split a Windows or POSIX path into ``(anchor, segments)`` for comparison.

    Handles ``D:\\a\\b``, ``\\\\server\\share\\x`` and ``/a/b`` without touching
    the filesystem, so containment can be checked on any host OS.  ``.`` and
    ``..`` are resolved lexically: a path that climbs above its own root keeps a
    ``..`` sentinel that can never match a real directory name, so it can never
    be reported as contained.
    """
    anchor, parts = _split_path(path)
    resolved: List[str] = []
    for part in parts:
        if part == ".":
            continue
        if part == "..":
            if resolved:
                resolved.pop()
            else:
                resolved.append("..")
            continue
        resolved.append(part)
    return anchor, tuple(resolved)


def is_within(child: str, parent: str) -> bool:
    """True when ``child`` is ``parent`` or lives underneath it."""
    child_anchor, child_parts = normalize_for_containment(child)
    parent_anchor, parent_parts = normalize_for_containment(parent)
    if child_anchor != parent_anchor:
        return False
    if len(child_parts) < len(parent_parts):
        return False
    return child_parts[: len(parent_parts)] == parent_parts


def same_directory(left: str, right: str) -> bool:
    """True when two paths name the same directory (case-insensitive on Windows)."""
    left_anchor, left_parts = normalize_for_containment(left)
    right_anchor, right_parts = normalize_for_containment(right)
    if left_anchor != right_anchor:
        return False
    return tuple(p.lower() for p in left_parts) == tuple(p.lower() for p in right_parts)


# --------------------------------------------------------------------------- #
# Record directory legality (checked *before* a recording starts)
# --------------------------------------------------------------------------- #
def is_absolute_path(path: str) -> bool:
    """True for ``D:\\x``, ``\\\\server\\share\\x`` and ``/x``."""
    raw = str(path or "").strip()
    if not raw:
        return False
    if raw.startswith("\\\\") or raw.startswith("//"):
        return True
    if len(raw) >= 2 and raw[1] == ":" and raw[0].isalpha():
        return True
    return raw.startswith("/")


def check_record_directory(directory: Optional[str], configured: Optional[str]) -> str:
    """Prove the output directory is known and legal, before anything records.

    Recording first and discovering afterwards that the destination is unknown
    (or outside the dedicated directory) leaves an unverifiable take behind, so
    this runs during preflight and again immediately before ``StartRecord``.
    """
    if not directory:
        raise CaptureError(
            OUTPUT_PATH_INVALID,
            "no record directory could be determined (neither configured nor reported by OBS); "
            "refusing to start a recording whose output could not be verified",
            {"configured": configured, "reported": directory},
        )
    if not is_absolute_path(directory):
        raise CaptureError(
            OUTPUT_PATH_INVALID,
            "the record directory is not an absolute path, so containment cannot be proven",
            {"directory": directory},
        )
    _, parts = normalize_for_containment(directory)
    if not parts:
        raise CaptureError(
            OUTPUT_PATH_INVALID,
            "the record directory resolves to a filesystem root; refusing to record there",
            {"directory": directory},
        )
    if configured and not same_directory(directory, configured):
        raise CaptureError(
            OUTPUT_PATH_INVALID,
            "OBS is writing to a different directory than the dedicated one configured for this task",
            {"configured": configured, "reported": directory},
        )
    return directory


# --------------------------------------------------------------------------- #
# Output identity (does the *running* output still belong to this task?)
# --------------------------------------------------------------------------- #
#: Allowed drift between the expected and observed output duration before the
#: output is judged to have been restarted (or to predate us).
#:
#: When a calibration sample exists (the duration and wall-clock time observed
#: right after StartRecord) the expectation is OBS-side duration plus our own
#: elapsed time, so the only error sources are scheduling jitter and the two
#: clocks' rates -- a tight tolerance is safe.
#:
#: Without a calibration sample the expectation is our whole elapsed time since
#: the reservation, which includes however long source preparation took, so the
#: tolerance has to be looser.
DURATION_TOLERANCE_CALIBRATED_MS = 2_000.0
DURATION_TOLERANCE_CALIBRATED_RATIO = 0.10
DURATION_TOLERANCE_MIN_MS = 5_000.0
DURATION_TOLERANCE_RATIO = 0.25


def duration_tolerance_ms(expected_ms: float, *, calibrated: bool = False) -> float:
    if calibrated:
        return max(
            DURATION_TOLERANCE_CALIBRATED_MS, abs(expected_ms) * DURATION_TOLERANCE_CALIBRATED_RATIO
        )
    return max(DURATION_TOLERANCE_MIN_MS, abs(expected_ms) * DURATION_TOLERANCE_RATIO)


def verify_output_identity(
    *,
    state: Dict[str, Any],
    live_record: Dict[str, Any],
    session: Dict[str, Any],
    source: Dict[str, Any],
    record_directory: Optional[str],
    obs_version: str,
    obs_websocket_version: str,
    now: float,
) -> Dict[str, Any]:
    """Prove the active record output is still the one this task started.

    A session token plus "more bytes than our baseline" is **not** enough: if
    the user stops our recording and starts a new one, the new output also has
    bytes beyond that baseline.  Identity therefore also requires the same
    profile / scene collection / scene, the same source bound to the same
    window, the same record directory, the same OBS build, a byte counter that
    has not gone backwards, and an output duration consistent with our own
    elapsed time.

    Anything that cannot be confirmed raises ``NOT_OWNER`` rather than being
    assumed to still be ours.
    """
    reasons: List[str] = []
    detail: Dict[str, Any] = {}

    def mismatch(label: str, expected: Any, actual: Any) -> None:
        if expected is None or expected == "":
            return
        if actual != expected:
            reasons.append("%s changed (%r -> %r)" % (label, expected, actual))
            detail.setdefault("mismatches", {})[label] = {"expected": expected, "actual": actual}

    mismatch("profile", state.get("profile"), session.get("profile"))
    mismatch("scene_collection", state.get("scene_collection"), session.get("scene_collection"))
    mismatch("scene", state.get("scene"), session.get("program_scene"))
    mismatch("obs_version", state.get("obs_version"), obs_version)
    mismatch("obs_websocket_version", state.get("obs_websocket_version"), obs_websocket_version)

    if not source.get("exists"):
        reasons.append("the dedicated capture source no longer exists")
    else:
        kind = str(source.get("kind") or "")
        if kind and kind != WINDOW_CAPTURE_KIND:
            reasons.append("the capture source changed kind (%s)" % (kind,))
        current_window = str((source.get("settings_effective") or {}).get("window") or "")
        expected_window = str(state.get("target_window_string") or "")
        if expected_window and current_window != expected_window:
            reasons.append("the source was re-pointed at a different window")

    recorded_directory = str(state.get("record_directory") or "")
    if recorded_directory and record_directory and not same_directory(record_directory, recorded_directory):
        reasons.append("the record directory changed (%r -> %r)" % (recorded_directory, record_directory))

    duration_ms = live_record.get("outputDuration")
    if isinstance(duration_ms, (int, float)) and not isinstance(duration_ms, bool):
        first_duration = state.get("first_observed_duration_ms")
        first_at = state.get("first_observed_at")
        calibrated = isinstance(first_duration, (int, float)) and isinstance(first_at, (int, float))
        if calibrated:
            # Self-calibrating: the output's own duration at a known wall-clock
            # moment, plus however long ago that was.  A restarted output's
            # duration falls far below this, even if the restart happened only a
            # second ago.
            expected_ms = max(0.0, float(first_duration) + (now - float(first_at)) * 1000.0)
        else:
            started_at = float(state.get("started_at") or 0.0)
            expected_ms = max(0.0, (now - started_at) * 1000.0)
        tolerance = duration_tolerance_ms(expected_ms, calibrated=calibrated)
        detail["duration_basis"] = "first_observed_sample" if calibrated else "reservation_time"
        detail["expected_duration_ms"] = round(expected_ms, 1)
        detail["observed_duration_ms"] = round(float(duration_ms), 1)
        detail["duration_tolerance_ms"] = round(tolerance, 1)
        if float(duration_ms) < expected_ms - tolerance:
            reasons.append(
                "the active output is younger than this task's recording, so it was stopped and "
                "started again by something else"
            )
        elif float(duration_ms) > expected_ms + tolerance:
            reasons.append(
                "the active output is older than this task's recording, so it did not start when "
                "this task started it"
            )
    else:
        reasons.append("OBS did not report the output duration, so identity cannot be confirmed")

    current_bytes = live_record.get("outputBytes")
    last_seen = state.get("last_seen_bytes")
    if isinstance(last_seen, (int, float)) and not isinstance(last_seen, bool):
        if isinstance(current_bytes, (int, float)) and not isinstance(current_bytes, bool):
            detail["last_seen_bytes"] = last_seen
            detail["current_bytes"] = current_bytes
            if float(current_bytes) < float(last_seen):
                reasons.append("the output byte counter went backwards, so the output was restarted")

    report = {
        "verified": not reasons,
        "reasons": reasons,
        "checked": [
            "profile",
            "scene_collection",
            "scene",
            "source_exists",
            "source_kind",
            "source_window",
            "record_directory",
            "obs_version",
            "output_duration_consistency",
            "output_bytes_monotonic",
        ],
    }
    report.update(detail)
    if reasons:
        raise CaptureError(
            NOT_OWNER,
            "the active record output cannot be confirmed as this task's recording: "
            + "; ".join(reasons),
            dict(report, session=session),
        )
    return report


def _basename(path: str) -> str:
    _, parts = normalize_for_containment(path)
    return parts[-1] if parts else ""


def check_output_path(
    *,
    output_path: str,
    record_directory: str,
    preexisting_outputs: Optional[Set[str]] = None,
    started_at: Optional[float] = None,
    exists: Optional[bool] = None,
    size_bytes: Optional[int] = None,
    mtime: Optional[float] = None,
) -> Dict[str, Any]:
    """Verify a stopped recording before anyone calls it a success.

    Refuses: a path outside the dedicated directory, a traversal, a file that
    already existed before this task started (the user's master copy), a
    missing file, and an empty file.  Filesystem facts are passed in so the
    checks stay testable off Windows.

    ``preexisting_outputs`` may hold bare file names (as ``os.listdir`` returns)
    or full paths; both are reduced to a case-insensitive base name, because the
    question being asked is "did this file already exist before we started?".
    """
    if not output_path:
        raise CaptureError(OUTPUT_PATH_INVALID, "StopRecord returned no outputPath", {})

    detail = {"output_path": output_path, "record_directory": record_directory}
    if not record_directory:
        raise CaptureError(
            OUTPUT_PATH_INVALID,
            "no dedicated record directory is configured; cannot prove the output is ours",
            detail,
        )
    if not is_within(output_path, record_directory):
        raise CaptureError(
            OUTPUT_PATH_INVALID,
            "the recording was written outside the dedicated directory",
            dict(detail, containment=False),
        )

    if has_traversal_segment(output_path):
        raise CaptureError(OUTPUT_PATH_INVALID, "output path contains a traversal segment", detail)

    if preexisting_outputs:
        names = {_basename(item).lower() for item in preexisting_outputs if item}
        if _basename(output_path).lower() in names:
            raise CaptureError(
                OUTPUT_PATH_INVALID,
                "the output path names a file that already existed before this recording started; "
                "refusing to treat it as this task's output",
                dict(detail, preexisting=True, name=_basename(output_path)),
            )

    if exists is False:
        raise CaptureError(OUTPUT_NOT_FOUND, "the recording file does not exist", detail)
    if size_bytes is not None and size_bytes <= 0:
        raise CaptureError(OUTPUT_EMPTY, "the recording file is empty", detail)
    if mtime is not None and started_at is not None and mtime + 1.0 < started_at:
        raise CaptureError(
            OUTPUT_PATH_INVALID,
            "the recording file predates this recording session",
            dict(detail, mtime=mtime, started_at=started_at),
        )

    return {
        "path": output_path,
        "dir": record_directory,
        "exists": bool(exists) if exists is not None else None,
        "size_bytes": size_bytes,
        "verified": True,
        "containment": True,
        "preexisting": False,
    }


# --------------------------------------------------------------------------- #
# RPC error classification
# --------------------------------------------------------------------------- #
#: obs-websocket RequestStatus codes that mean "you asked for something that
#: is not there" (official enum).
_NOT_FOUND_CODES = (600,)
_NOT_READY_CODES = (207, 604)
_RUNNING_CODES = (500,)

_NOT_FOUND_REQUESTS = (
    "GetInputSettings",
    "SetInputSettings",
    "GetInputMute",
    "GetInputVolume",
    "GetInputAudioTracks",
    "GetInputPropertiesListPropertyItems",
    "GetSceneItemList",
    "GetSourceActive",
)


def classify_rpc_error(exc: ObsRpcError) -> CaptureError:
    """Turn a server-side request failure into a stable category."""
    detail = exc.to_detail()
    if exc.code == 204:  # UnknownRequestType
        return CaptureError(RPC_UNSUPPORTED, "%s is not implemented by this obs-websocket" % (exc.request_type,), detail)
    if exc.code in _NOT_FOUND_CODES:
        if exc.request_type in _NOT_FOUND_REQUESTS:
            return CaptureError(TARGET_NOT_FOUND, "%s could not find the requested resource" % (exc.request_type,), detail)
        return CaptureError(TARGET_NOT_FOUND, "%s: %s" % (exc.request_type, exc.comment or "resource not found"), detail)
    if exc.code in _RUNNING_CODES:
        return CaptureError(OBS_BUSY, "%s: %s" % (exc.request_type, exc.comment or "output already running"), detail)
    if exc.code in _NOT_READY_CODES:
        return CaptureError(OBS_BUSY, "%s: %s" % (exc.request_type, exc.comment or "not ready"), detail)
    return CaptureError(
        RPC_UNSUPPORTED if exc.code in (203, 206) else "INTERNAL",
        "%s failed: %s" % (exc.request_type, exc.comment or exc.status_name),
        detail,
    )
