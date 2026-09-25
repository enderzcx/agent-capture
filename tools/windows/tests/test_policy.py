"""Policy tests: the refusal and verification rules, with no socket involved."""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from agent_capture_win.errors import (  # noqa: E402
    GLOBAL_AUDIO_MIXED,
    MIC_MIXED,
    OBS_BUSY,
    OUTPUT_EMPTY,
    OUTPUT_NOT_FOUND,
    OUTPUT_PATH_INVALID,
    PROFILE_MISMATCH,
    RPC_UNSUPPORTED,
    SCENE_COLLECTION_MISMATCH,
    SCENE_MISMATCH,
    SOURCE_DRIFT,
    TARGET_AMBIGUOUS,
    TARGET_EMPTY,
    TARGET_NOT_FOUND,
    TARGET_UNSAFE_PRIORITY,
    CaptureError,
)
from agent_capture_win import policy  # noqa: E402
from agent_capture_win.targets import (  # noqa: E402
    PRIORITY_CLASS,
    PRIORITY_EXE,
    PRIORITY_TITLE,
    WindowTarget,
    build_window_string,
    is_generic_class,
    parse_window_string,
    redact_window_item,
)
from agent_capture_win.transport import ObsRpcError  # noqa: E402


# --------------------------------------------------------------------------- #
# window string semantics (must match libobs exactly)
# --------------------------------------------------------------------------- #
def test_window_string_round_trip_escapes_separators():
    title = "weird:title#with both"
    built = build_window_string(title, "UnityWndClass", "game.exe")
    assert built == "weird#3Atitle#22with both:UnityWndClass:game.exe"
    assert parse_window_string(built) == (title, "UnityWndClass", "game.exe")


def test_parse_window_string_tolerates_short_values():
    assert parse_window_string("only-a-title") == ("", "", "")
    assert parse_window_string("") == ("", "", "")
    assert parse_window_string(None) == ("", "", "")


def test_priority_values_match_libobs_header():
    # enum window_priority { WINDOW_PRIORITY_CLASS, WINDOW_PRIORITY_TITLE, WINDOW_PRIORITY_EXE };
    assert (PRIORITY_CLASS, PRIORITY_TITLE, PRIORITY_EXE) == (0, 1, 2)


def test_generic_class_detection_matches_obs_list():
    assert is_generic_class("Chrome_WidgetWin_1")
    assert is_generic_class("SDL_app")
    assert not is_generic_class("UnityWndClass")


# --------------------------------------------------------------------------- #
# target validation
# --------------------------------------------------------------------------- #
def test_empty_target_is_refused():
    with pytest.raises(CaptureError) as excinfo:
        WindowTarget().validate()
    assert excinfo.value.category == TARGET_EMPTY


def test_missing_dimensions_are_refused_by_default():
    with pytest.raises(CaptureError) as excinfo:
        WindowTarget(title="Slay the Spire 2", window_class="", exe="", priority=PRIORITY_TITLE).validate()
    assert excinfo.value.category == TARGET_AMBIGUOUS
    assert "class" in excinfo.value.message


def test_partial_match_can_be_opted_into_but_is_still_reported():
    target = WindowTarget(title="Slay the Spire 2", window_class="", exe="", priority=PRIORITY_TITLE)
    assert target.validate(allow_partial_match=True) == []
    assert "title_equality_only" in target.match_risk()


def test_generic_class_priority_is_refused_because_obs_degrades_it():
    target = WindowTarget(
        title="Some Page", window_class="Chrome_WidgetWin_1", exe="chrome.exe", priority=PRIORITY_CLASS
    )
    with pytest.raises(CaptureError) as excinfo:
        target.validate()
    assert excinfo.value.category == TARGET_UNSAFE_PRIORITY
    assert "degraded" in excinfo.value.message


def test_priority_for_the_wrong_dimension_is_refused():
    with pytest.raises(CaptureError) as excinfo:
        WindowTarget(title="t", window_class="c", exe="", priority=PRIORITY_EXE).validate()
    assert excinfo.value.category == TARGET_UNSAFE_PRIORITY


def test_priority_parsing_rejects_nonsense():
    with pytest.raises(CaptureError) as excinfo:
        WindowTarget.from_config({"title": "t", "priority": "nearest-window"})
    assert excinfo.value.category == TARGET_UNSAFE_PRIORITY
    with pytest.raises(CaptureError):
        WindowTarget.from_config({"title": "t", "priority": 7})


def test_exe_priority_target_is_accepted_and_states_its_risk():
    target = WindowTarget.from_config(
        {"title": "Slay the Spire 2", "class": "UnityWndClass", "exe": "SlayTheSpire2.exe", "priority": "exe"}
    )
    assert target.validate() == []
    assert target.priority == PRIORITY_EXE
    assert "another_window_of_same_exe_can_take_over" in target.match_risk()


# --------------------------------------------------------------------------- #
# capabilities
# --------------------------------------------------------------------------- #
def test_capability_gate_refuses_unadvertised_requests():
    caps = policy.capabilities_from(
        obs_version="30.0.0",
        obs_websocket_version="5.2.0",
        rpc_version=1,
        available_requests=["GetVersion", "GetRecordStatus"],
    )
    with pytest.raises(CaptureError) as excinfo:
        caps.require_request("SetRecordDirectory", policy.MIN_WS_VERSION_SET_RECORD_DIRECTORY)
    assert excinfo.value.category == RPC_UNSUPPORTED

    caps_ok = policy.capabilities_from(
        obs_version="31.0.0",
        obs_websocket_version="5.5.2",
        rpc_version=1,
        available_requests=["GetVersion", "SetRecordDirectory", "GetRecordDirectory"],
    )
    caps_ok.require_request("SetRecordDirectory", policy.MIN_WS_VERSION_SET_RECORD_DIRECTORY)
    assert caps_ok.to_dict()["set_record_directory_supported"] is True
    assert caps.to_dict()["set_record_directory_supported"] is False


def test_version_gate_applies_even_when_request_is_advertised():
    caps = policy.capabilities_from(
        obs_version="30.0.0",
        obs_websocket_version="5.2.0",
        rpc_version=1,
        available_requests=["SetRecordDirectory"],
    )
    with pytest.raises(CaptureError) as excinfo:
        caps.require_request("SetRecordDirectory", (5, 3, 0))
    assert excinfo.value.category == RPC_UNSUPPORTED
    assert excinfo.value.detail["server_version"] == "5.2.0"


def test_unknown_available_requests_is_not_treated_as_supported():
    """An unreported request list must fail closed, not assume everything works."""
    caps = policy.capabilities_from(
        obs_version="31.0.0", obs_websocket_version="", rpc_version=1, available_requests=None
    )
    assert caps.request_list_known is False
    assert caps.supports_request("Anything") is False
    with pytest.raises(CaptureError) as excinfo:
        caps.require_request("StartRecord")
    assert excinfo.value.category == RPC_UNSUPPORTED
    assert "did not report which requests it supports" in excinfo.value.message
    assert caps.supports_window_capture() is None


# --------------------------------------------------------------------------- #
# idle guard
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "kwargs",
    [
        {"streaming": True},
        {"recording": True},
        {"replay_buffer": True},
        {"virtual_cam": True},
    ],
)
def test_idle_guard_refuses_a_busy_obs(kwargs):
    with pytest.raises(CaptureError) as excinfo:
        policy.check_idle(policy.ObsActivity(**kwargs))
    assert excinfo.value.category == OBS_BUSY


def test_idle_guard_allows_idle_obs_and_opt_in_streaming():
    policy.check_idle(policy.ObsActivity())
    policy.check_idle(policy.ObsActivity(streaming=True), allow_streaming=True)
    with pytest.raises(CaptureError):
        policy.check_idle(policy.ObsActivity(streaming=True, recording=True), allow_streaming=True)


# --------------------------------------------------------------------------- #
# session selection
# --------------------------------------------------------------------------- #
def test_dedicated_session_requires_explicit_selection():
    with pytest.raises(CaptureError) as excinfo:
        policy.check_session_selection(
            profile=None,
            scene_collection=None,
            scene=None,
            current_profile="Default",
            current_scene_collection="Default",
            current_scene="Scene",
            require_dedicated=True,
        )
    assert excinfo.value.category in (PROFILE_MISMATCH, SCENE_MISMATCH)
    assert "require_dedicated_profile" in excinfo.value.message


@pytest.mark.parametrize(
    "current,expected_category",
    [
        ({"profile": "Default"}, PROFILE_MISMATCH),
        ({"scene_collection": "Default"}, SCENE_COLLECTION_MISMATCH),
        ({"scene": "Other Scene"}, SCENE_MISMATCH),
    ],
)
def test_session_mismatches_are_refused(current, expected_category):
    base = {
        "profile": "agent-capture",
        "scene_collection": "agent-capture",
        "scene": "agent-capture-window",
    }
    base.update(current)
    with pytest.raises(CaptureError) as excinfo:
        policy.check_session_selection(
            profile="agent-capture",
            scene_collection="agent-capture",
            scene="agent-capture-window",
            current_profile=base["profile"],
            current_scene_collection=base["scene_collection"],
            current_scene=base["scene"],
            require_dedicated=True,
        )
    assert excinfo.value.category == expected_category


def test_matching_dedicated_session_passes():
    policy.check_session_selection(
        profile="agent-capture",
        scene_collection="agent-capture",
        scene="agent-capture-window",
        current_profile="agent-capture",
        current_scene_collection="agent-capture",
        current_scene="agent-capture-window",
        require_dedicated=True,
    )


# --------------------------------------------------------------------------- #
# audio isolation
# --------------------------------------------------------------------------- #
#: A dedicated profile with the global inputs disabled.
SPECIAL_OFF = {"desktop1": None, "desktop2": None, "desktop3": None,
               "mic1": None, "mic2": None, "mic3": None, "mic4": None}
#: The default OBS install: global audio configured but not a scene item.
SPECIAL_ON = {"desktop1": "Desktop Audio", "desktop2": None, "desktop3": None,
              "mic1": "Mic/Aux", "mic2": None, "mic3": None, "mic4": None}

MANAGED = "agent-capture-window-capture"


def _isolate(**kwargs):
    kwargs.setdefault("scene_items", [])
    kwargs.setdefault("special_inputs", SPECIAL_OFF)
    kwargs.setdefault("input_kinds_by_name", {})
    kwargs.setdefault("managed_source_name", MANAGED)
    return policy.check_audio_isolation(**kwargs)


def test_global_desktop_audio_special_input_is_refused_even_with_no_scene_items():
    """The P0: Desktop Audio is usually NOT in the scene's item list and still
    gets recorded, so enumerating scene items alone misses it."""
    with pytest.raises(CaptureError) as excinfo:
        _isolate(scene_items=[{"sourceName": MANAGED, "sceneItemEnabled": True}],
                 special_inputs=SPECIAL_ON)
    assert excinfo.value.category == GLOBAL_AUDIO_MIXED
    assert "not a scene item" in excinfo.value.detail["why"]


def test_global_microphone_special_input_is_refused_with_only_the_target_in_scene():
    """Required counter-example: scene contains only the target, mic1 exists."""
    with pytest.raises(CaptureError) as excinfo:
        _isolate(
            scene_items=[{"sourceName": MANAGED, "sceneItemEnabled": True}],
            special_inputs={"desktop1": None, "mic1": "Mic/Aux"},
        )
    assert excinfo.value.category == MIC_MIXED
    assert "Mic/Aux" in excinfo.value.message


def test_desktop_audio_scene_item_is_refused():
    items = [{"sourceName": "Desktop Audio", "sceneItemEnabled": True,
              "inputKind": "wasapi_output_capture"}]
    with pytest.raises(CaptureError) as excinfo:
        _isolate(scene_items=items, special_inputs=SPECIAL_ON, input_kinds_by_name={})
    assert excinfo.value.category == GLOBAL_AUDIO_MIXED


def test_microphone_scene_item_is_refused():
    items = [{"sourceName": "Mic/Aux", "sceneItemEnabled": True, "inputKind": "wasapi_input_capture"}]
    with pytest.raises(CaptureError) as excinfo:
        _isolate(scene_items=items, input_kinds_by_name={})
    assert excinfo.value.category == MIC_MIXED


def test_disabled_global_audio_source_is_ignored():
    items = [{"sourceName": "Desktop Audio", "sceneItemEnabled": False,
              "inputKind": "wasapi_output_capture"}]
    result = _isolate(scene_items=items)
    assert result.offending_global == []


def test_global_audio_kind_is_caught_even_with_a_renamed_source():
    items = [{"sourceName": "My Desktop Thing", "sceneItemEnabled": True, "inputKind": "wasapi_output_capture"}]
    with pytest.raises(CaptureError) as excinfo:
        _isolate(scene_items=items, input_kinds_by_name={"My Desktop Thing": "wasapi_output_capture"})
    assert excinfo.value.category == GLOBAL_AUDIO_MIXED


def test_any_other_enabled_source_makes_the_scene_not_dedicated():
    """Nested scenes / groups / extra captures are refused, not inspected."""
    items = [
        {"sourceName": MANAGED, "sceneItemEnabled": True, "inputKind": "window_capture"},
        {"sourceName": "Some Nested Scene", "sceneItemEnabled": True, "inputKind": "scene"},
    ]
    with pytest.raises(CaptureError) as excinfo:
        _isolate(scene_items=items)
    assert excinfo.value.category == "SCENE_NOT_DEDICATED"
    assert "Some Nested Scene" in excinfo.value.message


def test_a_media_source_is_also_refused():
    items = [{"sourceName": "Intro Video", "sceneItemEnabled": True, "inputKind": "ffmpeg_source"}]
    with pytest.raises(CaptureError) as excinfo:
        _isolate(scene_items=items)
    assert excinfo.value.category == "SCENE_NOT_DEDICATED"


def test_only_the_managed_source_enabled_passes():
    items = [{"sourceName": MANAGED, "sceneItemEnabled": True, "inputKind": "window_capture"},
             {"sourceName": "Disabled Thing", "sceneItemEnabled": False, "inputKind": "ffmpeg_source"}]
    result = _isolate(scene_items=items)
    assert result.extra_enabled_sources == []


def test_explicit_opt_in_allows_global_audio_and_mic():
    items = [
        {"sourceName": "Desktop Audio", "sceneItemEnabled": True, "inputKind": "wasapi_output_capture"},
        {"sourceName": "Mic/Aux", "sceneItemEnabled": True, "inputKind": "wasapi_input_capture"},
    ]
    result = _isolate(
        scene_items=items,
        special_inputs=SPECIAL_ON,
        allow_desktop_audio=True,
        allow_mic=True,
    )
    assert set(result.offending_global) == {"Desktop Audio"}
    assert set(result.offending_mic) == {"Mic/Aux"}


# --------------------------------------------------------------------------- #
# effective settings + audio evidence
# --------------------------------------------------------------------------- #
def test_merge_input_settings_overlays_defaults():
    defaults = {"method": 0, "client_area": True, "cursor": False, "capture_audio": False}
    overlay = {"window": "t:c:e", "priority": 2, "capture_audio": True}
    merged = policy.merge_input_settings(overlay, defaults)
    assert merged["client_area"] is True  # only visible via defaults
    assert merged["capture_audio"] is True  # overlay wins
    assert merged["window"] == "t:c:e"


def test_audio_evidence_never_claims_samples():
    ok, problems, level = policy.check_audio_settings(
        effective_settings={"capture_audio": True},
        audio_requested=True,
        source_muted=False,
        source_volume_db=0.0,
        audio_tracks={"track1": True},
    )
    assert ok and problems == []
    assert level == policy.AUDIO_EVIDENCE_SETTINGS
    assert level != policy.AUDIO_EVIDENCE_UNOBSERVABLE  # a *level*, not a claim


@pytest.mark.parametrize(
    "kwargs,needle",
    [
        ({"effective_settings": {"capture_audio": False}}, "is disabled"),
        ({"effective_settings": {}}, "no 'capture_audio' value"),
        ({"effective_settings": {"capture_audio": True}, "source_muted": True}, "muted"),
        ({"effective_settings": {"capture_audio": True}, "source_volume_db": -120.0}, "effectively zero"),
        ({"effective_settings": {"capture_audio": True}, "audio_tracks": {"track1": False}}, "track 1"),
        (
            {"effective_settings": {"capture_audio": True}, "capture_audio_supported": False},
            "no window_capture application-audio capability",
        ),
    ],
)
def test_silent_recordings_are_refused_not_silently_accepted(kwargs, needle):
    ok, problems, _ = policy.check_audio_settings(audio_requested=True, **kwargs)
    assert ok is False
    assert any(needle in p for p in problems)


def test_audio_not_requested_skips_the_checks():
    ok, problems, level = policy.check_audio_settings(
        effective_settings={"capture_audio": False}, audio_requested=False
    )
    assert ok and problems == []
    assert level == policy.AUDIO_EVIDENCE_NONE


# --------------------------------------------------------------------------- #
# drift + target presence
# --------------------------------------------------------------------------- #
def test_detect_source_drift_reports_changed_keys():
    before = {"window": "a:b:c", "priority": 2, "capture_audio": True}
    after = {"window": "x:y:z", "priority": 2, "capture_audio": False}
    assert policy.detect_source_drift(before, after) == ["window", "capture_audio"]
    assert policy.detect_source_drift(before, before) == []


def test_require_no_drift_raises_with_the_changed_keys():
    with pytest.raises(CaptureError) as excinfo:
        policy.require_no_drift({"window": "a:b:c"}, {"window": "x:y:z"})
    assert excinfo.value.category == SOURCE_DRIFT
    assert excinfo.value.detail["changed"] == ["window"]


def test_target_presence_is_checked_against_obs_window_list():
    items = [{"itemValue": "Game:Class:game.exe", "itemEnabled": True}]
    policy.check_target_present(window_string="Game:Class:game.exe", property_items=items)
    with pytest.raises(CaptureError) as excinfo:
        policy.check_target_present(window_string="Gone:Class:game.exe", property_items=items)
    assert excinfo.value.category == TARGET_NOT_FOUND


def test_target_presence_refuses_when_obs_cannot_list_windows():
    # None means "not observable".  Unknown is not "found", so this must refuse
    # rather than let a recording start against an unverified match.
    with pytest.raises(CaptureError) as excinfo:
        policy.check_target_present(window_string="Game:Class:game.exe", property_items=None)
    assert excinfo.value.category == "TARGET_UNCONFIRMED"


# --------------------------------------------------------------------------- #
# path containment
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "child,parent,expected",
    [
        ("D:\\rec\\a.mkv", "D:\\rec", True),
        ("D:\\rec\\sub\\a.mkv", "D:\\rec", True),
        ("D:\\rec", "D:\\rec", True),
        ("D:\\recorder\\a.mkv", "D:\\rec", False),
        ("E:\\rec\\a.mkv", "D:\\rec", False),
        ("D:\\rec\\..\\master\\a.mkv", "D:\\rec", False),
        ("\\\\nas\\share\\rec\\a.mkv", "\\\\nas\\share\\rec", True),
        ("\\\\nas\\other\\a.mkv", "\\\\nas\\share\\rec", False),
        ("/home/example/rec/a.mkv", "/home/example/rec", True),
        ("/home/example/rec2/a.mkv", "/home/example/rec", False),
        ("d:/rec/a.mkv", "D:/rec", True),
    ],
)
def test_is_within_handles_windows_and_posix_paths(child, parent, expected):
    assert policy.is_within(child, parent) is expected


def test_same_directory_is_case_insensitive_and_trailing_slash_agnostic():
    assert policy.same_directory("D:\\Rec\\", "d:/rec")
    assert not policy.same_directory("D:\\Rec\\sub", "D:\\Rec")


def test_check_output_path_accepts_a_contained_new_file():
    result = policy.check_output_path(
        output_path="D:\\rec\\2026-09-25 18-00-00.mkv",
        record_directory="D:\\rec",
        preexisting_outputs={"D:\\rec\\old.mkv"},
        started_at=1000.0,
        exists=True,
        size_bytes=1024,
        mtime=1010.0,
    )
    assert result["verified"] is True and result["containment"] is True


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({"output_path": "D:\\elsewhere\\a.mkv"}, OUTPUT_PATH_INVALID),
        ({"output_path": ""}, OUTPUT_PATH_INVALID),
        ({"output_path": "D:\\rec\\..\\master\\a.mkv"}, OUTPUT_PATH_INVALID),
        ({"output_path": "D:\\rec\\old.mkv", "preexisting_outputs": {"old.mkv"}}, OUTPUT_PATH_INVALID),
        ({"output_path": "D:\\rec\\old.mkv", "preexisting_outputs": {"D:\\rec\\OLD.MKV"}}, OUTPUT_PATH_INVALID),
        ({"output_path": "D:\\rec\\a.mkv", "exists": False}, OUTPUT_NOT_FOUND),
        ({"output_path": "D:\\rec\\a.mkv", "size_bytes": 0}, OUTPUT_EMPTY),
        ({"output_path": "D:\\rec\\a.mkv", "mtime": 10.0, "started_at": 1000.0}, OUTPUT_PATH_INVALID),
    ],
)
def test_check_output_path_refusals(kwargs, expected):
    base = {"record_directory": "D:\\rec", "exists": True, "size_bytes": 1024, "mtime": 1010.0, "started_at": 1000.0}
    base.update(kwargs)
    with pytest.raises(CaptureError) as excinfo:
        policy.check_output_path(**base)
    assert excinfo.value.category == expected


def test_check_output_path_requires_a_dedicated_directory():
    with pytest.raises(CaptureError) as excinfo:
        policy.check_output_path(output_path="D:\\rec\\a.mkv", record_directory="")
    assert excinfo.value.category == OUTPUT_PATH_INVALID


# --------------------------------------------------------------------------- #
# ownership
# --------------------------------------------------------------------------- #
def _ownership(**kwargs):
    base = dict(
        run_token="rt",
        session_token="sess-1",
        started_at=1000.0,
        baseline_bytes=500,
        record_directory="D:\\rec",
        max_record_seconds=60.0,
    )
    base.update(kwargs)
    return policy.RecordingOwnership(**base)


def test_ownership_requires_the_matching_session_token():
    ownership = _ownership()
    ownership.require_owner("sess-1")
    with pytest.raises(CaptureError) as excinfo:
        ownership.require_owner("sess-2")
    assert excinfo.value.category == "NOT_OWNER"
    with pytest.raises(CaptureError):
        ownership.require_owner("")


def test_staleness_by_age_and_by_inactive_output():
    ownership = _ownership()
    ownership.require_fresh(now=1030.0, running=True)
    with pytest.raises(CaptureError) as excinfo:
        ownership.require_fresh(now=2000.0, running=True)
    assert excinfo.value.category == "STALE_STATE"
    with pytest.raises(CaptureError):
        ownership.require_fresh(now=1030.0, running=False)


def test_progress_must_exceed_our_own_baseline():
    ownership = _ownership(baseline_bytes=500)
    ownership.require_progress(501)
    for value in (500, 499, 0):
        with pytest.raises(CaptureError) as excinfo:
            ownership.require_progress(value)
        assert excinfo.value.category == "NOT_OWNER"


# --------------------------------------------------------------------------- #
# rpc error classification
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "code,request_type,expected",
    [
        (204, "GetVersion", RPC_UNSUPPORTED),
        (600, "GetInputSettings", TARGET_NOT_FOUND),
        (500, "StartRecord", OBS_BUSY),
        (207, "StopRecord", OBS_BUSY),
        (604, "SetInputSettings", OBS_BUSY),
        (700, "CreateInput", "INTERNAL"),
    ],
)
def test_rpc_error_classification(code, request_type, expected):
    error = policy.classify_rpc_error(ObsRpcError(request_type, code, "comment"))
    assert error.category == expected
    assert error.detail["obs_code"] == code


# --------------------------------------------------------------------------- #
# redaction
# --------------------------------------------------------------------------- #
def test_redaction_hides_other_windows_by_default():
    item = {"itemName": "[chrome.exe]: Private Bank Tab", "itemValue": "Private Bank Tab:Chrome_WidgetWin_1:chrome.exe", "itemEnabled": True}
    redacted = redact_window_item(item)
    assert redacted["redacted"] is True
    assert "Private Bank Tab" not in str(redacted)
    assert "chrome.exe" not in str(redacted)
    assert redacted["title_fingerprint"].startswith("sha256:")
    revealed = redact_window_item(item, revealed=True)
    assert revealed["title"] == "Private Bank Tab"


def test_redaction_is_stable_and_distinguishing():
    a = redact_window_item({"itemValue": "A:c:e"})
    b = redact_window_item({"itemValue": "A:c:e"})
    c = redact_window_item({"itemValue": "B:c:e"})
    assert a["title_fingerprint"] == b["title_fingerprint"]
    assert a["title_fingerprint"] != c["title_fingerprint"]
