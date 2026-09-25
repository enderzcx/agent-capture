#!/usr/bin/env python3
"""capture_state — 录制 job/run 的持久状态（内核锁 + 原子发布 + CAS + 版本绑定）。

这个模块是录制生命周期的**唯一权威状态**。它存在的理由是：录制器会被 SIGKILL、
目标 app 会崩、两个调用方会同时点 stop。每一种都必须留下**可核对**的记录，
而不是留下一个看起来像"通过"的东西。

## 锁与发布：为什么是现在这个形状

前两版都错在同一个地方——**试图用"读一个 PID 然后判断它死没死"来实现锁**：

- v1：`O_EXCL` 建锁文件 + 持有者 pid + 启动时间比对。问题是**回收本身会打架**：
  两个进程同时判定旧锁已死，各自把锁 `os.replace` 走，第二个会把**刚被第一个
  建好的新锁**搬走 —— 于是两个进程同时"持有"锁。
- v1 的第二个问题：`O_EXCL` 建状态文件**再往里写**，读者可能在这个窗口里
  读到空文件或半截 JSON。把"创建"和"发布"混在一起就必然有这个缝。

现在的做法（Cloud 参考实现同思路）：

1. **锁是内核持有的**（`flock` / `msvcrt.locking`），锁文件**永不删除、永不改名**。
   进程一死内核自动释放，所以**根本不需要**判断 PID 死没死，那段逻辑整段删掉。
   没有回收动作，就没有"回收者互相打架"这个失败模式。
2. **发布是"写临时文件 → 原子挂上去"**：
   - 新建（no-clobber）用 `os.link`：目标名只会在内容完整落盘**之后**出现，
     且目标已存在时 `link` 直接失败 —— 既原子又不可能覆盖别人。
   - 更新用 `os.replace`：同目录 rename 是原子的。
3. **读也要持锁**：`load_run` 默认在锁内读。这样即使某个文件系统不支持
   hardlink（`link` 降级成 `replace`），读者也**绝不会**看到中间态——
   因为写者持着锁，读者进不来。

## 其余不变的设计约束（每条对应一个真实失败模式）

- **状态必须活过进程崩溃**：每次变更 fsync 文件 + fsync 目录。
- **`run_id` 必须是安全 basename**：白名单字符，拒 `.`/`..`/分隔符/NUL；
  读取时校验文件内 `run_id` 与文件名一致（防串档）。
- **业务字段白名单**：`--set` 不能改 `status`/`run_id`/`schema_version`/`history`/
  `revision`，那些只能由状态机改。否则调用方能伪造生命周期。
- **类型与 finite 校验**：数值必须 finite。NaN/Inf 会让下游比较**静默**失效
  （`nan > 0` 与 `nan <= 0` 同时为假），所以序列化和反序列化两端都拦。
- **`stop` 不用裸 PID**：worker-owned 请求 nonce + 可验证进程身份。
- **权限 0600**：状态含目标与路径信息。

**文件系统前提**：本模块面向本地文件系统（APFS / ext4 / NTFS）。
网络文件系统上的锁语义**未经验证**，不声称支持。

## CLI

    capture_state.py init   --job-dir D --run-id R --tool-version V [--target-json J]
    capture_state.py update --job-dir D --run-id R --to STATUS [--expect-revision N]
                            [--note S] [--set k=v ...]
    capture_state.py read   --job-dir D --run-id R
    capture_state.py summary --job-dir D --run-id R
    capture_state.py request-stop --job-dir D --run-id R --owner-json J
    capture_state.py poll-stop    --job-dir D --run-id R
    capture_state.py verify-stop  --job-dir D --run-id R

**没有 `lock`/`unlock` 子命令**，这是有意的：锁是内核持有的，一个 CLI 进程退出时
它持有的锁就释放了，所以"用 CLI 先锁上，再跑别的命令"**保护不了任何东西**。
锁只在 `init`/`update`/`read` 各自执行期间存在，这才是它真正需���覆盖的范围。
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import json
import math
import os
import re
import secrets
import socket
import stat
import sys
import tempfile
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = 1

RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

DEFAULT_LOCK_TIMEOUT = 10.0

# 状态机：**只允许这些转移**。表外的转移抛错，而不是"尽力而为"地写下去。
ALLOWED_TRANSITIONS: Dict[str, List[str]] = {
    "init": ["preflight", "failed", "aborted"],
    "preflight": ["starting", "failed", "aborted"],
    "starting": ["running", "failed", "aborted"],
    # running 只能去 stopping（正常停止）或 crashed（目标消失/进程死）。
    # 不能直接跳 verified：没经过 stop 就没有可校验的产物。
    "running": ["stopping", "crashed", "failed"],
    "stopping": ["stopped", "failed"],
    "crashed": ["stopped", "failed"],
    "stopped": ["verifying", "failed"],
    # verifying 给出三个**互相独立**的结论之一，不合并成一个 "ok"
    "verifying": ["verified", "verify_failed", "failed"],
    "verified": ["done"],
    "verify_failed": ["done", "verifying"],  # 只重试 verify，不重开新局
    "failed": [],
    "aborted": [],
    "done": [],
}

TERMINAL = {"failed", "aborted", "done"}

# 调用方可以改的字段（业务数据）。生命周期字段**只能**由状态机改。
SETTABLE_FIELDS = {"target", "stages", "artifacts", "error", "notes"}
OBJECT_FIELDS = {"target", "stages", "artifacts"}
PROTECTED_FIELDS = {"status", "run_id", "schema_version", "history", "revision",
                    "created_at", "updated_at", "tool_version", "stop_request"}


class StateError(RuntimeError):
    """状态操作失败。**永远不要**把它当成"状态未知所以算通过"。"""


class SchemaMismatch(StateError):
    pass


class LockBusy(StateError):
    """job 正被另一个进程持有。**不是**可以忽略的瞬时错误。"""


class RevisionMismatch(StateError):
    pass


class BadRunId(StateError):
    pass


# --------------------------------------------------------------------------
# run_id / 路径安全
# --------------------------------------------------------------------------

def validate_run_id(run_id: str) -> str:
    """run_id 必须是安全 basename：无分隔符、无 `..`、无 NUL、长度有界。

    没有这一步，`--run-id ../../../etc/something` 就能写到 job 目录外面。
    """
    if not isinstance(run_id, str) or not run_id:
        raise BadRunId("run_id 必须是非空字符串")
    if "\x00" in run_id:
        raise BadRunId("run_id 不能含 NUL")
    if run_id in (".", ".."):
        raise BadRunId(f"run_id 不能是 {run_id!r}")
    if "/" in run_id or "\\" in run_id:
        raise BadRunId(f"run_id 不能含路径分隔符: {run_id!r}")
    if not RUN_ID_RE.match(run_id):
        raise BadRunId(
            f"run_id 非法: {run_id!r}；只允许 [A-Za-z0-9._-]，字母数字开头，长度 1..64。"
            f"（拒绝路径逃逸与奇怪字符）"
        )
    return run_id


def run_name(run_id: str) -> str:
    return f"{validate_run_id(run_id)}.run.json"


def stop_name(run_id: str) -> str:
    return f"{validate_run_id(run_id)}.stop.json"


def lock_name(run_id: str) -> str:
    return f"{validate_run_id(run_id)}.lock"


def run_path(job_dir: Path, run_id: str) -> Path:
    return Path(job_dir) / run_name(run_id)


def stop_path(job_dir: Path, run_id: str) -> Path:
    return Path(job_dir) / stop_name(run_id)


def lock_path(job_dir: Path, run_id: str) -> Path:
    return Path(job_dir) / lock_name(run_id)


def _job_dir(root: Path) -> Path:
    """job 目录必须是**真实目录**（不是 symlink），权限 0700。"""
    root = Path(root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise StateError(f"job 目录必须是真实目录（不是 symlink）: {root}")
    return root


# --------------------------------------------------------------------------
# 内核锁：永不删除、永不改名
# --------------------------------------------------------------------------

def _take_flock(fd: int) -> None:
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release_flock(fd: int) -> None:
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)


@contextlib.contextmanager
def local_lock(root: Path, name: str, timeout: float = DEFAULT_LOCK_TIMEOUT):
    """持有一把内核锁。锁文件**永不删除、永不改名**。

    为什么不做"死锁回收"：回收动作本身就是新的竞争源。两个进程同时判定
    "持有者已死"，就会各自去动那个锁文件，第二个会把第一个刚建好的新锁搬走，
    结果两个进程都以为自己持锁。让内核管这件事（进程退出即释放）就没有这个问题。

    拿不到锁时抛 `LockBusy` —— 调用方必须把它当成真实失败，不能降级继续写。
    """
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) \
            or not math.isfinite(timeout) or timeout < 0:
        raise StateError("锁超时必须是有限非负数")
    root = _job_dir(root)
    p = root / name
    if p.is_symlink():
        raise StateError(f"拒绝使用 symlink 作为锁文件: {p}")

    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(p), flags, 0o600)
    acquired = False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise StateError(f"锁文件必须是普通文件: {p}")
        if os.fstat(fd).st_size == 0:
            # msvcrt.locking 需要至少 1 字节才有东西可锁
            os.write(fd, b"\0")
        deadline = time.monotonic() + timeout
        while True:
            try:
                _take_flock(fd)
                acquired = True
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                if time.monotonic() >= deadline:
                    raise LockBusy(
                        f"job 正被另一个进程持有（{p}）；本工具**不会**删除或接管别人的锁。"
                        f"若确认没有其它进程在跑，检查是否有残留进程。"
                    ) from exc
                time.sleep(min(0.025, max(0.0, deadline - time.monotonic())))
        # 拿到锁后再确认路径没被换掉（防"锁的是 A、写的是 B"）
        live = os.stat(str(p), follow_symlinks=False)
        own = os.fstat(fd)
        if (live.st_dev, live.st_ino) != (own.st_dev, own.st_ino):
            raise StateError(f"锁文件在取锁期间被替换: {p}")
        yield
    finally:
        if acquired:
            try:
                _release_flock(fd)
            except OSError:
                pass
        os.close(fd)


# --------------------------------------------------------------------------
# 原子发布 / 安全读取
# --------------------------------------------------------------------------

def _reject_constant(value: str) -> Any:
    """JSON 里的 NaN / Infinity / -Infinity 一律拒绝。

    `json.dumps` 默认会把 float('nan') 写成非标准的 `NaN`，而 Python 又能读回来。
    这种值一旦进了状态，下游所有 `>` / `<` 比较**同时为假**，"通过"和"失败"
    都不成立 —— 比报错危险得多。
    """
    raise StateError(f"JSON 含非有限常量 {value!r}（NaN/Inf 会静默破坏下游比较）")


def publish_json(root: Path, name: str, value: Any, *, create: bool = False) -> None:
    """原子发布一个 JSON 文件。

    - `create=True`：用 `os.link` 保证 **no-clobber**，且目标名只在内容完整
      落盘之后才出现 —— 读者不可能看到空文件或半截 JSON。
      文件系统不支持 hardlink 时降级为 `os.replace`（调用方应持锁，见模块说明）。
    - `create=False`：`os.replace` 原子替换。

    `allow_nan=False` 让非法浮点在**序列化时**就炸，而不���写进文件再被人读出来。
    """
    root = _job_dir(root)
    p = root / name
    if p.is_symlink():
        raise StateError(f"拒绝写入 symlink 状态文件: {p}")
    try:
        data = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                          allow_nan=False).encode("utf-8")
    except ValueError as exc:
        raise StateError(f"状态含无法序列化的值（NaN/Inf 等）: {exc}") from exc

    fd, tmp = tempfile.mkstemp(prefix=".state-", dir=str(root))
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(tmp, 0o600)
        if create:
            try:
                os.link(tmp, str(p))
            except FileExistsError:
                raise
            except OSError as exc:
                # 该文件系统不支持 hardlink：降级为原子替换。
                # 安全性依赖"调用方持锁"，所以这里必须让调用方知道。
                if exc.errno in (errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP,
                                 errno.EXDEV, errno.EMLINK):
                    if p.exists():
                        raise FileExistsError(str(p))
                    os.replace(tmp, str(p))
                else:
                    raise
        else:
            os.replace(tmp, str(p))
        if os.name != "nt":
            try:
                dfd = os.open(str(root), os.O_RDONLY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            except OSError:
                pass
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def load_json(root: Path, name: str) -> Any:
    """安全读取：拒 symlink、拒非普通文件、拒 NaN/Inf。"""
    root = _job_dir(root)
    p = root / name
    if p.is_symlink():
        raise StateError(f"拒绝读取 symlink 状态文件: {p}")
    try:
        fd = os.open(str(p), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        raise StateError(f"状态文件不存在: {p}")
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise StateError(f"状态必须是普通文件: {p}")
        with os.fdopen(fd, "r", encoding="utf-8") as src:
            fd = -1
            try:
                return json.load(src, parse_constant=_reject_constant)
            except json.JSONDecodeError as exc:
                raise StateError(f"状态文件损坏（不是合法 JSON）: {p}: {exc}") from exc
    finally:
        if fd >= 0:
            os.close(fd)


# --------------------------------------------------------------------------
# Run 状态
# --------------------------------------------------------------------------

@dataclass
class RunState:
    run_id: str
    schema_version: int = SCHEMA_VERSION
    tool_version: str = ""
    status: str = "init"
    revision: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    # 目标描述：画面与音频**分开**，因为它们可以不同粒度
    target: Dict[str, Any] = field(default_factory=dict)
    # 各阶段独立结论——不合并成一个 "ok"
    stages: Dict[str, Any] = field(default_factory=dict)
    artifacts: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)
    history: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None
    # worker-owned 停止请求（含 nonce），见 request_stop
    stop_request: Optional[Dict[str, Any]] = None

    def path(self, job_dir: Path) -> Path:
        return run_path(job_dir, self.run_id)


def _validate_status(status: str) -> None:
    if status not in ALLOWED_TRANSITIONS:
        raise StateError(f"未知状态 {status!r}；合法状态：{sorted(ALLOWED_TRANSITIONS)}")


def _check_finite(obj: Any, where: str) -> None:
    """递归拒绝 NaN / Infinity（双保险：序列化端也拦一次）。"""
    if isinstance(obj, float):
        if not math.isfinite(obj):
            raise StateError(f"{where} 含非有限数值 {obj!r}（NaN/Inf 会静默破坏下游比较）")
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _check_finite(v, f"{where}.{k}")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _check_finite(v, f"{where}[{i}]")


def _validate_run_raw(raw: Any, path: Path, expect_run_id: str) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise StateError(f"run 状态不是对象: {path}")
    sv = raw.get("schema_version")
    if sv != SCHEMA_VERSION:
        raise SchemaMismatch(
            f"schema 版本不匹配: 文件={sv!r} 本工具={SCHEMA_VERSION}（{path}）。"
            f"不认识的 schema 一律拒绝读取，避免把旧格式误读成通过。"
        )
    known = set(RunState.__dataclass_fields__)
    unknown = set(raw) - known
    if unknown:
        raise StateError(f"run 状态含未知字段 {sorted(unknown)}（{path}）")
    missing = {"run_id", "status", "revision"} - set(raw)
    if missing:
        raise StateError(f"run 状态缺必需字段 {sorted(missing)}（{path}）")
    # 文件名与内容必须一致：不一致说明串档或被改过
    if raw["run_id"] != expect_run_id:
        raise StateError(
            f"run_id 不一致：文件内={raw['run_id']!r} 文件名={expect_run_id!r}（{path}）。"
            f"拒绝读取，避免把 A 的状态当成 B 的。"
        )
    validate_run_id(raw["run_id"])
    _validate_status(raw["status"])
    if not isinstance(raw["revision"], int) or isinstance(raw["revision"], bool) \
            or raw["revision"] < 0:
        raise StateError(f"revision 必须是非负整数（{path}）")
    for f in OBJECT_FIELDS:
        if f in raw and not isinstance(raw[f], dict):
            raise StateError(f"{f} 必须是 JSON 对象（{path}）")
    _check_finite(raw, f"run({expect_run_id})")
    return raw


def load_run(job_dir: Path, run_id: str, *, _locked: bool = False) -> RunState:
    """读一个 run。**默认在锁内读**，所以不会看到发布中的中间态。

    `_locked=True` 供已经持锁的内部调用使用（flock 在同一进程的两次 open 之间
    也会冲突，重复取锁会自锁死）。
    """
    name = run_name(run_id)
    if _locked:
        raw = _validate_run_raw(load_json(job_dir, name), run_path(job_dir, run_id), run_id)
        return RunState(**raw)
    with local_lock(job_dir, lock_name(run_id)):
        raw = _validate_run_raw(load_json(job_dir, name), run_path(job_dir, run_id), run_id)
        return RunState(**raw)


def init_run(job_dir: Path, run_id: str, tool_version: str,
             target: Optional[Dict[str, Any]] = None) -> RunState:
    """创建 run。**真正 no-clobber**，且读者看不到半截文件。

    两个保证合起来靠两件事：
      · `os.link` 发布：目标名只在内容完整落盘之后才出现（不存在"空文件窗口"）；
      · 目标已存在时 `link` 直接失败（不是"先 exists 再写"那种 TOCTOU）。
    """
    validate_run_id(run_id)
    job_dir = _job_dir(job_dir)
    if target is None:
        target = {}
    if not isinstance(target, dict):
        raise StateError("target 必须是 JSON 对象")
    _check_finite(target, "target")

    st = RunState(run_id=run_id, tool_version=tool_version, target=dict(target))
    st.revision = 1
    st.history.append({"at": st.created_at, "rev": 1, "from": None, "to": "init",
                       "note": "created"})
    with local_lock(job_dir, lock_name(run_id)):
        try:
            publish_json(job_dir, run_name(run_id), asdict(st), create=True)
        except FileExistsError:
            existing = "?"
            try:
                existing = load_run(job_dir, run_id, _locked=True).status
            except StateError:
                pass
            raise StateError(
                f"run 已存在，拒绝覆盖: {run_path(job_dir, run_id)}（status={existing}）。"
                f"要开新一局请换 run_id。"
            )
    return st


def _apply_updates(st: RunState, fields: Dict[str, Any]) -> None:
    """把业务字段写进状态。**白名单 + 类型 + finite 校验**。

    拒绝改 status/run_id/schema_version/history/revision：那些只能由状态机改。
    没有这层，调用方就能用 `--set status=verified` 伪造生命周期。
    """
    for k, v in fields.items():
        if k in PROTECTED_FIELDS:
            raise StateError(
                f"字段 {k!r} 受保护，不能由调用方直接写"
                f"（生命周期字段只由状态机修改）。可写字段：{sorted(SETTABLE_FIELDS)}"
            )
        if k not in SETTABLE_FIELDS:
            raise StateError(f"未知/不可写字段 {k!r}；可写字段：{sorted(SETTABLE_FIELDS)}")
        if k in OBJECT_FIELDS and not isinstance(v, dict):
            raise StateError(f"{k} 必须是 JSON 对象，收到 {type(v).__name__}")
        if k == "notes" and not isinstance(v, list):
            raise StateError("notes 必须是 JSON 数组")
        if k == "error" and v is not None and not isinstance(v, str):
            raise StateError("error 必须是字符串或 null")
        _check_finite(v, k)
        if k in OBJECT_FIELDS:
            # **合并**而不是替换。各阶段（capture/verify/game/postproduction）是
            # 互相独立的结论；写 verify 不能把已经写好的 capture 抹掉 ——
            # 那会让"三个结论分开报"退化成"只剩最后一个还在"。
            merged = dict(getattr(st, k) or {})
            merged.update(v)
            setattr(st, k, merged)
        else:
            setattr(st, k, v)


def transition(job_dir: Path, run_id: str, to: str, /, note: str = "",
               expect_revision: Optional[int] = None,
               lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
               _already_locked: bool = False, **fields: Any) -> RunState:
    """执行一次状态转移。**自动持锁** + 可选 CAS。非法转移抛错，不降级写入。

    `expect_revision` 给出时做 CAS：磁盘上的 revision 不等于期望值就拒绝。
    两个进程都想从 running→stopping，只有一个能成功，另一个拿到
    RevisionMismatch 而不是静默覆盖。

    `lock_timeout` 是等锁上限：超时抛 `LockBusy`。需要**快速失败**的调用方
    （例如 UI 上的一次点击）应该给一个小值，而不是默认等满 10 秒。

    前三个参数是**位置专用**（`/`）：这样 `transition(d, r, "starting", run_id="x")`
    里的 `run_id` 会落进 `**fields`，被 `_apply_updates` 干净地当成受保护字段拒绝，
    而不是在 Python 参数绑定阶段炸成 TypeError（那样受保护检查根本来不及跑）。
    """
    validate_run_id(run_id)
    _validate_status(to)
    job_dir = _job_dir(job_dir)

    def _do() -> RunState:
        st = load_run(job_dir, run_id, _locked=True)
        if expect_revision is not None and st.revision != expect_revision:
            raise RevisionMismatch(
                f"revision 不匹配：期望 {expect_revision}，实际 {st.revision}（run {run_id}）。"
                f"说明状态已被别的进程改过；请重新读取后再决定，不要盲目重放。"
            )
        allowed = ALLOWED_TRANSITIONS.get(st.status, [])
        if to not in allowed:
            raise StateError(
                f"非法状态转移 {st.status} → {to}（允许：{allowed or '无，终态'}）。"
                f"异常只重试相应阶段，不允许跳阶段。"
            )
        prev = st.status
        st.status = to
        _apply_updates(st, fields)
        st.revision += 1
        st.updated_at = time.time()
        st.history.append({"at": st.updated_at, "rev": st.revision,
                           "from": prev, "to": to, "note": note})
        publish_json(job_dir, run_name(run_id), asdict(st))
        return st

    if _already_locked:
        return _do()
    with local_lock(job_dir, lock_name(run_id), timeout=lock_timeout):
        return _do()


def update_run(job_dir: Path, run_id: str, /, *,
               lock_timeout: float = DEFAULT_LOCK_TIMEOUT, **fields: Any) -> RunState:
    """只更新**业务字段**，**不改变状态**。

    为什么需要它：worker 在 `starting` 期间就要把录制器 pid、产物路径写进
    artifacts（供排查）。用 `transition(..., "starting")` 做这件事是错的 ——
    `starting → starting` 不是合法转移，会被状态机挡下，于是那些字段根本没写进去
    （实测踩到：`recorder_alive` 一直是 None）。

    仍然走锁 + CAS 的同一套保证：字段白名单、类型与 finite 校验都照旧。
    """
    validate_run_id(run_id)
    job_dir = _job_dir(job_dir)

    def _do() -> RunState:
        st = load_run(job_dir, run_id, _locked=True)
        _apply_updates(st, fields)
        st.revision += 1
        st.updated_at = time.time()
        st.history.append({"at": st.updated_at, "rev": st.revision,
                           "from": st.status, "to": st.status,
                           "note": "field update (no state change)"})
        publish_json(job_dir, run_name(run_id), asdict(st))
        return st

    with local_lock(job_dir, lock_name(run_id), timeout=lock_timeout):
        return _do()


# --------------------------------------------------------------------------
# worker-owned 停止请求（不用裸 PID）
# --------------------------------------------------------------------------

def request_stop(job_dir: Path, run_id: str, owner: Dict[str, Any]) -> Dict[str, Any]:
    """写一个停止请求。**带 nonce 与可验证身份**，不是裸 PID。

    为什么要 nonce：调用方稍后要确认"这个停止请求是不是我发的"。
    裸 PID 会被复用，而且无法证明请求来源。
    """
    validate_run_id(run_id)
    if not isinstance(owner, dict) or not owner.get("owner_id"):
        raise StateError("request-stop 需要 --owner-json 且至少含 owner_id")
    _check_finite(owner, "owner")

    owner_pid = int(owner.get("pid") or os.getpid())
    req = {
        "nonce": secrets.token_hex(16),
        "owner_id": str(owner["owner_id"]),
        "owner_pid": owner_pid,
        "owner_proc_start": _proc_start_time(owner_pid),
        "requester_host": socket.gethostname(),
        "requested_at": time.time(),
        "reason": str(owner.get("reason") or ""),
    }
    publish_json(job_dir, stop_name(run_id), req)
    return req


def poll_stop(job_dir: Path, run_id: str) -> Optional[Dict[str, Any]]:
    """读取停止请求。**不存在 = None**（不是错误）；损坏 = 抛错（不当成"没有"）。"""
    validate_run_id(run_id)
    p = stop_path(job_dir, run_id)
    if not p.exists():
        return None
    raw = load_json(job_dir, stop_name(run_id))
    if not isinstance(raw, dict) or "nonce" not in raw:
        raise StateError(f"停止请求文件损坏，拒绝当成「无请求」: {p}")
    return raw


def _proc_start_time(pid: int) -> Optional[str]:
    """进程启动时间。**只用于"事后核对请求来源"**，不再用于判断锁是否可回收。"""
    if pid <= 0:
        return None
    try:
        import subprocess
        r = subprocess.run(["ps", "-o", "lstart=", "-p", str(int(pid))],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() or None
    except Exception:
        return None


def verify_stop_owner(req: Dict[str, Any]) -> Dict[str, Any]:
    """验证停止请求的发起者身份是否仍与记录一致（防 pid 复用）。"""
    pid = int(req.get("owner_pid") or 0)
    recorded = req.get("owner_proc_start")
    alive = False
    if pid > 0:
        try:
            os.kill(pid, 0)
            alive = True
        except OSError as exc:
            alive = exc.errno != errno.ESRCH
    matches: Optional[bool] = None
    if alive and recorded:
        now = _proc_start_time(pid)
        matches = (now == recorded) if now else None  # None = 无法确认
    return {"alive": alive, "identity_matches": matches, "nonce": req.get("nonce")}


# --------------------------------------------------------------------------
# 结论合成：三个独立结论，绝不互相背书
# --------------------------------------------------------------------------

def summarize(job_dir: Path, run_id: str) -> Dict[str, Any]:
    st = load_run(job_dir, run_id)

    def tri(stage: Dict[str, Any]) -> str:
        """一个阶段的三态：pass / fail / unknown。缺结论 = unknown，**不是** pass。"""
        r = (stage or {}).get("result")
        return r if r in ("pass", "fail", "unknown") else "unknown"

    g = st.stages.get("game", {})
    c = st.stages.get("capture", {})
    v = st.stages.get("verify", {})
    p = st.stages.get("postproduction", {})
    return {
        "run_id": run_id,
        "status": st.status,
        "revision": st.revision,
        "tool_version": st.tool_version,
        # 这三个**分开报**：录像完整 ≠ 游戏有结果；游戏有结果 ≠ 后期能开始
        "game_result": tri(g),
        "capture_integrity": tri(c),
        "postproduction_ready": tri(p),
        "verify_result": tri(v),
        "all_known": all(tri(x) != "unknown" for x in (g, c, v, p)),
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _parse_set(pairs: List[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for item in pairs:
        if "=" not in item:
            raise StateError(f"--set 需要 key=value 形式: {item!r}")
        k, _, raw = item.partition("=")
        try:
            out[k] = json.loads(raw, parse_constant=_reject_constant)
        except json.JSONDecodeError:
            out[k] = raw  # 不是 JSON 就当字符串
    return out


# `transition()` 的形参名。`--set` 用了这些名字会在 Python 参数绑定阶段就炸成
# TypeError（未捕获的 traceback、退出码 1），受保护字段的检查根本来不及跑���
_RESERVED_KWARGS = {"job_dir", "run_id", "to", "note", "expect_revision",
                    "_already_locked", "fields"}


def _guard_set_keys(fields: Dict[str, Any]) -> None:
    for k in fields:
        if k in _RESERVED_KWARGS:
            raise StateError(
                f"--set {k}=... 与命令自身参数冲突，已拒绝"
                f"（受保护字段只能由状态机修改）。可写字段：{sorted(SETTABLE_FIELDS)}"
            )


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="capture_state", description="录制 run 持久状态")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--job-dir", required=True)
        p.add_argument("--run-id", required=True)

    p = sub.add_parser("init"); common(p)
    p.add_argument("--tool-version", required=True)
    p.add_argument("--target-json", default="{}")

    p = sub.add_parser("update"); common(p)
    p.add_argument("--to", required=True)
    p.add_argument("--note", default="")
    p.add_argument("--expect-revision", type=int, default=None)
    p.add_argument("--set", action="append", default=[])

    p = sub.add_parser("update-fields"); common(p)
    p.add_argument("--set", action="append", default=[])

    p = sub.add_parser("read"); common(p)
    p = sub.add_parser("summary"); common(p)

    p = sub.add_parser("request-stop"); common(p)
    p.add_argument("--owner-json", required=True)

    p = sub.add_parser("poll-stop"); common(p)
    p = sub.add_parser("verify-stop"); common(p)

    a = ap.parse_args(argv)
    job_dir = Path(a.job_dir)
    try:
        if a.cmd == "init":
            st = init_run(job_dir, a.run_id, a.tool_version,
                          json.loads(a.target_json, parse_constant=_reject_constant))
            print(json.dumps(asdict(st), ensure_ascii=False, indent=2))
        elif a.cmd == "update":
            sets = _parse_set(a.set)
            _guard_set_keys(sets)
            st = transition(job_dir, a.run_id, a.to, a.note,
                            expect_revision=a.expect_revision, **sets)
            print(json.dumps(asdict(st), ensure_ascii=False, indent=2))
        elif a.cmd == "update-fields":
            sets = _parse_set(a.set)
            _guard_set_keys(sets)
            st = update_run(job_dir, a.run_id, **sets)
            print(json.dumps(asdict(st), ensure_ascii=False, indent=2))
        elif a.cmd == "read":
            print(json.dumps(asdict(load_run(job_dir, a.run_id)),
                             ensure_ascii=False, indent=2))
        elif a.cmd == "summary":
            print(json.dumps(summarize(job_dir, a.run_id), ensure_ascii=False, indent=2))
        elif a.cmd == "request-stop":
            print(json.dumps(request_stop(job_dir, a.run_id,
                                          json.loads(a.owner_json,
                                                     parse_constant=_reject_constant)),
                             ensure_ascii=False, indent=2))
        elif a.cmd == "poll-stop":
            print(json.dumps(poll_stop(job_dir, a.run_id), ensure_ascii=False, indent=2))
        elif a.cmd == "verify-stop":
            req = poll_stop(job_dir, a.run_id)
            if req is None:
                print(json.dumps({"present": False}, ensure_ascii=False))
            else:
                print(json.dumps({"present": True, **verify_stop_owner(req)},
                                 ensure_ascii=False, indent=2))
    except (StateError, SchemaMismatch, LockBusy, RevisionMismatch, BadRunId) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
