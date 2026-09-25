#!/usr/bin/env python3
"""agent_capture — 录制一个**明确指定**的窗口或应用（画面与音频分开指定）。

    agent_capture.py capability                      本后端能做什么、验证到哪一层
    agent_capture.py targets [--json]                列出当前可录的应用与窗口
    agent_capture.py preflight  <选择器...>          启动前检查（不采集）
    agent_capture.py start      <选择器...> --out P  真正开始录（后台 worker）
    agent_capture.py status     --job-dir D --run-id R
    agent_capture.py stop       --job-dir D --run-id R
    agent_capture.py verify     --job-dir D --run-id R

## 三条不会妥协的规则

1. **没有默认目标。** 不给选择器 = 用法错误。绝不"那就录整屏吧"。
2. **给了目标但没命中 = 失败。** 绝不回退去录别的 app 或窗口。
3. **未知不报通过。** 拿不到证据的结论一律 `unknown`。

## 选择器

    画面：--video-app <bundle id 或名字>   或  --video-window <窗口id 或标题>
    音频：--audio-app <bundle id 或名字>   默认跟随画面 app；--audio-mode none 可关掉

`--video-window <数字>` 是**窗口 id**（精确）；非数字按标题匹配，此时**必须**
同时给 `--video-app` 把范围钉死（标题会变、也可能重名）。
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import capture_state as cs          # noqa: E402
import capture_targets as ct        # noqa: E402

TOOL_VERSION = "agent-capture 0.1.0"


def _load_backend(platform: str):
    """按平台加载后端模块。缺失就**明确报错**，不静默退化成"没有后端也能跑"。"""
    if platform == "macos":
        sys.path.insert(0, str(HERE.parent / "tools" / "macos"))
        return importlib.import_module("backend")
    if platform == "windows":
        p = HERE.parent / "tools" / "windows" / "backend.py"
        if not p.exists():
            raise RuntimeError(
                f"Windows 后端模块不存在：{p}\n"
                f"（该模块由独立会话交付；未集成前 Windows 一律按「未实现」处理）")
        spec = importlib.util.spec_from_file_location("win_backend", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    raise RuntimeError(f"平台 {platform!r} 没有可用后端")


def _target_from_args(a) -> ct.CaptureTarget:
    plat = a.platform or ct.detect_platform()
    return ct.resolve_target(
        plat, video_app=a.video_app, video_window=a.video_window,
        video_pid=a.video_pid, display=a.display, audio_app=a.audio_app,
        audio_pid=a.audio_pid, audio_mode=a.audio_mode, microphone=a.microphone)


def _add_selector_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--platform", default="")
    p.add_argument("--video-app", default="", help="画面目标 app（bundle id 或名字）")
    p.add_argument("--video-window", default="", help="画面目标窗口（id 或标题）")
    p.add_argument("--video-pid", type=int, default=0)
    p.add_argument("--display", action="store_true", help="整屏（本后端未实现，会被拒）")
    p.add_argument("--audio-app", default="", help="音频目标 app；默认跟随画面 app")
    p.add_argument("--audio-pid", type=int, default=0)
    p.add_argument("--audio-mode", default="", help="none|app|window|system")
    p.add_argument("--microphone", action="store_true", help="本后端未实现，会被拒")


def _out(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def cmd_capability(a) -> int:
    plat = a.platform or ct.detect_platform()
    cap = ct.capability(plat)
    info = {"platform": plat, "backend": cap.backend,
            "video_granularities": cap.video_granularities,
            "audio_granularities": cap.audio_granularities,
            "unimplemented": cap.unimplemented,
            "verified_level": cap.verified_level,
            "verified_scope": cap.verified_scope,
            "notes": cap.notes}
    try:
        b = _load_backend(plat)
        info["availability"] = b.availability()
        # 后端自报的验证层级优先（后端比语义层更清楚自己做过什么）
        if hasattr(b, "VERIFIED_LEVEL"):
            info["verified_level"] = b.VERIFIED_LEVEL
            info["verified_scope"] = getattr(b, "VERIFIED_SCOPE", "")
    except RuntimeError as exc:
        info["availability"] = {"available": False, "reason": str(exc)}
    _out(info)
    return 0


def cmd_targets(a) -> int:
    plat = a.platform or ct.detect_platform()
    try:
        b = _load_backend(plat)
    except RuntimeError as exc:
        _out({"ok": False, "error": str(exc)})
        return 2
    pj = b.probe()
    if not pj.get("ok"):
        _out({"ok": False, "error": pj.get("error") or pj.get("hint"),
              "hint": pj.get("hint"), "verified_level": pj.get("verified_level")})
        return 2
    if a.json:
        _out(pj)
        return 0
    print(f"后端 {pj.get('backend')}（验证层级 {pj.get('verified_level')}）")
    print(f"\n应用（{len(pj.get('applications') or [])}）：")
    for ap_ in (pj.get("applications") or [])[:40]:
        print(f"  {ap_.get('name','')[:34]:34} {ap_.get('bundle_id','')} pid={ap_.get('pid')}")
    print(f"\n在屏窗口（{len(pj.get('windows') or [])}）：")
    for w in (pj.get("windows") or [])[:40]:
        print(f"  #{str(w.get('windowID')):8} {(w.get('title') or '')[:34]:34} "
              f"owner={w.get('owner_name','')} pid={w.get('owner_pid')}")
    print("\n提示：单窗口画面用 --video-window <#id>；标题匹配必须同时给 --video-app。")
    return 0


def cmd_preflight(a) -> int:
    try:
        t = _target_from_args(a)
    except ct.UsageError as exc:
        _out({"ok": False, "problems": [str(exc)]})
        return 2
    try:
        b = _load_backend(t.platform)
    except RuntimeError as exc:
        _out({"ok": False, "problems": [str(exc)]})
        return 2

    pj = b.probe()
    facts = None
    if pj.get("ok"):
        try:
            facts = ct.LiveFacts.from_probe_json(pj)
        except ct.TargetError:
            facts = None

    problems = ct.validate_target(t, t.platform, facts)
    res = {"ok": not problems, "problems": problems, "target": t.to_json(),
           "live_checked": facts is not None,
           "verified_level": getattr(b, "VERIFIED_LEVEL", "none"),
           "verified_scope": getattr(b, "VERIFIED_SCOPE", "")}
    if problems:
        _out(res)
        return 2
    pf = b.preflight(t.to_json(), probe_json=pj)
    res["backend_preflight"] = pf
    res["ok"] = bool(pf.get("ok"))
    if not res["ok"]:
        res["problems"] = pf.get("problems") or ["后端预检未通过"]
    _out(res)
    return 0 if res["ok"] else 2


def cmd_start(a) -> int:
    try:
        t = _target_from_args(a)
    except ct.UsageError as exc:
        _out({"ok": False, "problems": [str(exc)]})
        return 2
    try:
        b = _load_backend(t.platform)
    except RuntimeError as exc:
        _out({"ok": False, "problems": [str(exc)]})
        return 2

    job_dir = Path(a.job_dir)
    run_id = a.run_id
    out = Path(a.out)

    # 预检必须先过：不预检直接开录 = 可能录到错的东西
    pj = b.probe()
    facts = None
    if pj.get("ok"):
        try:
            facts = ct.LiveFacts.from_probe_json(pj)
        except ct.TargetError:
            facts = None
    problems = ct.validate_target(t, t.platform, facts)
    if problems:
        _out({"ok": False, "problems": problems, "phase": "validate"})
        return 2
    pf = b.preflight(t.to_json(), probe_json=pj)
    if not pf.get("ok"):
        _out({"ok": False, "problems": pf.get("problems"), "phase": "preflight"})
        return 2

    if out.exists() and not a.overwrite:
        _out({"ok": False, "phase": "start",
              "problems": [f"输出已存在，拒绝覆盖：{out}（要覆盖请显式 --overwrite，"
                           f"或换新路径）"]})
        return 2

    # 建 run（no-clobber：同 run_id 再来一次会被拒）
    try:
        cs.init_run(job_dir, run_id, TOOL_VERSION, t.to_json())
    except cs.StateError as exc:
        _out({"ok": False, "phase": "init", "problems": [str(exc)]})
        return 2
    cs.transition(job_dir, run_id, "preflight", note="preflight passed")
    cs.transition(job_dir, run_id, "starting", note="spawn worker")

    worker = [sys.executable, str(HERE / "capture_worker.py"),
              "--job-dir", str(job_dir), "--run-id", run_id, "--out", str(out),
              "--duration", str(a.duration), "--expect", a.expect,
              "--fps", str(a.fps), "--max-width", str(a.max_width)]
    if a.no_video:
        worker.append("--no-video")

    wlog = open(job_dir / f"{run_id}.worker.stdout.log", "ab")
    try:
        w = subprocess.Popen(worker, stdout=wlog, stderr=wlog, start_new_session=True)
    except OSError as exc:
        cs.transition(job_dir, run_id, "failed", note="worker spawn failed",
                      error=str(exc))
        _out({"ok": False, "phase": "spawn", "problems": [str(exc)]})
        return 2

    _out({"ok": True, "run_id": run_id, "job_dir": str(job_dir),
          "worker_pid": w.pid, "media": str(out),
          "state": "starting",
          "next": f"agent_capture.py status --job-dir {job_dir} --run-id {run_id}",
          "note": "worker 会等「采集初始化 + 有效首帧」都到位才把状态推到 running；"
                  "超时会判 failed，而不是假装在录。"})
    return 0


def cmd_status(a) -> int:
    job_dir = Path(a.job_dir)
    try:
        st = cs.load_run(job_dir, a.run_id)
        summ = cs.summarize(job_dir, a.run_id)
    except cs.StateError as exc:
        _out({"ok": False, "error": str(exc)})
        return 2

    live = None
    lp = job_dir / f"{a.run_id}.live.json"
    if lp.exists():
        try:
            live = json.loads(lp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            live = {"error": "实时进度文件损坏"}

    # worker 存活判断：只用于**展示**，不用于任何决策
    wpid = (st.artifacts or {}).get("recorder_pid")
    recorder_alive = None
    if isinstance(wpid, int) and wpid > 0:
        try:
            os.kill(wpid, 0)
            recorder_alive = True
        except OSError:
            recorder_alive = False

    _out({"ok": True, "run": {"run_id": st.run_id, "status": st.status,
                              "revision": st.revision, "tool_version": st.tool_version},
          "summary": summ, "live": live, "recorder_alive": recorder_alive,
          "artifacts": st.artifacts, "stages": st.stages,
          "error": st.error})
    return 0


def cmd_stop(a) -> int:
    job_dir = Path(a.job_dir)
    try:
        st = cs.load_run(job_dir, a.run_id)
    except cs.StateError as exc:
        _out({"ok": False, "error": str(exc)})
        return 2
    if st.status in cs.TERMINAL:
        _out({"ok": True, "already_terminal": True, "status": st.status})
        return 0
    req = cs.request_stop(job_dir, a.run_id,
                          {"owner_id": a.owner_id or f"cli-{os.getpid()}",
                           "reason": a.reason or "requested by CLI"})
    _out({"ok": True, "stop_request": {k: req[k] for k in
                                       ("nonce", "owner_id", "requested_at", "reason")},
          "note": "这是一个**请求**：worker 会核对 nonce 后再让录制器有界收尾。"
                  "CLI 不会用旧 PID 直接杀进程。"})
    return 0


def cmd_verify(a) -> int:
    job_dir = Path(a.job_dir)
    try:
        st = cs.load_run(job_dir, a.run_id)
    except cs.StateError as exc:
        _out({"ok": False, "error": str(exc)})
        return 2
    media = a.media or (st.artifacts or {}).get("media")
    if not media:
        _out({"ok": False, "error": "没有可校验的媒体路径（state.artifacts.media 为空）"})
        return 2
    try:
        b = _load_backend((st.target or {}).get("platform") or ct.detect_platform())
    except RuntimeError as exc:
        _out({"ok": False, "error": str(exc)})
        return 2
    res = b.verify(media, expect=a.expect)
    res["media"] = media
    _out(res)
    return 0 if res.get("result") == "pass" else 2


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="agent_capture",
        description="录制一个明确指定的窗口或应用（没有默认目标）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("capability"); p.add_argument("--platform", default="")
    p.set_defaults(fn=cmd_capability)

    p = sub.add_parser("targets")
    p.add_argument("--platform", default="")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_targets)

    p = sub.add_parser("preflight"); _add_selector_args(p)
    p.set_defaults(fn=cmd_preflight)

    p = sub.add_parser("start"); _add_selector_args(p)
    p.add_argument("--job-dir", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--duration", type=float, default=0.0)
    p.add_argument("--expect", default="av", choices=["av", "audio", "auto"])
    p.add_argument("--fps", type=int, default=30)
    p.add_argument("--max-width", type=int, default=1920)
    p.add_argument("--no-video", action="store_true")
    p.add_argument("--overwrite", action="store_true")
    p.set_defaults(fn=cmd_start)

    p = sub.add_parser("status")
    p.add_argument("--job-dir", required=True); p.add_argument("--run-id", required=True)
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("stop")
    p.add_argument("--job-dir", required=True); p.add_argument("--run-id", required=True)
    p.add_argument("--owner-id", default=""); p.add_argument("--reason", default="")
    p.set_defaults(fn=cmd_stop)

    p = sub.add_parser("verify")
    p.add_argument("--job-dir", required=True); p.add_argument("--run-id", required=True)
    p.add_argument("--media", default="")
    p.add_argument("--expect", default="av", choices=["av", "audio", "auto"])
    p.set_defaults(fn=cmd_verify)

    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except (cs.StateError, ct.TargetError, ct.UsageError) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
