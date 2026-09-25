#!/usr/bin/env python3
"""capture_targets 的离线测试：目标防误录 + 跨 OS 能力边界。

这些用例全部离线可跑，不需要任何采集权限、显示器或目标 app。

运行：  python3 tests/test_capture_targets.py
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))

import capture_targets as ct  # noqa: E402

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


def resolve(**kw):
    plat = kw.pop("platform", "macos")
    t = ct.resolve_target(plat, **kw)
    return t, ct.validate_target(t, plat)


# --------------------------------------------------------------------------
# 防误录：没有默认目标
# --------------------------------------------------------------------------

def test_no_default_target() -> None:
    """不给选择器 = 用法错误。绝不回退成整屏。"""
    for plat in ("macos", "windows", "linux"):
        t, p = resolve(platform=plat)
        ck(f"[{plat}] 无目标被拒", not p and False or bool(p), f"problems={p}")
        ck(f"[{plat}] 无目标时画面粒度为空（不是 display）",
           t.video.granularity != "display", f"granularity={t.video.granularity!r}")


def test_display_is_explicit_not_fallback() -> None:
    """整屏必须显式声明；手工构造一个"粒度=display 但没声明"的目标要被拒。"""
    t = ct.CaptureTarget(platform="macos")
    t.video.granularity = "display"
    t.video.explicit_display = False
    p = ct.validate_target(t, "macos")
    ck("display 未显式声明被拒", any("显式" in x for x in p), f"{p}")

    t.video.explicit_display = True
    p2 = ct.validate_target(t, "macos")
    ck("display 显式声明后通过", not any("显式" in x for x in p2), f"{p2}")


def test_window_requires_selector() -> None:
    t = ct.CaptureTarget(platform="macos")
    t.video.granularity = "window"
    p = ct.validate_target(t, "macos")
    ck("window 粒度缺 --video-window 被拒", any("video-window" in x for x in p), f"{p}")


def test_window_title_needs_app_scope() -> None:
    """只给窗口标题不给 app：标题会变也可能重名，必须被拒。"""
    t = ct.CaptureTarget(platform="macos")
    t.video.granularity = "window"
    t.video.window_title = "My Game"
    p = ct.validate_target(t, "macos")
    ck("只给窗口标题（无 app 限定）被拒",
       any("钉死" in x or "所属 app" in x for x in p), f"{p}")

    # 给了 app 限定就可以
    t.video.bundle_id = "com.x.Game"
    p2 = ct.validate_target(t, "macos")
    ck("窗口标题 + app 限定通过", not any("钉死" in x for x in p2), f"{p2}")


# --------------------------------------------------------------------------
# 跨 OS 能力边界
# --------------------------------------------------------------------------

def test_macos_window_video_supported() -> None:
    t, p = resolve(video_app="com.x.Game", video_window="12345", audio_app="com.x.Game")
    ck("macOS 支持单窗口画面", not p and t.video.granularity == "window", f"{p}")


def test_macos_no_window_audio() -> None:
    """macOS 没有窗口级音频：必须被拒，不能静默降级。"""
    t, p = resolve(video_app="com.x.Game", audio_mode="window")
    ck("macOS 窗口级音频被拒（措辞为后端未实现）",
       any("本后端未实现" in x for x in p), f"{p}")


def test_macos_cross_app_audio_rejected() -> None:
    """画面 app ≠ 音频 app：一条流做不到，必须明确拒绝。"""
    t, p = resolve(video_app="com.x.Game", audio_app="com.x.Other")
    ck("macOS 跨 app 音源被拒", any("不同 app" in x for x in p), f"{p}")

    t2, p2 = resolve(video_app="com.x.Game", audio_app="com.x.Game")
    ck("macOS 同 app 音源通过", not p2, f"{p2}")


def test_macos_window_video_app_audio_note() -> None:
    """单窗口画面 + app 音频：允许，但必须给出"音频范围更大"的说明。"""
    t, p = resolve(video_app="com.x.Game", video_window="12345", audio_app="com.x.Game")
    ck("单窗画面+app音频通过", not p, f"{p}")
    ck("并给出音频范围说明",
       any("其它窗口" in n for n in t.notes), f"notes={t.notes}")


def test_windows_marked_unverified() -> None:
    """Windows 必须如实标注未实机验证——不能因为代码存在就报 verified。"""
    cap = ct.capability("windows")
    ck("Windows verified_level 不是 recorded", cap.verified_level != "recorded",
       cap.verified_level)
    ck("Windows verified_level 是 none（无实机证据）", cap.verified_level == "none")
    ck("Windows verified_scope 说明未实机", "未" in cap.verified_scope, cap.verified_scope)
    ck("Windows 标注 OBS 后端", "OBS" in cap.backend or "obs" in cap.backend.lower(),
       cap.backend)


def test_macos_verified_level_not_inflated() -> None:
    """macOS 编译通过**不等于**真实录过：不能报 recorded。"""
    cap = ct.capability("macos")
    ck("macOS verified_level 不是 recorded（编译通过≠实录）",
       cap.verified_level != "recorded", cap.verified_level)
    ck("macOS verified_level 合法", cap.verified_level in ct.VERIFIED_LEVELS)
    ck("macOS scope 有出处", bool(cap.verified_scope))
    ck("macOS 明确 audio_per_window_os_capable=False",
       cap.audio_per_window_os_capable is False)


def test_linux_unsupported() -> None:
    """Linux 未实现：必须明确不支持，而不是猜测其能力。"""
    cap = ct.capability("linux")
    ck("Linux 未实现且 verified_level=none",
       cap.verified_level == "none" and cap.video_granularities == [])
    t, p = resolve(platform="linux", video_app="com.x.Game")
    ck("Linux 上请求画面被拒", bool(p), f"{p}")


def test_unknown_platform() -> None:
    cap = ct.capability("plan9")
    ck("未知平台不支持且不猜",
       cap.verified_level == "none" and cap.video_granularities == [])
    ck("未知平台有说明", bool(cap.notes), str(cap.notes))


# --------------------------------------------------------------------------
# 默认与显式语义
# --------------------------------------------------------------------------

def test_audio_defaults_follow_video_app() -> None:
    """未指定音频时默认跟随画面 app（这是安全默认），且不是 system。"""
    t, p = resolve(video_app="com.x.Game")
    ck("音频默认跟随画面 app", t.audio.bundle_id == "com.x.Game"
       and t.audio.granularity == "app", f"{t.audio}")
    ck("默认不是 system 混音", t.audio.granularity != "system")


def test_audio_none_is_expressible() -> None:
    """必须能表达"只要画面不要声音"，而不是被迫收声。"""
    t, p = resolve(video_app="com.x.Game", audio_mode="none")
    ck("--audio-mode none 通过", not p, f"{p}")
    ck("音频粒度为 none", t.audio.granularity == "none")


def test_system_audio_rejected() -> None:
    """整机混音**本后端未实现**：必须拒绝，不能默默收全机声音。"""
    t, p = resolve(video_app="com.x.Game", audio_mode="system")
    ck("system 音频被拒（本后端未实现）",
       any("本后端未实现" in x for x in p), f"{p}")


def test_microphone_default_off() -> None:
    t, _ = resolve(video_app="com.x.Game")
    ck("麦克风默认关闭", t.audio.include_microphone is False)
    t2, p2 = resolve(video_app="com.x.Game", microphone=True)
    ck("显式请求麦克风被拒（本后端未实现）",
       t2.audio.include_microphone is True and any("麦克风" in x for x in p2),
       f"{p2}")


def test_bundle_vs_name_detection() -> None:
    """带点且无空格 → bundle id；否则按 app 名。"""
    t, _ = resolve(video_app="com.x.Game")
    ck("bundle id 识别", t.video.bundle_id == "com.x.Game" and not t.video.app_name)
    t2, _ = resolve(video_app="Slay the Spire 2")
    ck("app 名识别", t2.video.app_name == "Slay the Spire 2" and not t2.video.bundle_id)


def test_serialization_roundtrip() -> None:
    """CaptureTarget 要能进出 JSON（状态文件里存的就是它）。"""
    t, _ = resolve(video_app="com.x.Game", video_window="12345", audio_app="com.x.Game")
    t2 = ct.CaptureTarget.from_json(t.to_json())
    ck("序列化往返一致",
       t2.video.granularity == t.video.granularity
       and t2.video.window_id == t.video.window_id
       and t2.audio.granularity == t.audio.granularity
       and t2.audio_effective_granularity == t.audio_effective_granularity)


def test_detect_platform() -> None:
    p = ct.detect_platform()
    ck("detect_platform 返回已知值", p in ("macos", "windows", "linux", "unknown"), p)



# --------------------------------------------------------------------------
# live 事实校对（rev5：解析成确定身份再比对，不做字符串比较）
# --------------------------------------------------------------------------

def _facts(apps=None, windows=None):
    """构造一份 live 事实。默认：一个游戏 app（pid 4242）带一个窗口。"""
    if apps is None:
        apps = [{"pid": 4242, "bundle_id": "com.x.Game", "name": "Game"},
                {"pid": 777, "bundle_id": "com.x.Other", "name": "Other"}]
    if windows is None:
        windows = [{"windowID": 501, "title": "Game Window", "owner_pid": 4242},
                   {"windowID": 502, "title": "Other Window", "owner_pid": 777}]
    return ct.LiveFacts.from_probe_json({"applications": apps, "windows": windows})


def test_live_window_id_resolves_owner() -> None:
    """纯窗口 ID 也要能反查所属 app（并据此确定音频范围）。"""
    t, _ = resolve(video_app="com.x.Game", video_window="501", audio_app="com.x.Game")
    p = ct.validate_target(t, "macos", _facts())
    ck("纯窗口 ID 能反查 owner 并通过", not p, f"{p}")


def test_live_window_owner_mismatch_rejected() -> None:
    """窗口 id 与声明的 app 不一致 → 拒绝（不能靠裁剪"凑合"）。"""
    t, _ = resolve(video_app="com.x.Other", video_window="501", audio_app="com.x.Other")
    p = ct.validate_target(t, "macos", _facts())
    ck("窗口 id 与声明 app 不一致被拒",
       any("实际所属 app 不一致" in x for x in p), f"{p}")


def test_live_window_missing() -> None:
    t, _ = resolve(video_app="com.x.Game", video_window="9999", audio_app="com.x.Game")
    p = ct.validate_target(t, "macos", _facts())
    ck("不存在的窗口 id 被拒", any("找不到" in x for x in p), f"{p}")


def test_live_offscreen_window_rejected() -> None:
    f = _facts(windows=[{"windowID": 501, "title": "Game Window",
                         "owner_pid": 4242, "on_screen": False}])
    t, _ = resolve(video_app="com.x.Game", video_window="501", audio_app="com.x.Game")
    p = ct.validate_target(t, "macos", f)
    ck("离屏窗口被拒（拿不到帧）", any("不在屏上" in x for x in p), f"{p}")


def test_live_pid_and_bundle_equivalence() -> None:
    """**关键**：同一个 app 用 pid 写和用 bundle 写必须等价，不能因字符串不同就拒绝。"""
    # 画面用 bundle，音频用 pid —— 指向同一个 app
    t = ct.CaptureTarget(platform="macos")
    t.video.granularity = "app"
    t.video.bundle_id = "com.x.Game"
    t.audio.granularity = "app"
    t.audio.pid = 4242
    p = ct.validate_target(t, "macos", _facts())
    ck("画面用 bundle、音频用 pid（同一 app）不被误拒", not p, f"{p}")

    # 反过来：画面用 pid，音频用 bundle
    t2 = ct.CaptureTarget(platform="macos")
    t2.video.granularity = "app"
    t2.video.pid = 4242
    t2.audio.granularity = "app"
    t2.audio.bundle_id = "com.x.Game"
    p2 = ct.validate_target(t2, "macos", _facts())
    ck("画面用 pid、音频用 bundle（同一 app）不被误拒", not p2, f"{p2}")


def test_live_contradictory_selectors_rejected() -> None:
    """pid 指向 A、bundle 指向 B → 必须拒绝，不能"优先 pid 忽略 bundle"。"""
    t = ct.CaptureTarget(platform="macos")
    t.video.granularity = "app"
    t.video.pid = 4242            # Game
    t.video.bundle_id = "com.x.Other"   # Other —— 矛盾
    t.audio.granularity = "app"
    t.audio.pid = 4242
    p = ct.validate_target(t, "macos", _facts())
    ck("互相矛盾的画面选择器被拒",
       any("互相矛盾" in x for x in p), f"{p}")


def test_live_cross_app_audio_rejected_by_identity() -> None:
    """真正的跨 app（pid 4242 vs pid 777）必须被拒。"""
    t = ct.CaptureTarget(platform="macos")
    t.video.granularity = "app"
    t.video.pid = 4242
    t.audio.granularity = "app"
    t.audio.pid = 777
    p = ct.validate_target(t, "macos", _facts())
    ck("跨 app 音源按解析后的身份被拒",
       any("画面 app 与音频 app 不一致" in x for x in p), f"{p}")


def test_live_unknown_app_rejected() -> None:
    t = ct.CaptureTarget(platform="macos")
    t.video.granularity = "app"
    t.video.bundle_id = "com.x.Ghost"
    t.audio.granularity = "app"
    t.audio.bundle_id = "com.x.Ghost"
    p = ct.validate_target(t, "macos", _facts())
    ck("live 列表里没有的 app 被拒（不猜）",
       any("找不到" in x for x in p), f"{p}")


def test_live_title_ambiguous_rejected() -> None:
    f = _facts(windows=[{"windowID": 601, "title": "Same", "owner_pid": 4242},
                        {"windowID": 602, "title": "Same", "owner_pid": 4242}])
    t = ct.CaptureTarget(platform="macos")
    t.video.granularity = "window"
    t.video.window_title = "Same"
    t.video.bundle_id = "com.x.Game"
    t.audio.granularity = "app"
    t.audio.bundle_id = "com.x.Game"
    p = ct.validate_target(t, "macos", f)
    ck("同名窗口被拒（不能随便挑一个）", any("命中 2 个窗口" in x for x in p), f"{p}")


# --------------------------------------------------------------------------
# 未实现的能力必须拒绝（rev5：不假支持）
# --------------------------------------------------------------------------

def test_unimplemented_granularities_rejected() -> None:
    """display / system 音频 / 麦克风都**未实现**：必须拒绝，不能静默降级。"""
    t, p = resolve(display=True)
    ck("display 未实现被拒", any("本后端未实现" in x for x in p), f"{p}")

    t2, p2 = resolve(video_app="com.x.Game", audio_mode="system")
    ck("system 音频未实现被拒", any("本后端未实现" in x for x in p2), f"{p2}")

    t3, p3 = resolve(video_app="com.x.Game", microphone=True)
    ck("麦克风未实现被拒", any("本后端未实现" in x for x in p3), f"{p3}")


def test_wording_says_backend_not_os() -> None:
    """措辞必须是"本后端未实现"，不能用局部实现断言 OS 能力。"""
    t, p = resolve(video_app="com.x.Game", audio_mode="window")
    joined = " ".join(p)
    ck("措辞用「本后端未实现」", "本后端未实现" in joined, joined)
    ck("不写「平台不支持」", "平台" not in joined or "不支持" not in joined, joined)


def test_invalid_audio_mode_rejected() -> None:
    ck("非法 audio-mode 被拒", ct.validate_audio_mode("bogus") is not None)
    ck("合法 audio-mode 通过",
       all(ct.validate_audio_mode(m) is None
           for m in ("", "none", "app", "window", "system")))
    # 未知模式在库层面也必须炸，不能静默落回默认（回归：曾静默变成 app 级）
    try:
        ct.resolve_target("macos", video_app="com.x.Game", audio_mode="bogus")
        ck("未知 audio_mode 在 resolve 层抛错（不静默降级）", False, "没抛")
    except ct.UsageError:
        ck("未知 audio_mode 在 resolve 层抛错（不静默降级）", True)


def test_window_audio_mode_not_silently_downgraded() -> None:
    """`--audio-mode window` 必须如实报"未实现"，**不能**悄悄变成 app 级。"""
    t, p = resolve(video_app="com.x.Game", audio_mode="window")
    ck("请求 window 音频时粒度如实记为 window", t.audio.granularity == "window",
       f"granularity={t.audio.granularity!r}")
    ck("并被判为未实现", any("本后端未实现" in x for x in p), f"{p}")


def test_windows_exe_not_treated_as_bundle() -> None:
    """Windows 的 game.exe 不能被套 macOS 的 bundle id 规则。"""
    t, p = resolve(platform="windows", video_app="game.exe")
    ck("Windows .exe 按 app 名处理（不是 bundle_id）",
       t.video.app_name == "game.exe" and not t.video.bundle_id,
       f"app_name={t.video.app_name!r} bundle={t.video.bundle_id!r}")
    ck("Windows .exe 目标通过基本校验", not p, f"{p}")


def test_negative_ids_rejected() -> None:
    t = ct.CaptureTarget(platform="macos")
    t.video.granularity = "app"
    t.video.pid = -5
    p = ct.validate_target(t, "macos")
    ck("负 pid 被拒", any("不能为负" in x for x in p), f"{p}")

    t2 = ct.CaptureTarget(platform="macos")
    t2.video.granularity = "window"
    t2.video.window_id = -1
    t2.video.bundle_id = "com.x.Game"
    p2 = ct.validate_target(t2, "macos")
    ck("负 window id 被拒", any("不能为负" in x for x in p2), f"{p2}")


def test_verified_levels_documented() -> None:
    """每个后端都要有合法的 verified_level，且只有 recorded 才算验证过。"""
    for plat in ("macos", "windows", "linux"):
        cap = ct.capability(plat)
        ck(f"[{plat}] verified_level 合法", cap.verified_level in ct.VERIFIED_LEVELS,
           cap.verified_level)
        ck(f"[{plat}] scope 非空", bool(cap.verified_scope))
    ck("recorded 是唯一的「已验证」等级", ct.VERIFIED_LEVELS[0] == "recorded")


def main() -> int:
    print("═══ capture_targets 离线测试 ═══")
    test_no_default_target()
    test_display_is_explicit_not_fallback()
    test_window_requires_selector()
    test_window_title_needs_app_scope()
    test_macos_window_video_supported()
    test_macos_no_window_audio()
    test_macos_cross_app_audio_rejected()
    test_macos_window_video_app_audio_note()
    test_windows_marked_unverified()
    test_macos_verified_level_not_inflated()
    test_linux_unsupported()
    test_unknown_platform()
    test_audio_defaults_follow_video_app()
    test_audio_none_is_expressible()
    test_system_audio_rejected()
    test_microphone_default_off()
    test_bundle_vs_name_detection()
    test_serialization_roundtrip()
    test_detect_platform()
    test_live_window_id_resolves_owner()
    test_live_window_owner_mismatch_rejected()
    test_live_window_missing()
    test_live_offscreen_window_rejected()
    test_live_pid_and_bundle_equivalence()
    test_live_contradictory_selectors_rejected()
    test_live_cross_app_audio_rejected_by_identity()
    test_live_unknown_app_rejected()
    test_live_title_ambiguous_rejected()
    test_unimplemented_granularities_rejected()
    test_wording_says_backend_not_os()
    test_invalid_audio_mode_rejected()
    test_window_audio_mode_not_silently_downgraded()
    test_windows_exe_not_treated_as_bundle()
    test_negative_ids_rejected()
    test_verified_levels_documented()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
