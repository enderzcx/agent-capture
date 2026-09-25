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
import importlib.util
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


class BackendUnavailable(RuntimeError):
    """后端**当前不可用**（例如没装 OBS / 不在 Windows 上）。

    这与"未实现"是两件事：不可用是**正常的运行时状态**，调用方应当得到
    一条明确的 unavailable 说明并继续别的路径，而不是撞上一个 NotImplemented 堆栈。
    """


def _load_backend(platform: str):
    """按平台加载后端模块。缺失就**明确报错**，不静默退化成"没有后端也能跑"。

    `AGENT_CAPTURE_BACKEND_DIR` 是**显式**的后端覆盖（测试、或后端装在非默认位置）。
    默认只用仓库内实现。
    """
    if platform == "macos":
        # 顺序很重要：后插入的排在 sys.path 更前面。
        # 先放仓库内实现，再放覆盖目录 —— 这样覆盖才真的**覆盖**得了。
        # （反过来写会让覆盖静默失效，测试里表现为"用了真后端"，很难查。）
        sys.path.insert(0, str(HERE.parent / "tools" / "macos"))
        override = os.environ.get("AGENT_CAPTURE_BACKEND_DIR")
        if override:
            sys.path.insert(0, override)
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



def _windows_lifecycle(command: str, a) -> int:
    """Windows 的生命周期命令**直接分派**给模块的 run_command。

    为什么不复用 generic worker：那是"起一个录制器子进程 + 盯着它"的形态，
    而 Windows 后端的价值恰恰在它自己那套 —— 专用 profile/scene 隔离、
    启动前原子预约 run、实例锁、owner-only stop（stop 必须带 session_token）。
    硬塞进子进程模型会把它们全丢掉。

    后端不可用（没装 OBS / 不在 Windows）是**正常状态**：返回明确的
    unavailable 说明，退出码 2，而不是抛 NotImplementedError。
    """
    try:
        b = _load_backend("windows")
    except RuntimeError as exc:
        _out({"ok": False, "backend": "windows-obs-websocket",
              "available": False, "phase": command, "problems": [str(exc)]})
        return 2
    av = b.availability()
    if not av.get("available"):
        _out({"ok": False, "backend": av.get("backend"), "available": False,
              "phase": command, "verified_level": av.get("verified_level"),
              "verified_scope": av.get("verified_scope"),
              "problems": [av.get("reason")],
              "note": "后端不可用是正常状态（例如没装/没开 OBS 或不在 Windows）"})
        return 2

    # —— 按**真实模块**的 config 契约映射；不支持的一律 usage 拒绝 ——
    # 模块的 schema 是显式白名单，未知键直接 CONFIG_INVALID。
    # 早期薄分派把 --out/--duration/--job-dir 之类**默默丢掉**，于是"用户以为录
    # 10 秒到 A，实际 OBS 一直录到 B"。宁可明确拒绝，也不做假映射。
    import math as _m
    problems = []
    cfg: Dict[str, Any] = {}

    for attr, key in (("obs_host", "host"), ("obs_port", "port"),
                      ("obs_password_env", "password_env"), ("obs_profile", "profile"),
                      ("obs_scene_collection", "scene_collection"), ("obs_scene", "scene"),
                      ("obs_source_name", "source_name"), ("obs_record_dir", "record_dir")):
        v = getattr(a, attr, None)
        if v not in (None, "", 0):
            cfg[key] = v

    # 时长：模块的键是 max_record_seconds；必须**有限且为正**
    if command == "start":
        d = getattr(a, "duration", 0.0) or 0.0
        if not _m.isfinite(d) or d < 0:
            problems.append(f"--duration 必须是有限非负数，收到 {d!r}")
        elif d > 0:
            cfg["max_record_seconds"] = float(d)

    # record_dir 是**唯一**决定产物落点的地方；--out 在 Windows 上没有对应语义
    if getattr(a, "out", ""):
        problems.append(
            "--out 在 Windows 后端上没有对应语义：产物路径由 OBS 的 record_dir 决定。"
            "请改用 --obs-record-dir <目录>，产物名由 OBS 生成。"
            "（不静默忽略 --out，否则你会以为录到了指定文件。）")

    # 这些在本后端没有实现：明确拒绝，而不是收下不管
    if getattr(a, "no_video", False):
        problems.append("--no-video 在 Windows 后端未实现（只做 window capture）。")
    if getattr(a, "no_audio", False):
        problems.append("--no-audio 请改用 --audio-mode none（映射到 capture_audio=false）。")

    if command in ("start", "preflight"):
        try:
            t = _target_from_args(a)
        except Exception as exc:
            problems.append(f"目标无法解析：{exc}")
            t = None
        if t is not None:
            tprobs = ct.validate_target(t, "windows")
            problems.extend(tprobs)
            cfg["target"] = b._target_from_capture_target(t.to_json())
            # capture_audio 按**真实模块**的格式：布尔
            cfg["capture_audio"] = (t.audio.granularity != "none")
        if command == "start":
            rid = getattr(a, "run_id", "") or ""
            if rid:
                cfg["run_id"] = rid

    if problems:
        _out({"ok": False, "backend": "windows-obs-websocket", "phase": command,
              "usage_errors": problems,
              "note": "Windows 后端的参数按真实模块的 config 契约校验；"
                      "不支持的参数明确拒绝，不做假映射。"})
        return 2

    # owner-only stop：把 start 拿到的 token 原样带回
    tok = getattr(a, "session_token", "")
    if tok:
        cfg["session_token"] = tok

    try:
        res = b.run_command(command, cfg)
    except Exception as exc:  # 模块自己会分类错误；这里兜底成结构化输出
        _out({"ok": False, "phase": command, "backend": "windows-obs-websocket",
              "problems": [f"{type(exc).__name__}: {exc}"]})
        return 2
    res["phase"] = command
    res.setdefault("verified_level", av.get("verified_level"))
    res.setdefault("verified_scope", av.get("verified_scope"))
    _out(res)
    return 0 if res.get("ok", True) and not res.get("error") else 2


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
    # —— Windows（experimental，无实机）——
    p.add_argument("--windows-obs", action="store_true",
                   help="走 Windows OBS WebSocket 后端（experimental；无实机验证）")
    p.add_argument("--obs-host", default="")
    p.add_argument("--obs-port", type=int, default=0)
    p.add_argument("--obs-password-env", default="",
                   help="密码所在的环境变量**名**（不接收明文密码）")
    p.add_argument("--obs-profile", default="")
    p.add_argument("--obs-scene-collection", default="")
    p.add_argument("--obs-scene", default="")
    p.add_argument("--obs-source-name", default="")
    p.add_argument("--obs-record-dir", default="")
    p.add_argument("--session-token", default="",
                   help="start 返回的 owner token；stop 必须带它（owner-only stop）")
    p.add_argument("--state-dir", default="")


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
    # Windows：生命周期/预检都走模块自己的 API（保留 owner 锁与 profile 隔离）
    if t.platform == "windows" and getattr(a, "windows_obs", False):
        return _windows_lifecycle("preflight", a)

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
    # Windows：**不**落回 generic worker（那会丢掉 owner-only stop 与实例锁）
    if t.platform == "windows" and getattr(a, "windows_obs", False):
        return _windows_lifecycle("start", a)

    try:
        b = _load_backend(t.platform)
    except RuntimeError as exc:
        _out({"ok": False, "problems": [str(exc)]})
        return 2

    # Windows 走模块分派，不需要 job-dir/out（由 OBS record_dir 决定落点）
    if getattr(a, "windows_obs", False):
        pass
    else:
        missing = [n for n, v in (("--job-dir", a.job_dir), ("--run-id", a.run_id),
                                  ("--out", a.out)) if not v]
        if missing:
            _out({"ok": False, "phase": "usage",
                  "problems": [f"缺少必需参数：{', '.join(missing)}"]})
            return 2

    job_dir = Path(a.job_dir) if a.job_dir else Path(".")
    run_id = a.run_id
    out = Path(a.out) if a.out else Path(".")

    # `--duration` 必须是**有限非负数**。NaN/Inf 传下去会让录制器的定时器永远不触发
    # （`queue.asyncAfter(deadline: .now() + nan)` 不会按时到），负数更没有意义。
    # 这类值**确定性拒绝**，不放行到原生层。
    import math as _m
    if isinstance(a.duration, bool) or not _m.isfinite(a.duration) or a.duration < 0:
        _out({"ok": False, "phase": "usage",
              "problems": [f"--duration 必须是有限非负数，收到 {a.duration!r}"]})
        return 2

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

    # **窗口标题 pin**：把预检这一刻看到的标题钉进 target，起录时核对。
    # 窗口 ID 稳定，但浏览器切个标签内容就全换了 —— 实测踩过一次误采。
    if t.video.granularity == "window" and t.video.window_id and facts is not None:
        wf = facts.window_by_id(t.video.window_id)
        if wf is not None and wf.title:
            t.video.expect_window_title = wf.title

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
    # `--overwrite` 必须真的传下去：CLI 层放行了、worker/录制器却仍然拒绝覆盖，
    # 就会出现"用户显式同意覆盖，却拿到一个莫名其妙的拒绝"。
    if a.overwrite:
        worker.append("--overwrite")

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
    if getattr(a, "windows_obs", False):
        return _windows_lifecycle("status", a)
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
    if getattr(a, "windows_obs", False):
        return _windows_lifecycle("stop", a)
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



def _artifact_ref(path: Path) -> dict:
    """把一个产物文件变成 production_run 认的 artifact 引用（真实哈希）。"""
    import hashlib
    p = path.resolve()
    if not p.is_file():
        raise ValueError(f"产物不是普通文件: {p}")
    h = hashlib.sha256()
    with p.open("rb") as fh:
        a = os.fstat(fh.fileno())
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
        b = os.fstat(fh.fileno())
    if (a.st_size, a.st_mtime_ns, a.st_ino) != (b.st_size, b.st_mtime_ns, b.st_ino):
        raise ValueError(f"产物在计算哈希时被改动: {p}")
    if a.st_size == 0:
        raise ValueError(f"产物为空文件: {p}")
    return {"path": str(p), "sha256": h.hexdigest(), "bytes": a.st_size}


def _capture_report(job_dir: Path, run_id: str, production_run_id: str,
                    expect: str = "av") -> dict:
    """把本工具**真实的** run 状态薄转换到 gameplay-production 的规范化 report。

    原则：**每一项都来自实测**，不手填 true 去绕验收。
    拿不到证据的项一律保守（False / 缺失），让对方按"未验证"处理。

    形状对应 production_run._fresh_live_capture / capture-finish：
      producer, run_id(=**production** 的 run_id), capture_id, status,
      first_video_frame, audio_scope, observed_at, media[], checks{}, unexpected_stop
    """
    st = cs.load_run(job_dir, run_id)
    cap = (st.stages or {}).get("capture") or {}
    ver = (st.stages or {}).get("verify") or {}
    arts = st.artifacts or {}
    live_path = Path(arts.get("live_status") or (job_dir / f"{run_id}.live.json"))
    live, _lerr = (None, None)
    try:
        live = json.loads(live_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        live = None

    # capture_id：用录制器写进 live 文件的 run_token —— 它按次生成、真实唯一。
    # 没有令牌就**没有** capture_id（宁可让上游拒绝，也不编一个）。
    capture_id = ""
    if isinstance(live, dict) and isinstance(live.get("run_token"), str):
        capture_id = f"{run_id}:{live['run_token']}"

    # status：本工具的状态机 -> 上游的三个值
    if st.status in ("verified", "verify_failed", "stopped", "done"):
        status = "stopped"
    elif st.status == "failed":
        status = "failed"
    else:
        status = "recording"

    # 音频范围：来自 target，不是猜的
    audio_gran = str(((st.target or {}).get("audio") or {}).get("granularity") or "none")
    audio_scope = {"none": "none", "app": "app", "system": "app"}.get(audio_gran, "none")

    media = []
    mp = Path(arts.get("media") or "")
    if st.status in ("verified", "verify_failed", "done", "stopped") and mp.is_file():
        try:
            media.append(_artifact_ref(mp))
        except ValueError:
            media = []

    verdict = {}  # 真实 metrics 里的实测
    metrics_path = Path(arts.get("metrics") or "")
    if metrics_path.is_file():
        try:
            mj = json.loads(metrics_path.read_text(encoding="utf-8"))
            verdict = (mj.get("verdict") or {}) if isinstance(mj, dict) else {}
        except (OSError, ValueError):
            verdict = {}

    # —— 完整性判定：**任何 capture/verify 的 fail 或 unknown 都要影响它** ——
    # 早期版本只看 video_continuity 与 stop，于是"verify 没通过"或"音频检查失败"
    # 照样能被判成完整。凡是没拿到的结论一律算作未通过（unknown 不是 pass）。
    verify_result = ((st.stages or {}).get("verify") or {}).get("result")
    capture_result = cap.get("result")
    audio_required = audio_scope != "none"   # audio_scope=none 明确不要求声音
    audio_path_ok = bool(cap.get("audio_path_has_data")) or bool(verdict.get("audio_track_present"))

    blockers = []
    if capture_result != "pass":
        # 把**具体原因**带出来，而不只是一句"capture 不是 pass"：
        # 读 blockers 的人要能立刻看出是 writer 失败、目标消失还是别的。
        cap_problems = [str(x) for x in (cap.get("problems") or [])]
        if cap_problems:
            blockers.append(f"capture 结论不是 pass（{capture_result!r}）；具体：" +
                            "；".join(cap_problems[:3]))
        else:
            blockers.append(f"capture 结论不是 pass（{capture_result!r}）")
    if verify_result != "pass":
        blockers.append(f"verify 结论不是 pass（{verify_result!r}）")
    if audio_required and not audio_path_ok:
        blockers.append("需要音频但音频通路没有数据")

    container_readable = bool(verdict.get("tracks_verified")) and bool(media)
    # verify 未过时不算"帧连续"—— 连续性是从 verify 与 metrics 一起得出的结论
    video_continuity = (verify_result == "pass") and bool(verdict.get("video_frames_arriving"))

    first_video_frame = False
    if isinstance(live, dict):
        first_video_frame = bool(live.get("first_video_frame")) or bool(verdict.get("video_frames_present"))
    else:
        first_video_frame = bool(verdict.get("video_frames_present"))

    obs = {
        "schema": "gameplay-production/capture-report-v1",
        "producer": "agent-capture",
        "run_id": production_run_id,
        "capture_run_id": run_id,
        "capture_id": capture_id,
        "status": status,
        "first_video_frame": first_video_frame,
        "audio_scope": audio_scope,
        "audio_signal_observed": bool(cap.get("audio_signal_observed")),
        "observed_at": time.time(),
        "media": media,
        "checks": {
            "container_readable": container_readable,
            "video_continuity": video_continuity,
        },
        # 提前停止 / 目标消失 / 非零退出 / 任何没通过的结论 —— 任一为真都算 unexpected。
        # 上游 `capture_result=complete` 正是靠这个与 video_continuity 判定的。
        "unexpected_stop": bool(blockers)
        or bool(cap.get("problems")) or (cap.get("recorder_exit") not in (0, None)),
        "completeness_blockers": blockers,
        "tool_status": st.status,
        "verified_level": cap.get("verified_level"),
        "not_proof_of": ["音画同步", "画面内容质量", "这段声音确实是目标应用发出的"],
    }
    return obs


def cmd_report(a) -> int:
    """输出规范化 capture report（给 gameplay-production 的 production_run 用）。"""
    job_dir = Path(a.job_dir)
    try:
        obs = _capture_report(job_dir, a.run_id, a.production_run_id, a.expect)
    except (cs.StateError, ValueError) as exc:
        _out({"ok": False, "error": str(exc)})
        return 2
    text = json.dumps(obs, ensure_ascii=False, indent=2, allow_nan=False)
    if a.out:
        Path(a.out).write_text(text + "\n", encoding="utf-8")
        _out({"ok": True, "report": str(Path(a.out).resolve()),
              "capture_id": obs["capture_id"], "status": obs["status"],
              "first_video_frame": obs["first_video_frame"],
              "container_readable": obs["checks"]["container_readable"],
              "video_continuity": obs["checks"]["video_continuity"]})
    else:
        print(text)
    return 0

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
    # job-dir/out 在 Windows 后端上没有语义（产物落点由 OBS record_dir 决定），
    # 所以不在这里硬性 required；分平台校验放在 cmd_start 里做。
    p.add_argument("--job-dir", default="")
    p.add_argument("--run-id", default="")
    p.add_argument("--out", default="")
    p.add_argument("--duration", type=float, default=0.0)
    p.add_argument("--expect", default="av", choices=["av", "audio", "auto"])
    p.add_argument("--fps", type=int, default=30)
    p.add_argument("--max-width", type=int, default=1920)
    p.add_argument("--no-video", action="store_true")
    p.add_argument("--overwrite", action="store_true")
    p.set_defaults(fn=cmd_start)

    p = sub.add_parser("status")
    p.add_argument("--job-dir", default=""); p.add_argument("--run-id", default="")
    p.add_argument("--windows-obs", action="store_true")
    p.add_argument("--obs-host", default=""); p.add_argument("--obs-port", type=int, default=0)
    p.add_argument("--obs-password-env", default=""); p.add_argument("--obs-profile", default="")
    p.add_argument("--obs-scene-collection", default=""); p.add_argument("--obs-scene", default="")
    p.add_argument("--obs-source-name", default=""); p.add_argument("--obs-record-dir", default="")
    p.add_argument("--session-token", default=""); p.add_argument("--state-dir", default="")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("stop")
    p.add_argument("--job-dir", default=""); p.add_argument("--run-id", default="")
    p.add_argument("--windows-obs", action="store_true")
    p.add_argument("--obs-host", default=""); p.add_argument("--obs-port", type=int, default=0)
    p.add_argument("--obs-password-env", default=""); p.add_argument("--obs-profile", default="")
    p.add_argument("--obs-scene-collection", default=""); p.add_argument("--obs-scene", default="")
    p.add_argument("--obs-source-name", default=""); p.add_argument("--obs-record-dir", default="")
    p.add_argument("--session-token", default=""); p.add_argument("--state-dir", default="")
    p.add_argument("--owner-id", default=""); p.add_argument("--reason", default="")
    p.set_defaults(fn=cmd_stop)

    p = sub.add_parser("report")
    p.add_argument("--job-dir", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--production-run-id", required=True,
                   help="gameplay-production 那次 run 的 run_id（report 必须与它一致）")
    p.add_argument("--expect", default="av", choices=["av", "audio", "auto"])
    p.add_argument("--out", default="")
    p.set_defaults(fn=cmd_report)

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
