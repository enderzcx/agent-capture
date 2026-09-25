#!/usr/bin/env bash
# 离线测试总入口。**不需要任何权限、显示器、目标 app 或网络。**
#
#   ./tests/run_offline_tests.sh
#
# 退出码：0 = 全过；非 0 = 有失败。
#
# 这里只跑**离线**可判定的东西：目标解析/防误录/跨后端能力边界、
# 状态机/并发/原子写/CAS/版本绑定/路径逃逸。
# 真实采集（需要屏幕录制权限）**不在**这里，它在 tests/live/ 下单独跑，
# 且不会被本脚本算作通过。
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
PY="${PY:-python3}"

rc=0
run() {
  local name="$1"; shift
  printf '\n\033[36m═══ %s ═══\033[0m\n' "$name"
  if "$@"; then :; else rc=1; fi
}

run "capture_targets（目标防误录 / live 校对 / 后端能力边界）" \
    "$PY" "$ROOT/tests/test_capture_targets.py"

run "capture_state（状态机 / 并发 / 原子写 / CAS / 版本绑定）" \
    "$PY" "$ROOT/tests/test_capture_state.py"

run "capture_worker（假录制器反例：令牌/期望轨道/提前退出/组合判定/清理）" \
    "$PY" "$ROOT/tests/test_capture_worker.py"

run "agent_capture CLI（命令分发/退出码/生命周期/不覆盖）" \
    "$PY" "$ROOT/tests/test_agent_capture_cli.py"

run "capture report（无证据不许 true：缺 metrics/无令牌/无产物/帧不连续）" \
    "$PY" "$ROOT/tests/test_capture_report.py"

run "Cloud 收尾三项（完整性判定 / duration 拒绝 / Windows 桥接语义）" \
    "$PY" "$ROOT/tests/test_final_regressions.py"

run "--no-audio 条件路径（真的不录音频 / 对照 / 腐败文件仍失败）" \
    "$PY" "$ROOT/tests/test_no_audio.py"

run "Windows 统一 CLI 假 transport 合同（unavailable / owner-only stop / 薄分派）" \
    "$PY" "$ROOT/tests/test_windows_cli_contract.py"

printf '\n'
if [ "$rc" -eq 0 ]; then
  printf '\033[32m离线测试全部通过\033[0m\n'
  printf '注意：这只说明**离线可判定的部分**成立，不代表真实采集可用。\n'
else
  printf '\033[31m离线测试有失败\033[0m\n' >&2
fi
exit "$rc"
