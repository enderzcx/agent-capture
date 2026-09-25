#!/usr/bin/env python3
"""capture_state 的对抗性测试。

这些用例不是"跑一遍看有没有报错"，而是**针对已知失败模式的反例**：
并发 init/update、路径逃逸、字段绕过、损坏锁、pid 复用、NaN。

运行：  python3 tests/test_capture_state.py
退出码：0 = 全过；1 = 有失败。
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import capture_state as cs  # noqa: E402

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


def expect_raises(name: str, exc, fn, *a, **kw) -> None:
    try:
        fn(*a, **kw)
    except exc:
        ck(name, True)
    except Exception as e:  # 抛了别的异常也是失败：说明拦错了地方
        ck(name, False, f"期望 {exc.__name__}，实际 {type(e).__name__}: {e}")
    else:
        ck(name, False, f"期望抛 {exc.__name__}，但没抛")


def newjob() -> Path:
    return Path(tempfile.mkdtemp(prefix="capstate-test."))


# --------------------------------------------------------------------------
# 并发 worker（必须是真进程，线程测不出 TOCTOU）
# --------------------------------------------------------------------------

def _w_init(job_dir: str, run_id: str, barrier, results) -> None:
    import capture_state as c
    barrier.wait()
    try:
        c.init_run(Path(job_dir), run_id, "test-1.0")
        results.put(("ok", os.getpid()))
    except c.StateError as e:
        results.put(("err", str(e)[:80]))
    except Exception as e:
        results.put(("boom", f"{type(e).__name__}: {e}"))


def _w_hold_lock(job_dir: str, run_id: str, seconds: float, ready=None) -> None:
    """持有锁并**明确告知父进程已持有**。

    用固定 sleep 等子进程取锁是不可靠的：spawn 一个子进程要重新 import 模块，
    慢的时候 1 秒都还没拿到锁，父进程就会以为"锁没挡住"——那是测试的假失败。
    """
    import capture_state as c
    with c.local_lock(Path(job_dir), c.lock_name(run_id), timeout=10.0):
        if ready is not None:
            ready.put("locked")
        time.sleep(seconds)


def _w_init_and_report(job_dir: str, run_id: str, barrier, results) -> None:
    import capture_state as c
    barrier.wait()
    try:
        c.init_run(Path(job_dir), run_id, "test-1.0")
        results.put(("ok", os.getpid()))
    except c.StateError as e:
        results.put(("err", str(e)[:80]))
    except Exception as e:
        results.put(("boom", f"{type(e).__name__}: {e}"))


def _w_update(job_dir: str, run_id: str, to: str, barrier, results) -> None:
    """先读、再在 barrier 处集合、然后一起抢着写。

    顺序很关键：如果先 wait 再读，先跑完的那个已经写了新 revision，
    后读的进程会读到**新** revision 并因此通过 CAS —— 那测的就不是 CAS，
    而是状态机合法性了。必须先读，让所有进程拿着**同一个** revision 去竞争。
    """
    import capture_state as c
    try:
        st = c.load_run(Path(job_dir), run_id)
    except Exception as e:
        results.put(("boom", f"load: {type(e).__name__}: {e}"))
        barrier.wait()
        return
    barrier.wait()
    try:
        c.transition(Path(job_dir), run_id, to, expect_revision=st.revision)
        results.put(("ok", os.getpid()))
    except c.RevisionMismatch:
        results.put(("cas", "revision mismatch"))
    except c.StateError as e:
        results.put(("err", str(e)[:80]))
    except Exception as e:
        results.put(("boom", f"{type(e).__name__}: {e}"))


def test_concurrent_init() -> None:
    """并发 init 只能有一个赢家，且不能覆盖别人的内容。"""
    d = newjob()
    n = 8
    barrier = mp.Barrier(n)
    results = mp.Queue()
    procs = [mp.Process(target=_w_init, args=(str(d), "race", barrier, results))
             for _ in range(n)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)
    out = [results.get(timeout=5) for _ in range(n)]
    wins = [o for o in out if o[0] == "ok"]
    ck("并发 init：恰好 1 个成功", len(wins) == 1, f"实际 {len(wins)} 个成功: {out}")
    # 赢家的内容必须是完整合法的（没被后来者写坏）
    try:
        st = cs.load_run(d, "race")
        ck("并发 init：赢家状态完整可读", st.status == "init" and st.revision == 1,
           f"status={st.status} rev={st.revision}")
    except Exception as e:
        ck("并发 init：赢家状态完整可读", False, str(e))
    shutil.rmtree(d, ignore_errors=True)


def test_concurrent_update_cas() -> None:
    """并发 update 走 CAS：只有一个能推进，其余拿到 RevisionMismatch。"""
    d = newjob()
    cs.init_run(d, "cas", "test-1.0")
    cs.transition(d, "cas", "preflight")
    n = 6
    barrier = mp.Barrier(n)
    results = mp.Queue()
    procs = [mp.Process(target=_w_update, args=(str(d), "cas", "starting", barrier, results))
             for _ in range(n)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)
    out = [results.get(timeout=5) for _ in range(n)]
    oks = [o for o in out if o[0] == "ok"]
    cas = [o for o in out if o[0] == "cas"]
    ck("并发 update：恰好 1 个成功", len(oks) == 1, f"ok={len(oks)} cas={len(cas)} out={out}")
    ck("并发 update：其余被 CAS 拦下", len(cas) == n - 1, f"cas={len(cas)}")
    st = cs.load_run(d, "cas")
    ck("并发 update：最终 revision 只 +1", st.revision == 3, f"rev={st.revision}")
    shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------------
# 路径逃逸
# --------------------------------------------------------------------------

def test_run_id_escape() -> None:
    d = newjob()
    for bad in ["../escape", "..", ".", "a/b", "a\\b", "", "x" * 65, "has space",
                ".hidden", "-lead", "a\x00b"]:
        expect_raises(f"run_id 逃逸被拒: {bad!r}", cs.BadRunId,
                      cs.init_run, d, bad, "v")
    # 确认没有在 job 目录外留下东西
    outside = d.parent / "escape.run.json"
    ck("路径逃逸：目录外无残留", not outside.exists())
    shutil.rmtree(d, ignore_errors=True)


def test_symlink_state_rejected() -> None:
    """状态文件是 symlink 时必须拒绝写（否则可被诱导覆盖任意文件）。"""
    d = newjob()
    victim = d / "victim.txt"
    victim.write_text("original")
    link = d / "evil.run.json"
    link.symlink_to(victim)
    expect_raises("symlink 状态文件：init 拒绝", cs.StateError,
                  cs.init_run, d, "evil", "v")
    ck("symlink 状态文件：受害者未被改写", victim.read_text() == "original")
    shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------------
# 字段绕过
# --------------------------------------------------------------------------

def test_field_bypass() -> None:
    d = newjob()
    cs.init_run(d, "f", "v")
    cs.transition(d, "f", "preflight")

    # 生命周期字段不能由调用方写
    for k, v in [("status", "verified"), ("run_id", "other"),
                 ("schema_version", 99), ("history", []), ("revision", 999)]:
        expect_raises(f"--set {k} 被拒（受保护）", cs.StateError,
                      cs.transition, d, "f", "starting", "", None, False, **{k: v})
    # 未知字段
    expect_raises("--set 未知字段被拒", cs.StateError,
                  cs.transition, d, "f", "starting", "", None, False, bogus=1)
    # 类型错误
    expect_raises("--set stages 非对象被拒", cs.StateError,
                  cs.transition, d, "f", "starting", "", None, False, stages=[1, 2])
    expect_raises("--set notes 非数组被拒", cs.StateError,
                  cs.transition, d, "f", "starting", "", None, False, notes="x")
    # NaN / Inf
    expect_raises("--set 含 NaN 被拒", cs.StateError,
                  cs.transition, d, "f", "starting", "", None, False,
                  artifacts={"peak": float("nan")})
    expect_raises("--set 含 Inf 被拒", cs.StateError,
                  cs.transition, d, "f", "starting", "", None, False,
                  artifacts={"peak": float("inf")})

    # 状态没被这些失败改动
    st = cs.load_run(d, "f")
    ck("字段绕过：失败尝试未改变状态", st.status == "preflight" and st.revision == 2,
       f"status={st.status} rev={st.revision}")
    shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------------
# 锁语义
# --------------------------------------------------------------------------

def test_lock_never_removed_or_stolen() -> None:
    """锁是内核持有的：**永不删除、永不改名**，也不做"死锁回收"。

    旧实现用 pid 判断持有者死活并 os.replace 走旧锁 —— 两个回收者会互相打架，
    第二个会把第一个刚建好的新锁搬走。现在没有回收动作，也就没有这个竞争。
    """
    d = newjob()
    cs.init_run(d, "lk", "v")
    lp = cs.lock_path(d, "lk")
    ck("锁文件在 init 后存在", lp.exists())

    # 损坏的锁内容**不影响**加锁（内核锁不看内容），也不会被删掉
    lp.write_text("{ this is not json")
    try:
        with cs.local_lock(d, cs.lock_name("lk"), timeout=1.0):
            pass
        ck("锁内容损坏不影响加锁（内核锁不看内容）", True)
    except cs.StateError as e:
        ck("锁内容损坏不影响加锁（内核锁不看内容）", False, str(e))
    ck("加锁后锁文件仍在（从不删除）", lp.exists())
    ck("且没有被改名成 .dead.*", not list(d.glob("lk.lock.dead.*")))
    shutil.rmtree(d, ignore_errors=True)


def test_live_lock_blocks_and_releases() -> None:
    """活着的持有者必须真的挡住别人；持有者退出后内核自动释放。"""
    d = newjob()
    cs.init_run(d, "live", "v")
    with cs.local_lock(d, cs.lock_name("live"), timeout=1.0):
        # 同进程的第二个 open+flock 也会冲突 —— 这正是"持锁时不能再 load_run"的原因
        expect_raises("持锁期间再取锁被挡（LockBusy）", cs.LockBusy,
                      lambda: cs.local_lock(d, cs.lock_name("live"), timeout=0.2).__enter__())
    # 退出 with 之后可以再取
    with cs.local_lock(d, cs.lock_name("live"), timeout=1.0):
        ck("释放后可再次加锁", True)
    shutil.rmtree(d, ignore_errors=True)


def test_lock_busy_is_error_not_silent() -> None:
    """拿不到锁必须是**显式失败**，不能降级继续写。"""
    d = newjob()
    cs.init_run(d, "busy", "v")
    ready = mp.Queue()
    holder = mp.Process(target=_w_hold_lock, args=(str(d), "busy", 8.0, ready))
    holder.start()
    ck("子进程已确认持有锁", ready.get(timeout=20) == "locked")
    try:
        # 给一个**小于**持有时间的等锁上限：必须快速失败，而不是等满默认 10 秒
        t0 = time.time()
        expect_raises("别的进程持锁时 transition 抛 LockBusy", cs.LockBusy,
                      cs.transition, d, "busy", "preflight", "", None, 0.4)
        ck("LockBusy 是快速失败（< 3s）", time.time() - t0 < 3.0,
           f"耗时 {time.time()-t0:.1f}s")
        # 状态必须没被动过
        with cs.local_lock(d, cs.lock_name("busy"), timeout=8.0):
            st = cs.load_run(d, "busy", _locked=True)
            ck("被锁挡住时状态未被改动", st.status == "init" and st.revision == 1,
               f"status={st.status} rev={st.revision}")
    finally:
        holder.join(timeout=15)
        if holder.is_alive():
            holder.terminate()
    shutil.rmtree(d, ignore_errors=True)


def test_lock_released_on_crash() -> None:
    """持有者被 SIGKILL 后，内核自动释放锁 —— 不需要任何"回收逻辑"。"""
    d = newjob()
    cs.init_run(d, "crash", "v")
    ready = mp.Queue()
    holder = mp.Process(target=_w_hold_lock, args=(str(d), "crash", 60.0, ready))
    holder.start()
    ck("子进程已确认持有锁（崩溃测试）", ready.get(timeout=20) == "locked")
    expect_raises("崩溃前确实持锁", cs.LockBusy,
                  lambda: cs.local_lock(d, cs.lock_name("crash"), timeout=0.2).__enter__())
    holder.terminate()   # SIGTERM
    holder.join(timeout=10)
    if holder.is_alive():
        holder.kill()
        holder.join(timeout=10)
    # 内核已释放：立刻可以加锁，且不需要删任何文件
    with cs.local_lock(d, cs.lock_name("crash"), timeout=5.0):
        ck("持有者死后内核自动释放锁（无需回收逻辑）", True)
    ck("全程没有产生 .dead 锁文件", not list(d.glob("*.dead.*")))
    shutil.rmtree(d, ignore_errors=True)


def test_reader_never_sees_partial_json() -> None:
    """**关键回归**：并发 init 时，读者绝不能读到空文件或半截 JSON。

    旧实现先 O_EXCL 建空文件再写内容，中间有一个窗口读者会看到 `""` 或半截。
    现在用 os.link 发布：目标名只在内容完整落盘之后才出现。
    """
    d = newjob()
    n_writers = 6
    barrier = mp.Barrier(n_writers)
    results = mp.Queue()
    procs = [mp.Process(target=_w_init_and_report, args=(str(d), "partial", barrier, results))
             for _ in range(n_writers)]
    for p in procs:
        p.start()

    # 读者循环：在写者并发期间不断尝试读，记录任何一次"文件存在但内容不完整"
    bad = []
    deadline = time.time() + 6.0
    seen_ok = 0
    while time.time() < deadline:
        try:
            st = cs.load_run(d, "partial")
            seen_ok += 1
            if st.run_id != "partial" or st.revision != 1:
                bad.append(f"内容异常: run_id={st.run_id} rev={st.revision}")
        except cs.StateError as e:
            msg = str(e)
            if "不存在" in msg:
                pass          # 还没发布，正常
            else:
                bad.append(msg)   # 存在但读不出来 = 半截文件
        except cs.LockBusy:
            pass              # 写者持锁中，正常
        if any(not p.is_alive() for p in procs) and seen_ok > 0:
            break
        time.sleep(0.002)

    for p in procs:
        p.join(timeout=30)
    ck("并发 init 期间读者从未读到半截 JSON", not bad, f"{bad[:3]}")
    ck("并发 init 期间确实读到过完整状态", seen_ok > 0, f"seen_ok={seen_ok}")
    outs = [results.get(timeout=5) for _ in range(n_writers)]
    ck("并发 init 仍然恰好 1 个赢家",
       len([o for o in outs if o[0] == "ok"]) == 1, f"{outs}")
    shutil.rmtree(d, ignore_errors=True)


def test_symlink_lock_rejected() -> None:
    d = newjob()
    cs.init_run(d, "sl", "v")
    lp = cs.lock_path(d, "sl")
    lp.unlink()  # 换成 symlink：必须被拒
    victim = d / "victim.txt"
    victim.write_text("original")
    lp.symlink_to(victim)
    # local_lock 是 contextmanager：必须真的进去才会执行函数体
    def _enter():
        with cs.local_lock(d, cs.lock_name("sl")):
            pass
    expect_raises("symlink 锁文件被拒", cs.StateError, _enter)
    ck("symlink 锁：受害者未被改写", victim.read_text() == "original")
    shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------------
# 状态机
# --------------------------------------------------------------------------

def test_transitions() -> None:
    d = newjob()
    cs.init_run(d, "sm", "v")
    # 合法链
    for to in ["preflight", "starting", "running", "stopping", "stopped",
               "verifying", "verified", "done"]:
        cs.transition(d, "sm", to)
    ck("合法状态链可以走完", cs.load_run(d, "sm").status == "done")

    # 跳阶段被拒
    d2 = newjob()
    cs.init_run(d2, "jump", "v")
    expect_raises("init→running 跳阶段被拒", cs.StateError,
                  cs.transition, d2, "jump", "running")
    # 未知状态
    expect_raises("未知状态被拒", cs.StateError, cs.transition, d2, "jump", "bogus")
    # 终态不可再动：先把 d2 推到真正的终态
    cs.transition(d2, "jump", "aborted")
    expect_raises("终态 aborted 不可再转移", cs.StateError,
                  cs.transition, d2, "jump", "preflight")
    # verify 失败后只重试 verify（不重开新局）
    d3 = newjob()
    cs.init_run(d3, "vr", "v")
    for to in ["preflight", "starting", "running", "stopping", "stopped", "verifying"]:
        cs.transition(d3, "vr", to)
    cs.transition(d3, "vr", "verify_failed")
    cs.transition(d3, "vr", "verifying")
    ck("verify_failed 可以只重试 verifying", cs.load_run(d3, "vr").status == "verifying")

    shutil.rmtree(d, ignore_errors=True)
    shutil.rmtree(d2, ignore_errors=True)
    shutil.rmtree(d3, ignore_errors=True)


def test_no_clobber() -> None:
    d = newjob()
    cs.init_run(d, "once", "v")
    expect_raises("重复 init 拒绝覆盖", cs.StateError, cs.init_run, d, "once", "v2")
    ck("重复 init：原状态未被改", cs.load_run(d, "once").tool_version == "v")
    shutil.rmtree(d, ignore_errors=True)


def test_schema_and_corruption() -> None:
    d = newjob()
    cs.init_run(d, "sc", "v")
    p = cs.run_path(d, "sc")

    raw = json.loads(p.read_text())
    raw["schema_version"] = 999
    p.write_text(json.dumps(raw))
    expect_raises("未来 schema 被拒", cs.SchemaMismatch, cs.load_run, d, "sc")

    raw["schema_version"] = 1
    raw["unknown_field"] = 1
    p.write_text(json.dumps(raw))
    expect_raises("未知字段被拒", cs.StateError, cs.load_run, d, "sc")

    raw.pop("unknown_field")
    raw["run_id"] = "someone-else"
    p.write_text(json.dumps(raw))
    expect_raises("run_id 与文件名不一致被拒", cs.StateError, cs.load_run, d, "sc")

    p.write_text("{ broken")
    expect_raises("损坏 JSON 被拒", cs.StateError, cs.load_run, d, "sc")

    p.write_text(json.dumps({"run_id": "sc", "status": "init", "revision": 1,
                             "artifacts": {"x": float("nan")}}))
    # json.dumps 会把 NaN 写成 NaN（非标准 JSON），解析回来仍是 nan
    expect_raises("文件内 NaN 被拒", cs.StateError, cs.load_run, d, "sc")
    shutil.rmtree(d, ignore_errors=True)


def test_permissions() -> None:
    """状态与锁必须是 0600：含目标/路径信息，不该给同机其他用户读。"""
    d = newjob()
    cs.init_run(d, "perm", "v")
    with cs.local_lock(d, cs.lock_name("perm")):
        pass
    for f, label in [(cs.run_path(d, "perm"), "run 状态"),
                     (cs.lock_path(d, "perm"), "锁文件")]:
        mode = stat.S_IMODE(os.stat(f).st_mode)
        ck(f"{label} 权限为 0600", mode == 0o600, f"实际 {oct(mode)}")
    shutil.rmtree(d, ignore_errors=True)


def test_summary_three_conclusions() -> None:
    """三个结论必须分开，且缺结论 = unknown（不是 pass）。"""
    d = newjob()
    cs.init_run(d, "sum", "v")
    for to in ["preflight", "starting", "running", "stopping", "stopped", "verifying"]:
        cs.transition(d, "sum", to)
    cs.transition(d, "sum", "verified",
                  stages={"capture": {"result": "pass"},
                          "game": {"result": "fail"},
                          "verify": {"result": "pass"}})
    s = cs.summarize(d, "sum")
    ck("summary：capture=pass", s["capture_integrity"] == "pass")
    ck("summary：game=fail（与 capture 独立）", s["game_result"] == "fail")
    ck("summary：postproduction 缺结论=unknown", s["postproduction_ready"] == "unknown")
    ck("summary：all_known=False（因为有一项 unknown）", s["all_known"] is False)
    ck("summary：verify=pass", s["verify_result"] == "pass")
    shutil.rmtree(d, ignore_errors=True)


def test_stop_nonce() -> None:
    """停止请求要带 nonce 与可验证身份，不能是裸 PID。"""
    d = newjob()
    cs.init_run(d, "sp", "v")
    ck("无停止请求时 poll 返回 None", cs.poll_stop(d, "sp") is None)
    req = cs.request_stop(d, "sp", {"owner_id": "tester", "reason": "done"})
    ck("停止请求含 nonce", bool(req.get("nonce")) and len(req["nonce"]) == 32)
    ck("停止请求含 owner_proc_start", "owner_proc_start" in req)
    got = cs.poll_stop(d, "sp")
    ck("poll 读回同一 nonce", got["nonce"] == req["nonce"])
    v = cs.verify_stop_owner(got)
    ck("verify-stop：身份匹配", v["identity_matches"] is True and v["alive"] is True,
       str(v))
    expect_raises("缺 owner_id 被拒", cs.StateError,
                  cs.request_stop, d, "sp", {"reason": "x"})
    shutil.rmtree(d, ignore_errors=True)


def test_cli_smoke() -> None:
    """CLI 层：退出码与错误路径。"""
    d = newjob()
    script = SCRIPTS / "capture_state.py"
    r = subprocess.run([sys.executable, str(script), "init", "--job-dir", str(d),
                        "--run-id", "cli", "--tool-version", "v"],
                       capture_output=True, text=True)
    ck("CLI init 退出码 0", r.returncode == 0, r.stderr[:200])
    r = subprocess.run([sys.executable, str(script), "init", "--job-dir", str(d),
                        "--run-id", "cli", "--tool-version", "v"],
                       capture_output=True, text=True)
    ck("CLI 重复 init 退出码 2", r.returncode == 2, f"rc={r.returncode}")
    r = subprocess.run([sys.executable, str(script), "update", "--job-dir", str(d),
                        "--run-id", "cli", "--to", "running"],
                       capture_output=True, text=True)
    ck("CLI 非法转移退出码 2", r.returncode == 2, f"rc={r.returncode}")
    r = subprocess.run([sys.executable, str(script), "update", "--job-dir", str(d),
                        "--run-id", "cli", "--to", "preflight",
                        "--set", "status=verified"],
                       capture_output=True, text=True)
    ck("CLI --set status 被拒", r.returncode == 2 and "受保护" in r.stderr,
       r.stderr[:200])
    r = subprocess.run([sys.executable, str(script), "read", "--job-dir", str(d),
                        "--run-id", "../escape"], capture_output=True, text=True)
    ck("CLI 逃逸 run_id 退出码 2", r.returncode == 2, f"rc={r.returncode}")

    # 回归：--set 用保留字曾导致未捕获 TypeError（退出码 1 + traceback），
    # 受保护字段检查来不及跑。必须干净拒绝（退出码 2，无 traceback）。
    for reserved in ["run_id", "to", "note", "job_dir", "expect_revision"]:
        r = subprocess.run([sys.executable, str(script), "update", "--job-dir", str(d),
                            "--run-id", "cli", "--to", "preflight",
                            "--set", f"{reserved}=x"],
                           capture_output=True, text=True)
        ck(f"CLI --set {reserved} 干净拒绝（rc=2 无 traceback）",
           r.returncode == 2 and "Traceback" not in r.stderr,
           f"rc={r.returncode} err={r.stderr[:120]}")
    shutil.rmtree(d, ignore_errors=True)



def test_stages_merge_not_replace() -> None:
    """**回归**：写 verify 不能把已经写好的 capture 抹掉。

    旧实现 `setattr(st, k, v)` 直接替换整个 stages 字典，于是 worker 先写
    capture 再写 verify 时，capture 整段消失 —— "三个结论分开报"退化成了
    "只剩最后一个"。stages/artifacts 必须**按 key 合并**。
    """
    d = newjob()
    cs.init_run(d, "mg", "v")
    for to in ["preflight", "starting", "running", "stopping", "stopped"]:
        cs.transition(d, "mg", to)
    cs.transition(d, "mg", "verifying",
                  stages={"capture": {"result": "pass", "frames": 100}})
    cs.transition(d, "mg", "verified", stages={"verify": {"result": "pass"}})
    st = cs.load_run(d, "mg")
    ck("写 verify 后 capture 仍在", "capture" in st.stages, f"stages={list(st.stages)}")
    ck("capture 内容未被破坏", st.stages.get("capture", {}).get("frames") == 100)
    ck("verify 也已写入", st.stages.get("verify", {}).get("result") == "pass")

    # 同一个 stage 再写时，**整段替换该 stage**（只合并顶层 stage 名，不深合并）。
    # 这是刻意的：阶段内容是写者一次给全的；深合并会让"清空 problems 列表"
    # 变成做不到的事。要保证的只是"不同阶段互不覆盖"。
    cs.transition(d, "mg", "done", stages={"capture": {"result": "fail"}})
    st2 = cs.load_run(d, "mg")
    ck("同 stage 重写＝整段替换（frames 被清掉，语义清晰）",
       "frames" not in st2.stages["capture"], str(st2.stages.get("capture")))
    ck("同 stage 重写后新值生效", st2.stages["capture"].get("result") == "fail")
    ck("其它阶段仍未被波及", st2.stages.get("verify", {}).get("result") == "pass")

    # artifacts 同样合并
    d2 = newjob()
    cs.init_run(d2, "ma", "v")
    cs.transition(d2, "ma", "preflight", artifacts={"media": "/x.mp4"})
    cs.transition(d2, "ma", "starting", artifacts={"recorder_pid": 42})
    st3 = cs.load_run(d2, "ma")
    ck("artifacts 合并（media 保留）", st3.artifacts.get("media") == "/x.mp4",
       str(st3.artifacts))
    ck("artifacts 合并（pid 写入）", st3.artifacts.get("recorder_pid") == 42)
    shutil.rmtree(d, ignore_errors=True)
    shutil.rmtree(d2, ignore_errors=True)


def main() -> int:
    print("═══ capture_state 对抗性测试 ═══")
    test_concurrent_init()
    test_concurrent_update_cas()
    test_run_id_escape()
    test_symlink_state_rejected()
    test_field_bypass()
    test_lock_never_removed_or_stolen()
    test_live_lock_blocks_and_releases()
    test_lock_busy_is_error_not_silent()
    test_lock_released_on_crash()
    test_reader_never_sees_partial_json()
    test_symlink_lock_rejected()
    test_transitions()
    test_no_clobber()
    test_schema_and_corruption()
    test_permissions()
    test_summary_three_conclusions()
    test_stages_merge_not_replace()
    test_stop_nonce()
    test_cli_smoke()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
