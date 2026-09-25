"""Backend tests: the required refusal and evidence cases, end to end through ``run()``.

Each test drives the public ``run(command, config)`` entry point with a
simulated OBS session, so the config validation, the capability gate, the
policy checks and the envelope shape are all exercised together.

Nothing here proves that a real Windows capture works.  It proves the protocol
handling and the refusal logic, and the envelopes say so via
``verification_level="mock"``.
"""

from __future__ import annotations

import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from agent_capture_win import run  # noqa: E402
from agent_capture_win.fake import (  # noqa: E402
    DEFAULT_RECORD_DIR,
    DEFAULT_SOURCE_NAME,
    WINDOW_STRING,
    FakeObs,
)

TARGET = {
    "title": "Slay the Spire 2",
    "class": "UnityWndClass",
    "exe": "SlayTheSpire2.exe",
    "priority": "exe",
}


def cfg(state_dir, **overrides):
    base = {
        "profile": "agent-capture",
        "scene_collection": "agent-capture",
        "scene": "agent-capture-window",
        "source_name": DEFAULT_SOURCE_NAME,
        "target": dict(TARGET),
        "record_dir": DEFAULT_RECORD_DIR,
        "run_id": "take-001",
        "state_dir": str(state_dir),
        "capture_audio": True,
    }
    base.update(overrides)
    return base


def invoke(command, obs, state_dir, **overrides):
    """Run one command against a simulated OBS; output files are probed, not read."""
    file_probe = overrides.pop("file_probe", None) or (
        lambda path: {"exists": True, "size_bytes": 4096, "mtime": time.time()}
    )
    dir_probe = overrides.pop("dir_probe", None) or (lambda path: [])
    kwargs = {}
    for key in ("clock", "sleep"):
        if key in overrides:
            kwargs[key] = overrides.pop(key)
    return run(
        command,
        cfg(state_dir, **overrides),
        transport=obs.transport(),
        file_probe=file_probe,
        dir_probe=dir_probe,
        **kwargs,
    )


def started_recording(obs, state_dir, **overrides):
    result = invoke("start", obs, state_dir, **overrides)
    assert result["ok"] is True, result.get("error")
    return result


# --------------------------------------------------------------------------- #
# envelope shape
# --------------------------------------------------------------------------- #
def test_every_envelope_has_the_documented_keys(tmp_path):
    result = invoke("preflight", FakeObs(), tmp_path / "state")
    for key in ("schema", "backend", "command", "ok", "verification_level", "obs", "target", "recording", "output", "error"):
        assert key in result, key
    assert result["schema"] == "agent-capture-win/1"
    assert result["backend"] == "obs-websocket"


def test_injected_transport_is_reported_as_mock_not_live(tmp_path):
    result = invoke("capabilities", FakeObs(), tmp_path / "state")
    assert result["ok"] is True
    assert result["verification_level"] == "mock"


# --------------------------------------------------------------------------- #
# capabilities
# --------------------------------------------------------------------------- #
def test_capabilities_emits_the_main_registry_row(tmp_path):
    result = invoke("capabilities", FakeObs(), tmp_path / "state")
    row = result["platform_capability"]
    assert result["backend_id"] == "windows-obs-websocket"
    assert row["backend"] == "windows-obs-websocket"
    assert row["video_granularities"] == ["window"]  # never claims app-level video
    assert row["audio_granularities"] == ["none", "app"]
    assert row["audio_per_window_os_capable"] is False
    assert row["verified_level"] == "mock"
    assert "display" in row["unimplemented"] and "microphone" in row["unimplemented"]


def test_capabilities_reports_honest_limits(tmp_path):
    result = invoke("capabilities", FakeObs(), tmp_path / "state")
    caps = result["capabilities"]
    assert caps["hwnd_pinning"] is False
    assert caps["whole_screen_fallback"] is False
    assert caps["audio_granularity"] == "process"
    assert caps["mic_supported"] is False
    assert caps["desktop_audio_supported"] is False
    assert "pid" in caps["unsupported_match_dimensions"]
    assert "title" in caps["supported_match_dimensions"]


def test_capabilities_detects_missing_record_directory_requests(tmp_path):
    obs = FakeObs(
        ws_version="5.2.0",
        available_requests=[r for r in FakeObs().available_requests if r not in ("SetRecordDirectory",)],
    )
    result = invoke("capabilities", obs, tmp_path / "state")
    caps = result["capabilities"]
    assert caps["set_record_directory_supported"] is False
    assert caps["get_record_directory_supported"] is True


# --------------------------------------------------------------------------- #
# preflight is read-only
# --------------------------------------------------------------------------- #
def test_preflight_ok_path_and_no_mutations(tmp_path):
    obs = FakeObs()
    result = invoke("preflight", obs, tmp_path / "state")
    assert result["ok"] is True, result.get("error")
    assert result["read_only"] is True
    assert obs.mutations == []
    assert result["checks"]["idle"] is True
    assert result["actual_source"]["settings_source"] == "GetInputSettings overlaid on GetInputDefaultSettings"


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({"streaming": True}, "OBS_BUSY"),
        ({"recording": True}, "OBS_BUSY"),
        ({"replay_buffer": True}, "OBS_BUSY"),
        ({"virtual_cam": True}, "OBS_BUSY"),
    ],
)
def test_preflight_refuses_a_busy_obs(tmp_path, kwargs, expected):
    result = invoke("preflight", FakeObs(**kwargs), tmp_path / "state")
    assert result["ok"] is False
    assert result["error"]["category"] == expected


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({"profile": "Default"}, "PROFILE_MISMATCH"),
        ({"scene_collection": "Default"}, "SCENE_COLLECTION_MISMATCH"),
        ({"scene": "Some Other Scene"}, "SCENE_MISMATCH"),
    ],
)
def test_preflight_refuses_the_wrong_session(tmp_path, kwargs, expected):
    result = invoke("preflight", FakeObs(**kwargs), tmp_path / "state")
    assert result["ok"] is False
    assert result["error"]["category"] == expected


def test_preflight_refuses_when_no_dedicated_session_is_configured(tmp_path):
    result = invoke(
        "preflight", FakeObs(), tmp_path / "state", profile=None, scene_collection=None, scene=None
    )
    assert result["ok"] is False
    assert result["error"]["category"] in ("PROFILE_MISMATCH", "SCENE_MISMATCH")


# --------------------------------------------------------------------------- #
# audio isolation
# --------------------------------------------------------------------------- #
def test_preflight_refuses_global_desktop_audio(tmp_path):
    obs = FakeObs(special_inputs={"desktop1": "Desktop Audio"})
    result = invoke("preflight", obs, tmp_path / "state")
    assert result["ok"] is False
    assert result["error"]["category"] == "GLOBAL_AUDIO_MIXED"


def test_preflight_refuses_a_microphone(tmp_path):
    obs = FakeObs(special_inputs={"mic1": "Mic/Aux"})
    result = invoke("preflight", obs, tmp_path / "state")
    assert result["ok"] is False
    assert result["error"]["category"] == "MIC_MIXED"


def test_preflight_refuses_silent_configuration(tmp_path):
    obs = FakeObs(source_settings={"capture_audio": False})
    result = invoke("preflight", obs, tmp_path / "state")
    assert result["ok"] is False
    assert result["error"]["category"] == "AUDIO_NOT_CAPTURED"


def test_muted_source_is_refused(tmp_path):
    obs = FakeObs(source_muted=True)
    result = invoke("preflight", obs, tmp_path / "state")
    assert result["ok"] is False
    assert result["error"]["category"] == "AUDIO_NOT_CAPTURED"
    assert "muted" in result["error"]["message"]


# --------------------------------------------------------------------------- #
# targets
# --------------------------------------------------------------------------- #
def test_targets_are_redacted_by_default(tmp_path):
    obs = FakeObs(
        window_items=[
            {"itemName": "[chrome.exe]: Private Bank Tab", "itemValue": "Private Bank Tab:Chrome_WidgetWin_1:chrome.exe", "itemEnabled": True},
            {"itemName": "[SlayTheSpire2.exe]: Slay the Spire 2", "itemValue": WINDOW_STRING, "itemEnabled": True},
        ]
    )
    result = invoke("targets", obs, tmp_path / "state")
    assert result["ok"] is True
    assert result["targets"]["redacted"] is True
    body = json.dumps(result)
    assert "Private Bank Tab" not in body
    assert "chrome.exe" not in body
    assert result["targets"]["candidate_count"] == 2


def test_targets_reveal_only_when_asked(tmp_path):
    obs = FakeObs()
    result = invoke("targets", obs, tmp_path / "state", reveal_window_details=True)
    assert result["targets"]["redacted"] is False
    assert any(item.get("exe") for item in result["targets"]["candidates"])


def test_targets_states_that_hwnd_pinning_is_not_available(tmp_path):
    result = invoke("targets", FakeObs(), tmp_path / "state")
    assert result["target"]["hwnd_pinning"] is False
    assert result["target"]["match_semantics"] == "obs_window_priority"


def test_targets_reports_when_obs_cannot_enumerate_windows(tmp_path):
    obs = FakeObs(window_items_supported=False)
    result = invoke("targets", obs, tmp_path / "state")
    assert result["targets"]["enumeration_available"] is False
    assert result["targets"]["candidates"] == []


# --------------------------------------------------------------------------- #
# target refusals
# --------------------------------------------------------------------------- #
def test_empty_target_is_refused(tmp_path):
    result = invoke("preflight", FakeObs(), tmp_path / "state", target={})
    assert result["ok"] is False
    assert result["error"]["category"] == "TARGET_EMPTY"


def test_unsafe_default_priority_is_refused(tmp_path):
    result = invoke(
        "preflight",
        FakeObs(),
        tmp_path / "state",
        target={"title": "Some Page", "class": "Chrome_WidgetWin_1", "exe": "chrome.exe", "priority": "class"},
    )
    assert result["ok"] is False
    assert result["error"]["category"] == "TARGET_UNSAFE_PRIORITY"


def test_partial_target_is_refused_by_default(tmp_path):
    result = invoke(
        "preflight", FakeObs(), tmp_path / "state", target={"title": "Slay the Spire 2", "priority": "title"}
    )
    assert result["ok"] is False
    assert result["error"]["category"] == "TARGET_AMBIGUOUS"


def test_target_missing_from_obs_window_list_is_refused(tmp_path):
    obs = FakeObs(window_items=[{"itemValue": "Something Else:Class:other.exe", "itemEnabled": True}])
    result = invoke("preflight", obs, tmp_path / "state")
    assert result["ok"] is False
    assert result["error"]["category"] == "TARGET_NOT_FOUND"


def test_missing_source_is_refused_by_preflight(tmp_path):
    result = invoke("preflight", FakeObs(source_exists=False), tmp_path / "state")
    assert result["ok"] is False
    assert result["error"]["category"] == "TARGET_NOT_FOUND"


def test_non_window_capture_source_is_refused(tmp_path):
    result = invoke("preflight", FakeObs(source_kind="monitor_capture"), tmp_path / "state")
    assert result["ok"] is False
    assert result["error"]["category"] == "TARGET_NOT_FOUND"
    assert "not a window_capture" in result["error"]["message"]


# --------------------------------------------------------------------------- #
# capability gating on start
# --------------------------------------------------------------------------- #
def test_start_refuses_when_set_record_directory_is_unavailable(tmp_path):
    obs = FakeObs(
        ws_version="5.2.0",
        record_directory="C:\\Users\\example\\Videos",
        available_requests=[r for r in FakeObs().available_requests if r != "SetRecordDirectory"],
    )
    result = invoke("start", obs, tmp_path / "state")
    assert result["ok"] is False
    assert result["error"]["category"] == "RPC_UNSUPPORTED"
    assert "SetRecordDirectory" in result["error"]["message"]
    assert "StartRecord" not in obs.mutations


# --------------------------------------------------------------------------- #
# start / status / stop happy path
# --------------------------------------------------------------------------- #
def test_start_records_evidence_not_just_a_200(tmp_path):
    obs = FakeObs()
    result = started_recording(obs, tmp_path / "state")
    assert result["recording"]["evidence"] == "output_bytes_advanced"
    assert result["recording"]["evidence_scope"] == "output_write_progress_only__not_picture_proof"
    assert result["recording"]["picture_verified"] is False
    assert result["recording"]["artifact_verified"] is False
    assert result["recording"]["advanced"] is True
    assert result["ownership"]["session_token"]
    assert result["ownership"]["baseline_bytes"] == 0
    assert "StartRecord" in obs.mutations


def test_start_status_stop_round_trip(tmp_path):
    state_dir = tmp_path / "state"
    obs = FakeObs()
    started = started_recording(obs, state_dir)
    token = started["ownership"]["session_token"]

    status = invoke("status", obs, state_dir, session_token=token)
    assert status["ok"] is True, status.get("error")
    assert status["ownership"]["owned"] is True
    assert status["recording"]["active"] is True

    stopped = invoke("stop", obs, state_dir, session_token=token)
    assert stopped["ok"] is True, stopped.get("error")
    assert stopped["output"]["verified"] is True
    assert stopped["ownership"]["verified_owner"] is True
    assert "StopRecord" in obs.mutations
    assert not os.path.exists(os.path.join(str(state_dir), "take-001.json"))


def test_start_creates_its_own_source_when_missing(tmp_path):
    obs = FakeObs(source_exists=False)
    result = started_recording(obs, tmp_path / "state")
    assert "CreateInput" in obs.mutations
    assert obs.source_exists is True
    assert result["actual_source"]["kind"] == "window_capture"
    assert result["actual_source"]["settings_effective"]["capture_audio"] is True


def test_start_refuses_to_create_a_source_without_a_dedicated_scene(tmp_path):
    obs = FakeObs(source_exists=False)
    result = invoke("start", obs, tmp_path / "state", scene=None)
    assert result["ok"] is False
    assert result["error"]["category"] in ("CONFIG_INVALID", "SCENE_MISMATCH")
    assert "CreateInput" not in obs.mutations


def test_start_refuses_when_source_creation_is_disabled(tmp_path):
    obs = FakeObs(source_exists=False)
    result = invoke("start", obs, tmp_path / "state", create_source_if_missing=False)
    assert result["ok"] is False
    assert result["error"]["category"] == "TARGET_NOT_FOUND"


def test_start_refuses_when_obs_is_already_recording(tmp_path):
    obs = FakeObs(recording=True)
    result = invoke("start", obs, tmp_path / "state")
    assert result["ok"] is False
    assert result["error"]["category"] == "OBS_BUSY"


# --------------------------------------------------------------------------- #
# no progress after start
# --------------------------------------------------------------------------- #
def test_start_without_progress_fails_and_cleans_up(tmp_path):
    obs = FakeObs(advance_on_poll=False)
    result = invoke("start", obs, tmp_path / "state", start_evidence_timeout_s=0.0)
    assert result["ok"] is False
    assert result["error"]["category"] == "NO_PROGRESS"
    assert result["error"]["detail"]["cleanup"] == "stopped_after_no_progress"
    assert "StopRecord" in obs.mutations  # the output we started was cleaned up
    assert obs.recording is False
    assert not os.path.exists(os.path.join(str(tmp_path / "state"), "take-001.json"))


def test_global_frame_counter_is_not_evidence_of_a_picture(tmp_path):
    """GetStats.outputTotalFrames is a global counter, so it must not count.

    Bytes stay put while the frame counter climbs: the recording still has to
    fail with NO_PROGRESS rather than be reported as capturing.
    """
    obs = FakeObs(advance_bytes=0, advance_frames=300)
    result = invoke("start", obs, tmp_path / "state", start_evidence_timeout_s=0.0)
    assert result["ok"] is False
    assert result["error"]["category"] == "NO_PROGRESS"


# --------------------------------------------------------------------------- #
# source drift / scene change
# --------------------------------------------------------------------------- #
def test_status_reports_source_drift(tmp_path):
    state_dir = tmp_path / "state"
    obs = FakeObs()
    started = started_recording(obs, state_dir)
    token = started["ownership"]["session_token"]

    # Someone re-pointed the source at another window mid-recording.
    obs.source_settings["window"] = "Other Window:OtherClass:other.exe"
    status = invoke("status", obs, state_dir, session_token=token)
    assert status["ok"] is False
    assert status["error"]["category"] == "SOURCE_DRIFT"
    assert "window" in status["source_drift"]


def test_status_reports_scene_change(tmp_path):
    state_dir = tmp_path / "state"
    obs = FakeObs()
    started = started_recording(obs, state_dir)
    token = started["ownership"]["session_token"]

    obs.scene = "Someone Elses Scene"
    status = invoke("status", obs, state_dir, session_token=token)
    assert status["ok"] is False
    assert status["error"]["category"] == "SCENE_MISMATCH"


def test_status_refuses_an_output_this_task_does_not_own(tmp_path):
    obs = FakeObs(recording=True, record_bytes=100_000)
    status = invoke("status", obs, tmp_path / "state")
    assert status["ok"] is False
    assert status["error"]["category"] == "NOT_OWNER"
    assert status["recording"]["active"] is True  # still reports the live truth


def test_status_refuses_a_foreign_session_token(tmp_path):
    state_dir = tmp_path / "state"
    obs = FakeObs()
    started_recording(obs, state_dir)
    status = invoke("status", obs, state_dir, session_token="someone-elses-token")
    assert status["ok"] is False
    assert status["error"]["category"] == "NOT_OWNER"


# --------------------------------------------------------------------------- #
# owner-only stop
# --------------------------------------------------------------------------- #
def test_stop_requires_a_session_token(tmp_path):
    state_dir = tmp_path / "state"
    obs = FakeObs()
    started_recording(obs, state_dir)
    result = invoke("stop", obs, state_dir)
    assert result["ok"] is False
    assert result["error"]["category"] == "NOT_OWNER"
    assert "StopRecord" not in obs.mutations


def test_stop_refuses_a_wrong_session_token(tmp_path):
    state_dir = tmp_path / "state"
    obs = FakeObs()
    started_recording(obs, state_dir)
    result = invoke("stop", obs, state_dir, session_token="not-the-token")
    assert result["ok"] is False
    assert result["error"]["category"] == "NOT_OWNER"
    assert "StopRecord" not in obs.mutations
    assert obs.recording is True  # the user's output was left alone


def test_stop_refuses_a_stale_state(tmp_path):
    state_dir = tmp_path / "state"
    obs = FakeObs()
    started = started_recording(obs, state_dir)
    token = started["ownership"]["session_token"]
    result = invoke(
        "stop", obs, state_dir, session_token=token, max_record_seconds=1, clock=lambda: time.time() + 3600
    )
    assert result["ok"] is False
    assert result["error"]["category"] == "STALE_STATE"
    assert "StopRecord" not in obs.mutations


def test_stop_refuses_when_bytes_never_advanced(tmp_path):
    state_dir = tmp_path / "state"
    obs = FakeObs()
    started = started_recording(obs, state_dir)
    token = started["ownership"]["session_token"]
    obs.advance_on_poll = False
    obs.record_bytes = 0
    result = invoke("stop", obs, state_dir, session_token=token)
    assert result["ok"] is False
    assert result["error"]["category"] == "NOT_OWNER"
    assert "StopRecord" not in obs.mutations


def test_stop_refuses_when_nothing_is_running(tmp_path):
    state_dir = tmp_path / "state"
    obs = FakeObs()
    started = started_recording(obs, state_dir)
    token = started["ownership"]["session_token"]
    obs.recording = False
    result = invoke("stop", obs, state_dir, session_token=token)
    assert result["ok"] is False
    assert result["error"]["category"] == "STALE_STATE"


def test_stop_refuses_without_a_state_file(tmp_path):
    obs = FakeObs(recording=True, record_bytes=10_000)
    result = invoke("stop", obs, tmp_path / "state", session_token="whatever")
    assert result["ok"] is False
    assert result["error"]["category"] == "STALE_STATE"
    assert "StopRecord" not in obs.mutations


# --------------------------------------------------------------------------- #
# output verification
# --------------------------------------------------------------------------- #
def test_stop_refuses_an_output_outside_the_dedicated_directory(tmp_path):
    state_dir = tmp_path / "state"
    obs = FakeObs(stop_record_output_path="C:\\Users\\example\\Videos\\master.mkv")
    started = started_recording(obs, state_dir)
    token = started["ownership"]["session_token"]
    result = invoke("stop", obs, state_dir, session_token=token)
    assert result["ok"] is False
    assert result["error"]["category"] == "OUTPUT_PATH_INVALID"
    assert result["output"]["verified"] is False


def test_stop_refuses_to_claim_a_preexisting_master_file(tmp_path):
    state_dir = tmp_path / "state"
    master = DEFAULT_RECORD_DIR + "\\master.mkv"
    obs = FakeObs(stop_record_output_path=master)
    started = started_recording(obs, state_dir, dir_probe=lambda path: ["master.mkv", "old-take.mkv"])
    token = started["ownership"]["session_token"]
    result = invoke(
        "stop", obs, state_dir, session_token=token, dir_probe=lambda path: ["master.mkv", "old-take.mkv"]
    )
    assert result["ok"] is False
    assert result["error"]["category"] == "OUTPUT_PATH_INVALID"
    assert result["error"]["detail"]["preexisting"] is True


def test_stop_reports_a_missing_output_file(tmp_path):
    state_dir = tmp_path / "state"
    obs = FakeObs()
    started = started_recording(obs, state_dir)
    token = started["ownership"]["session_token"]
    result = invoke(
        "stop",
        obs,
        state_dir,
        session_token=token,
        file_probe=lambda path: {"exists": False, "size_bytes": None, "mtime": None},
    )
    assert result["ok"] is False
    assert result["error"]["category"] == "OUTPUT_NOT_FOUND"
    assert result["notes"]  # tells the operator not to treat the file as a good take


def test_stop_reports_an_empty_output_file(tmp_path):
    state_dir = tmp_path / "state"
    obs = FakeObs()
    started = started_recording(obs, state_dir)
    token = started["ownership"]["session_token"]
    result = invoke(
        "stop",
        obs,
        state_dir,
        session_token=token,
        file_probe=lambda path: {"exists": True, "size_bytes": 0, "mtime": time.time()},
    )
    assert result["ok"] is False
    assert result["error"]["category"] == "OUTPUT_EMPTY"


def test_stop_reports_a_file_that_predates_the_recording(tmp_path):
    state_dir = tmp_path / "state"
    obs = FakeObs()
    started = started_recording(obs, state_dir)
    token = started["ownership"]["session_token"]
    result = invoke(
        "stop",
        obs,
        state_dir,
        session_token=token,
        file_probe=lambda path: {"exists": True, "size_bytes": 4096, "mtime": time.time() - 100_000},
    )
    assert result["ok"] is False
    assert result["error"]["category"] == "OUTPUT_PATH_INVALID"


# --------------------------------------------------------------------------- #
# config validation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "bad,needle",
    [
        ({"unknown_field": 1}, "unknown config field"),
        ({"port": "4455"}, "wrong type"),
        ({"port": 0}, "out of range"),
        ({"start_evidence_timeout_s": float("nan")}, "finite"),
        ({"poll_interval_s": 0}, "> 0"),
        ({"run_id": "../escape"}, "run_id"),
        ({"run_id": "a/b"}, "run_id"),
        ({"password_env": "A", "password_file": "/tmp/x"}, "not both"),
        ({"target": "Slay the Spire 2"}, "wrong type"),
    ],
)
def test_bad_config_is_refused_before_touching_obs(tmp_path, bad, needle):
    obs = FakeObs()
    result = run("preflight", cfg(tmp_path / "state", **bad), transport=obs.transport())
    assert result["ok"] is False
    assert result["error"]["category"] == "CONFIG_INVALID"
    assert needle in result["error"]["message"]
    assert obs.requests_seen == []


def test_unknown_command_is_refused():
    result = run("record-everything", {})
    assert result["ok"] is False
    assert result["error"]["category"] == "CONFIG_INVALID"


def test_start_requires_a_run_id(tmp_path):
    result = invoke("start", FakeObs(), tmp_path / "state", run_id=None)
    assert result["ok"] is False
    assert result["error"]["category"] == "CONFIG_INVALID"


def test_credentials_are_never_echoed_in_the_result(tmp_path, monkeypatch):
    monkeypatch.setenv("SOME_SECRET_VAR", "hunter2-do-not-leak")
    result = invoke("preflight", FakeObs(), tmp_path / "state", password_env="SOME_SECRET_VAR")
    assert "hunter2-do-not-leak" not in json.dumps(result)


def test_config_serialisation_reports_the_env_name_only(tmp_path):
    from agent_capture_win.backend import Config

    dumped = Config.from_dict(cfg(tmp_path / "state", password_env="SOME_SECRET_VAR")).to_dict()
    assert dumped["password_env"] == "SOME_SECRET_VAR"
    assert dumped["session_token"] is None
    assert "password" not in json.dumps(dumped).replace("password_env", "").replace("password_file", "")
