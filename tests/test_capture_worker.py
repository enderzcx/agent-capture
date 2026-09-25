#!/usr/bin/env python3
"""capture_worker 的反例测试：用**假录制器**复现上线即出错的五类场景。

假录制器是一个脚本化的 Python 进程，能精确控制：
  · 实时进度文件写什么（含 run_token）、什么时候写、写不写
  · metrics 写不写、写坏的、写 problems
  · 退出码、退出时机（有效开始前 / 后）
这样不用真权限、不用真窗口，就能把每条失败路径钉死。

运行：  python3 tests/test_capture_worker.py
退出码：0 = 全过。
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


# ---------------------------------------------------------------------------
# 假录制器：一个能被精确编排的 recorder
# ---------------------------------------------------------------------------
FAKE = r'''#!/usr/bin/env python3
"""假录制器：按 env 编排行为。参数与真录制器同名，便于同一个 worker 驱动。"""
import json, os, signal, sys, time
from pathlib import Path

def arg(name, default=""):
    a = sys.argv
    return a[a.index(name) + 1] if name in a else default

out = Path(arg("--out"))
live = Path(arg("--live-status")) if arg("--live-status") else None
token = arg("--live-token")
metrics = Path(arg("--json")) if arg("--json") else None
dur = float(arg("--duration", "0") or 0)
mode = os.environ.get("FAKE_MODE", "ok")

stop = {"v": False}
signal.signal(signal.SIGTERM, lambda *_: stop.__setitem__("v", True))

def write_live(**kw):
    if live is None: return
    obj = {"tool": "FakeRec", "run_token": token, "recorder_pid": os.getpid(),
           "capture_initialized": False, "first_video_frame": False,
           "audio_path_has_data": False, "audio_signal_observed": False,
           "effectively_started": False, "video_frames": 0, "audio_buffers": 0}
    obj.update(kw)
    live.write_text(json.dumps(obj), encoding="utf-8")

def write_metrics(problems=None, video=True, audio_track=True, audio_signal=True, broken=False):
    if metrics is None: return
    if broken:
        metrics.write_text("{ not json", encoding="utf-8"); return
    metrics.write_text(json.dumps({
        "writer_status": "completed", "problems": problems or [], "advisories": [],
        "verdict": {"video_frames_present": video, "video_frames_arriving": video,
                    "audio_track_present": audio_track, "audio_has_signal": audio_signal,
                    "audio_peak_dbfs": -12.0 if audio_signal else -160.0,
                    "tracks_verified": True},
    }), encoding="utf-8")

if mode == "never_start":            # 从不写 live，直接退非零
    write_metrics(problems=["内部错误"], video=False, audio_track=False, audio_signal=False)
    sys.exit(7)

if mode == "stale_live":             # 只留下**别人的** live（令牌不符）
    if live: live.write_text(json.dumps({"run_token": "STALE-TOKEN",
        "capture_initialized": True, "first_video_frame": True,
        "audio_path_has_data": True, "effectively_started": True}), encoding="utf-8")
    time.sleep(60); sys.exit(0)

if mode == "no_metrics":             # 有效开始，但从不写 metrics
    write_live(capture_initialized=True, first_video_frame=True,
               audio_path_has_data=True, effectively_started=True)
    time.sleep(dur or 1); sys.exit(0)

if mode == "broken_metrics":         # metrics 写坏
    write_live(capture_initialized=True, first_video_frame=True,
               audio_path_has_data=True, effectively_started=True)
    time.sleep(dur or 1); write_metrics(broken=True); sys.exit(0)

if mode == "video_missing":          # 有音频没画面
    write_live(capture_initialized=True, first_video_frame=True,
               audio_path_has_data=True, effectively_started=True)
    time.sleep(dur or 1)
    write_metrics(video=False, audio_track=True, audio_signal=True); sys.exit(0)

if mode == "audio_missing":          # 有画面没音轨
    write_live(capture_initialized=True, first_video_frame=True,
               audio_path_has_data=True, effectively_started=True)
    time.sleep(dur or 1)
    write_metrics(video=True, audio_track=False, audio_signal=False); sys.exit(0)

if mode == "silent":                 # 通路正常但整段静音（**不是失败**）
    write_live(capture_initialized=True, first_video_frame=True,
               audio_path_has_data=True, audio_signal_observed=False,
               effectively_started=True)
    time.sleep(dur or 1)
    write_metrics(video=True, audio_track=True, audio_signal=False); sys.exit(0)

if mode == "exit_nonzero_after":     # 有效开始后非零退出
    write_live(capture_initialized=True, first_video_frame=True,
               audio_path_has_data=True, effectively_started=True)
    time.sleep(0.6)
    write_metrics(video=True, audio_track=True, audio_signal=True)
    sys.exit(9)

if mode == "slow_start":             # 有效开始很慢，用来测启动期取消
    for i in range(200):
        if stop["v"]:
            write_metrics(video=False, audio_track=False, audio_signal=False,
                          problems=["在有效开始前被停止"])
            sys.exit(1)
        time.sleep(0.2)
    sys.exit(1)

if mode == "no_video_only_audio":    # 只写音频通路，没有画面
    write_live(capture_initialized=True, first_video_frame=False,
               audio_path_has_data=True, effectively_started=True)
    time.sleep(dur or 1)
    write_metrics(video=False, audio_track=True, audio_signal=True); sys.exit(0)

# ok：正常一次录制
write_live(capture_initialized=True, first_video_frame=True, audio_path_has_data=True)
time.sleep(min(dur or 1.5, 3))
if stop["v"]:
    write_metrics(video=True, audio_track=True, audio_signal=True); sys.exit(0)
write_live(capture_initialized=True, first_video_frame=True,
           audio_path_has_data=True, audio_signal_observed=True,
           effectively_started=True)
end = time.time() + (dur or 2)
while time.time() < end and not stop["v"]:
    time.sleep(0.1)
write_metrics(video=True, audio_track=True, audio_signal=True)
sys.exit(0)
'''

# 假后端：让 worker 走 fake recorder，并且 verify 可控
FAKE_BACKEND = r'''"""假后端：只用于测试 worker 的编排逻辑。"""
import json, os, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FAKE_REC = HERE / "fake_recorder.py"

def availability():
    return {"available": True, "reason": "ok", "backend": "fake",
            "platform": "macos", "verified_level": "mock_only",
            "verified_scope": "test double"}

def build_start_argv(target, out_path, *, duration=0.0, fps=30, max_width=1920,
                     focus_log="", status_file="", metrics_json="",
                     live_status="", live_token="", overwrite=False, no_video=False,
                     no_audio=False):
    argv = [sys.executable, str(FAKE_REC), "--out", out_path]
    if duration: argv += ["--duration", str(duration)]
    if metrics_json: argv += ["--json", metrics_json]
    if live_status: argv += ["--live-status", live_status]
    if live_token: argv += ["--live-token", live_token]
    if no_video: argv += ["--no-video"]
    if no_audio: argv += ["--no-audio"]
    return argv

def verify(media_path, expect="av"):
    mode = os.environ.get("FAKE_VERIFY", "pass")
    if mode == "pass":
        return {"result": "pass", "media": media_path}
    if mode == "fail":
        return {"result": "fail", "media": media_path, "notes": "容器层未通过"}
    return {"result": "unknown", "media": media_path, "notes": "无法判定"}
'''

# 老后端：不接受 live_token（用于验证"不静默退化"）
OLD_BACKEND = r'''"""老后端：build_start_argv 不接受 live_token。"""
import sys

def availability():
    return {"available": True, "reason": "ok", "backend": "old",
            "platform": "macos", "verified_level": "none", "verified_scope": "old"}

def build_start_argv(target, out_path, *, duration=0.0, fps=30, max_width=1920,
                     focus_log="", status_file="", metrics_json="",
                     live_status="", overwrite=False, no_video=False):
    return [sys.executable, "-c", "import time; time.sleep(0.1)"]

def verify(media_path, expect="av"):
    return {"result": "pass", "media": media_path}
'''


def setup_case(tmp: Path, mode: str, *, target=None, expect="av", no_video=False,
               fake_verify="pass", backend_src=FAKE_BACKEND):
    """建一个 job 目录 + 假后端，返回 (job_dir, run_id, env)。"""
    job = tmp / "job"
    job.mkdir(parents=True, exist_ok=True)
    tools = tmp / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    (tools / "fake_recorder.py").write_text(FAKE, encoding="utf-8")
    (tools / "backend.py").write_text(backend_src, encoding="utf-8")

    tgt = target or {"platform": "macos",
                     "video": {"granularity": "window"},
                     "audio": {"granularity": "app"}}
    cs.init_run(job, "r1", "test", tgt)
    cs.transition(job, "r1", "preflight")
    cs.transition(job, "r1", "starting")
    env = dict(os.environ)
    env["FAKE_MODE"] = mode
    env["FAKE_VERIFY"] = fake_verify
    return job, "r1", env, tools


def run_worker(job: Path, tools: Path, env: dict, *, expect="av", no_video=False,
               timeout=60, out=None):
    """跑 worker，让它用假后端。"""
    cmd = [sys.executable, str(SCRIPTS / "capture_worker.py"),
           "--job-dir", str(job), "--run-id", "r1",
           "--out", str(out or (job / "r1.mp4")), "--expect", expect,
           "--duration", "1"]
    if no_video:
        cmd.append("--no-video")
    # worker 通过 sys.path 找 backend；把假后端的目录放在最前
    env = dict(env)
    # 显式指定后端目录，让 worker 用假后端而不是仓库里的真后端
    env["AGENT_CAPTURE_BACKEND_DIR"] = str(tools)
    env["PYTHONPATH"] = f"{SCRIPTS}{os.pathsep}" + env.get("PYTHONPATH", "")
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)


# ---------------------------------------------------------------------------
# 反例
# ---------------------------------------------------------------------------

def test_stale_live_is_rejected() -> None:
    """① 上一次的残留 live 文件（令牌不符）**不能**被读成"已开始"。"""
    tmp = Path(tempfile.mkdtemp(prefix="cw-stale."))
    job, rid, env, tools = setup_case(tmp, "stale_live")
    # 先放一个"看起来已开始"的陈旧文件（无令牌 / 别的令牌）
    (job / f"{rid}.live.json").write_text(json.dumps({
        "capture_initialized": True, "first_video_frame": True,
        "audio_path_has_data": True, "effectively_started": True}), encoding="utf-8")
    r = run_worker(job, tools, env, timeout=90)
    st = cs.load_run(job, rid)
    ck("残留 live 不被当成已开始", st.status == "failed", f"status={st.status}")
    cap = st.stages.get("capture") or {}
    ck("并且如实报为 fail", cap.get("result") == "fail", str(cap.get("result")))
    ck("worker 退出码为启动失败(3)", r.returncode == 3, f"rc={r.returncode}")
    shutil.rmtree(tmp, ignore_errors=True)


def test_no_metrics_is_not_pass() -> None:
    """③ metrics 缺失不能静默变成 {} 当 pass。"""
    tmp = Path(tempfile.mkdtemp(prefix="cw-nometrics."))
    job, rid, env, tools = setup_case(tmp, "no_metrics")
    r = run_worker(job, tools, env, timeout=90)
    st = cs.load_run(job, rid)
    cap = st.stages.get("capture") or {}
    ck("缺 metrics 时 capture 不是 pass", cap.get("result") != "pass",
       str(cap.get("result")))
    ck("缺 metrics 时明确进 problems",
       any("metrics" in p for p in (cap.get("problems") or [])), str(cap.get("problems")))
    ck("最终不是 verified", st.status != "verified", f"status={st.status}")
    ck("worker 非零退出", r.returncode != 0, f"rc={r.returncode}")
    shutil.rmtree(tmp, ignore_errors=True)


def test_broken_metrics_is_not_pass() -> None:
    """③ metrics 写坏同样不能当 pass。"""
    tmp = Path(tempfile.mkdtemp(prefix="cw-broken."))
    job, rid, env, tools = setup_case(tmp, "broken_metrics")
    r = run_worker(job, tools, env, timeout=90)
    st = cs.load_run(job, rid)
    cap = st.stages.get("capture") or {}
    ck("坏 metrics 时 capture 不是 pass", cap.get("result") != "pass", str(cap.get("result")))
    ck("坏 metrics 时记录了读取错误", bool(cap.get("metrics_read_error")),
       str(cap.get("metrics_read_error")))
    ck("最终不是 verified", st.status != "verified", f"status={st.status}")
    shutil.rmtree(tmp, ignore_errors=True)


def test_exit_before_start_fails_not_stuck() -> None:
    """③ 未开始但录制器提前退出 → 明确 fail，**且不能卡在 starting**。"""
    tmp = Path(tempfile.mkdtemp(prefix="cw-early."))
    job, rid, env, tools = setup_case(tmp, "never_start")
    r = run_worker(job, tools, env, timeout=90)
    st = cs.load_run(job, rid)
    ck("提前退出时状态是 failed（不是卡在 starting）",
       st.status == "failed", f"status={st.status}")
    ck("worker 没有抛未捕获异常", "Traceback" not in (r.stderr or ""),
       (r.stderr or "")[-200:])
    cap = st.stages.get("capture") or {}
    ck("记下了真实退出码 7", cap.get("recorder_exit") == 7, str(cap.get("recorder_exit")))
    ck("退出码为 3", r.returncode == 3, f"rc={r.returncode}")
    shutil.rmtree(tmp, ignore_errors=True)


def test_capture_fail_verify_pass_is_not_verified() -> None:
    """④ capture 失败 + verify 通过 → **不能**判 verified。"""
    tmp = Path(tempfile.mkdtemp(prefix="cw-combined."))
    job, rid, env, tools = setup_case(tmp, "exit_nonzero_after", fake_verify="pass")
    r = run_worker(job, tools, env, timeout=90)
    st = cs.load_run(job, rid)
    cap = st.stages.get("capture") or {}
    ver = st.stages.get("verify") or {}
    ck("verify 容器层是 pass", ver.get("result") == "pass", str(ver.get("result")))
    ck("capture 是 fail（非零退出）", cap.get("result") == "fail", str(cap.get("result")))
    ck("组合判定后**不是** verified", st.status != "verified", f"status={st.status}")
    ck("并说明为何不判 verified", bool(ver.get("combined_note")), str(ver.get("combined_note")))
    ck("worker 退出码非 0（5）", r.returncode == 5, f"rc={r.returncode}")
    shutil.rmtree(tmp, ignore_errors=True)


def test_both_pass_is_verified() -> None:
    """④ 反向：capture 与 verify 都通过才 verified 且退出 0。"""
    tmp = Path(tempfile.mkdtemp(prefix="cw-ok."))
    job, rid, env, tools = setup_case(tmp, "ok", fake_verify="pass")
    r = run_worker(job, tools, env, timeout=90)
    st = cs.load_run(job, rid)
    cap = st.stages.get("capture") or {}
    ck("capture pass", cap.get("result") == "pass", str(cap.get("problems")))
    ck("最终 verified", st.status == "verified", f"status={st.status}")
    ck("worker 退出码 0", r.returncode == 0, f"rc={r.returncode}")
    shutil.rmtree(tmp, ignore_errors=True)


def test_silent_audio_is_not_failure() -> None:
    """② 通路正常但整段静音 → **不是**失败（开局前本来就静音）。"""
    tmp = Path(tempfile.mkdtemp(prefix="cw-silent."))
    job, rid, env, tools = setup_case(tmp, "silent")
    r = run_worker(job, tools, env, timeout=90)
    st = cs.load_run(job, rid)
    cap = st.stages.get("capture") or {}
    ck("静音不算失败", cap.get("result") == "pass", str(cap.get("problems")))
    ck("但明确记下没有信号", cap.get("audio_signal_observed") is False)
    ck("并给出不能声称录到声音的说明", bool(cap.get("audio_note")))
    ck("最终仍 verified", st.status == "verified", f"status={st.status}")
    shutil.rmtree(tmp, ignore_errors=True)


def test_audio_only_expect_does_not_require_video() -> None:
    """② `--expect audio`：只要音频，**不能**因为没有画面而失败/死等。"""
    tmp = Path(tempfile.mkdtemp(prefix="cw-audioonly."))
    job, rid, env, tools = setup_case(tmp, "no_video_only_audio", expect="audio")
    r = run_worker(job, tools, env, expect="audio", timeout=90)
    st = cs.load_run(job, rid)
    cap = st.stages.get("capture") or {}
    ck("audio-only 不要求画面", cap.get("required_video") is False,
       str(cap.get("required_video")))
    ck("audio-only 要求音频", cap.get("required_audio") is True)
    ck("没有画面也不算失败", cap.get("result") == "pass", str(cap.get("problems")))
    ck("没有在启动阶段死等（已 verified）", st.status == "verified", f"status={st.status}")
    shutil.rmtree(tmp, ignore_errors=True)


def test_video_missing_when_required_fails() -> None:
    """② 反向：需要画面却没有画面 → 必须失败。"""
    tmp = Path(tempfile.mkdtemp(prefix="cw-novideo."))
    job, rid, env, tools = setup_case(tmp, "video_missing", expect="av")
    r = run_worker(job, tools, env, expect="av", timeout=90)
    st = cs.load_run(job, rid)
    cap = st.stages.get("capture") or {}
    ck("需要画面时缺画面 = fail", cap.get("result") == "fail", str(cap.get("result")))
    ck("problems 说明缺画面",
       any("画面" in p for p in (cap.get("problems") or [])), str(cap.get("problems")))
    ck("不判 verified", st.status != "verified", f"status={st.status}")
    shutil.rmtree(tmp, ignore_errors=True)


def test_audio_required_but_missing_fails() -> None:
    """② 需要音频却没有音轨 → 必须失败。"""
    tmp = Path(tempfile.mkdtemp(prefix="cw-noaudio."))
    job, rid, env, tools = setup_case(tmp, "audio_missing", expect="av")
    r = run_worker(job, tools, env, expect="av", timeout=90)
    st = cs.load_run(job, rid)
    cap = st.stages.get("capture") or {}
    ck("需要音频时缺音轨 = fail", cap.get("result") == "fail", str(cap.get("result")))
    ck("problems 说明缺音频轨",
       any("音频轨" in p for p in (cap.get("problems") or [])), str(cap.get("problems")))
    shutil.rmtree(tmp, ignore_errors=True)


def test_audio_none_target_does_not_require_audio() -> None:
    """② 目标是"不要声音"（audio granularity=none）→ 不要求音频。"""
    tmp = Path(tempfile.mkdtemp(prefix="cw-audionone."))
    tgt = {"platform": "macos", "video": {"granularity": "window"},
           "audio": {"granularity": "none"}}
    job, rid, env, tools = setup_case(tmp, "audio_missing", target=tgt, expect="av")
    r = run_worker(job, tools, env, expect="av", timeout=90)
    st = cs.load_run(job, rid)
    cap = st.stages.get("capture") or {}
    ck("audio=none 时不要求音频", cap.get("required_audio") is False,
       str(cap.get("required_audio")))
    ck("缺音轨也不算失败", cap.get("result") == "pass", str(cap.get("problems")))
    shutil.rmtree(tmp, ignore_errors=True)


def test_stop_during_startup_is_honored() -> None:
    """⑤ 启动阶段也能被取消 —— 不必等满 45s 超时。"""
    tmp = Path(tempfile.mkdtemp(prefix="cw-stopstart."))
    job, rid, env, tools = setup_case(tmp, "slow_start")
    cmd = [sys.executable, str(SCRIPTS / "capture_worker.py"),
           "--job-dir", str(job), "--run-id", rid,
           "--out", str(job / "r1.mp4"), "--expect", "av", "--duration", "1"]
    env2 = dict(env)
    env2["AGENT_CAPTURE_BACKEND_DIR"] = str(tools)
    env2["PYTHONPATH"] = f"{SCRIPTS}{os.pathsep}" + env2.get("PYTHONPATH", "")
    t0 = time.time()
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, env=env2)
    # 等它进入"等有效开始"，然后发停止请求
    time.sleep(2.5)
    cs.request_stop(job, rid, {"owner_id": "test", "reason": "cancel during startup"})
    try:
        out, err = p.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        p.kill(); out, err = p.communicate()
    elapsed = time.time() - t0
    st = cs.load_run(job, rid)
    ck("启动阶段取消被响应（<45s 超时）", elapsed < 40, f"耗时 {elapsed:.1f}s")
    ck("状态进入 failed", st.status == "failed", f"status={st.status}")
    cap = st.stages.get("capture") or {}
    ck("记下是启动阶段被取消", cap.get("stopped_during_start") is True,
       str(cap.get("stopped_during_start")))
    shutil.rmtree(tmp, ignore_errors=True)


def test_backend_without_live_token_is_refused() -> None:
    """① 后端不支持 live_token 时**明确拒绝**，不静默退化。"""
    tmp = Path(tempfile.mkdtemp(prefix="cw-oldbackend."))
    job, rid, env, tools = setup_case(tmp, "ok", backend_src=OLD_BACKEND)
    r = run_worker(job, tools, env, timeout=60)
    st = cs.load_run(job, rid)
    ck("缺 live_token 支持 → failed", st.status == "failed", f"status={st.status}")
    ck("退出码 3", r.returncode == 3, f"rc={r.returncode}")
    ck("说明原因（指向参数问题，不掩盖）",
       ("live_token" in (st.error or "") or "live-token" in (st.error or "")), str(st.error))
    shutil.rmtree(tmp, ignore_errors=True)


def test_no_orphan_on_startup_failure() -> None:
    """⑤ 启动失败时��留孤儿录制器进程。"""
    tmp = Path(tempfile.mkdtemp(prefix="cw-orphan."))
    job, rid, env, tools = setup_case(tmp, "slow_start")
    r = run_worker(job, tools, env, timeout=90)
    st = cs.load_run(job, rid)
    pid = (st.artifacts or {}).get("recorder_pid")
    alive = False
    if isinstance(pid, int) and pid > 0:
        try:
            os.kill(pid, 0)
            alive = True
        except OSError:
            alive = False
    ck("失败后录制器进程已被回收（无孤儿）", not alive, f"pid={pid} alive={alive}")
    ck("状态是 failed", st.status == "failed", f"status={st.status}")
    shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    print("═══ capture_worker 反例测试（假录制器）═══")
    test_stale_live_is_rejected()
    test_no_metrics_is_not_pass()
    test_broken_metrics_is_not_pass()
    test_exit_before_start_fails_not_stuck()
    test_capture_fail_verify_pass_is_not_verified()
    test_both_pass_is_verified()
    test_silent_audio_is_not_failure()
    test_audio_only_expect_does_not_require_video()
    test_video_missing_when_required_fails()
    test_audio_required_but_missing_fails()
    test_audio_none_target_does_not_require_audio()
    test_stop_during_startup_is_honored()
    test_backend_without_live_token_is_refused()
    test_no_orphan_on_startup_failure()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
