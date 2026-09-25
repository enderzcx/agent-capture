#!/usr/bin/env python3
"""`--no-audio` 的条件路径回归：**真的关掉音频**，而不只是"不要求声音"。

Cloud 复审指出：早期 `audio:none` 只在 worker 层不要求声音，原生仍
`capturesAudio=true` 并挂上音频输入/输出 —— 那会**意外收到同一个 app 其它窗口的
声音**，而调用方以为关掉了。

这里覆盖：
  1. 原生在有二进制时：`--no-audio` 的产物**没有音频轨**；app 模式**有**（对照）
  2. 原生拒绝 `--no-video + --no-audio`（什么都没录）
  3. worker 拒绝 `audio-only + no-audio` 自相矛盾组合
  4. 元数据 / verdict 如实（captures_audio=false、basis=not_requested）
  5. **腐败文件仍然算失败** —— 不能靠"删掉静音 problems"把坏文件判成功

无二进制时相关用例 SKIP（不假装通过）。

运行：  python3 tests/test_no_audio.py
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
BIN = ROOT / "tools" / "macos" / "build" / "GameAVRec.app" / "Contents" / "MacOS" / "GameAVRec"
FFPROBE = "/opt/homebrew/bin/ffprobe" if Path("/opt/homebrew/bin/ffprobe").exists() else "ffprobe"

sys.path.insert(0, str(SCRIPTS))
import capture_state as cs  # noqa: E402

PASS = 0
FAIL = 0
SKIP = 0


def ck(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"\033[32m✓\033[0m {name}")
    else:
        FAIL += 1
        print(f"\033[31m✗\033[0m {name}" + (f"  — {detail}" if detail else ""))


def skip(name, why):
    global SKIP
    SKIP += 1
    print(f"\033[33m−\033[0m SKIP {name}（{why}）")


def streams(path):
    r = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "stream=codec_type",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    return sorted(x.strip() for x in r.stdout.splitlines() if x.strip())


# ---------------------------------------------------------------------------
# 1/2. 原生行为（需要已构建的二进制）
# ---------------------------------------------------------------------------

def find_audio_app():
    """找一个当前在屏、可被 app 级采集的目标。没有就返回 None（不编）。"""
    try:
        sys.path.insert(0, str(ROOT / "tools" / "macos"))
        import backend as mb
        pj = mb.probe()
        if not pj.get("ok"):
            return None
        for w in pj.get("windows") or []:
            b = w.get("owner_bundle_id") or ""
            if b and w.get("on_screen"):
                return b, w.get("windowID")
    except Exception:
        return None
    return None


def test_native_no_audio_contrast():
    if not BIN.exists():
        skip("原生 --no-audio 对照", "录制器未构建")
        return
    tgt = find_audio_app()
    if not tgt:
        skip("原生 --no-audio 对照", "没有可用的在屏目标（或未授权屏幕录制）")
        return
    bundle, _wid = tgt
    tmp = Path(tempfile.mkdtemp(prefix="noaudio-"))

    # A) --no-audio：产物应**没有**音频轨
    a = tmp / "noaudio.mp4"
    rc = subprocess.run([str(BIN), "--bundle-id", bundle, "--no-audio",
                         "--out", str(a), "--duration", "3", "--quiet"],
                        capture_output=True, text=True, timeout=120).returncode
    if a.exists() and a.stat().st_size > 0:
        st = streams(a)
        ck("--no-audio 的产物没有音频轨", "audio" not in st, str(st))
        ck("--no-audio 的产物有视频轨", "video" in st, str(st))
    else:
        skip("原生 --no-audio 对照（A）", f"没产出文件（rc={rc}）")

    # B) 默认（app 音频）：应有音频轨 —— 回归保持
    b = tmp / "withaudio.mp4"
    rc2 = subprocess.run([str(BIN), "--bundle-id", bundle,
                          "--out", str(b), "--duration", "3", "--quiet"],
                         capture_output=True, text=True, timeout=120).returncode
    if b.exists() and b.stat().st_size > 0:
        st2 = streams(b)
        ck("app 音频模式仍**有**音频轨（回归保持）", "audio" in st2, str(st2))
    else:
        skip("原生 app 音频对照（B）", f"没产出文件（rc={rc2}）")
    shutil.rmtree(tmp, ignore_errors=True)


def test_native_rejects_both_off():
    if not BIN.exists():
        skip("原生拒绝 --no-video + --no-audio", "录制器未构建")
        return
    tgt = find_audio_app()
    if not tgt:
        skip("原生拒绝 --no-video + --no-audio", "没有可用目标")
        return
    bundle, _ = tgt
    out = Path(tempfile.mkdtemp(prefix="bothoff-")) / "x.mp4"
    r = subprocess.run([str(BIN), "--bundle-id", bundle, "--no-video", "--no-audio",
                        "--out", str(out), "--duration", "2"],
                       capture_output=True, text=True, timeout=60)
    ck("原生拒绝 --no-video + --no-audio（退出码 64）", r.returncode == 64, f"rc={r.returncode}")
    ck("且不产出文件", not out.exists())
    shutil.rmtree(out.parent, ignore_errors=True)


# ---------------------------------------------------------------------------
# 3/4. worker 与元数据（离线）
# ---------------------------------------------------------------------------

def test_worker_rejects_audio_only_with_no_audio():
    """`expect=audio`（只要音频）+ 音频粒度 none = 自相矛盾，必须拒绝。"""
    src = (SCRIPTS / "capture_worker.py").read_text(encoding="utf-8")
    ck("worker 有 audio-only + no-audio 的拒绝分支", "audio-only + no-audio" in src)
    ck("worker 把 no_audio 传给后端", "no_audio=no_audio_flag" in src)
    ck("判据来自 target 的 audio granularity（不是硬编码）",
       'audio_gran not in ("", "none")' in src)


def test_native_metadata_truthful():
    if not BIN.exists():
        skip("元数据如实", "录制器未构建")
        return
    tgt = find_audio_app()
    if not tgt:
        skip("元数据如实", "没有可用目标")
        return
    bundle, _ = tgt
    tmp = Path(tempfile.mkdtemp(prefix="meta-"))
    out = tmp / "m.mp4"
    mj = tmp / "m.metrics.json"
    r = subprocess.run([str(BIN), "--bundle-id", bundle, "--no-audio",
                        "--out", str(out), "--json", str(mj), "--duration", "3", "--quiet"],
                       capture_output=True, text=True, timeout=120)
    if not mj.exists():
        skip("元数据如实", f"没写出 metrics（rc={r.returncode}）")
        shutil.rmtree(tmp, ignore_errors=True)
        return
    m = json.loads(mj.read_text(encoding="utf-8"))
    ck("target.captures_audio 如实为 false", m["target"]["captures_audio"] is False,
       str(m["target"].get("captures_audio")))
    ck("audio.enabled 如实为 false", m["audio"].get("enabled") is False)
    ck("verdict 不声称有音轨（basis=not_requested）",
       m["verdict"].get("audio_track_present_basis") == "not_requested",
       str(m["verdict"].get("audio_track_present_basis")))
    ck("verdict.audio_requested=false", m["verdict"].get("audio_requested") is False)
    ck("不因缺音轨而报 problem", not (m.get("problems") or []), str(m.get("problems")))
    shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# 5. 腐败文件仍算失败
# ---------------------------------------------------------------------------

def test_corrupt_recording_still_fails():
    """**不能**靠"删掉静音 problems"把坏文件判成功。

    用假录制器产出一个"writer failed + 文件不可读"的结局，确认结论是 fail。
    """
    tmp = Path(tempfile.mkdtemp(prefix="corrupt-"))
    job = tmp / "job"
    media = job / "r1.mp4"
    job.mkdir(parents=True, exist_ok=True)
    media.write_bytes(b"not a real mp4 at all")
    cs.init_run(job, "r1", "t", {"platform": "macos",
                                 "video": {"granularity": "window"},
                                 "audio": {"granularity": "none"}})
    for st in ("preflight", "starting", "running", "stopping", "stopped", "verifying"):
        cs.transition(job, "r1", st)
    (job / "r1.live.json").write_text(json.dumps(
        {"run_token": "tk", "capture_initialized": True, "first_video_frame": True}),
        encoding="utf-8")
    (job / "r1.metrics.json").write_text(json.dumps({
        "writer_status": "failed", "problems": ["writer.status=3（非 completed）"],
        "verdict": {"video_frames_arriving": True, "tracks_verified": False}}),
        encoding="utf-8")
    cs.update_run(job, "r1", artifacts={"media": str(media),
                                        "metrics": str(job / "r1.metrics.json"),
                                        "live_status": str(job / "r1.live.json")},
                  stages={"capture": {"result": "fail",
                                      "problems": ["writer.status=3（非 completed）"],
                                      "recorder_exit": 1, "audio_path_has_data": False}})
    cs.transition(job, "r1", "verified", stages={"verify": {"result": "pass"}})
    out = tmp / "rep.json"
    subprocess.run([sys.executable, str(SCRIPTS / "agent_capture.py"), "report",
                    "--job-dir", str(job), "--run-id", "r1",
                    "--production-run-id", "p1", "--out", str(out)],
                   capture_output=True, text=True, timeout=60)
    o = json.loads(out.read_text(encoding="utf-8"))
    ck("writer failed 的录制：unexpected_stop=True", o["unexpected_stop"] is True)
    ck("container_readable=False（文件不可读）", o["checks"]["container_readable"] is False)
    ck("problems 里保留 writer 失败", any("writer" in p for p in o.get("completeness_blockers", [])),
       str(o.get("completeness_blockers")))
    shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    print("═══ --no-audio 条件路径回归 ═══")
    test_native_no_audio_contrast()
    test_native_rejects_both_off()
    test_worker_rejects_audio_only_with_no_audio()
    test_native_metadata_truthful()
    test_corrupt_recording_still_fails()
    print(f"\n{PASS} passed, {FAIL} failed, {SKIP} skipped")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
