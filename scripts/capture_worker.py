#!/usr/bin/env python3
"""capture_worker — 真正跑录制并推进 run 状态的后台进程。

它由 `agent_capture.py start` 以**分离会话**方式启动，所以 CLI 能立刻返回，
而 `status`/`stop` 操作的是这个**活着的** run。

## 为什么需要 worker，而不是直接跑录制器

三件事必须由一个"活着且知道 run_id"的进程来做：

1. **判断"有效开始"**：进程起来了 ≠ 录到了东西。worker 读录制器的实时进度文件，
   等到 `capture_initialized` **且** `first_video_frame` 都为真，才把状态推到
   `running`。超时没等到 → `failed`，而不是假装在录。
2. **响应停止请求**：`stop` 写的是一个带 nonce 的**请求**（不是拿旧 PID 直接杀）。
   worker 轮询到请求后，先核对 nonce，再对录制器发 SIGTERM（录制器自己是有界收尾）。
3. **收尾并给结论**：录制器退出后，worker 读 metrics，写 `stages.capture`
   （录像完整性）、`artifacts`（原片/指标/焦点日志），再走 verify。

## 音频的诚实口径

`audio_path_has_data`（通路有数据）和 `audio_signal_observed`（真的超过静音门槛）
**分开报**。游戏开局前没声音是正常的，**不**把它当失败，也**不**因此阻塞。
只有"通路一直没有数据"才会进 problems。

退出码：0 正常收尾；2 参数/状态错误；3 启动失败；4 收尾失败。
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "tools" / "macos"))

import capture_state as cs  # noqa: E402

# 等到"有效开始"的上限。超过就判定启动失败 —— 不无限等。
EFFECTIVE_START_TIMEOUT = 45.0
# 停止请求后等录制器退出的上限（录制器自己有 duration+30s 的收尾上限）
STOP_GRACE = 60.0


def _load_backend(platform: str):
    if platform == "macos":
        import importlib
        return importlib.import_module("backend")
    if platform == "windows":
        import importlib.util
        p = HERE.parent / "tools" / "windows" / "backend.py"
        if not p.exists():
            raise RuntimeError(
                f"Windows 后端模块不存在：{p}（由独立会话交付，尚未集成）")
        spec = importlib.util.spec_from_file_location("win_backend", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    raise RuntimeError(f"平台 {platform!r} 没有可用后端")


def _read_json_quiet(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="capture_worker")
    ap.add_argument("--job-dir", required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--duration", type=float, default=0.0)
    ap.add_argument("--expect", default="av", choices=["av", "audio", "auto"])
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--max-width", type=int, default=1920)
    ap.add_argument("--no-video", action="store_true")
    a = ap.parse_args(argv)

    job_dir = Path(a.job_dir)
    run_id = a.run_id
    out = Path(a.out)
    metrics = out.with_suffix(".metrics.json")
    focus = out.with_suffix(".focus.jsonl")
    live = job_dir / f"{run_id}.live.json"
    worker_log = job_dir / f"{run_id}.worker.log"

    def log(msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}\n"
        try:
            with open(worker_log, "a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError:
            pass

    try:
        st = cs.load_run(job_dir, run_id)
    except cs.StateError as exc:
        log(f"读状态失败: {exc}")
        return 2

    target = st.target or {}
    platform = target.get("platform") or "macos"
    try:
        backend = _load_backend(platform)
    except RuntimeError as exc:
        log(str(exc))
        cs.transition(job_dir, run_id, "failed", note=str(exc), error=str(exc))
        return 3

    av = backend.availability()
    if not av.get("available"):
        log(f"后端不可用: {av.get('reason')}")
        cs.transition(job_dir, run_id, "failed", note="backend unavailable",
                      error=av.get("reason"))
        return 3

    # —— 构造启动参数并起录制器 ——
    try:
        rec_argv = backend.build_start_argv(
            target, str(out), duration=a.duration, fps=a.fps,
            max_width=a.max_width, focus_log=str(focus), metrics_json=str(metrics),
            live_status=str(live), no_video=a.no_video)
    except (ValueError, AttributeError) as exc:
        log(f"构造启动参数失败: {exc}")
        cs.transition(job_dir, run_id, "failed", note="bad target", error=str(exc))
        return 2

    log("启动录制器: " + " ".join(rec_argv))
    rec_out = open(worker_log, "ab")
    try:
        proc = subprocess.Popen(rec_argv, stdout=rec_out, stderr=rec_out,
                                start_new_session=True)
    except OSError as exc:
        log(f"启动录制器失败: {exc}")
        cs.transition(job_dir, run_id, "failed", note="spawn failed", error=str(exc))
        return 3

    # 先把录制器 pid 与产物路径记进 artifacts（供排查；**不**用它做停止决策）。
    # 注意：这里**还不**把状态推到 running —— 进程起来不等于录到了东西。
    try:
        # 用 update_run（不改状态）：此刻状态已经是 starting，
        # 再 transition 到 starting 是非法转移，会把这次写入整个丢掉。
        cs.update_run(job_dir, run_id,
                      artifacts={"recorder_pid": proc.pid, "media": str(out),
                                 "metrics": str(metrics), "focus_log": str(focus),
                                 "live_status": str(live), "worker_log": str(worker_log)})
    except cs.StateError as exc:
        log(f"记录 artifacts 失败: {exc}")

    # —— 等"有效开始"：初始化 + 有效首帧 ——
    started = False
    deadline = time.time() + EFFECTIVE_START_TIMEOUT
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        ls = _read_json_quiet(live)
        if ls and ls.get("effectively_started"):
            started = True
            break
        time.sleep(0.2)

    if not started:
        # **没有观察到有效开始**。无论是因为超时，还是录制器自己先退出了，
        # 都**不能**声称"有效开始" —— 那正是"把没验证的当通过"。
        # 只有一种情况可以继续：录制器正常退出且 metrics 证明真的录到了内容
        # （例如 duration 很短，进度文件还没落盘就收尾了）。这交给下面的收尾逻辑判定。
        still_running = proc.poll() is None
        log(f"未观察到有效开始标记（recorder {'仍在跑' if still_running else '已退出'}）")
        if still_running:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except OSError:
                pass
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except OSError:
                    pass
            cs.transition(job_dir, run_id, "failed", note="no effective start",
                          error=f"等待有效首帧超时（{EFFECTIVE_START_TIMEOUT}s）："
                                f"进程起来了但没有收到画面帧，拒绝把这段当成在录")
            return 3
    else:
        log(f"有效开始：首帧已到达（recorder pid={proc.pid}）")
        try:
            cs.transition(job_dir, run_id, "running",
                          note="effective start observed (init + first frame)")
        except cs.StateError as exc:
            log(f"状态推进到 running 失败: {exc}")

    # —— 轮询停止请求，直到录制器自己退��� ——
    while proc.poll() is None:
        try:
            req = cs.poll_stop(job_dir, run_id)
        except cs.StateError as exc:
            log(f"停止请求文件损坏（拒绝当成「无请求」）: {exc}")
            req = None
        if req is not None:
            v = cs.verify_stop_owner(req)
            log(f"收到停止请求 nonce={v.get('nonce')} 身份匹配={v.get('identity_matches')}")
            if v.get("identity_matches") is False:
                # 发起者进程已被替换（pid 复用）→ 不当真，继续录
                log("停止请求身份不匹配（疑似 pid 复用），忽略")
            else:
                try:
                    cs.transition(job_dir, run_id, "stopping",
                                  note=f"stop nonce={v.get('nonce')}")
                except cs.StateError:
                    pass
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except OSError:
                    pass
                break
        time.sleep(0.25)

    try:
        rc = proc.wait(timeout=STOP_GRACE)
    except subprocess.TimeoutExpired:
        log(f"录制器未在 {STOP_GRACE}s 内退出，强杀")
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except OSError:
            pass
        rc = proc.wait(timeout=15)

    log(f"录制器退出码 {rc}")

    # —— 收尾：取**实测**结论 ——
    #
    # 两个证据源，分工不同：
    #   · live 文件：录制**过程中**的里程碑（用来判断"有效开始"）
    #   · metrics JSON：录制**结束后**的权威实测（轨道是否存在、帧是否在到达、
    #     音频是否真的有信号）。录制器**总是**会写它，所以最终结论以它为准。
    # 只用 live 文件是错的：短录制里进度文件可能还没落盘就收尾了，
    # 那样会把一次**成功**的录制误报成"没有收到任何画面帧"。
    ls = _read_json_quiet(live) or {}
    mj = _read_json_quiet(metrics) or {}
    verdict = (mj.get("verdict") or {}) if isinstance(mj, dict) else {}

    problems = []
    if rc != 0:
        problems.append(f"录制器退出码 {rc}（非 0）")
    for pr in (mj.get("problems") or []):
        problems.append(str(pr))
    # advisories 与 problems **分开**：标题变化之类的观察不影响"录像是否完整"，
    # 但审片需要知道。混进 problems 会把一次**好**的录制误判成 fail。
    advisories = [str(x) for x in (mj.get("advisories") or [])]

    have_metrics = bool(verdict)
    if not have_metrics:
        # 没有 metrics 就**无法**给出可靠结论 —— 报 unknown，不报 pass
        problems.append("没有 metrics 证据（录制器未产出指标文件）：无法判定录像完整性")

    # 画面：以 metrics 为准，live 作为补充
    video_ok = bool(verdict.get("video_frames_present")) or bool(ls.get("first_video_frame"))
    if have_metrics and not verdict.get("video_frames_present"):
        problems.append("metrics 显示没有画面帧到达")
    elif not have_metrics and not ls.get("first_video_frame"):
        problems.append("没有收到任何��面帧")

    # 音频：**通路有数据** 与 **真的有信号** 分开报
    audio_path_ok = bool(verdict.get("audio_track_present")) or bool(ls.get("audio_path_has_data"))
    audio_signal = bool(verdict.get("audio_has_signal")) or bool(ls.get("audio_signal_observed"))
    if a.expect != "audio" and have_metrics and not verdict.get("audio_track_present"):
        problems.append("产出文件里没有音频轨")
    elif a.expect != "audio" and not have_metrics and not ls.get("audio_path_has_data"):
        problems.append("音频通路没有数据（可能是���标 app 没有音频输出）")

    audio_note = ""
    if audio_path_ok and not audio_signal:
        # 通路正常但��段没声音：**不是失败**，如实说明即可（游戏可能本来就是静音的）
        audio_note = ("音频通路有数据，但整段没有超过静音门槛的信号："
                      "不能声称「录到了声音」")

    if not have_metrics:
        capture_result = "unknown"
    else:
        capture_result = "pass" if not problems else "fail"

    capture_stage = {
        "result": capture_result,
        "problems": problems,
        "advisories": advisories,
        "recorder_exit": rc,
        "evidence_source": "metrics" if have_metrics else "live_only",
        "audio_path_has_data": audio_path_ok,
        # **分开报**：通路有数据 ≠ 真的有声音
        "audio_signal_observed": audio_signal,
        "audio_note": audio_note,
        "audio_peak_dbfs": verdict.get("audio_peak_dbfs"),
        "video_frames": ls.get("video_frames"),
        "video_frames_arriving": verdict.get("video_frames_arriving"),
        "audio_buffers": ls.get("audio_buffers"),
        "tracks_verified": verdict.get("tracks_verified"),
        "verified_level": av.get("verified_level"),
    }

    # 先到 stopped（若已是 stopping 就直接走；否则先转 stopping）
    cur = cs.load_run(job_dir, run_id).status
    if cur == "running":
        cs.transition(job_dir, run_id, "stopping", note="recorder exited")
        cur = "stopping"
    if cur in ("stopping", "crashed"):
        cs.transition(job_dir, run_id, "stopped", note="finalize",
                      stages={"capture": capture_stage})

    # —— verify ——
    cs.transition(job_dir, run_id, "verifying", note="verify media")
    try:
        vres = backend.verify(str(out), expect=a.expect)
    except Exception as exc:  # 后端 verify 出问题 → unknown，不假装通过
        vres = {"result": "unknown", "notes": f"verify 抛错: {exc}"}
    vres["audio_signal_observed"] = audio_signal
    try:
        cs.transition(job_dir, run_id,
                      "verified" if vres.get("result") == "pass" else "verify_failed",
                      note=f"verify={vres.get('result')}",
                      stages={"verify": vres})
    except cs.StateError as exc:
        log(f"写 verify 结论失败: {exc}")

    log(f"完成：capture={capture_result} verify={vres.get('result')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
