#!/usr/bin/env bash
# install.sh — 把 window-recording skill 与 agent-capture 工具装到本机。
#
#   ./install.sh [--skill-home ~/.agents/skills] [--dry-run] [--yes]
#
# 设计原则（每一条都对应一个真实事故）：
#
#  1. **只动自有入口。** 只写 `$SKILL_HOME/window-recording` 与
#     `$TOOLS_HOME/agent-capture/<版本>`。**不碰**任何别的 skill、不碰全局配置、
#     不碰任何既有插件的配置（包括 gemini-companion 的代理/凭据）。
#  2. **固定版本 + 快照。** 工具装进按版本命名的目录，入口是指向该快照的软链。
#     这样"装的到底是哪一版"永远可查，也能原子回滚。
#  3. **冲突即停。** 目标位置已存在**且不是我们装的**（没有我们的 manifest）→
#     拒绝，并告诉你怎么处理。绝不覆盖别人的东西。
#  4. **先备份再动。** 替换入口前把旧入口移进备份目录，保留时间戳。
#  5. **失败即关闭。** 任何一步失败立刻退出，不留半装状态。
#
# 退出码：0 成功；2 冲突/用法错误；3 缺依赖；4 安装步骤失败。
set -euo pipefail

SKILL_NAME="window-recording"
TOOL_NAME="agent-capture"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

SKILL_HOME="${SKILL_HOME:-$HOME/.agents/skills}"
TOOLS_HOME="${TOOLS_HOME:-$HOME/.local/share}"
DRY_RUN=0
ASSUME_YES=0

while [ $# -gt 0 ]; do
  case "$1" in
    --skill-home) SKILL_HOME="$2"; shift 2 ;;
    --tools-home) TOOLS_HOME="$2"; shift 2 ;;
    --dry-run)    DRY_RUN=1; shift ;;
    --yes|-y)     ASSUME_YES=1; shift ;;
    -h|--help)    sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

say()  { printf '%s\n' "$*"; }
ok()   { printf '\033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit "${2:-2}"; }

# —— 版本：优先 git 描述，退化为内容哈希（不用时间戳：不可复现） ——
if [ -d "$REPO_ROOT/.git" ] && command -v git >/dev/null 2>&1; then
  VERSION="$(git -C "$REPO_ROOT" describe --tags --always --dirty 2>/dev/null || true)"
fi
if [ -z "${VERSION:-}" ]; then
  # 没有 git：用关键文件内容哈希当版本（可复现、可对账）
  VERSION="sha-$(cat "$REPO_ROOT/scripts/agent_capture.py" \
                   "$REPO_ROOT/scripts/capture_state.py" \
                   "$REPO_ROOT/scripts/capture_targets.py" \
                   "$REPO_ROOT/SKILL.md" 2>/dev/null | shasum -a 256 | cut -c1-12)"
fi

SNAP_DIR="$TOOLS_HOME/$TOOL_NAME/$VERSION"
SKILL_DEST="$SKILL_HOME/$SKILL_NAME"
MANIFEST_NAME=".agent-capture-install.json"
BACKUP_ROOT="$TOOLS_HOME/$TOOL_NAME/.backups"

say "═══ agent-capture 安装 ═══"
say "  skill 入口 : $SKILL_DEST"
say "  工具快照   : $SNAP_DIR"
say "  版本       : $VERSION"
[ "$DRY_RUN" = 1 ] && warn "DRY-RUN：只显示计划，不写任何文件"

command -v python3 >/dev/null 2>&1 || die "缺少 python3（本工具需要 Python 3.9+）" 3

# ---------------------------------------------------------------------------
# 冲突校验：目标已存在时，只有"确实是我们装的"才允许替换
# ---------------------------------------------------------------------------
check_owned_or_absent() {
  local dest="$1" label="$2"
  if [ ! -e "$dest" ] && [ ! -L "$dest" ]; then
    ok "$label 不存在（将新建）"
    return 0
  fi
  if [ -f "$dest/$MANIFEST_NAME" ]; then
    ok "$label 已存在且带我们的 manifest（可安全替换）"
    return 0
  fi
  # 已存在但没有我们的 manifest = 别人的东西
  die "$label 已存在且**不是本工具安装的**：$dest
   本安装器**不会覆盖别人的东西**。请先自行确认并处理，例如：
     mv '$dest' '$dest.manual-backup-$(date +%Y%m%d%H%M%S)'
   然后重跑本脚本。" 2
}

# 入口是软链时，确认它指向我们的快照目录（不是指向别处）
check_symlink_ours() {
  local link="$1"
  if [ -L "$link" ]; then
    local tgt
    tgt="$(readlink "$link")"
    case "$tgt" in
      "$TOOLS_HOME/$TOOL_NAME"/*) return 0 ;;
      *) die "入口软链指向非本工具目录，拒绝替换：$link → $tgt" 2 ;;
    esac
  fi
  return 0
}

check_owned_or_absent "$SKILL_DEST" "skill 目录"
check_symlink_ours "$SKILL_DEST"

if [ -e "$SNAP_DIR" ]; then
  warn "该版本快照已存在，将复用（内容应一致）：$SNAP_DIR"
fi

if [ "$DRY_RUN" = 1 ]; then
  say ""
  say "计划："
  say "  1) 复制 skill → $SNAP_DIR/skill"
  say "  2) 复制 scripts/ 与 tools/ → $SNAP_DIR"
  say "  3) 写 manifest → $SNAP_DIR/$MANIFEST_NAME"
  say "  4) 备份并替换入口 → $SKILL_DEST"
  exit 0
fi

if [ "$ASSUME_YES" != 1 ]; then
  printf '继续安装？[y/N] '
  read -r ans </dev/tty || ans="n"
  case "$ans" in y|Y|yes|YES) ;; *) die "用户取消" 2 ;; esac
fi

# ---------------------------------------------------------------------------
# 1) 快照（先写临时目录，全部成功后再原子改名 → 不留半装状态）
# ---------------------------------------------------------------------------
mkdir -p "$TOOLS_HOME/$TOOL_NAME"
TMP_SNAP="$TOOLS_HOME/$TOOL_NAME/.tmp-$$"
rm -rf "$TMP_SNAP"
mkdir -p "$TMP_SNAP"

cleanup() { rm -rf "$TMP_SNAP" 2>/dev/null || true; }
trap cleanup EXIT

say ""
say "→ 复制文件到临时快照"
cp -R "$REPO_ROOT/scripts" "$TMP_SNAP/scripts"
mkdir -p "$TMP_SNAP/tools"
cp -R "$REPO_ROOT/tools/macos" "$TMP_SNAP/tools/macos"
[ -d "$REPO_ROOT/tools/windows" ] && cp -R "$REPO_ROOT/tools/windows" "$TMP_SNAP/tools/windows" || true
cp -R "$REPO_ROOT/tests" "$TMP_SNAP/tests" 2>/dev/null || true
cp "$REPO_ROOT/SKILL.md" "$TMP_SNAP/SKILL.md"
[ -f "$REPO_ROOT/README.md" ] && cp "$REPO_ROOT/README.md" "$TMP_SNAP/README.md" || true

# 编译好的录制器**不进**快照：它是本机构建产物，按机器重编
rm -rf "$TMP_SNAP/tools/macos/build"
# 夹具二进制也不进（源码进，二进制按需构建）
find "$TMP_SNAP" -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true

# ---------------------------------------------------------------------------
# 2) manifest（安装对账口径）
# ---------------------------------------------------------------------------
python3 - "$TMP_SNAP" "$VERSION" "$REPO_ROOT" "$SKILL_DEST" <<'PY'
import hashlib, json, os, sys
from pathlib import Path
snap, version, repo, skill_dest = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
snap_p = Path(snap)
key = ["SKILL.md", "scripts/agent_capture.py", "scripts/capture_state.py",
       "scripts/capture_targets.py", "scripts/capture_worker.py",
       "tools/macos/backend.py", "tools/macos/main.swift"]
files = {}
for rel in key:
    p = snap_p / rel
    if p.exists():
        files[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
man = {
    "tool": "agent-capture",
    "skill": "window-recording",
    "version": version,
    "source_repo": str(repo),
    "skill_entry": skill_dest,
    "installed_key_sha256": files,
    "note": "只记录本工具自有入口；不含任何凭据、媒体或私人路径。",
}
(snap_p / ".agent-capture-install.json").write_text(
    json.dumps(man, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print("  manifest: %d 个关键文件已记录 sha256" % len(files))
PY

if [ -e "$SNAP_DIR" ]; then
  rm -rf "$SNAP_DIR"
fi
mv "$TMP_SNAP" "$SNAP_DIR"
trap - EXIT
ok "快照就位：$SNAP_DIR"

# ---------------------------------------------------------------------------
# 3) 入口：备份旧的，再建新软链（原子 replace）
# ---------------------------------------------------------------------------
say ""
say "→ 更新 skill 入口"
mkdir -p "$SKILL_HOME" "$BACKUP_ROOT"
if [ -e "$SKILL_DEST" ] || [ -L "$SKILL_DEST" ]; then
  B="$BACKUP_ROOT/$(basename "$SKILL_DEST").$(date +%Y%m%d%H%M%S)"
  mv "$SKILL_DEST" "$B"
  ok "旧入口已备份：$B"
fi
ln -s "$SNAP_DIR" "$SKILL_DEST"
ok "入口已指向快照：$SKILL_DEST → $SNAP_DIR"

# ---------------------------------------------------------------------------
# 4) 回读校验（装完必须能读回来，而不是"应该装好了"）
# ---------------------------------------------------------------------------
say ""
say "→ 回读校验"
[ -f "$SKILL_DEST/SKILL.md" ] || die "回读失败：$SKILL_DEST/SKILL.md 不存在" 4
head -3 "$SKILL_DEST/SKILL.md" | grep -q "name: $SKILL_NAME" \
  || die "回读失败：SKILL.md 的 name 不是 $SKILL_NAME" 4
ok "SKILL.md 可读且 name 正确"

python3 "$SKILL_DEST/scripts/agent_capture.py" capability >/dev/null 2>&1 \
  && ok "CLI 可执行（capability 正常）" \
  || warn "CLI 暂时不可用（可能需要先构建录制器：$SKILL_DEST/tools/macos/build.sh）"

# 录制器是否已构建（不自动构建：编译要几分钟，应由用户显式触发）
if [ -x "$SKILL_DEST/tools/macos/build/GameAVRec.app/Contents/MacOS/GameAVRec" ]; then
  ok "录制器已构建"
else
  warn "录制器尚未构建 → 跑：$SKILL_DEST/tools/macos/build.sh"
fi

say ""
ok "安装完成（版本 ${VERSION}）"
say "  卸载：rm -rf '$SKILL_DEST' '$SNAP_DIR'"
say "  回滚：mv '$SKILL_DEST' /tmp/x && mv '$BACKUP_ROOT/<最新备份>' '$SKILL_DEST'"
