#!/usr/bin/env python3
"""agent_capture CLI 的测试：命令分发、退出码、错误路径、与 worker 的衔接。

分两部分：
  · **不需要权限**的部分（capability / 参数校验 / 错误路径）—— 直接跑真 CLI；
  · **生命周期**部分（start → status → stop → verify）—— 用假后端 + 假录制器，
    这样不需要屏幕录制权限、不需要真窗口，也不会打扰用户正在用的东西。

真权限下的实录证据见 EARLY-CHECK.md（本测试**不**声称覆盖了真实采集）。

运行：  python3 tests/test_agent_capture_cli.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import capture_state as cs  # noqa: E402

PASS = 0
FAIL = 0


def ck(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"\033[32m✓\033[0m {name}")
    else:
        FAIL += 1
        print(f"\033[31m✗\033[0m {name}" + (f"  — {detail}" if detail else ""))


def cli(*args, env=None, timeout=90):
    e = dict(os.environ)
    if env:
        e.update(env)
    return subprocess.run([sys.executable, str(SCRIPTS / "agent_capture.py"), *args],
                          capture_output=True, text=True, timeout=timeout, env=e)


def cli_json(*args, **kw):
    r = cli(*args, **kw)
    try:
        return json.loads(r.stdout), r
    except ValueError:
        return None, r


# ---------------------------------------------------------------------------
# 假后端 + 假录制器（生命周期测试用）
# ---------------------------------------------------------------------------
FAKE_REC = r'''#!/usr/bin/env python3
import json, os, signal, sys, time
from pathlib import Path
def arg(n, d=""):
    a = sys.argv
    return a[a.index(n) + 1] if n in a else d
out = Path(arg("--out")); live = Path(arg("--live-status")) if arg("--live-status") else None
token = arg("--live-token"); metrics = Path(arg("--json")) if arg("--json") else None
dur = float(arg("--duration", "0") or 0)
stop = {"v": False}
signal.signal(signal.SIGTERM, lambda *_: stop.__setitem__("v", True))
def wl(**kw):
    if live is None: return
    o = {"tool": "FakeRec", "run_token": token, "capture_initialized": False,
         "first_video_frame": False, "audio_path_has_data": False,
         "audio_signal_observed": False, "effectively_started": False,
         "video_frames": 0, "audio_buffers": 0}
    o.update(kw); live.write_text(json.dumps(o), encoding="utf-8")
def wm(video=True, audio=True):
    if metrics is None: return
    metrics.write_text(json.dumps({"writer_status": "completed", "problems": [],
        "advisories": [], "verdict": {"video_frames_present": video,
        "video_frames_arriving": video, "audio_track_present": audio,
        "audio_has_signal": audio, "audio_peak_dbfs": -12.0 if audio else -160.0,
        "tracks_verified": True}}), encoding="utf-8")
wl(capture_initialized=True, first_video_frame=True, audio_path_has_data=True)
time.sleep(0.8)
wl(capture_initialized=True, first_video_frame=True, audio_path_has_data=True,
   audio_signal_observed=True, effectively_started=True)
end = time.time() + (dur or 3)
while time.time() < end and not stop["v"]:
    time.sleep(0.1)
wm(); sys.exit(0)
'''

FAKE_BACKEND = r'''import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
def availability():
    return {"available": True, "reason": "ok", "backend": "fake", "platform": "macos",
            "verified_level": "mock_only", "verified_scope": "test double"}
def probe(timeout=30.0):
    return {"ok": True, "backend": "fake", "platform": "macos",
            "verified_level": "mock_only", "verified_scope": "test double",
            "applications": [{"name": "FakeApp", "bundle_id": "com.fake.app", "pid": 4242}],
            "windows": [{"windowID": 501, "title": "Fake Window",
                         "owner_pid": 4242, "on_screen": True,
                         "owner_bundle_id": "com.fake.app"}]}
def preflight(target, probe_json=None, timeout=30.0):
    return {"ok": True, "problems": [], "argv": ["--fake"], "read_only": True}
def build_start_argv(target, out_path, *, duration=0.0, fps=30, max_width=1920,
                     focus_log="", status_file="", metrics_json="",
                     live_status="", live_token="", overwrite=False, no_video=False):
    argv = [sys.executable, str(HERE / "fake_recorder.py"), "--out", out_path]
    if duration: argv += ["--duration", str(duration)]
    if metrics_json: argv += ["--json", metrics_json]
    if live_status: argv += ["--live-status", live_status]
    if live_token: argv += ["--live-token", live_token]
    return argv
def verify(media_path, expect="av"):
    return {"result": "pass", "media": media_path}
'''


def fake_env(tmp: Path):
    tools = tmp / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    (tools / "fake_recorder.py").write_text(FAKE_REC, encoding="utf-8")
    (tools / "backend.py").write_text(FAKE_BACKEND, encoding="utf-8")
    return {"AGENT_CAPTURE_BACKEND_DIR": str(tools)}


# ---------------------------------------------------------------------------
# 不需要权限的部分
# ---------------------------------------------------------------------------

def test_capability() -> None:
    d, r = cli_json("capability")
    ck("capability 退出码 0", r.returncode == 0, f"rc={r.returncode}")
    ck("含 backend", bool(d and d.get("backend")), str(d)[:120] if d else r.stderr[:120])
    ck("含 verified_level", bool(d and d.get("verified_level")), str(d)[:120] if d else "")
    ck("含 unimplemented 说明", bool(d and d.get("unimplemented")))


def test_no_target_is_usage_error() -> None:
    d, r = cli_json("preflight")
    ck("无目标 preflight 退出码 2", r.returncode == 2, f"rc={r.returncode}")
    ck("明确报「没有画面目标」",
       bool(d and any("没有画面目标" in p for p in (d.get("problems") or []))),
       str(d)[:160] if d else r.stderr[:160])


def test_unimplemented_granularity_rejected() -> None:
    d, r = cli_json("preflight", "--display")
    ck("整屏请求被拒（本后端未实现）", r.returncode == 2, f"rc={r.returncode}")
    ck("说明是后端未实现",
       bool(d and any("本后端未实现" in p for p in (d.get("problems") or []))),
       str(d)[:160] if d else "")


def test_cross_app_audio_rejected_by_cli() -> None:
    d, r = cli_json("preflight", "--video-app", "com.x.A", "--audio-app", "com.x.B")
    ck("跨 app 音源被拒", r.returncode == 2, f"rc={r.returncode}")
    ck("说明原因",
       bool(d and any("不同 app" in p for p in (d.get("problems") or []))),
       str(d)[:160] if d else "")


def test_invalid_audio_mode_rejected() -> None:
    d, r = cli_json("preflight", "--video-app", "com.x.A", "--audio-mode", "bogus")
    ck("非法 --audio-mode 被拒", r.returncode == 2, f"rc={r.returncode}")
    ck("列出合法值",
       bool(d and any("合法" in p for p in (d.get("problems") or []))),
       str(d)[:160] if d else "")


def test_status_unknown_run() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="cli-unknown."))
    d, r = cli_json("status", "--job-dir", str(tmp), "--run-id", "nope")
    ck("未知 run 的 status 退出码 2", r.returncode == 2, f"rc={r.returncode}")
    ck("给出错误信息", bool(d and d.get("error")), str(d)[:120] if d else r.stderr[:120])
    shutil.rmtree(tmp, ignore_errors=True)


def test_stop_unknown_run() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="cli-stopunknown."))
    d, r = cli_json("stop", "--job-dir", str(tmp), "--run-id", "nope")
    ck("未知 run 的 stop 退出码 2", r.returncode == 2, f"rc={r.returncode}")
    shutil.rmtree(tmp, ignore_errors=True)


def test_verify_without_media() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="cli-verify."))
    cs.init_run(tmp, "r1", "t", {"platform": "macos"})
    d, r = cli_json("verify", "--job-dir", str(tmp), "--run-id", "r1")
    ck("没有媒体路径时 verify 退出码 2", r.returncode == 2, f"rc={r.returncode}")
    ck("说明缺媒体", bool(d and "媒体" in (d.get("error") or "")),
       str(d)[:140] if d else r.stderr[:140])
    shutil.rmtree(tmp, ignore_errors=True)


def test_run_id_escape_rejected_by_cli() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="cli-escape."))
    d, r = cli_json("status", "--job-dir", str(tmp), "--run-id", "../../etc/passwd")
    ck("逃逸 run_id 被拒（退出码 2）", r.returncode == 2, f"rc={r.returncode}")
    shutil.rmtree(tmp, ignore_errors=True)


def test_start_requires_args() -> None:
    r = cli("start")
    ck("start 缺必需参数 → argparse 报错", r.returncode != 0, f"rc={r.returncode}")


def test_targets_without_permission_is_honest() -> None:
    """targets 在拿不到目标时**明确报错**，不返回空列表当成功。"""
    d, r = cli_json("targets", "--platform", "linux")
    ck("无后端平台 targets 退出码 2", r.returncode == 2, f"rc={r.returncode}")
    ck("给出错误而非空成功", bool(d and d.get("error")), str(d)[:140] if d else "")


# ---------------------------------------------------------------------------
# 生命周期（假后端 + 假录制器）
# ---------------------------------------------------------------------------

def test_lifecycle_start_status_stop_verify() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="cli-life."))
    env = fake_env(tmp)
    job = tmp / "job"
    out = job / "r1.mp4"

    d, r = cli_json("start", "--video-app", "com.fake.app", "--video-window", "501",
                    "--audio-app", "com.fake.app",
                    "--job-dir", str(job), "--run-id", "r1", "--out", str(out),
                    "--duration", "4", "--expect", "av", env=env)
    ck("start 退出码 0", r.returncode == 0, f"rc={r.returncode} {r.stderr[:160]}")
    ck("start 返回 run_id 与 worker_pid",
       bool(d and d.get("run_id") == "r1" and d.get("worker_pid")), str(d)[:160])

    # 等 worker 走到 running（有效开始）
    status = None
    for _ in range(60):
        sd, _ = cli_json("status", "--job-dir", str(job), "--run-id", "r1", env=env)
        status = (sd or {}).get("run", {}).get("status")
        if status in ("running", "stopping", "stopped", "verifying",
                      "verified", "verify_failed", "failed"):
            if status != "starting":
                break
        time.sleep(0.25)
    ck("status 观察到有效开始后的状态", status == "running", f"status={status}")

    # 停止（请求语义）
    sd, sr = cli_json("stop", "--job-dir", str(job), "--run-id", "r1", env=env)
    ck("stop 退出码 0", sr.returncode == 0, f"rc={sr.returncode}")
    ck("stop 返回 nonce（请求语义，不是裸 PID）",
       bool(sd and (sd.get("stop_request") or {}).get("nonce")), str(sd)[:160])

    final = None
    for _ in range(80):
        sd, _ = cli_json("status", "--job-dir", str(job), "--run-id", "r1", env=env)
        final = (sd or {}).get("run", {}).get("status")
        if final in ("verified", "verify_failed", "failed", "done"):
            break
        time.sleep(0.25)
    ck("停止后收敛到 verified", final == "verified", f"final={final}")

    sd, _ = cli_json("status", "--job-dir", str(job), "--run-id", "r1", env=env)
    summ = (sd or {}).get("summary") or {}
    ck("summary 三结论分开报",
       set(("capture_integrity", "game_result", "postproduction_ready")) <= set(summ),
       str(summ))
    ck("capture_integrity=pass", summ.get("capture_integrity") == "pass", str(summ))

    vd, vr = cli_json("verify", "--job-dir", str(job), "--run-id", "r1", env=env)
    ck("verify 退出码 0（后端 pass）", vr.returncode == 0, f"rc={vr.returncode}")
    shutil.rmtree(tmp, ignore_errors=True)


def test_start_refuses_existing_output() -> None:
    """固定输出不覆盖：同路径再来一次必须被拒。"""
    tmp = Path(tempfile.mkdtemp(prefix="cli-noover."))
    env = fake_env(tmp)
    job = tmp / "job"
    out = job / "r1.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"existing")
    before = out.read_bytes()
    d, r = cli_json("start", "--video-app", "com.fake.app", "--video-window", "501",
                    "--audio-app", "com.fake.app",
                    "--job-dir", str(job), "--run-id", "r1", "--out", str(out),
                    "--duration", "2", env=env)
    ck("已存在输出时 start 退出码 2", r.returncode == 2, f"rc={r.returncode}")
    ck("明确报拒绝覆盖",
       bool(d and any("拒绝覆盖" in p for p in (d.get("problems") or []))),
       str(d)[:160])
    ck("原文件字节未变", out.read_bytes() == before)
    shutil.rmtree(tmp, ignore_errors=True)


def test_start_refuses_same_run_id() -> None:
    """no-clobber：同 run_id 再开一次必须被拒。"""
    tmp = Path(tempfile.mkdtemp(prefix="cli-clobber."))
    env = fake_env(tmp)
    job = tmp / "job"
    cli_json("start", "--video-app", "com.fake.app", "--video-window", "501",
             "--audio-app", "com.fake.app",
             "--job-dir", str(job), "--run-id", "dup", "--out", str(job / "a.mp4"),
             "--duration", "2", env=env)
    d, r = cli_json("start", "--video-app", "com.fake.app", "--video-window", "501",
                    "--audio-app", "com.fake.app",
                    "--job-dir", str(job), "--run-id", "dup", "--out", str(job / "b.mp4"),
                    "--duration", "2", env=env)
    ck("同 run_id 再开被拒（退出码 2）", r.returncode == 2, f"rc={r.returncode}")
    ck("说明 run 已存在",
       bool(d and any("run 已存在" in p for p in (d.get("problems") or []))),
       str(d)[:160])
    shutil.rmtree(tmp, ignore_errors=True)


def test_preflight_uses_live_facts() -> None:
    """CLI 的 preflight 会拉 live 事实做校对（假后端给一个已知窗口）。"""
    tmp = Path(tempfile.mkdtemp(prefix="cli-pf."))
    env = fake_env(tmp)
    d, r = cli_json("preflight", "--video-app", "com.fake.app",
                    "--video-window", "501", "--audio-app", "com.fake.app", env=env)
    ck("命中 live 事实的 preflight 通过", r.returncode == 0, f"rc={r.returncode} {r.stderr[:140]}")
    ck("标明做过 live 校对", bool(d and d.get("live_checked")), str(d)[:140])

    d2, r2 = cli_json("preflight", "--video-app", "com.fake.app",
                      "--video-window", "9999", "--audio-app", "com.fake.app", env=env)
    ck("不存在的窗口被 live 校对接住", r2.returncode == 2, f"rc={r2.returncode}")
    ck("说明找不到窗口",
       bool(d2 and any("找不到" in p for p in (d2.get("problems") or []))),
       str(d2)[:160])
    shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    print("═══ agent_capture CLI 测试 ═══")
    test_capability()
    test_no_target_is_usage_error()
    test_unimplemented_granularity_rejected()
    test_cross_app_audio_rejected_by_cli()
    test_invalid_audio_mode_rejected()
    test_status_unknown_run()
    test_stop_unknown_run()
    test_verify_without_media()
    test_run_id_escape_rejected_by_cli()
    test_start_requires_args()
    test_targets_without_permission_is_honest()
    test_lifecycle_start_status_stop_verify()
    test_start_refuses_existing_output()
    test_start_refuses_same_run_id()
    test_preflight_uses_live_facts()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
