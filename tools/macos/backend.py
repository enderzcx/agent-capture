#!/usr/bin/env python3
"""macOS 采集后端：驱动 tools/macos 的 ScreenCaptureKit 录制器。

这一层只做三件事：
  1. 把 `CaptureTarget` 翻译成录制器的命令行参数（画面与音频**分开**表达）；
  2. 把录制器的 JSON 输出翻译回状态/结论；
  3. **如实**标注哪些能力真的实现了（见 `verified_level`）。

刻意不做的事：
  - 不猜目标：没有选择器就直接失败（录制器本身也会拒绝）。
  - 不把"编译通过"说成"验证过"：`VERIFIED_LEVEL` 只有在**真实录过一次**
    并留下证据后才改成 `recorded`。
  - 不静默降级：跨 app 音源、窗口级音频、整屏、麦克风都会明确失败。

录制器二进制：`tools/macos/build/GameAVRec.app/Contents/MacOS/GameAVRec`
（源码与二进制同仓；二进制在 .gitignore 里，由 `build.sh` 生成。）
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
BIN = HERE / "build" / "GameAVRec.app" / "Contents" / "MacOS" / "GameAVRec"
BUILD_SH = HERE / "build.sh"

NAME = "macos-screencapturekit"
PLATFORM = "macos"

# 真实录制证据出来之前，这里**必须**是 compile_only。
# 改成 recorded 的前提：evidence/ 里有真实 mp4 + metrics + verify 通过记录。
VERIFIED_LEVEL = "compile_only"
VERIFIED_SCOPE = ("本机 macOS 编译通过 + 离线用例；真实采集证据见 evidence/。"
                  "编译通过不等于验证过录制。")

REQUIRES = ["macOS 15+", "屏幕与系统音频录制权限", "已构建的 GameAVRec（tools/macos/build.sh）"]


def availability() -> Dict[str, Any]:
    """本后端在这台机器上能不能跑。**不抛异常**，把原因说清楚。"""
    out: Dict[str, Any] = {
        "available": False,
        "reason": "",
        "backend": NAME,
        "platform": PLATFORM,
        "verified_level": VERIFIED_LEVEL,
        "verified_scope": VERIFIED_SCOPE,
        "binary": str(BIN),
    }
    if sys.platform != "darwin":
        out["reason"] = f"本后端只在 macOS 上可用（当前 sys.platform={sys.platform!r}）"
        return out
    if not BIN.exists():
        out["reason"] = (f"录制器没构建：先跑 {BUILD_SH}。"
                         f"（源码在 {HERE}/main.swift，二进制不进版本库）")
        return out
    if not os.access(str(BIN), os.X_OK):
        out["reason"] = f"录制器存在但不可执行：{BIN}"
        return out
    out["available"] = True
    out["reason"] = "ok"
    return out



# 外部工具不一定在 PATH 里：本机实测（DSH 环境）PATH 只有
# /usr/bin:/bin:/usr/sbin:/sbin，而 ffprobe/ffmpeg 装在 /opt/homebrew/bin。
# 只信 PATH 会让"工具其实装好了"被误判成"缺少依赖"，进而把好素材报成 unknown。
_TOOL_FALLBACK_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin")


def _find_tool(name: str) -> Optional[str]:
    """按 PATH → 已知绝对目录 的顺序找可执行文件。找不到返回 None（**不猜**）。"""
    p = shutil.which(name)
    if p:
        return p
    for d in _TOOL_FALLBACK_DIRS:
        cand = Path(d) / name
        if cand.exists() and os.access(str(cand), os.X_OK):
            return str(cand)
    return None


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """从录制器输出里取出 JSON。

    录制器把 JSON **美化打印**（多行），所以不能按"找一行以 { 开头"来解析 ——
    那样只会拿到第一个 `{` 然后 json.loads 失败。这里按"第一个 { 到最后一个 }"
    截取，能同时吃下美化和单行两种输出。
    """
    if not text:
        return None
    t = text.strip()
    try:
        v = json.loads(t)
        return v if isinstance(v, dict) else None
    except json.JSONDecodeError:
        pass
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        v = json.loads(t[i:j + 1])
        return v if isinstance(v, dict) else None
    except json.JSONDecodeError:
        return None

def _run(argv: List[str], timeout: float = 60.0) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def probe(timeout: float = 30.0) -> Dict[str, Any]:
    """探测：权限 / 显示器 / 应用 / 窗口。返回录制器的原始 JSON（并补上 windows 列表）。

    录制器的 `--probe` 会给出 `applications` 与 `match_windows`；为了做 live 校对，
    这里把窗口列表规整成 `windows`（含 owner_pid），与 `capture_targets.LiveFacts`
    的期望字段对齐。
    """
    av = availability()
    if not av["available"]:
        return {"ok": False, "error": av["reason"], **av}

    # 探测本身也需要一个选择器（录制器不内置默认目标）。
    # 这里用一个"必然不存在"的选择器，只为拿到 applications/windows 列表；
    # 没命中是**预期**的，不当成失败。
    argv = [str(BIN), "--probe", "--bundle-id", "__agent_capture_probe__"]
    try:
        cp = _run(argv, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"probe 超时（{timeout}s）", **av}

    raw: Dict[str, Any] = _extract_json(cp.stdout or "") or {}
    if not raw:
        return {"ok": False,
                "error": f"probe 没有返回可解析的 JSON（rc={cp.returncode}）",
                "stderr": (cp.stderr or "")[-400:], **av}

    # 规整窗口列表：LiveFacts 需要 owner_pid
    wins: List[Dict[str, Any]] = []
    apps_by_pid = {int(a.get("pid") or 0): a for a in raw.get("applications") or []}
    for w in raw.get("match_windows") or []:
        # match_windows 只含"命中目标"的窗口；探测用假目标时通常为空，
        # 所以真正的窗口来源是 windows（录制器未提供时留空，**不编造**）
        wins.append({
            "windowID": w.get("windowID"),
            "title": w.get("title") or "",
            "owner_pid": 0,
            "on_screen": bool(w.get("on_screen", True)),
        })
    if isinstance(raw.get("windows"), list):
        wins = []
        for w in raw["windows"]:
            pid = int(w.get("owner_pid") or w.get("pid") or 0)
            wins.append({
                "windowID": w.get("windowID") or w.get("window_id"),
                "title": w.get("title") or "",
                "owner_pid": pid,
                "on_screen": bool(w.get("on_screen", True)),
                "owner_bundle_id": (apps_by_pid.get(pid) or {}).get("bundle_id", ""),
            })

    raw["windows"] = wins
    raw["ok"] = bool(raw.get("cg_preflight_screen_capture"))
    if not raw["ok"]:
        raw["hint"] = ("没有「屏幕与系统音频录制」权限：系统设置 → 隐私与安全性 → "
                       "屏幕与系统音频录制，勾选运行本程序的 app")
    raw.update({k: av[k] for k in ("backend", "platform", "verified_level",
                                   "verified_scope")})
    return raw


def _target_to_argv(target: Dict[str, Any]) -> List[str]:
    """把 CaptureTarget 翻成录制器参数。画面与音频**分开**表达。"""
    v = (target or {}).get("video") or {}
    a = (target or {}).get("audio") or {}
    argv: List[str] = []

    gran = v.get("granularity")
    if gran == "window":
        if v.get("window_id"):
            argv += ["--window", str(int(v["window_id"]))]
        elif v.get("window_title"):
            argv += ["--window-title", str(v["window_title"])]
    # —— 窗口标题 pin：**防"ID 不变但内容换了"** ——
    # 预检时看到的标题必须与起录时一致；不一致由录制器直接失败。
    expect_title = v.get("expect_window_title") or ""
    if expect_title:
        argv += ["--expect-window-title", str(expect_title)]
    # app 选择器（窗口粒度下也用来把范围钉死到一个 app）
    if v.get("pid"):
        argv += ["--pid", str(int(v["pid"]))]
    if v.get("bundle_id"):
        argv += ["--bundle-id", str(v["bundle_id"])]
    if v.get("app_name"):
        argv += ["--app-name", str(v["app_name"])]

    # 音频：只支持 app / none。跨 app 音源由 capture_targets 拦下；
    # 这里再传一次 audio-bundle-id 让录制器**二次确认**（纵深防御）。
    if a.get("granularity") == "app" and a.get("bundle_id"):
        argv += ["--audio-bundle-id", str(a["bundle_id"])]
    if a.get("include_microphone"):
        # 录制器没有麦克风开关，也不该有：显式拒绝而不是静默忽略
        raise ValueError("本后端未实现麦克风采集，拒绝启动（不会静默忽略这个请求）")
    return argv


def preflight(target: Dict[str, Any], *, probe_json: Optional[Dict[str, Any]] = None,
              timeout: float = 30.0) -> Dict[str, Any]:
    """启动前检查。**不启动任何采集。**

    返回 `{"ok": bool, "problems": [...], "facts": {...}}`。
    `ok=False` 时调用方**必须**停下，不允许"先录了再说"。
    """
    av = availability()
    problems: List[str] = []
    if not av["available"]:
        problems.append(av["reason"])
        return {"ok": False, "problems": problems, "facts": {}, "verified_level":
                VERIFIED_LEVEL, "verified_scope": VERIFIED_SCOPE}

    try:
        argv = _target_to_argv(target)
    except ValueError as exc:
        return {"ok": False, "problems": [str(exc)], "facts": {},
                "verified_level": VERIFIED_LEVEL, "verified_scope": VERIFIED_SCOPE}

    if not argv:
        problems.append("目标没有可用的选择器：拒绝启动（本后端没有默认目标）")
        return {"ok": False, "problems": problems, "facts": {},
                "verified_level": VERIFIED_LEVEL, "verified_scope": VERIFIED_SCOPE}

    pj = probe_json if probe_json is not None else probe(timeout=timeout)
    if not pj.get("ok"):
        problems.append(pj.get("error") or pj.get("hint") or "probe 未通过")
        return {"ok": False, "problems": problems, "facts": pj,
                "verified_level": VERIFIED_LEVEL, "verified_scope": VERIFIED_SCOPE}

    # 用录制器自己的 probe 做一次"这个目标命中吗"的确认
    sel = _probe_selector_argv(target)
    if sel:
        try:
            cp = _run([str(BIN), "--probe"] + sel, timeout=timeout)
            one: Dict[str, Any] = _extract_json(cp.stdout or "") or {}
            if one and one.get("match") is None:
                problems.append(
                    "目标没命中（不会回退去抓别的 app/窗口）："
                    + str(one.get("hint") or "")
                )
        except subprocess.TimeoutExpired:
            problems.append(f"目标确认 probe 超时（{timeout}s）")

    return {"ok": not problems, "problems": problems, "facts": pj,
            "argv": argv, "verified_level": VERIFIED_LEVEL,
            "verified_scope": VERIFIED_SCOPE}


def _probe_selector_argv(target: Dict[str, Any]) -> List[str]:
    """只取"能确认目标存在"的那部分参数（不含输出等）。"""
    v = (target or {}).get("video") or {}
    argv: List[str] = []
    if v.get("granularity") == "window" and v.get("window_id"):
        argv += ["--window", str(int(v["window_id"]))]
    if v.get("pid"):
        argv += ["--pid", str(int(v["pid"]))]
    if v.get("bundle_id"):
        argv += ["--bundle-id", str(v["bundle_id"])]
    if v.get("app_name"):
        argv += ["--app-name", str(v["app_name"])]
    return argv


def build_start_argv(target: Dict[str, Any], out_path: str, *, duration: float = 0.0,
                     fps: int = 30, max_width: int = 1920,
                     focus_log: str = "", status_file: str = "",
                     metrics_json: str = "", live_status: str = "",
                     live_token: str = "",
                     overwrite: bool = False,
                     no_video: bool = False,
                     no_audio: bool = False) -> List[str]:
    """构造**真正启动录制**的完整参数。"""
    argv = [str(BIN)] + _target_to_argv(target)
    argv += ["--out", out_path]
    if duration > 0:
        argv += ["--duration", str(duration)]
    if fps:
        argv += ["--fps", str(int(fps))]
    if max_width:
        argv += ["--max-width", str(int(max_width))]
    if metrics_json:
        argv += ["--json", metrics_json]
    if status_file:
        argv += ["--status", status_file]
    if focus_log:
        argv += ["--focus-log", focus_log]
    if live_status:
        argv += ["--live-status", live_status]
    if live_token:
        # 令牌让 worker 分辨"本次"与"上一次残留"的进度文件
        argv += ["--live-token", live_token]
    if no_video:
        argv += ["--no-video"]
    if no_audio:
        # **真的**不录音频（不是"不要求声音"）：cfg/writer/stream/元数据四处一致关闭
        argv += ["--no-audio"]
    if overwrite:
        argv += ["--overwrite"]
    return argv


def verify(media_path: str, expect: str = "av") -> Dict[str, Any]:
    """对已产出的媒体做**停止后探测**。

    这里复用 Studio 里那套久经使用的 verify（ffprobe + 逐秒峰值 + 抽帧 md5），
    通过 `record-game.sh verify` 调用；找不到就**如实报 unknown**，不假装通过。
    """
    out: Dict[str, Any] = {
        "result": "unknown", "media": media_path, "expect": expect,
        "verified_level": VERIFIED_LEVEL, "verified_scope": VERIFIED_SCOPE,
        "checks": {},
    }
    p = Path(media_path)
    if not p.exists():
        out["result"] = "fail"
        out["checks"]["exists"] = {"ok": False, "detail": "文件不存在"}
        return out
    out["checks"]["exists"] = {"ok": True, "bytes": p.stat().st_size}
    if p.stat().st_size == 0:
        out["result"] = "fail"
        out["checks"]["nonempty"] = {"ok": False, "detail": "文件为 0 字节"}
        return out

    for tool in ("ffprobe", "ffmpeg"):
        if _find_tool(tool) is None:
            out["checks"][tool] = {"ok": False, "detail": f"缺少 {tool}"}
            out["notes"] = (f"缺少 {tool}：**无法**判定媒体内容，"
                            f"因此报 unknown 而不是通过")
            return out

    # 把解析到的工具目录前置进 PATH，让 record-game.sh 里的裸 ffprobe/ffmpeg 也能找到
    env = dict(os.environ)
    dirs = []
    for t in ("ffprobe", "ffmpeg"):
        f = _find_tool(t)
        if f:
            dirs.append(str(Path(f).parent))
    if dirs:
        env["PATH"] = ":".join(dict.fromkeys(dirs)) + ":" + env.get("PATH", "")

    script = HERE / "record-game.sh"
    if not script.exists():
        out["notes"] = f"缺少 {script}：无法执行停止后探测，报 unknown"
        return out
    try:
        cp = subprocess.run(["bash", str(script), "verify", media_path,
                             "--expect", expect],
                            capture_output=True, text=True, timeout=600, env=env)
    except subprocess.TimeoutExpired:
        out["notes"] = "verify 超时"
        return out
    out["checks"]["verify_stdout_tail"] = (cp.stdout or "")[-2000:]
    if cp.returncode == 0:
        out["result"] = "pass"
    elif cp.returncode == 2:
        out["result"] = "fail"
    else:
        out["result"] = "unknown"
        out["notes"] = f"verify 退出码 {cp.returncode}（既非通过也非失败 → unknown）"
    return out


if __name__ == "__main__":
    print(json.dumps(availability(), ensure_ascii=False, indent=2))
