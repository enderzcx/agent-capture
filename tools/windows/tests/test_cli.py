"""CLI tests: argument handling, stdin config, exit codes, JSON output."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

from agent_capture_win.cli import main  # noqa: E402


def run_cli(args, stdin=""):
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "agent_capture_win", *args],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def test_cli_reports_connection_failure_instead_of_pretending_success():
    # Nothing is listening on this port, so this must fail loudly.
    result = run_cli(["capabilities", "--config", "-"], stdin='{"port": 45999}')
    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["error"]["category"] in ("CONNECT_FAILED", "DEPENDENCY_MISSING")
    assert payload["backend"] == "obs-websocket"


def test_cli_rejects_malformed_config_json():
    result = run_cli(["preflight", "--config", "-"], stdin="{not json")
    assert result.returncode == 3
    payload = json.loads(result.stdout)
    assert payload["error"]["category"] == "CONFIG_INVALID"


def test_cli_rejects_unknown_config_fields():
    result = run_cli(["preflight", "--config", "-"], stdin='{"profil": "typo"}')
    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["error"]["category"] == "CONFIG_INVALID"
    assert "unknown config field" in payload["error"]["message"]


def test_cli_rejects_an_unknown_command():
    result = run_cli(["record-forever", "--config", "-"], stdin="{}")
    assert result.returncode != 0
    assert "invalid choice" in result.stderr


def test_cli_accepts_a_config_file(tmp_path):
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps({"port": 45998}), encoding="utf-8")
    result = run_cli(["capabilities", "--config", str(path)])
    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["error"]["category"] in ("CONNECT_FAILED", "DEPENDENCY_MISSING")


def test_cli_empty_config_means_defaults():
    result = run_cli(["capabilities", "--config", "-"], stdin="")
    assert result.returncode == 2  # nothing to connect to, but the config parsed
    assert json.loads(result.stdout)["ok"] is False


def test_cli_main_returns_usage_error_for_a_missing_file():
    assert main(["preflight", "--config", "/definitely/not/here.json"]) == 3


def test_cli_reports_version():
    result = run_cli(["--version"])
    assert result.returncode == 0
    assert result.stdout.strip()
