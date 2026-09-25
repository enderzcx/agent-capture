#!/usr/bin/env python3
"""Windows 统一 CLI 的**假 transport 合同测试**（无实机）。

Cloud 复审点：`agent_capture.py` 的 Windows 路径原先落回 generic worker
（"起一个录制器子进程"），那会丢掉 Windows 后端真正有价值的东西 ——
专用 profile/scene 隔离、启动前原子预约 run、实例锁、**owner-only stop**。
现在改为薄分派到模块的 `run()`。

本测试用**假 transport** 覆盖合同，不需要 Windows、不需要 OBS：

  1. 缺 OBS / 不在 Windows → **正常 unavailable**，不是 NotImplemented 堆栈
  2. `--windows-obs` 的 start/status/stop 分派到模块的 run(command, config)
  3. stop 必须带 start 返回的 `session_token`（owner-only）
  4. 不把 `picture_verified` / `verified_level` 升级成 pass
  5. `build_start_argv` 明确拒绝（generic worker 不该驱动 Windows 生命周期）

**没有实机 ⇒ 一律 experimental**：本测试不证明能录到画面或声音。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

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


def cli(*args, env=None, timeout=90):
    e = dict(os.environ)
    if env:
        e.update(env)
    return subprocess.run([sys.executable, str(SCRIPTS / "agent_capture.py"), *args],
                          capture_output=True, text=True, timeout=timeout, env=e)


def jload(r):
    try:
        return json.loads(r.stdout)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 1. 不可用是正常状态
# ---------------------------------------------------------------------------

def test_unavailable_not_notimplemented():
    for cmd in ("preflight", "status", "stop"):
        args = [cmd, "--windows-obs"]
        if cmd == "preflight":
            args += ["--platform", "windows", "--video-app", "Game.exe"]
        r = cli(*args)
        d = jload(r)
        ck(f"{cmd}: 返回结构化 JSON（不崩）", d is not None, (r.stdout or r.stderr)[:120])
        if d is None:
            continue
        ck(f"{cmd}: available=false 且 ok=false",
           d.get("available") is False and d.get("ok") is False, str(d)[:150])
        ck(f"{cmd}: 明确说明是正常不可用",
           "正常状态" in (d.get("note") or ""), str(d.get("note"))[:80])
        ck(f"{cmd}: **没有** NotImplemented",
           "NotImplemented" not in json.dumps(d, ensure_ascii=False))
        ck(f"{cmd}: 退出码 2", r.returncode == 2, f"rc={r.returncode}")
        ck(f"{cmd}: 不声称已验证", d.get("verified_level") == "none",
           str(d.get("verified_level")))


# ---------------------------------------------------------------------------
# 2/3. 分派到模块 run(command, config) —— 用假 transport
# ---------------------------------------------------------------------------
FAKE_MOD = '''
"""假 agent_capture_win：记录收到的 command/config，返回脚本化结果。"""
import json, os
from pathlib import Path

CALLS = Path(os.environ["FAKE_CALLS"])

def _log(entry):
    with CALLS.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\\n")

def run(command, config):
    _log({"command": command, "config": config})
    if command == "start":
        return {"ok": True, "recording": {"evidence": "output_bytes_advanced",
                "picture_verified": False, "artifact_verified": False,
                "evidence_scope": "output_write_progress_only__not_picture_proof"},
                "ownership": {"session_token": "tok-abc123"},
                "verification_level": "live_connected"}
    if command == "status":
        if config.get("session_token") != "tok-abc123":
            return {"ok": False, "error": {"category": "NOT_OWNER"},
                    "message": "session token required"}
        return {"ok": True, "recording": {"advanced": True, "picture_verified": False}}
    if command == "stop":
        if config.get("session_token") != "tok-abc123":
            return {"ok": False, "error": {"category": "NOT_OWNER"},
                    "message": "owner-only stop: session token required"}
        return {"ok": True, "output": {"path": "D:\\\\rec\\\\x.mkv", "verified": True},
                "artifact_verified": True, "picture_verified": False}
    if command == "preflight":
        return {"ok": True, "read_only": True, "target_match": "exact"}
    return {"ok": False, "error": {"category": "UNKNOWN"}}
'''


def test_dispatch_to_module_run():
    tmp = Path(tempfile.mkdtemp(prefix="winfake-"))
    fake = tmp / "agent_capture_win.py"
    fake.write_text(FAKE_MOD, encoding="utf-8")
    calls = tmp / "calls.jsonl"
    env = {"FAKE_CALLS": str(calls),
           # 仅用于假 transport 合同测试：让可用性判定走 Windows 分支。
           # 它不会让本机真的能采集 —— 那取决于 OBS 与模块 transport。
           "AGENT_CAPTURE_TEST_PLATFORM": "win32",
           "AGENT_CAPTURE_BACKEND_DIR": str(ROOT / "tools" / "windows"),
           "PYTHONPATH": f"{tmp}{os.pathsep}" + os.environ.get("PYTHONPATH", "")}

    # start → 应分派到 run('start', ...) 并带回 session_token
    # 注意：Windows 上**不传 --out**（落点由 --obs-record-dir 决定；传 --out 会被明确拒绝）
    r = cli("start", "--platform", "windows", "--windows-obs",
            "--video-window", "Game Window", "--video-app", "Game.exe", "--run-id", "r1",
            "--obs-record-dir", "D:\\rec", env=env)
    d = jload(r)
    if d is None:
        skip("Windows 分派合同", f"假模块未生效：{(r.stdout or r.stderr)[:120]}")
        return
    ck("start 分派到模块的 run('start')",
       calls.exists() and any(json.loads(l)["command"] == "start"
                              for l in calls.read_text().splitlines() if l.strip()),
       (r.stdout or r.stderr)[:140])
    tok = ((d.get("ownership") or {}).get("session_token"))
    ck("start 带回 owner session_token", tok == "tok-abc123", str(tok))

    # status 不带 token → 模块拒绝
    r2 = cli("status", "--windows-obs", env=env)
    d2 = jload(r2)
    ck("status 不带 token 被模块拒（NOT_OWNER）",
       d2 is not None and (d2.get("error") or {}).get("category") == "NOT_OWNER",
       str(d2)[:140])

    # status 带 token → 放行，但仍不声称画面已验证
    r3 = cli("status", "--windows-obs", "--session-token", "tok-abc123", env=env)
    d3 = jload(r3)
    ck("status 带 token 放行", d3 is not None and d3.get("ok") is True, str(d3)[:140])
    ck("status 仍不声称画面已验证",
       (d3 or {}).get("recording", {}).get("picture_verified") is False)

    # stop 不带 token → 拒
    r4 = cli("stop", "--windows-obs", env=env)
    d4 = jload(r4)
    ck("stop 不带 token 被拒（owner-only）",
       d4 is not None and (d4.get("error") or {}).get("category") == "NOT_OWNER",
       str(d4)[:140])

    # stop 带 token → 放行
    r5 = cli("stop", "--windows-obs", "--session-token", "tok-abc123", env=env)
    d5 = jload(r5)
    ck("stop 带 token 放行", d5 is not None and d5.get("ok") is True, str(d5)[:140])
    ck("stop 不把 picture_verified 升级成 true",
       (d5 or {}).get("picture_verified") is False, str((d5 or {}).get("picture_verified")))

    # 合同：模块收到的 config 里**不带明文密码**，且 owner token 原样传回
    entries = ([json.loads(l) for l in calls.read_text().splitlines() if l.strip()]
               if calls.exists() else [])
    joined = json.dumps(entries, ensure_ascii=False)
    ck("config 里不含明文密码字段", '"password"' not in joined, joined[:200])
    # 真实断言：stop 那次调用必须把 session_token 带给模块
    stop_calls = [e for e in entries if e["command"] == "stop"]
    ck("stop 调用确实把 session_token 传给了模块",
       bool(stop_calls) and any(e["config"].get("session_token") == "tok-abc123"
                                for e in stop_calls),
       json.dumps([e["config"] for e in stop_calls], ensure_ascii=False)[:200])
    # 真实断言：--out 这类没有语义的参数**不得**出现在 config 里（我们改成拒绝了）
    ck("config 里不出现无语义的 out/job_dir 键",
       all(k not in e["config"] for e in entries for k in ("out", "job_dir")),
       json.dumps(entries, ensure_ascii=False)[:200])


# ---------------------------------------------------------------------------
# 4/5. 边界：generic worker 不该驱动 Windows 生命周期
# ---------------------------------------------------------------------------

def test_param_mapping_and_rejection():
    """按**真实模块**的 config 契约核对参数：支持的映射，不支持的直接拒绝。

    Cloud 复审点：早期薄分派把 --out/--duration/--job-dir 默默丢掉，
    于是"用户以为录 10 秒到 A，实际 OBS 一直录到 B"。
    """
    tmp = Path(tempfile.mkdtemp(prefix="winparam-"))
    fake = tmp / "agent_capture_win.py"
    fake.write_text(FAKE_MOD, encoding="utf-8")
    calls = tmp / "calls.jsonl"
    env = {"FAKE_CALLS": str(calls), "AGENT_CAPTURE_TEST_PLATFORM": "win32",
           "AGENT_CAPTURE_BACKEND_DIR": str(ROOT / "tools" / "windows"),
           "PYTHONPATH": f"{tmp}{os.pathsep}" + os.environ.get("PYTHONPATH", "")}

    # --out 必须被**明确拒绝**（不是静默忽略）
    r = cli("start", "--platform", "windows", "--windows-obs",
            "--video-window", "Game Window", "--video-app", "Game.exe",
            "--out", str(tmp / "x.mkv"), "--run-id", "r1",
            "--obs-record-dir", "D:\\rec", env=env)
    d = jload(r)
    ck("--out 被明确拒绝（不静默忽略）",
       d is not None and any("--out" in x for x in (d.get("usage_errors") or [])),
       str(d)[:200])
    ck("--out 被拒时**不**调用模块的 start",
       not calls.exists() or not any(json.loads(l)["command"] == "start"
                                     for l in calls.read_text().splitlines() if l.strip()))

    # --duration NaN 必须被拒（不跳过 finite 校验）
    r2 = cli("start", "--platform", "windows", "--windows-obs",
             "--video-window", "Game Window", "--video-app", "Game.exe",
             "--duration", "nan", "--run-id", "r1", "--obs-record-dir", "D:\\rec", env=env)
    d2 = jload(r2)
    ck("--duration NaN 被拒", d2 is not None and bool(d2.get("usage_errors")), str(d2)[:160])

    # --no-video 必须被拒
    r3 = cli("start", "--platform", "windows", "--windows-obs",
             "--video-window", "Game Window", "--video-app", "Game.exe",
             "--no-video", "--run-id", "r1", "--obs-record-dir", "D:\\rec", env=env)
    d3 = jload(r3)
    ck("--no-video 被拒", d3 is not None and bool(d3.get("usage_errors")), str(d3)[:160])

    # 合法调用：duration 映射到 max_record_seconds，capture_audio 为布尔
    if calls.exists():
        calls.unlink()
    r4 = cli("start", "--platform", "windows", "--windows-obs",
             "--video-window", "Game Window", "--video-app", "Game.exe",
             "--duration", "10", "--run-id", "r1", "--obs-record-dir", "D:\\rec",
             "--audio-mode", "none", env=env)
    d4 = jload(r4)
    if d4 is not None and d4.get("ok") is not False:
        entries = ([json.loads(l) for l in calls.read_text().splitlines() if l.strip()]
                   if calls.exists() else [])
        st = [e for e in entries if e["command"] == "start"]
        ck("--duration 映射到 max_record_seconds",
           bool(st) and st[0]["config"].get("max_record_seconds") == 10.0,
           json.dumps([e["config"] for e in st], ensure_ascii=False)[:200])
        ck("--obs-record-dir 映射到 record_dir",
           bool(st) and st[0]["config"].get("record_dir", "").endswith("rec"),
           json.dumps([e["config"] for e in st], ensure_ascii=False)[:200])
        ck("capture_audio 是布尔且 audio-mode none → false",
           bool(st) and st[0]["config"].get("capture_audio") is False,
           json.dumps([e["config"] for e in st], ensure_ascii=False)[:200])


def test_real_module_config_contract():
    """**真实模块**（不是假模块）的 config 契约：未知键必须 CONFIG_INVALID。

    这条保证薄分派不会"送进去一个模块不认识的键，然后被默默忽略或在别处炸掉"。
    """
    moddir = ROOT / "tools" / "windows"
    module_py = moddir / "src" / "agent_capture_win" / "backend.py"
    if not module_py.exists():
        skip("真实模块 config 契约", "未找到 tools/windows/src/agent_capture_win/backend.py")
        return
    text = module_py.read_text(encoding="utf-8")
    for key in ("max_record_seconds", "record_dir", "capture_audio", "password_env",
                "run_id", "target"):
        ck(f"真实模块契约包含 {key}", key in text, "未在 schema 中找到该键")
    ck("真实模块对未知键 fail-closed（CONFIG_INVALID）",
       "CONFIG_INVALID" in text)
    # 我传的键必须都在模块白名单里（否则会被 CONFIG_INVALID 拒）
    cli_src = (SCRIPTS / "agent_capture.py").read_text(encoding="utf-8")
    for bad in ('cfg["out"]', 'cfg["duration"]', 'cfg["job_dir"]'):
        ck(f"薄分派不向模块传无意义的 {bad}", bad not in cli_src)


def test_generic_worker_not_used_for_windows():
    src = (SCRIPTS / "agent_capture.py").read_text(encoding="utf-8")
    ck("CLI 在 --windows-obs 时**提前分派**，不落到 generic worker",
       "_windows_lifecycle" in src and src.count("_windows_lifecycle(") >= 4)
    ck("分派用到模块的 run_command（薄适配）",
       "b.run_command(command, cfg)" in src or "run_command(command, cfg)" in src)
    wb = (ROOT / "tools" / "windows" / "backend.py").read_text(encoding="utf-8")
    ck("桥接的 build_start_argv 仍明确拒绝（不伪装子进程）",
       "NotImplementedError" in wb)


def main() -> int:
    print("═══ Windows 统一 CLI 假 transport 合同测试 ═══")
    test_unavailable_not_notimplemented()
    test_dispatch_to_module_run()
    test_param_mapping_and_rejection()
    test_real_module_config_contract()
    test_generic_worker_not_used_for_windows()
    print(f"\n{PASS} passed, {FAIL} failed, {SKIP} skipped")
    print("注意：本测试**不**证明 Windows 能录到画面或声音（无实机）。")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
