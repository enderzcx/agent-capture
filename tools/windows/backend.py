#!/usr/bin/env python3
"""Windows 采集后端桥接：把 agent_capture_win 的 run() 接到本 CLI 的接口上。

## 为什么需要一层桥

`agent_capture_win` 是**命令式**的：`run(command, config) -> dict`，它自己管 OBS 的
录制生命周期（start/status/stop 都由它驱动，且 `stop` 必须先通过身份核验）。
而 macOS 后端是"启动一个录制器子进程 + worker 盯着它"的形态。
两者形状不同，硬套会让 Windows 丢掉它最重要的安全属性（owner-only stop、
启动前原子预约 run、实例锁）。

所以这层桥的取舍是**明确**的：

- **读/校验面**（capability / targets / preflight）→ 完整桥接，CLI 可以照常用。
- **生命周期面**（start / status / stop）→ **不假装**成"起个子进程"，
  而是让调用方走 `run()` 的真实语义（见 `run_command()`）。
  CLI 在 Windows 上遇到 start 会明确告诉调用方用这条路，而不是静默走一条错的。

## 关于"未知不报通过"（来自 WINDOWS-READY.md §8b）

obs-websocket **不传帧、也没有音频采样**，所以这两个里程碑在 Windows 上
**拿不到**，必须报 `unknown`：

| 里程碑 | Windows 能否给 |
|---|---|
| `capture_initialized` | 能：`StartRecord` 被接受且字节在涨 |
| `first_video_frame` | **不能** —— 协议层不可观测 |
| `audio_path_has_data` | 只能到"已配置"级别 |
| `audio_signal_observed` | **不能** —— 协议无音频采样 |

`outputBytes` 在涨**绝不**等于"录到了画面"：它只证明输出在写。
本桥原样透传 `picture_verified` / `artifact_verified` / `evidence_scope`，
**不把它们升级成 pass**。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent

NAME = "windows-obs-websocket"
PLATFORM = "windows"

# 在 Windows 实机验证之前**恒为** none。这与模块自己的 verification_level 是两件事：
# 那个描述"这次连接到了哪一层"，这个描述"这个后端有没有被实机验证过"。
VERIFIED_LEVEL = "none"
VERIFIED_SCOPE = (
    "**未在 Windows 实机验证**（本机 macOS；tailnet 内那台 Windows 主机无可用凭据）。"
    "模块自身的协议层已用真实 WebSocket + 模拟 OBS 跑通 257 例，"
    "但 mock 只证明协议与策略，**不证明能录到画面或声音**。"
)

REQUIRES = [
    "Windows 10/11",
    "OBS Studio 28+（含 obs-websocket 5.x）",
    "一个**专用** profile / scene collection / scene（里面不得有桌面音或麦克风）",
    "目标窗口先打开，且其 title/class/exe 三元组已知",
]


def _import_module():
    """导入 agent_capture_win。缺失时**明确报错**，不静默退化。"""
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    try:
        from agent_capture_win import run as _run  # type: ignore
        return _run
    except ImportError as exc:
        raise RuntimeError(
            f"找不到 agent_capture_win 模块（{HERE}/agent_capture_win）：{exc}\n"
            f"该模块运行期依赖 websocket-client；未安装时本后端不可用。"
        ) from exc


def availability() -> Dict[str, Any]:
    """本后端在这台机器上能不能用。**不抛异常**。"""
    out: Dict[str, Any] = {
        "available": False, "reason": "", "backend": NAME, "platform": PLATFORM,
        "verified_level": VERIFIED_LEVEL, "verified_scope": VERIFIED_SCOPE,
        "requires": REQUIRES,
    }
    # 平台判定。`AGENT_CAPTURE_TEST_PLATFORM` 是**仅供假 transport 合同测试**的开关：
    # 它只影响"可用性判定走哪条分支"，**不会**让 Windows 采集真的在本机可用 ——
    # 真正的能力仍然由 OBS 是否存在、以及模块自己的 transport 决定。
    effective = os.environ.get("AGENT_CAPTURE_TEST_PLATFORM") or sys.platform
    if effective != "win32":
        out["reason"] = (f"本后端只在 Windows 上可用（当前 sys.platform={sys.platform!r}）；"
                         f"协议层可离线测试，但采集需要 Windows + OBS")
        return out
    try:
        _import_module()
    except RuntimeError as exc:
        out["reason"] = str(exc)
        return out
    out["available"] = True
    out["reason"] = "ok"
    return out


def run_command(command: str, config: Dict[str, Any]) -> Dict[str, Any]:
    """直接调 `agent_capture_win.run`。生命周期面走这里。"""
    run = _import_module()
    res = run(command, config)
    if isinstance(res, dict):
        # 原样透传，**不**把 verification 升级成 pass
        res.setdefault("verified_level", VERIFIED_LEVEL)
        res.setdefault("verified_scope", VERIFIED_SCOPE)
    return res


def probe(timeout: float = 30.0) -> Dict[str, Any]:
    """列出可选目标（= 模块的 `targets`）。

    转成 `capture_targets.LiveFacts` 能吃的形状：Windows 上没有 bundle id，
    只有 title/class/exe —— 所以 `owner_bundle_id` 留空，`title` 用窗口标题。
    """
    av = availability()
    if not av["available"]:
        return {"ok": False, "error": av["reason"], **{k: av[k] for k in
                ("backend", "platform", "verified_level", "verified_scope")}}
    cfg = _config_from_env()
    try:
        res = run_command("targets", cfg)
    except Exception as exc:
        return {"ok": False, "error": f"targets 失败: {exc}", **av}
    wins: List[Dict[str, Any]] = []
    for w in (res.get("targets") or res.get("windows") or []):
        if not isinstance(w, dict):
            continue
        wins.append({
            "windowID": w.get("fingerprint") or w.get("window_id") or 0,
            "title": w.get("title") or "",
            "owner_pid": 0,          # Windows 后端不承诺 HWND/PID pinning
            "on_screen": True,
            "owner_bundle_id": "",
            "exe": w.get("exe") or "",
            "class": w.get("class") or "",
        })
    return {"ok": True, "backend": NAME, "platform": PLATFORM,
            "verified_level": res.get("verification_level", VERIFIED_LEVEL),
            "verified_scope": VERIFIED_SCOPE,
            "applications": [],
            "windows": wins,
            "raw": res,
            # 明确告知能力边界，避免上层误以为能做 app 级画面
            "hwnd_pinning": False,
            "unsupported_match_dimensions": ["hwnd", "pid", "process_creation_time", "z_order"]}


def _config_from_env() -> Dict[str, Any]:
    """从环境读配置。**不读全机配置、不回显密码。**

    密码只从指定的 env 名取（`AGENT_CAPTURE_OBS_PASSWORD`），本函数不打印其值。
    """
    import os
    cfg: Dict[str, Any] = {
        "host": os.environ.get("AGENT_CAPTURE_OBS_HOST", "127.0.0.1"),
        "port": int(os.environ.get("AGENT_CAPTURE_OBS_PORT", "4455")),
        "password_env": "AGENT_CAPTURE_OBS_PASSWORD",
        "profile": os.environ.get("AGENT_CAPTURE_OBS_PROFILE", "agent-capture"),
        "scene_collection": os.environ.get("AGENT_CAPTURE_OBS_SCENE_COLLECTION", "agent-capture"),
        "scene": os.environ.get("AGENT_CAPTURE_OBS_SCENE", "agent-capture-window"),
        "source_name": os.environ.get("AGENT_CAPTURE_OBS_SOURCE", "agent-capture-window-capture"),
        "record_dir": os.environ.get("AGENT_CAPTURE_OBS_RECORD_DIR", ""),
        "run_id": os.environ.get("AGENT_CAPTURE_RUN_ID", "take-001"),
        "capture_audio": os.environ.get("AGENT_CAPTURE_CAPTURE_AUDIO", "1") not in ("0", "false", "no"),
    }
    tgt: Dict[str, Any] = {}
    if os.environ.get("AGENT_CAPTURE_TARGET_TITLE"):
        tgt["title"] = os.environ["AGENT_CAPTURE_TARGET_TITLE"]
    if os.environ.get("AGENT_CAPTURE_TARGET_CLASS"):
        tgt["class"] = os.environ["AGENT_CAPTURE_TARGET_CLASS"]
    if os.environ.get("AGENT_CAPTURE_TARGET_EXE"):
        tgt["exe"] = os.environ["AGENT_CAPTURE_TARGET_EXE"]
    if tgt:
        tgt["priority"] = os.environ.get("AGENT_CAPTURE_TARGET_PRIORITY", "exe")
        cfg["target"] = tgt
    return cfg


def preflight(target: Dict[str, Any], *, probe_json: Optional[Dict[str, Any]] = None,
              timeout: float = 30.0) -> Dict[str, Any]:
    """只读预检（= 模块的 `preflight`，保证 `read_only=true`，不动 OBS 会话）。"""
    av = availability()
    if not av["available"]:
        # 即使不可用，也要把**匹配语义**带出来：下游若因为缺字段而把
        # "没有 target_match" 读成"匹配没问题"，就会在最不该放行时放行。
        return {"ok": False, "problems": [av["reason"]],
                "target_match": "unavailable",
                "hwnd_pinning": False,
                "match_dimensions": ["title", "class", "exe"],
                "match_note": ("后端不可用，未做任何目标确认。"
                               "OBS 官方 window capture 按 title/class/exe 匹配，"
                               "同名/同类/同 exe 的窗口可能被重绑；本后端不承诺精确 HWND。"),
                "verified_level": VERIFIED_LEVEL, "verified_scope": VERIFIED_SCOPE}
    cfg = _config_from_env()
    cfg["target"] = _target_from_capture_target(target)
    try:
        res = run_command("preflight", cfg)
    except Exception as exc:
        return {"ok": False, "problems": [f"preflight 失败: {exc}"],
                "verified_level": VERIFIED_LEVEL, "verified_scope": VERIFIED_SCOPE}
    problems = []
    err = res.get("error")
    cat = err.get("category") if isinstance(err, dict) else err
    if not res.get("ok", False):
        problems.append(f"后端预检未通过（{cat}）")

    # —— 目标匹配语义必须**如实**传下去 ——
    # OBS 的 Window Capture 官方会按 title/class/exe 匹配，**可能重绑到同类窗口**。
    # 模块的 check_target_present 只在"精确串命中"时放行，拿不到窗口列表就
    # TARGET_UNCONFIRMED 拒绝（unknown 不当 found）。桥接层不许把这个信息抹掉，
    # 更不许把"按 title 命中的对象"说成"精确的窗口 id"。
    if res.get("ok"):
        target_match = "exact"          # 模块只在精确命中时 ok
    elif cat == "TARGET_UNCONFIRMED":
        target_match = "unconfirmed"    # 枚举不可用 -> 未确认
    elif cat == "TARGET_NOT_FOUND":
        target_match = "not_found"
    else:
        target_match = "unconfirmed"

    return {"ok": res.get("ok", False), "problems": problems, "raw": res,
            "read_only": res.get("read_only", True),
            "target_match": target_match,
            # 这三条是给下游防误读用的：**没有** HWND pin，匹配可能被 OBS 重绑
            "hwnd_pinning": False,
            "match_dimensions": ["title", "class", "exe"],
            "match_note": ("OBS 官方 window capture 按 title/class/exe 匹配，"
                           "同名/同类/同 exe 的窗口**可能**被重绑；"
                           "本后端不承诺精确 HWND。target_match=exact 只表示"
                           "'请求的窗口串当前在 OBS 列表里'，不表示绑定了某个 HWND。"),
            "verified_level": res.get("verification_level", VERIFIED_LEVEL),
            "verified_scope": VERIFIED_SCOPE}


def _target_from_capture_target(target: Dict[str, Any]) -> Dict[str, Any]:
    """把本 CLI 的 CaptureTarget 翻成 Windows 后端的 target。

    只做**单窗口**：Windows 后端没有 app 级画面采集（不靠改名声称支持）。
    """
    v = (target or {}).get("video") or {}
    out: Dict[str, Any] = {}
    if v.get("window_title"):
        out["title"] = v["window_title"]
    if v.get("app_name"):
        # 非 macOS 平台下 _apply_app 把 `.exe` 放进 app_name
        out["exe"] = v["app_name"]
    out["priority"] = "exe" if out.get("exe") else ("title" if out.get("title") else "class")
    return out


def build_start_argv(*_a, **_kw) -> List[str]:
    """**故意不实现**：Windows 的生命周期不走"起一个录制器子进程"这条路。

    它由 agent_capture_win 自己驱动 OBS（启动前原子预约 run、实例锁、
    owner-only stop）。把它伪装成"起个子进程"会丢掉这些安全属性。
    调用方应该用 `run_command("start", cfg)`。
    """
    raise NotImplementedError(
        "Windows 后端的生命周期不由子进程驱动。请用 run_command('start', config)，"
        "它会在动 OBS 之前原子预约 run 并校验专用会话；"
        "stop 必须带 start 返回的 ownership.session_token。"
    )


def verify(media_path: str, expect: str = "av") -> Dict[str, Any]:
    """Windows 的产物核对走模块自己的 `output` 块（存在/非空/在专用目录/非母带）。

    **不**声称画面或声音内容正确 —— 协议层不可观测。
    """
    return {
        "result": "unknown",
        "media": media_path,
        "expect": expect,
        "verified_level": VERIFIED_LEVEL,
        "verified_scope": VERIFIED_SCOPE,
        "notes": ("Windows 后端的产物核对在 stop 时由模块完成（output 块）。"
                  "本函数不声称画面/声音内容正确：obs-websocket 不传帧、无音频采样，"
                  "协议层无法证明录到的是什么。"),
        "picture_verified": False,
        "audio_signal_observed": None,   # None = 不可观测，不是"没有"
    }


if __name__ == "__main__":
    print(json.dumps(availability(), ensure_ascii=False, indent=2))
