#!/usr/bin/env python3
"""capture_worker — 真正跑录制并推进 run 状态的后台进程。

它由 `agent_capture.py start` 以**分离会话**方式启动，所以 CLI 能立刻返回，
而 `status`/`stop` 操作的是这个**活着的** run。

## 为什么需要 worker，而不是直接跑录制器

三件事必须由一个"活着且知道 run_id"的进程来做：

1. **判断"有效开始"**：进程起来了 ≠ 录到了东西。worker 读录制器的实时进度文件，
   等到 `capture_initialized` **且**（需要画面时）`first_video_frame` 都为真，
   才把状态推到 `running`。没等到 → `failed`，而不是假装在录。
2. **响应停止请求**：`stop` 写的是一个带 nonce 的**请求**（不是拿旧 PID 直接杀）。
   worker 轮询到请求后，先核对 nonce，再对录制器发 SIGTERM（录制器自己是有界收尾）。
   **启动阶段也轮询** —— 否则"刚点开始就想取消"要等满超时。
3. **收尾并给结论**：录制器退出后读 metrics，写 `stages.capture`（录像完整性）、
   `artifacts`，再走 verify。**capture 与 verify 都通过才叫 verified。**

## 这一版修掉的五处上线即出错的问题（都有对应反例测试）

| # | 问题 | 后果 |
|---|---|---|
| 1 | 实时进度文件没有 run 令牌 | 上一次的**残留**文件会被读成 `effectively_started=true` → 把"没开始"误判成"已开始" |
| 2 | 期望轨道判断写反 | `--expect audio`（只要音频）时**跳过**音频检查；音频关掉或纯音频时又**无条件**要求画面 → 该失败的不失败、不该失败的死等 45s |
| 3 | 未开始但录制器已退出时继续往下走 | 状态机从 `starting` 直接跳 `verifying` → **未捕获异常**，run 卡在 `starting`、录制器成孤儿 |
| 4 | capture 失败但 verify 通过仍判 `verified` | 提前停止 / 目标消失 / 非零退出被 verify 的"容器看起来没问题"**盖掉** |
| 5 | 启动阶段不轮询停止请求；无 finally 回收 | 启动期无法取消；worker 异常时录制器**无限孤儿** |

## 音频的诚实口径

`audio_path_has_data`（通路有数据）与 `audio_signal_observed`（真的超过静音门槛）
**分开报**。开局前没声音是正常的，**不**当失败、**不**阻塞。
只有"该有音频却连通路都没数据"才进 problems。

退出码：0 成功收尾且结论为 verified；2 参数/状态错误；3 启动失败；
4 收尾异常；5 capture 或 verify 未通过（**不再一律 0**）。
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import capture_state as cs  # noqa: E402

# 等到"有效开始"的上限。超过就判定启动失败 —— 不无限等。
EFFECTIVE_START_TIMEOUT = 45.0
# 停止请求后等录制器退出的上限（录制器自己有 duration+30s 的收尾上限）
STOP_GRACE = 60.0
# finally 里回收进程组的硬上限
REAP_GRACE = 20.0

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_START_FAILED = 3
EXIT_FINALIZE_FAILED = 4
EXIT_NOT_VERIFIED = 5


def _backend_search_dirs(platform: str):
    """后端模块的搜索路径。

    `AGENT_CAPTURE_BACKEND_DIR` 是**显式**的后端覆盖（测试用假后端、
    或把后端放在非默认位置时用）。默认仍然只用仓库内实现 ——
    没有这个覆盖，测试就只能去改仓库里的真后端，那既危险又不可信。
    """
    dirs = []
    override = os.environ.get("AGENT_CAPTURE_BACKEND_DIR")
    if override:
        dirs.append(Path(override))
    dirs.append(HERE.parent / "tools" / platform)
    return dirs


def _load_backend(platform: str):
    if platform == "macos":
        import importlib
        # 把搜索路径按顺序放到 sys.path 最前（后插入的在更前面）
        for d in reversed(_backend_search_dirs("macos")):
            if d.exists():
                sys.path.insert(0, str(d))
        return importlib.import_module("backend")
    if platform == "windows":
        import importlib.util
        p = HERE.parent / "tools" / "windows" / "backend.py"
        if not p.exists():
            raise RuntimeError(f"Windows 后端模块不存在：{p}")
        spec = importlib.util.spec_from_file_location("win_backend", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    raise RuntimeError(f"平台 {platform!r} 没有可用后端")


def _read_json_strict(path: Path):
    """读 JSON。**返回 (值, 错误)**。

    读不到/坏了必须能被调用方区分出来 —— 早期版本用 `or {}` 把"没有 metrics"
    悄悄变成"空 metrics"，于是"无法判定"看起来像"没问题"。
    """
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, "文件不存在"
    except ValueError as exc:
        return None, f"不是合法 JSON: {exc}"
    except OSError as exc:
        return None, f"读取失败: {exc}"


def _target_requirements(target: dict, expect: str, no_video: bool):
    """这次录制**到底该有什么轨道** —— 由真实目标与参数决定，不用一刀切规则。

    早期版本写反了：`--expect audio`（只要音频）反而**跳过**音频检查；
    音频关掉或纯音频时又**无条件**要求画面帧。
    """
    audio_gran = str(((target or {}).get("audio") or {}).get("granularity") or "")
    video_gran = str(((target or {}).get("video") or {}).get("granularity") or "")

    if expect == "audio":
        # 只要音频：**不**要求画面
        return {"video": False, "audio": True}
    if expect == "auto":
        # 由录制器决定：不预设要求，只如实报告实际有什么
        return {"video": False, "audio": False}
    # expect == "av"：按目标与参数决定
    wants_video = (not no_video) and video_gran not in ("", "none")
    wants_audio = audio_gran not in ("", "none")
    return {"video": wants_video, "audio": wants_audio}


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

    # 本次 run 的令牌：写进实时进度文件，读的时候必须匹配。
    # 没有它，上一次残留的 live 文件会被读成"已开始"。
    run_token = secrets.token_hex(12)

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
        return EXIT_USAGE

    target = st.target or {}
    platform = target.get("platform") or "macos"
    req = _target_requirements(target, a.expect, a.no_video)

    try:
        backend = _load_backend(platform)
    except RuntimeError as exc:
        log(str(exc))
        cs.transition(job_dir, run_id, "failed", note=str(exc), error=str(exc))
        return EXIT_START_FAILED

    av = backend.availability()
    if not av.get("available"):
        log(f"后端不可用: {av.get('reason')}")
        cs.transition(job_dir, run_id, "failed", note="backend unavailable",
                      error=av.get("reason"))
        return EXIT_START_FAILED

    # —— 清掉可能残留的实时进度文件 ——
    # 必须在起录制器**之前**：否则残留文件会让下面第一次读取就"已开始"。
    try:
        if live.exists():
            live.unlink()
            log(f"清掉残留的实时进度文件: {live}")
    except OSError as exc:
        log(f"无法清理残留进度文件（{exc}）：仍会靠 run_token 校验拒绝它")

    # —— 构造启动参数并起录制器 ——
    try:
        rec_argv = backend.build_start_argv(
            target, str(out), duration=a.duration, fps=a.fps,
            max_width=a.max_width, focus_log=str(focus), metrics_json=str(metrics),
            live_status=str(live), live_token=run_token, no_video=a.no_video)
    except TypeError:
        # 后端还没支持 live_token（例如未更新的 Windows 桥）→ 明确失败，
        # **不**退化成"没有令牌也照跑"（那正是要修的问题）。
        log("后端 build_start_argv 不接受 live_token：无法保证进度文件属于本次 run")
        cs.transition(job_dir, run_id, "failed", note="backend lacks live_token",
                      error="后端不支持 --live-token，拒绝在没有 run 令牌的情况下继续")
        return EXIT_START_FAILED
    except (ValueError, AttributeError) as exc:
        log(f"构造启动参数失败: {exc}")
        cs.transition(job_dir, run_id, "failed", note="bad target", error=str(exc))
        return EXIT_USAGE

    log("启动录制器: " + " ".join(rec_argv))
    rec_out = open(worker_log, "ab")
    try:
        proc = subprocess.Popen(rec_argv, stdout=rec_out, stderr=rec_out,
                                start_new_session=True)
    except OSError as exc:
        log(f"启动录制器失败: {exc}")
        cs.transition(job_dir, run_id, "failed", note="spawn failed", error=str(exc))
        return EXIT_START_FAILED

    # 从这一刻起我们**拥有**这个进程组：任何退出路径都必须把它回收掉。
    owned = True
    got_signal: list = []

    def _on_signal(signum, _frame):
        got_signal.append(signum)
        log(f"worker 收到信号 {signum}：将走有界清理")

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _on_signal)
        except (ValueError, OSError):
            pass

    def _live_started():
        """读实时进度，并**核对令牌**。返回 (started, payload, note)。"""
        payload, err = _read_json_strict(live)
        if payload is None:
            return False, None, err
        tok = payload.get("run_token")
        if tok != run_token:
            # 不是本次 run 的文件（残留，或别的 run 写进来的）→ 拒绝采用
            return False, payload, f"run_token 不匹配（文件={tok!r} 本次={run_token!r}）"
        started = bool(payload.get("capture_initialized"))
        if req["video"]:
            started = started and bool(payload.get("first_video_frame"))
        if req["audio"]:
            # 音频只要求**通路有数据**：开局前没声音是正常的，不能因此阻塞
            started = started and bool(payload.get("audio_path_has_data"))
        return started, payload, None

    def _poll_stop_once() -> bool:
        """检查停止请求。返回 True 表示"应当停止录制器"。"""
        try:
            r = cs.poll_stop(job_dir, run_id)
        except cs.StateError as exc:
            log(f"停止请求文件损坏（拒绝当成「无请求」）: {exc}")
            return False
        if r is None:
            return False
        v = cs.verify_stop_owner(r)
        log(f"收到停止请求 nonce={v.get('nonce')} 身份匹配={v.get('identity_matches')}")
        if v.get("identity_matches") is False:
            log("停止请求身份不匹配（疑似 pid 复用），忽略")
            return False
        try:
            cs.transition(job_dir, run_id, "stopping", note=f"stop nonce={v.get('nonce')}")
        except cs.StateError:
            pass
        return True

    def _terminate_bounded(grace: float) -> int:
        """有界终止并回收进程组。返回退出码（拿不到就返回 -1）。"""
        nonlocal owned
        if not owned:
            try:
                return proc.returncode if proc.returncode is not None else -1
            except Exception:
                return -1
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except OSError:
            pass
        try:
            rc = proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            log(f"录制器未在 {grace}s 内退出，SIGKILL")
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except OSError:
                pass
            try:
                rc = proc.wait(timeout=REAP_GRACE)
            except subprocess.TimeoutExpired:
                log("SIGKILL 后仍未回收（放弃等待，标记为未回收）")
                rc = -1
        owned = False
        return rc

    rc = -1
    started = False
    stop_during_start = False
    start_note = ""
    try:
        try:
            cs.update_run(job_dir, run_id,
                          artifacts={"recorder_pid": proc.pid, "media": str(out),
                                     "metrics": str(metrics), "focus_log": str(focus),
                                     "live_status": str(live), "worker_log": str(worker_log),
                                     "run_token": run_token})
        except cs.StateError as exc:
            log(f"记��� artifacts 失败: {exc}")

        # —— 等"有效开始"：**同时**轮询停止请求 ——
        # 早期版本这里不轮询，于是"刚点开始就想取消"必须等满 45s 超时。
        deadline = time.time() + EFFECTIVE_START_TIMEOUT
        while time.time() < deadline:
            if got_signal:
                start_note = f"worker 收到信号 {got_signal[0]}"
                break
            if proc.poll() is not None:
                start_note = "录制器在有效开始之前就退出了"
                break
            if _poll_stop_once():
                stop_during_start = True
                start_note = "启动阶段收到停止请求"
                break
            ok, _payload, note = _live_started()
            if ok:
                started = True
                break
            if note and "不匹配" in note:
                # 令牌不符：继续等（本次的进度文件还没出现），���记下来
                log(f"实时进度：{note}")
            time.sleep(0.2)

        if not started:
            # **没有观察到有效开始 → 一律失败。**
            # 不管是因为超时、录制器提前退出、还是启动阶段被取消。
            # 关键：**不要**继续往下走 —— 从 starting 直接转 verifying 是非法转移，
            # 会抛未捕获异常，把 run 卡在 starting 且留下孤儿进程。
            rc = proc.returncode if proc.poll() is not None else _terminate_bounded(30.0)
            mj_raw, mj_err = _read_json_strict(metrics)
            detail = []
            if rc is not None and rc != 0:
                detail.append(f"录制器退出码 {rc}")
            if mj_err:
                detail.append(f"metrics 不可读（{mj_err}）")
            elif isinstance(mj_raw, dict):
                for pr in (mj_raw.get("problems") or [])[:3]:
                    detail.append(str(pr))
            reason = start_note or f"等待有效开始超时（{EFFECTIVE_START_TIMEOUT}s）"
            msg = (f"{reason}：拒绝把这段当成在录"
                   f"（需要 画面={req['video']} 音频={req['audio']}）"
                   + ("；" + "；".join(detail) if detail else ""))
            log(msg)
            cs.transition(job_dir, run_id, "failed", note="no effective start",
                          error=msg,
                          stages={"capture": {
                              "result": "fail", "problems": [msg],
                              "recorder_exit": rc, "started": False,
                              "stopped_during_start": stop_during_start,
                              "metrics_read_error": mj_err,
                              "run_token": run_token,
                          }})
            return EXIT_START_FAILED

        log(f"有效开始（recorder pid={proc.pid}, token={run_token[:8]}）")
        try:
            cs.transition(job_dir, run_id, "running",
                          note="effective start observed")
        except cs.StateError as exc:
            log(f"状态推进到 running 失败: {exc}")

        # —— 轮询停止请求，直到录制器退出 ——
        while proc.poll() is None:
            if got_signal:
                log(f"worker 收到信号 {got_signal[0]}：有界停止录制器")
                break
            if _poll_stop_once():
                break
            time.sleep(0.25)

        rc = _terminate_bounded(STOP_GRACE)

    finally:
        # **任何**退出路径都要把拥有的进程组回收掉，绝不留给孤儿。
        if owned:
            log("finally：回收仍存活的录制器进程组")
            try:
                _terminate_bounded(REAP_GRACE)
            except Exception as exc:  # 回收本身出错也不能让异常逃出去
                log(f"finally 回收异常（已记录，不抛出）: {exc}")

    log(f"录制器退出码 {rc}")

    # —— 收尾：取**实测**结论 ——
    # live 文件：过程中的里程碑（判断"有效开始"）
    # metrics：结束后的权威实测（轨道、帧、音频信号）
    # **两者都读不到就不给 pass。**
    ls, ls_err = _read_json_strict(live)
    ls = ls if isinstance(ls, dict) and ls.get("run_token") == run_token else {}
    mj, mj_err = _read_json_strict(metrics)
    mj = mj if isinstance(mj, dict) else None
    verdict = (mj.get("verdict") or {}) if mj else {}

    problems = []
    advisories = []
    if rc != 0:
        problems.append(f"录制器退出码 {rc}（非 0）")
    if got_signal:
        problems.append(f"worker 收到信号 {got_signal[0]}：本次录制被中断")
    if mj is None:
        # **没有 metrics 就是无法判定** —— 明确进 problems，不静默当 {} 用
        problems.append(f"没有可用的 metrics 证据（{mj_err or '未知原因'}）："
                        f"无法判定录像完整性")
    else:
        for pr in (mj.get("problems") or []):
            problems.append(str(pr))
        advisories = [str(x) for x in (mj.get("advisories") or [])]

    have_metrics = bool(verdict)

    # **优先级：metrics > live**。
    # metrics 是录制结束后对**产物文件**的实测；live 只是过程中的观察。
    # 两者不一致时以 metrics 为准 —— 用 `or` 把两者并起来是错的：
    # live 说"收到过第一帧"、metrics 说"文件里没有帧"时，`or` 会把结论判成 pass，
    # 而真相是那些帧没写进文件。
    # live 只在 **metrics 不可用** 时作为降级证据。
    if have_metrics:
        video_present = bool(verdict.get("video_frames_present"))
        audio_track = bool(verdict.get("audio_track_present"))
        audio_signal = bool(verdict.get("audio_has_signal"))
    else:
        video_present = bool(ls.get("first_video_frame"))
        audio_track = bool(ls.get("audio_path_has_data"))
        audio_signal = bool(ls.get("audio_signal_observed"))

    # 只有**该有**的轨道缺失才算失败
    if req["video"] and have_metrics and not video_present:
        problems.append("metrics 显示没有画面帧到达（本次需要画面）")
    if req["video"] and not have_metrics and not ls.get("first_video_frame"):
        problems.append("没有收到任何画面帧（本次需要画面）")
    if req["audio"] and have_metrics and not audio_track:
        problems.append("产出文件里没有音频轨（本次需要音频）")
    if req["audio"] and not have_metrics and not ls.get("audio_path_has_data"):
        problems.append("音频通路没有数据（本次需要音频）")
    if not req["video"] and not req["audio"]:
        advisories.append("本次未要求画面或音频轨道（expect=auto）：只如实报告实际内容")

    audio_note = ""
    if audio_track and not audio_signal:
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
        "started": started,
        "required_video": req["video"],
        "required_audio": req["audio"],
        "run_token": run_token,
        "evidence_source": "metrics" if have_metrics else "none",
        "metrics_read_error": mj_err,
        "live_read_error": ls_err,
        "audio_path_has_data": audio_track,
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

    # 状态推进：**只在合法转移上走**。not-started 的分支已经 return 了，
    # 走到这里 started 必为 True（状态已到 running 或 stopping）。
    try:
        cur = cs.load_run(job_dir, run_id).status
        if cur == "running":
            cs.transition(job_dir, run_id, "stopping", note="recorder exited")
            cur = "stopping"
        if cur in ("stopping", "crashed"):
            cs.transition(job_dir, run_id, "stopped", note="finalize",
                          stages={"capture": capture_stage})
        else:
            log(f"状态 {cur!r} 不允许收尾转移：把 capture 结论写入字段，不改状态")
            cs.update_run(job_dir, run_id, stages={"capture": capture_stage})
    except cs.StateError as exc:
        log(f"写 capture 结论失败: {exc}")
        return EXIT_FINALIZE_FAILED

    # —— verify ——
    try:
        cs.transition(job_dir, run_id, "verifying", note="verify media")
    except cs.StateError as exc:
        log(f"无法进入 verifying: {exc}")
        return EXIT_FINALIZE_FAILED
    try:
        vres = backend.verify(str(out), expect=a.expect)
    except Exception as exc:
        vres = {"result": "unknown", "notes": f"verify 抛错: {exc}"}
    vres["audio_signal_observed"] = audio_signal

    # **组合判定**：capture 与 verify 都通过才叫 verified。
    # 早期版本只看 verify —— 于是"提前停止 / 目标消失 / 非零退出"会被
    # verify 的"容器看起来没问题"盖掉。
    capture_pass = capture_result == "pass"
    verify_pass = vres.get("result") == "pass"
    if capture_pass and verify_pass:
        final_state, exit_code = "verified", EXIT_OK
    elif capture_result == "unknown" or vres.get("result") == "unknown":
        final_state, exit_code = "verify_failed", EXIT_NOT_VERIFIED
        vres["combined_note"] = (f"capture={capture_result} verify={vres.get('result')}："
                                 f"有结论未知，不判 verified")
    else:
        final_state, exit_code = "verify_failed", EXIT_NOT_VERIFIED
        # 说明**到底哪一侧**没过 —— 早期版本的措辞一律怪 capture，
        # 在 capture 通过、verify 未通过时是误导（读的人会去查错地方）。
        if not capture_pass:
            vres["combined_note"] = (
                f"capture={capture_result}（{'; '.join((capture_stage.get('problems') or [])[:2])}）"
                f" verify={vres.get('result')}：capture 未通过时，"
                f"不得因为容器层可读就判 verified")
        else:
            vres["combined_note"] = (
                f"capture=pass verify={vres.get('result')}："
                f"录像本身完整，但停止后探测未通过"
                f"（容器层可读不等于内容合格）；不判 verified")
    try:
        cs.transition(job_dir, run_id, final_state,
                      note=f"capture={capture_result} verify={vres.get('result')}",
                      stages={"verify": vres})
    except cs.StateError as exc:
        log(f"写 verify 结论失败: {exc}")
        return EXIT_FINALIZE_FAILED

    log(f"完成：capture={capture_result} verify={vres.get('result')} -> {final_state}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
