#!/usr/bin/env python3
"""capture_targets — 目标解析、live 事实校对与防误录（跨平台语义层，不做采集）。

这个模块只做一件事：把"用户想录什么"变成一份**可核对的、显式的**目标描述，
并在任何采集发生之前判断它是否成立。它存在的理由是防误录：

- **没有默认目标。** 不给选择器 = 用法错误。绝不"那就录全屏吧"。
- **选择器给了但没命中 = 失败。** 绝不回退到"随便找个像的"。
- **画面目标与音频目标分开表达。** 它们可以不同粒度。
- **不确定就报 unknown。** 不把"没查出来"说成"没问题"。

## 两个刻意的设计选择（都来自实际踩过的坑）

1. **能力按"后端"报，不按"操作系统"报。**
   早期版本写"平台 macos 不支持窗口级音频"，这是在**用局部实现断言 OS 能力** ——
   我们只是没实现，不代表系统做不到。现在一律说"**本后端未实现**"，
   并把它和"OS 确实没有这个能力"分开表达（`audio_per_window_os_capable` vs
   已实现列表）。没实现的粒度（display / system 音频 / 麦克风）**直接拒绝**，
   不静默降级、不假支持。

2. **`verified` 不等于"代码编译过"。**
   `verified_level` 描述的是"这条路径**被真实录过**吗"。编译通过、mock 通过都不是实录。
   取值：`recorded`（真实录过）/ `mock_only`（只有协议 mock）/ `compile_only`（只编译过）/
   `none`（没有证据）。只有 `recorded` 才允许对外说"验证过"。

## live 事实校对

`validate_target()` 可以接收一份 **live facts**（来自 `probe`：应用列表 + 窗口列表），
在采集前把选择器**解析成确定的身份**再比对：

- 纯 `--window <id>` 可以反查所属 app；
- 同一个 app 用 `pid` 写和用 `bundle_id` 写**必须等价**，不能因为字符串不同就拒绝；
- 两边都给了但**互相矛盾**（pid 指向 A、bundle 指向 B）→ 拒绝，
  不能"优先 pid、忽略矛盾的 bundle"。

`LiveFacts` 可以从 JSON 构造，所以这一层**完全离线可测**，不需要权限。
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

VIDEO_GRANULARITIES = ("display", "app", "window")
AUDIO_GRANULARITIES = ("none", "app", "window", "system")

# 用户可选的音频模式（CLI 层面）。
# 含 "window"：这是一个**被认识**的请求，由 validate 判"本后端未实现"，
# 而不是当成非法值 —— 这样报错更有信息量（"没实现"≠"你写错了"）。
AUDIO_MODES = ("", "none", "app", "window", "system")

VERIFIED_LEVELS = ("recorded", "mock_only", "compile_only", "none")


class TargetError(RuntimeError):
    """目标无法成立。**调用方必须失败**，不允许降级成"录别的"。"""


class UsageError(TargetError):
    pass


# --------------------------------------------------------------------------
# 后端能力：按"本后端实现了什么"报，并区分 OS 能力
# --------------------------------------------------------------------------

@dataclass
class BackendCapability:
    """一个采集后端的**实际**能力。

    关键区分：
      · `video_granularities` / `audio_granularities` = **本后端已实现**的
      · `audio_per_window_os_capable` = 底层框架**理论上有**窗口级音频吗
        （用来避免"用局部实现断言 OS 能力"）
      · `verified_level` = 这条路径有没有**真实录过**（不是编译过、不是 mock 过）
    """
    backend: str
    platform: str
    video_granularities: List[str]
    audio_granularities: List[str]
    audio_per_window_os_capable: bool
    verified_level: str
    verified_scope: str
    unimplemented: Dict[str, str] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)


BACKENDS: Dict[str, BackendCapability] = {
    "macos": BackendCapability(
        backend="macos-screencapturekit",
        platform="macos",
        # 只列**真的实现了**的：整屏模式没实现，所以不在这里
        video_granularities=["app", "window"],
        audio_granularities=["none", "app"],
        audio_per_window_os_capable=False,
        verified_level="compile_only",  # 真实录制证据出来后由 evidence 更新
        verified_scope="本机 macOS 编译通过 + 离线用例；真实录制证据见 evidence/",
        unimplemented={
            "display": "本后端未实现整屏采集（原工具就没有全屏模式）。"
                       "整屏不在本工具的设计目标内：它容易录到无关内容。",
            "system": "本后端未实现整机混音采集：只做目标 app 的原声。",
            "window_audio": "本后端未实现窗口级音频；ScreenCaptureKit 的音频过滤"
                            "跟随画面 filter，最小粒度是 app。",
            "microphone": "本后端未实现麦克风采集（固定 captureMicrophone=false）。",
        },
        notes=[
            "画面支持 app 级与单窗口级；单窗口用 SCContentFilter(desktopIndependentWindow:)。",
            "音频是 app 级：单窗口画面下，音频范围可能大于画面。",
            "跨 app 音源（画面 A、声音只要 B）**直接拒绝**：一条流做不到。",
        ],
    ),
    "windows": BackendCapability(
        backend="windows-obs-websocket",
        platform="windows",
        # 只做 window_capture（**单窗口**）。不把窗口采集改个名字就声称支持 app 级画面 ——
        # 真要 app 级得用 game_capture/进程匹配另做。此处如实只报 ["window"]。
        video_granularities=["window"],
        audio_granularities=["none", "app"],
        audio_per_window_os_capable=False,
        # 恒为 none 直到有 Windows 实机证据。这与后端自己的 verification_level
        # （描述"这次连到了哪一层"）是两件事，各说各的。
        verified_level="none",
        verified_scope="**未在 Windows 实机验证**（本机 macOS；tailnet 内那台 Windows "
                       "无可用凭据）。mock 只证明协议/状态机，不证明能录到画面或声音。",
        unimplemented={
            "display": "本后端未实现整屏采集（明确不录全局桌面）。",
            "system": "本后端不录全局桌面音：会把与目标无关的声音混进来。",
            "window_audio": "本后端未实现窗口级音频（走 OBS Application Audio Capture，"
                            "粒度是应用/进程）。",
            "microphone": "本后端不添加麦克风源。",
        },
        notes=[
            "**不接管**正在直播/录制的 OBS 实例：连接后先查状态，活跃即拒绝。",
            "**不让 OBS 按同类窗口自动重匹配**：要求显式可执行名/进程，"
            "创建后回读实际绑定目标，不一致即失败。",
            "Windows 的可执行名匹配不要套 macOS 的 bundle id 规则。",
        ],
    ),
    "linux": BackendCapability(
        backend="none",
        platform="linux",
        video_granularities=[],
        audio_granularities=[],
        audio_per_window_os_capable=False,
        verified_level="none",
        verified_scope="未实现",
        unimplemented={"*": "本任务未实现 Linux 后端。"},
        notes=["**不假装支持**：任何采集请求都会明确报「本后端未实现」。"],
    ),
}


def detect_platform() -> str:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    return "unknown"


def capability(platform: Optional[str] = None) -> BackendCapability:
    p = platform or detect_platform()
    cap = BACKENDS.get(p)
    if cap is None:
        return BackendCapability(
            backend="none", platform=p,
            video_granularities=[], audio_granularities=[],
            audio_per_window_os_capable=False,
            verified_level="none",
            verified_scope="未知平台",
            unimplemented={"*": f"未知平台 {p!r}：本工具不支持，也不猜测其能力。"},
            notes=[f"未知平台 {p!r}：不声称支持，也不猜测其能力。"],
        )
    return cap


# --------------------------------------------------------------------------
# live 事实（probe 的产物）—— 让校对可以离线测试
# --------------------------------------------------------------------------

@dataclass
class AppFact:
    pid: int
    bundle_id: str
    name: str


@dataclass
class WindowFact:
    window_id: int
    title: str
    owner_pid: int
    on_screen: bool = True
    owner_bundle_id: str = ""


@dataclass
class LiveFacts:
    apps: List[AppFact] = field(default_factory=list)
    windows: List[WindowFact] = field(default_factory=list)

    @staticmethod
    def from_probe_json(raw: Any) -> "LiveFacts":
        """从 `--probe` 的 JSON 构造。字段缺失**不猜**：缺了就是没有。"""
        if not isinstance(raw, dict):
            raise TargetError("probe JSON 必须是对象")
        apps: List[AppFact] = []
        for a in raw.get("applications") or []:
            if not isinstance(a, dict):
                continue
            apps.append(AppFact(pid=int(a.get("pid") or 0),
                                bundle_id=str(a.get("bundle_id") or ""),
                                name=str(a.get("name") or "")))
        wins: List[WindowFact] = []
        for w in raw.get("windows") or []:
            if not isinstance(w, dict):
                continue
            wins.append(WindowFact(
                window_id=int(w.get("windowID") or w.get("window_id") or 0),
                title=str(w.get("title") or ""),
                owner_pid=int(w.get("owner_pid") or 0),
                on_screen=bool(w.get("on_screen", True)),
                owner_bundle_id=str(w.get("owner_bundle_id") or ""),
            ))
        return LiveFacts(apps=apps, windows=wins)

    def app_by_pid(self, pid: int) -> Optional[AppFact]:
        for a in self.apps:
            if a.pid == pid:
                return a
        return None

    def app_by_bundle(self, bundle: str) -> Optional[AppFact]:
        if not bundle:
            return None
        for a in self.apps:
            if a.bundle_id == bundle:
                return a
        return None

    def app_by_name(self, name: str) -> Optional[AppFact]:
        if not name:
            return None
        for a in self.apps:
            if a.name == name:
                return a
        return None

    def window_by_id(self, wid: int) -> Optional[WindowFact]:
        for w in self.windows:
            if w.window_id == wid:
                return w
        return None


# --------------------------------------------------------------------------
# 目标描述
# --------------------------------------------------------------------------

@dataclass
class VideoTarget:
    granularity: str = ""
    bundle_id: str = ""
    app_name: str = ""
    pid: int = 0
    window_id: int = 0
    window_title: str = ""
    explicit_display: bool = False

    def selectors(self) -> List[str]:
        s: List[str] = []
        if self.pid:
            s.append(f"pid={self.pid}")
        if self.bundle_id:
            s.append(f"bundle_id={self.bundle_id}")
        if self.app_name:
            s.append(f"app_name={self.app_name}")
        if self.window_id:
            s.append(f"window_id={self.window_id}")
        if self.window_title:
            s.append(f"window_title={self.window_title!r}")
        return s


@dataclass
class AudioTarget:
    granularity: str = "none"
    bundle_id: str = ""
    app_name: str = ""
    pid: int = 0
    include_microphone: bool = False

    def selectors(self) -> List[str]:
        s: List[str] = []
        if self.pid:
            s.append(f"pid={self.pid}")
        if self.bundle_id:
            s.append(f"bundle_id={self.bundle_id}")
        if self.app_name:
            s.append(f"app_name={self.app_name}")
        return s


@dataclass
class CaptureTarget:
    video: VideoTarget = field(default_factory=VideoTarget)
    audio: AudioTarget = field(default_factory=AudioTarget)
    platform: str = ""
    audio_effective_granularity: str = ""
    notes: List[str] = field(default_factory=list)

    def to_json(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_json(d: Dict[str, Any]) -> "CaptureTarget":
        d = dict(d or {})
        return CaptureTarget(
            video=VideoTarget(**{k: v for k, v in (d.get("video") or {}).items()
                                 if k in VideoTarget.__dataclass_fields__}),
            audio=AudioTarget(**{k: v for k, v in (d.get("audio") or {}).items()
                                 if k in AudioTarget.__dataclass_fields__}),
            platform=d.get("platform", ""),
            audio_effective_granularity=d.get("audio_effective_granularity", ""),
            notes=list(d.get("notes") or []),
        )


def _apply_app(obj: Any, spec: str, platform: str) -> None:
    """app 选择器写成 bundle id 还是名字？

    bundle id 的形态判断**只对 macOS 成立**（`com.example.App`）。
    Windows 上是可执行名（`game.exe`）或路径，Linux 上可能是任意名字 ——
    所以不能把 macOS 的规则套到别的平台。
    """
    spec = spec.strip()
    if not spec:
        return
    if platform == "macos":
        # 带点、无空格、无路径分隔符 → 看起来像反向域名 bundle id
        if "." in spec and " " not in spec and "/" not in spec:
            obj.bundle_id = spec
            return
        obj.app_name = spec
        return
    # 非 macOS：`.exe` 是**可执行名**，不是 bundle id。统一按名字处理，
    # 由各后端按自己的规则匹配（Windows 后端自己认 exe/进程）。
    obj.app_name = spec


def resolve_target(platform: str, *, video_app: str = "", video_window: str = "",
                   video_pid: int = 0, display: bool = False,
                   audio_app: str = "", audio_pid: int = 0,
                   audio_mode: str = "", microphone: bool = False) -> CaptureTarget:
    """把 CLI 参数变成 CaptureTarget。**不猜**：没给就是没有。

    未知的 `audio_mode` **直接抛错**，绝不静默落回默认值 —— 静默降级会让调用方
    以为它要的模式生效了（早期版本就有这个 bug：`audio_mode="window"` 悄悄变成了
    app 级音频，且不报任何问题）。
    """
    if audio_mode not in AUDIO_MODES:
        raise UsageError(f"非法 audio_mode {audio_mode!r}；合法："
                         f"{[m for m in AUDIO_MODES if m]}")
    cap = capability(platform)
    t = CaptureTarget(platform=platform)

    if display:
        t.video.granularity = "display"
        t.video.explicit_display = True
    elif video_window:
        t.video.granularity = "window"
        vw = str(video_window).strip()
        if vw.isdigit():
            t.video.window_id = int(vw)
        else:
            t.video.window_title = vw
        if video_app:
            _apply_app(t.video, video_app, platform)
        if video_pid:
            t.video.pid = video_pid
    elif video_app or video_pid:
        t.video.granularity = "app"
        if video_app:
            _apply_app(t.video, video_app, platform)
        if video_pid:
            t.video.pid = video_pid

    if audio_mode == "none":
        t.audio.granularity = "none"
    elif audio_mode == "system":
        t.audio.granularity = "system"
    elif audio_mode == "window":
        # 如实记录这个请求；能不能做到由 validate_target 判（当前后端一律未实现）。
        # **不在这里静默降级成 app** —— 那等于冒称支持。
        t.audio.granularity = "window"
        if audio_app:
            _apply_app(t.audio, audio_app, platform)
        if audio_pid:
            t.audio.pid = audio_pid
    elif audio_mode == "app":
        t.audio.granularity = "app"
        if audio_app:
            _apply_app(t.audio, audio_app, platform)
        if audio_pid:
            t.audio.pid = audio_pid
    elif audio_app or audio_pid:
        t.audio.granularity = "app"
        if audio_app:
            _apply_app(t.audio, audio_app, platform)
        if audio_pid:
            t.audio.pid = audio_pid
    elif t.video.granularity in ("app", "window"):
        # 默认跟随画面 app：这是最常见且最安全的默认
        t.audio.granularity = "app" if "app" in cap.audio_granularities else "none"
        t.audio.bundle_id = t.video.bundle_id
        t.audio.app_name = t.video.app_name
        t.audio.pid = t.video.pid

    t.audio.include_microphone = bool(microphone)
    t.audio_effective_granularity = t.audio.granularity
    return t


# --------------------------------------------------------------------------
# 校验
# --------------------------------------------------------------------------

def validate_target(t: CaptureTarget, platform: Optional[str] = None,
                    facts: Optional[LiveFacts] = None) -> List[str]:
    """校验目标。返回**问题列表**；非空即失败。空列表 = 可以继续。

    返回列表而不是抛异常：调用方要把所有问题一次报全，而不是让用户修一个跑一次。

    `facts` 给了就做 **live 校对**：把选择器解析成确定身份再比对
    （pid↔bundle 等价、窗口反查 owner、矛盾选择器拒绝）。
    """
    problems: List[str] = []
    plat = platform or t.platform or detect_platform()
    cap = capability(plat)
    v, a = t.video, t.audio

    # —— 没有默认目标 ——
    if not v.granularity:
        problems.append(
            "没有画面目标：必须显式给出 --video-app / --video-window / --display。"
            "本工具**没有**默认目标，也**不会**回退成录整屏。"
        )
    elif v.granularity not in VIDEO_GRANULARITIES:
        problems.append(f"未知画面粒度 {v.granularity!r}；合法：{list(VIDEO_GRANULARITIES)}")

    # —— 粒度必须**本后端已实现**（措辞上不冒充 OS 能力）——
    if v.granularity and v.granularity not in cap.video_granularities:
        why = cap.unimplemented.get(v.granularity)
        problems.append(
            f"画面粒度 {v.granularity!r} **本后端未实现**（后端={cap.backend}）。"
            + (f" {why}" if why else "")
            + f" 已实现：{cap.video_granularities or '无'}。"
        )

    # 选择器完整性
    if v.granularity == "app" and not v.selectors():
        problems.append("画面粒度 app 但没给任何选择器（--video-app / --pid）")
    if v.granularity == "window":
        if not (v.window_id or v.window_title):
            problems.append("画面粒度 window 但没给 --video-window（窗口 id 或标题）")
        if v.window_title and not v.window_id and not (v.bundle_id or v.app_name or v.pid):
            problems.append(
                "只给窗口标题、没给所属 app：标题会变、也可能重名，"
                "请加 --video-app 把搜索范围钉死到一个 app"
            )
    if v.granularity == "display" and not v.explicit_display:
        problems.append("画面粒度 display 必须显式声明（--display），它不是兜底选项")

    # 负 ID
    if v.pid < 0:
        problems.append(f"画面 --pid 不能为负：{v.pid}")
    if v.window_id < 0:
        problems.append(f"画面窗口 id 不能为负：{v.window_id}")
    if a.pid < 0:
        problems.append(f"音频 --pid 不能为负：{a.pid}")

    # —— 音频 ——
    if a.granularity not in AUDIO_GRANULARITIES:
        problems.append(f"未知音频粒度 {a.granularity!r}；合法：{list(AUDIO_GRANULARITIES)}")
    elif a.granularity not in cap.audio_granularities:
        why = cap.unimplemented.get(a.granularity)
        if a.granularity == "window":
            why = why or cap.unimplemented.get("window_audio")
        problems.append(
            f"音频粒度 {a.granularity!r} **本后端未实现**（后端={cap.backend}）。"
            + (f" {why}" if why else "")
            + f" 已实现：{cap.audio_granularities or '无'}。"
        )
    if a.granularity == "app" and not a.selectors():
        problems.append("音频粒度 app 但没给任何选择器（--audio-app / --audio-pid）")
    if a.include_microphone:
        why = cap.unimplemented.get("microphone")
        problems.append("请求了麦克风，但**本后端未实现**麦克风采集"
                        + (f"：{why}" if why else "") + "。默认不录麦克风。")

    # —— 画面与音频粒度不匹配要提示（不阻止，但必须让用户知道） ——
    if a.granularity == "app" and v.granularity == "window":
        t.notes.append(
            "画面=单窗口，音频=该 app（app 级）：录到的声音可能包含该 app "
            "其它窗口的内容（如启动器/聊天）。本后端没有窗口级音频，这是后端限制。"
        )

    # —— 跨 app 音源：**同类选择器**下可以立即判定，不必等 live 事实 ——
    # 只在两边都是**同一种**选择器（都是 bundle，或都是名字）时比较：
    # 那时字符串不同就**确定**是不同的 app。
    # 混合写法（一个 pid 一个 bundle）无法在字符串层面比较，留给 live 校对
    # —— 那里会把两者解析成同一个身份对象再比，所以"同一个 app 的两种写法"
    # 不会被误拒。
    conflict = _same_kind_app_conflict(v, a)
    if conflict and a.granularity == "app" and cap.audio_per_window_os_capable is False:
        problems.append(
            f"画面目标与音频目标属于不同 app（画面={conflict[0]}，音频={conflict[1]}）："
            f"本后端的音频过滤跟随画面 filter，一条流无法同时满足。"
            f"请让两者一致，或分两次录制后合成"
            f"（本工具不会静默只满足其中一个，也不会假装支持）。"
        )

    # —— live 校对 ——
    if facts is not None:
        problems.extend(_cross_check(t, cap, facts))

    return problems


def _same_kind_app_conflict(v: VideoTarget, a: AudioTarget):
    """两边选择器**同类且不同值**时返回 (画面, 音频) 描述，否则 None。

    只比较同类：`bundle_id` 对 `bundle_id`、`app_name` 对 `app_name`。
    混用 pid/bundle 时返回 None（无法在字符串层面断定，交给 live 校对）。
    """
    if v.bundle_id and a.bundle_id:
        if v.bundle_id != a.bundle_id:
            return (f"bundle:{v.bundle_id}", f"bundle:{a.bundle_id}")
        return None
    if v.app_name and a.app_name and not (v.bundle_id or a.bundle_id):
        if v.app_name != a.app_name:
            return (f"name:{v.app_name}", f"name:{a.app_name}")
        return None
    return None


def _resolve_app_identity(pid: int, bundle: str, name: str, facts: LiveFacts,
                          label: str, problems: List[str]) -> Optional[AppFact]:
    """把一个 app 选择器（pid / bundle / name 的任意组合）解析成**唯一** app。

    - 多个选择器都给了：必须指向**同一个** app；矛盾 → 报错
      （不能"优先 pid、忽略矛盾的 bundle"）。
    - 只给一个：查 live 事实；查不到 → 报错（不猜）。
    - **等价性靠解析后的身份**，不是字符串比较：同一个 app 用 `pid` 写和用
      `bundle_id` 写会解析成同一个 `AppFact`，因此不会被误判为"不一致"。
    """
    got: List[tuple] = []
    if pid:
        f = facts.app_by_pid(pid)
        if f is None:
            problems.append(f"{label} --pid {pid} 在 live 应用列表里找不到")
            return None
        got.append(("pid", f))
    if bundle:
        f = facts.app_by_bundle(bundle)
        if f is None:
            problems.append(f"{label} --bundle-id {bundle} 在 live 应用列表里找不到")
            return None
        got.append(("bundle", f))
    if name:
        f = facts.app_by_name(name)
        if f is None:
            problems.append(f"{label} --app-name {name!r} 在 live 应用列表里找不到")
            return None
        got.append(("name", f))

    if not got:
        return None
    first = got[0][1]
    for _kind, f in got[1:]:
        if f.pid != first.pid or f.bundle_id != first.bundle_id:
            detail = "、".join(f"{k}→pid={x.pid}/{x.bundle_id or x.name}" for k, x in got)
            problems.append(
                f"{label} 的选择器互相矛盾（{detail}）：它们指向不同的 app。"
                f"拒绝「优先某一个、忽略矛盾」，请改成一致的选择器。"
            )
            return None
    return first


def _cross_check(t: CaptureTarget, cap: BackendCapability, facts: LiveFacts) -> List[str]:
    """用 live 事实把选择器解析成确定身份，再校对画面与音频是否一致。"""
    problems: List[str] = []
    v, a = t.video, t.audio
    video_app: Optional[AppFact] = None

    if v.granularity == "window" and v.window_id:
        # 纯窗口 ID：**可以反查所属 app**
        wf = facts.window_by_id(v.window_id)
        if wf is None:
            problems.append(f"画面窗口 id {v.window_id} 在 live 窗口列表里找不到")
        else:
            if not wf.on_screen:
                problems.append(f"画面窗口 {v.window_id} 当前不在屏上：拿不到帧")
            if wf.owner_pid:
                video_app = facts.app_by_pid(wf.owner_pid)
                if video_app is None:
                    problems.append(
                        f"窗口 {v.window_id} 的所属进程 pid={wf.owner_pid} "
                        f"不在 live 应用列表里（无法确定音频范围）"
                    )
            if video_app is not None and (v.bundle_id or v.app_name or v.pid):
                declared = _resolve_app_identity(v.pid, v.bundle_id, v.app_name, facts,
                                                 "画面", problems)
                if declared is not None and declared.pid != video_app.pid:
                    problems.append(
                        f"画面选择器与窗口 {v.window_id} 的实际所属 app 不一致："
                        f"选择器指向 {declared.name}({declared.bundle_id}, pid={declared.pid})，"
                        f"窗口属于 {video_app.name}({video_app.bundle_id}, pid={video_app.pid})。"
                    )
    elif v.granularity == "window" and v.window_title:
        matches = [w for w in facts.windows if w.title == v.window_title and w.on_screen]
        if v.bundle_id or v.app_name or v.pid:
            scoped = _resolve_app_identity(v.pid, v.bundle_id, v.app_name, facts,
                                           "画面", problems)
            if scoped is not None:
                matches = [w for w in matches if w.owner_pid == scoped.pid]
                video_app = scoped
        if len(matches) == 0:
            problems.append(
                f"画面窗口标题 {v.window_title!r} 在 live 窗口列表里找不到在屏窗口")
        elif len(matches) > 1:
            ids = ", ".join(str(w.window_id) for w in matches)
            problems.append(
                f"画面窗口标题 {v.window_title!r} 命中 {len(matches)} 个窗口（id: {ids}）："
                f"无法确定要哪一个，请改用 --video-window <windowID>。"
            )
        elif video_app is None and matches[0].owner_pid:
            video_app = facts.app_by_pid(matches[0].owner_pid)
    elif v.granularity == "app":
        video_app = _resolve_app_identity(v.pid, v.bundle_id, v.app_name, facts,
                                          "画面", problems)

    # —— 音频与画面必须指向同一个 app（音频要收声时）——
    if a.granularity == "app":
        audio_app = _resolve_app_identity(a.pid, a.bundle_id, a.app_name, facts,
                                          "音频", problems)
        if audio_app is not None and video_app is not None:
            # 比的是**解析后的身份**，不是字符串：同一个 app 用 pid 写或用 bundle 写，
            # 这里会解析成同一个 AppFact，因此不会被误拒。
            if audio_app.pid != video_app.pid:
                problems.append(
                    f"画面 app 与音频 app 不一致（画面={video_app.name}/pid={video_app.pid}，"
                    f"音频={audio_app.name}/pid={audio_app.pid}）："
                    f"本后端的音频过滤跟随画面 filter，一条流无法只收另一个 app 的声音。"
                    f"请让两者一致，或分两次录制后合成。"
                )
            elif v.granularity == "window":
                t.notes.append(
                    f"画面=单窗口，音频={audio_app.name}（app 级）："
                    f"录到的声音可能包含该 app 其它窗口的内容。"
                )
    return problems


def validate_audio_mode(mode: str) -> Optional[str]:
    """校验 `--audio-mode`。非法值必须被拒，不能当默认值用。"""
    if mode not in AUDIO_MODES:
        return f"非法 --audio-mode {mode!r}；合法：{[m for m in AUDIO_MODES if m]}"
    return None


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="capture_targets")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("capability", help="本后端的能力边界与 verified 等级")
    p.add_argument("--platform", default="")

    p = sub.add_parser("resolve", help="把参数解析成 CaptureTarget 并校验")
    p.add_argument("--platform", default="")
    p.add_argument("--video-app", default="")
    p.add_argument("--video-window", default="")
    p.add_argument("--video-pid", type=int, default=0)
    p.add_argument("--display", action="store_true")
    p.add_argument("--audio-app", default="")
    p.add_argument("--audio-pid", type=int, default=0)
    p.add_argument("--audio-mode", default="")
    p.add_argument("--microphone", action="store_true")
    p.add_argument("--probe-json", default="",
                   help="live 事实（probe 的 JSON 字符串或文件路径），给了就做校对")

    a = ap.parse_args(argv)

    if a.cmd == "capability":
        cap = capability(a.platform or None)
        print(json.dumps(asdict(cap), ensure_ascii=False, indent=2))
        return 0

    plat = a.platform or detect_platform()

    def fail(msgs: List[str]) -> int:
        print(json.dumps({"ok": False, "problems": msgs}, ensure_ascii=False, indent=2))
        return 2

    mode_err = validate_audio_mode(a.audio_mode)
    if mode_err:
        return fail([mode_err])
    for name, val in (("--video-pid", a.video_pid), ("--audio-pid", a.audio_pid)):
        if val < 0:
            return fail([f"{name} 不能为负：{val}"])

    t = resolve_target(plat, video_app=a.video_app, video_window=a.video_window,
                       video_pid=a.video_pid, display=a.display, audio_app=a.audio_app,
                       audio_pid=a.audio_pid, audio_mode=a.audio_mode,
                       microphone=a.microphone)

    facts = None
    if a.probe_json:
        try:
            s = a.probe_json.strip()
            raw = json.loads(s) if s.startswith("{") else \
                json.load(open(s, encoding="utf-8"))
            facts = LiveFacts.from_probe_json(raw)
        except (OSError, ValueError, TargetError) as exc:
            return fail([f"probe JSON 无法读取: {exc}"])

    problems = validate_target(t, plat, facts)
    cap = capability(plat)
    print(json.dumps({"target": t.to_json(), "problems": problems, "ok": not problems,
                      "backend": cap.backend, "verified_level": cap.verified_level,
                      "verified_scope": cap.verified_scope},
                     ensure_ascii=False, indent=2))
    return 0 if not problems else 2


if __name__ == "__main__":
    raise SystemExit(main())
