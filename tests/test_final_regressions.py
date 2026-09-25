#!/usr/bin/env python3
"""Cloud 收尾三项的针对性回归（只测这几处，不重跑全套）。

1. capture report 的**完整性**：任何 capture/verify 的 fail 或 unknown 都必须
   影响 `unexpected_stop` / `video_continuity`，不能"音频检查失败仍给 complete"；
   `audio_scope=none` 明确不要求声音。
2. CLI `--duration` 的 NaN/Inf/负数**确定性拒绝**，且 `--overwrite` 真的透传。
3. Windows 桥接：不把 fallback 重绑当精确窗口 id；不承诺 HWND pin；
   `verify` 不声称画面已验证。

运行：  python3 tests/test_final_regressions.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / "tools" / "windows"))

import capture_state as cs  # noqa: E402

PASS = 0
FAIL = 0


def ck(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"\033[32m✓\033[0m {name}")
    else:
        FAIL += 1
        print(f"\033[31m✗\033[0m {name}" + (f"  — {detail}" if detail else ""))


def cli(*args, timeout=90):
    return subprocess.run([sys.executable, str(SCRIPTS / "agent_capture.py"), *args],
                          capture_output=True, text=True, timeout=timeout)


# ---------------------------------------------------------------------------
# 1. 完整性判定
# ---------------------------------------------------------------------------

def _mkrun(tmp: Path, *, cap_result="pass", verify_result="pass",
           audio_gran="app", audio_track=True, continuity=True):
    job = tmp / "job"
    media = job / "r1.mp4"
    job.mkdir(parents=True, exist_ok=True)
    media.write_bytes(b"m" * 1024)
    cs.init_run(job, "r1", "t", {"platform": "macos",
                                 "video": {"granularity": "window"},
                                 "audio": {"granularity": audio_gran}})
    for st in ("preflight", "starting", "running", "stopping", "stopped", "verifying"):
        cs.transition(job, "r1", st)
    live = job / "r1.live.json"
    live.write_text(json.dumps({"run_token": "tk1", "capture_initialized": True,
                                "first_video_frame": True, "audio_path_has_data": True,
                                "effectively_started": True}), encoding="utf-8")
    metrics = job / "r1.metrics.json"
    metrics.write_text(json.dumps({"problems": [], "verdict": {
        "video_frames_present": True, "video_frames_arriving": continuity,
        "audio_track_present": audio_track, "audio_has_signal": audio_track,
        "tracks_verified": True}}), encoding="utf-8")
    cs.update_run(job, "r1", artifacts={
        "media": str(media), "metrics": str(metrics), "live_status": str(live),
        "run_token": "tk1"}, stages={"capture": {
            "result": cap_result, "problems": [], "audio_path_has_data": audio_track,
            "recorder_exit": 0}})
    cs.transition(job, "r1", "verified" if verify_result == "pass" else "verify_failed",
                  stages={"verify": {"result": verify_result}})
    out = tmp / "rep.json"
    cli("report", "--job-dir", str(job), "--run-id", "r1",
        "--production-run-id", "p1", "--out", str(out))
    return json.loads(out.read_text(encoding="utf-8"))


def test_completeness_gating():
    # verify=fail 必须影响完整性
    t = Path(tempfile.mkdtemp()); o = _mkrun(t, verify_result="fail")
    ck("verify=fail → unexpected_stop=True", o["unexpected_stop"] is True)
    ck("verify=fail → video_continuity=False", o["checks"]["video_continuity"] is False)
    ck("verify=fail → 列出 blocker", bool(o.get("completeness_blockers")),
       str(o.get("completeness_blockers")))
    shutil.rmtree(t, ignore_errors=True)

    # verify=unknown 同样必须影响
    t = Path(tempfile.mkdtemp()); o = _mkrun(t, verify_result="unknown")
    ck("verify=unknown → unexpected_stop=True（未知不算通过）", o["unexpected_stop"] is True)
    shutil.rmtree(t, ignore_errors=True)

    # capture=fail 必须影响
    t = Path(tempfile.mkdtemp()); o = _mkrun(t, cap_result="fail")
    ck("capture=fail → unexpected_stop=True", o["unexpected_stop"] is True)
    shutil.rmtree(t, ignore_errors=True)

    # 音频检查失败（需要声音却没轨）必须影响
    t = Path(tempfile.mkdtemp()); o = _mkrun(t, audio_track=False)
    ck("需要音频但没音轨 → unexpected_stop=True",
       o["unexpected_stop"] is True, str(o.get("completeness_blockers")))
    shutil.rmtree(t, ignore_errors=True)

    # audio_scope=none 明确不要求声音
    t = Path(tempfile.mkdtemp()); o = _mkrun(t, audio_gran="none", audio_track=False)
    ck("audio_scope=none 时不要求音频（全通过）",
       o["audio_scope"] == "none" and o["unexpected_stop"] is False,
       f"scope={o['audio_scope']} blockers={o.get('completeness_blockers')}")
    shutil.rmtree(t, ignore_errors=True)

    # 全好 → complete 该有的形状
    t = Path(tempfile.mkdtemp()); o = _mkrun(t)
    ck("全部通过 → unexpected_stop=False 且 continuity=True",
       o["unexpected_stop"] is False and o["checks"]["video_continuity"] is True)
    shutil.rmtree(t, ignore_errors=True)


# ---------------------------------------------------------------------------
# 2. CLI 确定性拒绝 + overwrite 透传
# ---------------------------------------------------------------------------

def test_duration_rejected():
    for d in ("nan", "inf", "-inf", "-1", "-0.5"):
        r = cli("start", "--video-app", "com.x.Y", "--video-window", "1",
                "--out", "/tmp/zz.mp4", "--job-dir", "/tmp/zzjob",
                "--run-id", "z1", "--duration", d)
        # 两种都算确定性拒绝：我们的 usage JSON，或 argparse 直接报错（如 "-inf" 被当成选项）
        try:
            obj = json.loads(r.stdout)
            ok = obj.get("phase") == "usage" and any("duration" in x for x in obj.get("problems", []))
        except ValueError:
            ok = r.returncode != 0 and "duration" in (r.stderr or "").lower()
        ck(f"--duration {d} 被确定性拒绝", ok, r.stdout[:120])


def test_overwrite_reaches_worker():
    """CLI 的 --overwrite 必须真的出现在 worker 命令行里。"""
    src = (SCRIPTS / "agent_capture.py").read_text(encoding="utf-8")
    ck("CLI 把 --overwrite 传给 worker", 'worker.append("--overwrite")' in src)
    wsrc = (SCRIPTS / "capture_worker.py").read_text(encoding="utf-8")
    ck("worker 接收 --overwrite", '"--overwrite"' in wsrc and "a.overwrite" in wsrc)


# ---------------------------------------------------------------------------
# 3. Windows 桥接语义
# ---------------------------------------------------------------------------

def test_windows_bridge_semantics():
    import importlib
    import backend as wb  # tools/windows/backend.py

    av = wb.availability()
    ck("非 Windows 上明确不可用（原因具体）",
       av["available"] is False and "Windows" in av["reason"], av["reason"][:80])
    ck("verified_level 恒为 none（无实机）", av["verified_level"] == "none")

    # build_start_argv 必须明确拒绝，而不是伪装成子进程
    try:
        wb.build_start_argv({}, "/tmp/x.mkv")
        ck("build_start_argv 明确拒绝（不伪装子进程）", False, "竟然成功了")
    except NotImplementedError as e:
        ck("build_start_argv 明确拒绝（不伪装子进程）", True)
        ck("并把调用方指向正确的生命周期 API", "run_command" in str(e), str(e)[:80])

    # preflight 必须如实带出匹配语义，不承诺 HWND
    pf = wb.preflight({"video": {"granularity": "window", "window_title": "X"}})
    ck("preflight 带 target_match", "target_match" in pf, str(pf)[:120])
    ck("preflight 不承诺 HWND pin", pf.get("hwnd_pinning") is False)
    ck("preflight 明说可能被 OBS 重绑", "重绑" in (pf.get("match_note") or ""),
       str(pf.get("match_note"))[:90])
    ck("preflight 只按 title/class/exe 匹配",
       pf.get("match_dimensions") == ["title", "class", "exe"], str(pf.get("match_dimensions")))

    # verify 不得声称画面已验证
    v = wb.verify("/tmp/x.mkv", "av")
    ck("verify 不声称画面已验证", v.get("picture_verified") is False)
    ck("verify 的 audio_signal 是 None（不可观测，不是 False）",
       v.get("audio_signal_observed") is None)
    ck("verify 结果是 unknown", v.get("result") == "unknown")

    # probe 不得声称能做 app 级画面
    src = (ROOT / "tools" / "windows" / "backend.py").read_text(encoding="utf-8")
    ck("桥接源码里保留 hwnd_pinning=false 的明示", "hwnd_pinning" in src)
    cap = json.loads(subprocess.run(
        [sys.executable, str(SCRIPTS / "capture_targets.py"), "capability",
         "--platform", "windows"], capture_output=True, text=True).stdout)
    ck("Windows 注册表只报 window 粒度（不声称 app 级画面）",
       cap["video_granularities"] == ["window"], str(cap["video_granularities"]))




def test_audio_none_silence_does_not_fail_verify():
    """`audio:none` 时，静音**不得**让 verify 判失败（Cloud 明确点过这一条）。

    源本身没有音频（纯网页画面）时录制器仍会写静音轨；verify 如实报静音不是缺陷，
    因为我们本来就没要声音。判据只用手上的实测证据。
    """
    import json as _j
    from pathlib import Path as _P
    src = (SCRIPTS / "capture_worker.py").read_text(encoding="utf-8")
    ck("worker 里有 audio:none 的静音豁免分支",
       "audio_scope=none" in src and "note_audio_not_required" in src)
    ck("豁免只用实测判据（帧到达 + 轨道核实）",
       "video_frames_arriving" in src and "tracks_verified" in src)
    ck("豁免只在本次**不要求**音频时生效",
       'if not req["audio"] and vres.get("result") == "fail"' in src)


def main() -> int:
    print("═══ Cloud 收尾三项针对性回归 ═══")
    test_completeness_gating()
    test_duration_rejected()
    test_overwrite_reaches_worker()
    test_windows_bridge_semantics()
    test_audio_none_silence_does_not_fail_verify()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
