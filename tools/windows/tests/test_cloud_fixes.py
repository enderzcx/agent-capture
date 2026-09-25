"""Counter-examples for the seven review findings.

Each section is one finding, with the fake response that reproduces the exact
failure the review described.  These are the tests that must fail if the fix is
ever reverted.
"""

from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from agent_capture_win import backend as backend_module  # noqa: E402
from agent_capture_win import run  # noqa: E402
from agent_capture_win.fake import DEFAULT_RECORD_DIR, FakeObs  # noqa: E402
from agent_capture_win.state import InstanceLock, OwnershipStore  # noqa: E402

from test_backend import TARGET, cfg, invoke, started_recording  # noqa: E402


def external_restart(obs: FakeObs) -> None:
    """Simulate the user stopping our recording and starting a fresh one."""
    obs._respond("StopRecord", {})
    obs._respond("StartRecord", {})


# --------------------------------------------------------------------------- #
# 1. status must not judge our own recording as OBS_BUSY
# --------------------------------------------------------------------------- #
def test_status_allows_our_own_recording(tmp_path):
    state_dir = tmp_path / "state"
    obs = FakeObs()
    started = started_recording(obs, state_dir)
    status = invoke("status", obs, state_dir, session_token=started["ownership"]["session_token"])
    assert status["ok"] is True, status.get("error")
    assert status["recording"]["active"] is True


@pytest.mark.parametrize(
    "attribute,reason",
    [("streaming", "streaming"), ("replay_buffer", "replay_buffer"), ("virtual_cam", "virtual_camera")],
)
def test_status_still_refuses_other_outputs(tmp_path, attribute, reason):
    """Exempting the record output must not exempt anything else."""
    state_dir = tmp_path / "state"
    obs = FakeObs()
    started = started_recording(obs, state_dir)
    setattr(obs, attribute, True)
    status = invoke("status", obs, state_dir, session_token=started["ownership"]["session_token"])
    assert status["ok"] is False
    assert status["error"]["category"] == "OBS_BUSY"
    assert reason in status["error"]["detail"]["busy"]


def test_status_reports_a_foreign_recording_as_not_owner(tmp_path):
    obs = FakeObs(recording=True, record_bytes=10_000)
    status = invoke("status", obs, tmp_path / "state")
    assert status["ok"] is False
    assert status["error"]["category"] == "NOT_OWNER"


# --------------------------------------------------------------------------- #
# 2. missing / mistyped fields must not read as "idle"
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "request_type,field,value",
    [
        ("GetStreamStatus", "outputActive", None),
        ("GetReplayBufferStatus", "outputActive", None),
        ("GetVirtualCamStatus", "outputActive", None),
        ("GetRecordStatus", "outputActive", None),
        ("GetRecordStatus", "outputBytes", None),
        ("GetRecordStatus", "outputDuration", None),
    ],
)
def test_missing_output_state_field_is_a_broken_response(tmp_path, request_type, field, value):
    """A dropped field used to read as False == "idle", which is how an in-use
    OBS gets taken over."""
    obs = FakeObs(omit_fields={request_type: [field]})
    result = invoke("preflight", obs, tmp_path / "state")
    assert result["ok"] is False
    assert result["error"]["category"] == "BAD_RESPONSE"
    assert result["error"]["detail"]["field"] == field
    assert result["error"]["detail"]["present"] is False


@pytest.mark.parametrize(
    "request_type,field,value",
    [
        ("GetStreamStatus", "outputActive", "true"),
        ("GetRecordStatus", "outputActive", 1),
        ("GetRecordStatus", "outputBytes", "1024"),
        ("GetRecordStatus", "outputDuration", "10000"),
        ("GetRecordStatus", "outputDuration", True),
        ("GetRecordStatus", "outputBytes", None),
    ],
)
def test_mistyped_output_state_field_is_a_broken_response(tmp_path, request_type, field, value):
    obs = FakeObs(mistype_fields={"%s.%s" % (request_type, field): value})
    result = invoke("preflight", obs, tmp_path / "state")
    assert result["ok"] is False
    assert result["error"]["category"] == "BAD_RESPONSE"
    assert result["error"]["detail"]["field"] == field


def test_missing_field_is_refused_before_any_mutation(tmp_path):
    obs = FakeObs(omit_fields={"GetRecordStatus": ["outputBytes"]})
    result = invoke("start", obs, tmp_path / "state")
    assert result["ok"] is False
    assert result["error"]["category"] == "BAD_RESPONSE"
    assert obs.mutations == []


def test_broken_record_status_during_stop_does_not_stop_anything(tmp_path):
    state_dir = tmp_path / "state"
    obs = FakeObs()
    started = started_recording(obs, state_dir)
    token = started["ownership"]["session_token"]
    obs.omit_fields = {"GetRecordStatus": ["outputDuration"]}
    result = invoke("stop", obs, state_dir, session_token=token)
    assert result["ok"] is False
    assert result["error"]["category"] == "BAD_RESPONSE"
    assert obs.mutations.count("StopRecord") == 0


# --------------------------------------------------------------------------- #
# 3. owner-only stop must survive an external stop + restart
# --------------------------------------------------------------------------- #
def test_stop_detects_an_external_stop_and_restart(tmp_path):
    """Token + "bytes past baseline" is not identity: a fresh output also has
    bytes past our baseline."""
    state_dir = tmp_path / "state"
    obs = FakeObs()
    started = started_recording(obs, state_dir)
    token = started["ownership"]["session_token"]

    # Let some bytes accumulate and be remembered by a status call.
    for _ in range(4):
        obs._respond("GetRecordStatus", {})
    status = invoke("status", obs, state_dir, session_token=token)
    assert status["ok"] is True, status.get("error")

    external_restart(obs)  # our output stopped, a new one started
    result = invoke("stop", obs, state_dir, session_token=token)
    assert result["ok"] is False
    assert result["error"]["category"] == "NOT_OWNER"
    assert obs.mutations.count("StopRecord") == 1  # only the external one
    assert obs.recording is True  # the new output was left alone


def test_stop_detects_a_restart_from_the_duration(tmp_path):
    """Even with no prior status call, an output younger than our recording is
    not ours."""
    state_dir = tmp_path / "state"
    obs = FakeObs()
    started = started_recording(obs, state_dir)
    token = started["ownership"]["session_token"]
    external_restart(obs)

    # 30s of our own elapsed time against an output that just started: far
    # outside the tolerance.
    result = invoke("stop", obs, state_dir, session_token=token, clock=lambda: time.time() + 30)
    assert result["ok"] is False
    assert result["error"]["category"] == "NOT_OWNER"
    assert "younger" in result["error"]["message"]
    assert obs.recording is True


def test_calibrated_duration_catches_a_restart_inside_the_loose_window(tmp_path):
    """A restart two seconds in must not slip through the loose tolerance.

    Without the calibration sample the expectation is measured from the
    reservation, so a 5s tolerance would swallow a 2s restart.  The sample taken
    right after StartRecord makes the expectation tight.
    """
    state_dir = tmp_path / "state"
    obs = FakeObs()
    started = started_recording(obs, state_dir)
    token = started["ownership"]["session_token"]

    # Two seconds of real recording, then somebody else restarts it.
    time.sleep(2.0)
    external_restart(obs)

    result = invoke("stop", obs, state_dir, session_token=token)
    assert result["ok"] is False
    assert result["error"]["category"] == "NOT_OWNER"
    assert result["error"]["detail"]["duration_basis"] == "first_observed_sample"
    assert obs.recording is True  # the new output was left alone


def test_status_detects_an_external_stop_and_restart(tmp_path):
    state_dir = tmp_path / "state"
    obs = FakeObs()
    started = started_recording(obs, state_dir)
    token = started["ownership"]["session_token"]
    for _ in range(4):
        obs._respond("GetRecordStatus", {})
    assert invoke("status", obs, state_dir, session_token=token)["ok"] is True

    external_restart(obs)
    status = invoke("status", obs, state_dir, session_token=token)
    assert status["ok"] is False
    assert status["error"]["category"] == "NOT_OWNER"


@pytest.mark.parametrize(
    "mutate,needle",
    [
        (lambda obs: setattr(obs, "profile", "Someone Elses Profile"), "profile"),
        (lambda obs: setattr(obs, "scene_collection", "Someone Elses Collection"), "scene_collection"),
        (lambda obs: setattr(obs, "source_exists", False), "no longer exists"),
        (lambda obs: obs.source_settings.__setitem__("window", "Other:Class:other.exe"), "re-pointed"),
        (lambda obs: setattr(obs, "record_directory", "C:\\Elsewhere"), "record directory"),
        (lambda obs: setattr(obs, "obs_version", "29.0.0"), "obs_version"),
    ],
)
def test_stop_refuses_when_the_session_identity_changed(tmp_path, mutate, needle):
    state_dir = tmp_path / "state"
    obs = FakeObs()
    started = started_recording(obs, state_dir)
    token = started["ownership"]["session_token"]
    mutate(obs)
    result = invoke("stop", obs, state_dir, session_token=token)
    assert result["ok"] is False
    assert result["error"]["category"] in ("NOT_OWNER", "SOURCE_DRIFT", "SCENE_MISMATCH")
    assert needle in result["error"]["message"]
    assert obs.mutations.count("StopRecord") == 0
    assert obs.recording is True


def test_stop_refuses_when_the_scene_changed(tmp_path):
    state_dir = tmp_path / "state"
    obs = FakeObs()
    started = started_recording(obs, state_dir)
    token = started["ownership"]["session_token"]
    obs.scene = "Someone Elses Scene"
    result = invoke("stop", obs, state_dir, session_token=token)
    assert result["ok"] is False
    # Identity verification covers the scene, so this is a NOT_OWNER refusal
    # naming the scene rather than a separate category.
    assert result["error"]["category"] == "NOT_OWNER"
    assert "scene" in result["error"]["message"]
    assert obs.mutations.count("StopRecord") == 0


def test_stop_requires_a_reported_output_duration(tmp_path):
    state_dir = tmp_path / "state"
    obs = FakeObs()
    started = started_recording(obs, state_dir)
    token = started["ownership"]["session_token"]
    # Drop outputDuration only for the stop call: identity cannot be confirmed,
    # so nothing is stopped.
    obs.omit_fields = {"GetRecordStatus": ["outputDuration"]}
    result = invoke("stop", obs, state_dir, session_token=token)
    assert result["ok"] is False
    assert result["error"]["category"] == "BAD_RESPONSE"
    assert obs.mutations.count("StopRecord") == 0


# --------------------------------------------------------------------------- #
# 4. the run is reserved before the output starts
# --------------------------------------------------------------------------- #
def test_existing_reservation_refuses_a_second_start_without_recording(tmp_path):
    state_dir = tmp_path / "state"
    OwnershipStore(str(state_dir)).create("take-001", {"session_token": "ghost", "started_at": 0.0})
    obs = FakeObs()
    result = invoke("start", obs, state_dir)
    assert result["ok"] is False
    assert result["error"]["category"] == "STALE_STATE"
    assert obs.mutations.count("StartRecord") == 0


def test_reservation_is_released_after_a_failed_start(tmp_path):
    state_dir = tmp_path / "state"
    obs = FakeObs(advance_on_poll=False)
    failed = invoke("start", obs, state_dir, start_evidence_timeout_s=0.0)
    assert failed["ok"] is False
    assert failed["error"]["category"] == "NO_PROGRESS"
    assert not os.path.exists(os.path.join(str(state_dir), "take-001.json"))

    # The same run_id can be retried because the reservation was released.
    obs2 = FakeObs()
    retry = invoke("start", obs2, state_dir)
    assert retry["ok"] is True, retry.get("error")


def test_start_refuses_while_another_command_holds_the_instance_lock(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    monkeypatch.setattr(backend_module, "INSTANCE_LOCK_TIMEOUT_S", 0.2)
    lock = InstanceLock(str(state_dir))
    lock.acquire(timeout=1.0)
    try:
        obs = FakeObs()
        result = invoke("start", obs, state_dir)
        assert result["ok"] is False
        assert result["error"]["category"] == "OBS_BUSY"
        assert obs.mutations == []
    finally:
        lock.release()


def test_lock_is_released_after_a_successful_start(tmp_path):
    state_dir = tmp_path / "state"
    obs = FakeObs()
    started_recording(obs, state_dir)
    lock = InstanceLock(str(state_dir))
    lock.acquire(timeout=1.0)  # would raise if start had leaked the lock
    lock.release()


# --------------------------------------------------------------------------- #
# 5. write progress is not a picture, and the level must not jump early
# --------------------------------------------------------------------------- #
def test_start_does_not_claim_the_recording_is_verified(tmp_path):
    obs = FakeObs()
    result = started_recording(obs, tmp_path / "state")
    assert result["verification_level"] != "live_output_file_verified"
    assert result["verification"]["artifact"] == "unverified"
    assert result["verification"]["picture"] == "unverified"
    assert result["recording"]["picture_verified"] is False
    assert result["recording"]["artifact_verified"] is False
    assert result["recording"]["evidence_scope"] == "output_write_progress_only__not_picture_proof"
    assert any("not proof" in note for note in result["notes"])


def test_bytes_advancing_alone_never_reaches_the_top_level(tmp_path):
    """Required: outputBytes rising must not be reported as a verified recording."""
    obs = FakeObs()
    result = started_recording(obs, tmp_path / "state")
    assert result["recording"]["advanced"] is True
    # Fake transport, so the protocol layer says "mock"; what matters is that
    # write progress did not promote anything.
    assert result["verification_level"] == "mock"
    assert result["verification"]["artifact"] == "unverified"
    assert result["verification"]["picture"] == "unverified"
    assert result["verification_level"] != "live_output_file_verified"


def test_picture_probe_is_reported_but_never_claims_proof(tmp_path):
    obs = FakeObs()
    result = started_recording(obs, tmp_path / "state")
    probe = result["picture_probe"]
    assert probe["proves_picture"] is False
    assert probe["available"] is True and probe["nonempty"] is True


def test_picture_probe_reports_unavailable_without_the_request(tmp_path):
    obs = FakeObs(screenshot_supported=False, available_requests=[
        r for r in FakeObs().available_requests if r != "GetSourceScreenshot"
    ])
    result = started_recording(obs, tmp_path / "state")
    assert result["picture_probe"]["available"] is False
    assert result["picture_probe"]["proves_picture"] is False


# --------------------------------------------------------------------------- #
# 6. an unconfirmable target must refuse, not read as "found"
# --------------------------------------------------------------------------- #
def test_preflight_refuses_when_obs_cannot_list_windows(tmp_path):
    obs = FakeObs(window_items_supported=False)
    result = invoke("preflight", obs, tmp_path / "state")
    assert result["ok"] is False
    assert result["error"]["category"] == "TARGET_UNCONFIRMED"
    assert result["checks"]["target_present"] == "unknown"


def test_start_refuses_when_obs_cannot_list_windows(tmp_path):
    obs = FakeObs(window_items_supported=False)
    result = invoke("start", obs, tmp_path / "state")
    assert result["ok"] is False
    assert result["error"]["category"] == "TARGET_UNCONFIRMED"
    assert obs.mutations.count("StartRecord") == 0
    assert obs.mutations == []


# --------------------------------------------------------------------------- #
# 7. the output directory must be provable before recording
# --------------------------------------------------------------------------- #
def test_start_refuses_an_unknown_record_directory_up_front(tmp_path):
    obs = FakeObs(record_directory="")
    result = invoke("start", obs, tmp_path / "state", record_dir=None)
    assert result["ok"] is False
    assert result["error"]["category"] == "OUTPUT_PATH_INVALID"
    assert "no record directory" in result["error"]["message"]
    assert obs.mutations.count("StartRecord") == 0


def test_start_refuses_a_relative_record_directory(tmp_path):
    obs = FakeObs()
    result = invoke("start", obs, tmp_path / "state", record_dir="recordings\\take")
    assert result["ok"] is False
    assert result["error"]["category"] == "OUTPUT_PATH_INVALID"
    assert "absolute" in result["error"]["message"]
    assert obs.mutations.count("StartRecord") == 0


def test_start_refuses_a_filesystem_root(tmp_path):
    obs = FakeObs()
    result = invoke("start", obs, tmp_path / "state", record_dir="D:\\")
    assert result["ok"] is False
    assert result["error"]["category"] == "OUTPUT_PATH_INVALID"
    assert "root" in result["error"]["message"]


def test_preflight_reports_an_unknown_directory_before_anything_records(tmp_path):
    obs = FakeObs(record_directory="")
    result = invoke("preflight", obs, tmp_path / "state", record_dir=None)
    assert result["ok"] is False
    assert result["error"]["category"] == "OUTPUT_PATH_INVALID"
    assert obs.mutations == []


# --------------------------------------------------------------------------- #
# 8. P0 follow-up: global audio that is not a scene item, and capability/
#    session values that are absent rather than wrong.
# --------------------------------------------------------------------------- #
def test_mic_special_input_refuses_with_only_the_target_in_the_scene(tmp_path):
    """Required counter-example: the scene holds just the target, mic1 exists.

    The global mic is not a scene item, so a scene-item-only check would pass
    and the microphone would be recorded.
    """
    obs = FakeObs(special_inputs={"mic1": "Mic/Aux"})
    result = invoke("preflight", obs, tmp_path / "state")
    assert result["ok"] is False
    assert result["error"]["category"] == "MIC_MIXED"
    assert "Mic/Aux" in result["error"]["message"]


def test_mic_special_input_blocks_start_without_touching_obs(tmp_path):
    obs = FakeObs(special_inputs={"mic1": "Mic/Aux"})
    result = invoke("start", obs, tmp_path / "state")
    assert result["ok"] is False
    assert result["error"]["category"] == "MIC_MIXED"
    assert obs.mutations == []


def test_desktop_special_input_refuses_with_only_the_target_in_the_scene(tmp_path):
    obs = FakeObs(special_inputs={"desktop1": "Desktop Audio"})
    result = invoke("preflight", obs, tmp_path / "state")
    assert result["ok"] is False
    assert result["error"]["category"] == "GLOBAL_AUDIO_MIXED"
    assert "not a scene item" in result["error"]["detail"]["why"]


def test_second_global_audio_channel_is_caught_too(tmp_path):
    obs = FakeObs(special_inputs={"desktop2": "Desktop Audio 2"})
    result = invoke("preflight", obs, tmp_path / "state")
    assert result["ok"] is False
    assert result["error"]["category"] == "GLOBAL_AUDIO_MIXED"


def test_an_extra_enabled_scene_source_is_refused(tmp_path):
    obs = FakeObs(
        extra_scene_items=[{"sourceName": "Nested Scene", "sceneItemEnabled": True, "inputKind": "scene"}]
    )
    result = invoke("preflight", obs, tmp_path / "state")
    assert result["ok"] is False
    assert result["error"]["category"] == "SCENE_NOT_DEDICATED"
    assert "Nested Scene" in result["error"]["message"]


def test_a_disabled_extra_source_is_tolerated(tmp_path):
    obs = FakeObs(
        extra_scene_items=[{"sourceName": "Nested Scene", "sceneItemEnabled": False, "inputKind": "scene"}]
    )
    result = invoke("preflight", obs, tmp_path / "state")
    assert result["ok"] is True, result.get("error")


def test_missing_current_profile_is_refused(tmp_path):
    """An absent current profile must not skip the comparison."""
    obs = FakeObs(mistype_fields={"GetProfileList.currentProfileName": ""})
    result = invoke("preflight", obs, tmp_path / "state")
    assert result["ok"] is False
    assert result["error"]["category"] == "PROFILE_MISMATCH"
    assert "did not report its current profile" in result["error"]["message"]


def test_absent_current_profile_field_is_a_broken_response(tmp_path):
    obs = FakeObs(omit_fields={"GetProfileList": ["currentProfileName"]})
    result = invoke("preflight", obs, tmp_path / "state")
    assert result["ok"] is False
    assert result["error"]["category"] == "BAD_RESPONSE"


def test_missing_current_scene_is_refused(tmp_path):
    obs = FakeObs(mistype_fields={"GetSceneList.currentProgramSceneName": ""})
    result = invoke("preflight", obs, tmp_path / "state")
    assert result["ok"] is False
    assert result["error"]["category"] == "SCENE_MISMATCH"


def test_missing_available_requests_is_a_broken_response(tmp_path):
    """availableRequests is a documented GetVersion field, not an optional hint."""
    obs = FakeObs(omit_fields={"GetVersion": ["availableRequests"]})
    result = invoke("capabilities", obs, tmp_path / "state")
    assert result["ok"] is False
    assert result["error"]["category"] == "BAD_RESPONSE"
    assert result["error"]["detail"]["field"] == "availableRequests"


def test_an_empty_request_list_refuses_everything(tmp_path):
    obs = FakeObs(available_requests=[])
    result = invoke("preflight", obs, tmp_path / "state")
    assert result["ok"] is False
    assert result["error"]["category"] == "RPC_UNSUPPORTED"


def test_capture_audio_is_sent_even_though_it_has_no_default(tmp_path):
    """`capture_audio` has no entry in GetInputDefaultSettings, so a
    defaults-based whitelist would silently drop it and record silence."""
    obs = FakeObs(source_settings={})  # a source that has never been configured
    assert "capture_audio" not in obs.default_settings
    assert "capture_audio" not in obs.source_settings

    result = started_recording(obs, tmp_path / "state")

    # The key reached OBS despite having no default, and the re-read confirms it.
    assert obs.source_settings["capture_audio"] is True
    assert result["actual_source"]["settings_effective"]["capture_audio"] is True
    assert result["audio"]["evidence_level"] == "settings_confirm_capture_enabled"


def test_preflight_still_reports_a_source_that_is_not_configured_for_audio(tmp_path):
    obs = FakeObs(source_settings={})
    result = invoke("preflight", obs, tmp_path / "state")
    assert result["ok"] is False
    assert result["error"]["category"] == "AUDIO_NOT_CAPTURED"
