#!/usr/bin/env python3
"""capture report 的反例测试：**没有证据就不许出现 true**。

`agent_capture.py report` 的作用是把本工具的真实状态薄转换到
gameplay-production 的规范化 report。它最容易出的错不是崩溃，而是
**手填 true 去绕验收** —— 那会让上游把"没验证"当成"已验证"。
所以这里逐项钉住：缺证据时必须保守。

运行：  python3 tests/test_capture_report.py
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


def cli(*args):
    return subprocess.run([sys.executable, str(SCRIPTS / "agent_capture.py"), *args],
                          capture_output=True, text=True, timeout=90)


def mkreport(tmp: Path, *, live=True, token="tok-abc", metrics=True,
             verdict=None, media=True, final_status="verified",
             target_audio="app", cap_problems=None, exit_code=0,
             first_frame=True, create_live=True):
    """建一个"真实形状"的 run，然后产出 report。"""
    job = tmp / "job"
    media_p = job / "r1.mp4"
    job.mkdir(parents=True, exist_ok=True)
    if media:
        media_p.write_bytes(b"x" * 2048)

    cs.init_run(job, "r1", "t", {
        "platform": "macos",
        "video": {"granularity": "window"},
        "audio": {"granularity": target_audio},
    })
    for st in ("preflight", "starting", "running", "stopping", "stopped", "verifying"):
        cs.transition(job, "r1", st)

    live_p = job / "r1.live.json"
    if create_live and live:
        live_p.write_text(json.dumps({
            "tool": "FakeRec", "run_token": token,
            "capture_initialized": True,
            "first_video_frame": first_frame,
            "audio_path_has_data": True,
            "audio_signal_observed": True,
            "effectively_started": True, "video_frames": 120,
        }), encoding="utf-8")
    elif create_live and not live:
        live_p.write_text("NOT JSON AT ALL", encoding="utf-8")

    metrics_p = job / "r1.metrics.json"
    if metrics:
        v = {"video_frames_present": True, "video_frames_arriving": True,
             "audio_track_present": True, "audio_has_signal": True,
             "tracks_verified": True, "audio_peak_dbfs": -12.0}
        if verdict:
            v.update(verdict)
        metrics_p.write_text(json.dumps({"writer_status": "completed",
                                         "problems": [], "advisories": [],
                                         "verdict": v}), encoding="utf-8")

    art = {"recorder_pid": 1, "media": str(media_p), "metrics": str(metrics_p),
           "focus_log": str(job / "r1.focus.jsonl"),
           "live_status": str(live_p), "worker_log": str(job / "r1.worker.log"),
           "run_token": token}
    cs.update_run(job, "r1", artifacts=art,
                  stages={"capture": {
                      "result": "fail" if cap_problems else "pass",
                      "problems": cap_problems or [],
                      "audio_signal_observed": True,
                      "verified_level": "compile_only",
                      "recorder_exit": exit_code,
                  }})
    # 真实的 run 一定写过 stages.verify（verify 的结论就是完整性判定的输入之一）
    cs.transition(job, "r1", "verified" if final_status == "verified" else "verify_failed",
                  stages={"verify": {"result": "pass" if final_status == "verified" else "fail"}})

    out = tmp / "report.json"
    r = cli("report", "--job-dir", str(job), "--run-id", "r1",
            "--production-run-id", "prod-1", "--out", str(out))
    obj = json.loads(out.read_text(encoding="utf-8")) if out.exists() else None
    return obj, r, job


def test_healthy_report() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="rep-ok."))
    o, r, _ = mkreport(tmp)
    ck("健康 run 产出 report", r.returncode == 0 and o is not None, r.stderr[:160])
    ck("producer 是 agent-capture", o.get("producer") == "agent-capture")
    ck("run_id 用 production 的", o.get("run_id") == "prod-1", str(o.get("run_id")))
    ck("capture_id 含真实令牌", o.get("capture_id") == "r1:tok-abc", str(o.get("capture_id")))
    ck("status=stopped", o.get("status") == "stopped")
    ck("first_video_frame=True（来自真实 live）", o.get("first_video_frame") is True)
    ck("container_readable=True（来自真实 metrics）", o["checks"]["container_readable"] is True)
    ck("video_continuity=True（来自真实 metrics）", o["checks"]["video_continuity"] is True)
    ck("media 带真实 sha256/bytes",
       bool(o["media"]) and len(o["media"][0]["sha256"]) == 64 and o["media"][0]["bytes"] == 2048)
    ck("unexpected_stop=False", o.get("unexpected_stop") is False)
    shutil.rmtree(tmp, ignore_errors=True)


def test_missing_metrics_never_true() -> None:
    """**没有 metrics 就不许出 true** —— 这是"不手填 true"的核心反例。"""
    tmp = Path(tempfile.mkdtemp(prefix="rep-nometrics."))
    o, r, _ = mkreport(tmp, metrics=False)
    ck("缺 metrics 时仍能产出 report（不崩）", r.returncode == 0 and o is not None, r.stderr[:160])
    ck("缺 metrics → container_readable=False", o["checks"]["container_readable"] is False,
       str(o["checks"]))
    ck("缺 metrics → video_continuity=False", o["checks"]["video_continuity"] is False,
       str(o["checks"]))
    shutil.rmtree(tmp, ignore_errors=True)


def test_missing_live_token_means_no_capture_id() -> None:
    """没有令牌就**没有** capture_id —— 宁可让上游拒绝，也不编一个。"""
    tmp = Path(tempfile.mkdtemp(prefix="rep-notoken."))
    o, r, _ = mkreport(tmp, live=False)
    ck("live 文件损坏时仍能产出 report", r.returncode == 0 and o is not None, r.stderr[:160])
    ck("无令牌 → capture_id 为空", o.get("capture_id") == "", repr(o.get("capture_id")))
    ck("无 live → first_video_frame 只能靠 metrics", o.get("first_video_frame") is True,
       "metrics 说有帧时可为 True")
    shutil.rmtree(tmp, ignore_errors=True)


def test_no_media_means_no_artifact() -> None:
    """没有产物文件 → media 空，container_readable 不能为 true。"""
    tmp = Path(tempfile.mkdtemp(prefix="rep-nomedia."))
    o, r, _ = mkreport(tmp, media=False)
    ck("无产物时 media 为空", o is not None and o["media"] == [], str(o and o["media"]))
    ck("无产物 → container_readable=False", o["checks"]["container_readable"] is False)
    shutil.rmtree(tmp, ignore_errors=True)


def test_verdict_false_propagates() -> None:
    """metrics 明说没有帧到达 → video_continuity 必须是 False（不能乐观）。"""
    tmp = Path(tempfile.mkdtemp(prefix="rep-noframes."))
    o, r, _ = mkreport(tmp, verdict={"video_frames_arriving": False,
                                     "video_frames_present": False},
                       first_frame=False)
    ck("metrics 说帧没在到达 → video_continuity=False",
       o["checks"]["video_continuity"] is False, str(o["checks"]))
    shutil.rmtree(tmp, ignore_errors=True)


def test_problems_and_exit_mark_unexpected() -> None:
    """有 problems 或非零退出 → unexpected_stop=True（提前停止不能被洗白）。"""
    tmp = Path(tempfile.mkdtemp(prefix="rep-unexpected."))
    o, _, _ = mkreport(tmp, cap_problems=["目标窗口在录制期间消失"], exit_code=1)
    ck("有 problems → unexpected_stop=True", o.get("unexpected_stop") is True)
    shutil.rmtree(tmp, ignore_errors=True)


def test_audio_scope_from_target() -> None:
    """audio_scope 来自真实 target，不是常量。"""
    tmp = Path(tempfile.mkdtemp(prefix="rep-audio."))
    o, _, _ = mkreport(tmp, target_audio="none")
    ck("target audio=none → audio_scope=none", o.get("audio_scope") == "none",
       str(o.get("audio_scope")))
    shutil.rmtree(tmp, ignore_errors=True)


def test_report_is_not_claiming_proof() -> None:
    """report 必须自带"不证明什么"，避免被读成签名真值。"""
    tmp = Path(tempfile.mkdtemp(prefix="rep-notproof."))
    o, _, _ = mkreport(tmp)
    ck("明确列出不证明的内容", bool(o.get("not_proof_of")), str(o.get("not_proof_of")))
    shutil.rmtree(tmp, ignore_errors=True)


def test_bad_run_id_rejected() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="rep-badid."))
    r = cli("report", "--job-dir", str(tmp), "--run-id", "nope",
            "--production-run-id", "prod-1")
    ck("未知 run 退出码 2", r.returncode == 2, f"rc={r.returncode}")
    shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    print("═══ capture report 反例测试（无证据不许 true）═══")
    test_healthy_report()
    test_missing_metrics_never_true()
    test_missing_live_token_means_no_capture_id()
    test_no_media_means_no_artifact()
    test_verdict_false_propagates()
    test_problems_and_exit_mark_unexpected()
    test_audio_scope_from_target()
    test_report_is_not_claiming_proof()
    test_bad_run_id_rejected()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
