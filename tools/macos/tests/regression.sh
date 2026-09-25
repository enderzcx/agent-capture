#!/usr/bin/env bash
# GameAVRec + record-game.sh 回归。
#
#   ./tests/regression.sh                 # 只跑离线用例（不需要任何权限）
#   GAME_BUNDLE_ID=<id> ./tests/regression.sh
#                                         # 加跑需要「屏幕与系统音频录制」权限的用例
#
# 为什么要分两组：下面这套是**真录制**，需要 TCC 权限、一块可捕获的显示器、
# 以及一个真的在出声的目标 app。没有这些条件时它只能 SKIP，不能假装通过；
# 所以权限类用例必须显式给 GAME_BUNDLE_ID 才跑。
#
# 离线组（永远跑，不需要任何权限）：
#   O1  未指定目标 = 用法错误（退出码 64），且在申请权限之前就失败、不产出文件
#   O2  --help 可用，且明确说明「本工具不内置默认目标」
#   O3  verify 对「数字静音」判失败，并报告逐秒覆盖范围（纯合成文件）
#   O4  verify 对「只有 1 帧」的视频结构化失败（纯合成文件）
#   O5  verify 的 --expect 模式不匹配要失败（av vs 纯音频，纯合成文件）
#   O6  record-game.sh 缺 GAME_BUNDLE_ID 时拒绝（退出码 64）
#   O7  **默认不覆盖**：--out 已存在时退出码 3、原文件字节不变，
#       且这是在**申请权限之前**发生的（所以离线可测）
#   O8  **默认不覆盖**同样适用于 --log（退出码 3）
#
# 权限组（需 GAME_BUNDLE_ID + 屏幕录制权限）：
#   P1  显式目标没命中 → 退出码 2，且**不**回退去抓别的 app、不产出文件
#   P2  --overwrite 明确给出后才允许覆盖（先过闸门，再真录一次）
#   P3  实录音频产物用 --expect audio 通过
#   P4  --no-video 走音频模式校验（start 自动切 --expect audio）
#   P5  时间基准 last_pts_rel 是相对值；状态/退出码/轨道三者自洽
#   P6  --log tee 不自我反馈（日志不爆炸式增长）
#   P7  早停（SIGTERM 早于 startCapture 完成）必须有界、有终态、不报成功
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RS="$HERE/../record-game.sh"
BIN="$HERE/../build/GameAVRec.app/Contents/MacOS/GameAVRec"
TARGET="${GAME_BUNDLE_ID:-}"
TMP="$(mktemp -d /tmp/gameavrec-regress.XXXXXX)"
pass=0; fail=0; skip=0

ck() { # ck <名字> <期望> <实际> [附加说明]
  if [ "$2" = "$3" ]; then printf '\033[32m✓\033[0m %s\n' "$1"; pass=$((pass+1))
  else printf '\033[31m✗\033[0m %s（期望 %s，实际 %s）%s\n' "$1" "$2" "$3" "${4:-}"; fail=$((fail+1)); fi
}
skipping() { printf '\033[33m−\033[0m SKIP %s\n' "$1"; skip=$((skip+1)); }
need() { command -v "$1" >/dev/null 2>&1 || { echo "缺少依赖：$1" >&2; exit 2; }; }

need python3
need ffmpeg
need ffprobe
[ -x "$BIN" ] || { echo "没编译：先跑 $HERE/../build.sh" >&2; exit 2; }

echo "═══ 离线组（临时目录 ${TMP}）═══"

# O1 没有目标 = 用法错误（本工具刻意不内置默认目标），且在申请权限之前就失败
out1="$TMP/notarget.mp4"
"$BIN" --out "$out1" --duration 2 --quiet >"$TMP/notarget.log" 2>&1
rc1=$?
ck "O1 未指定目标 → 用法错误（退出码 64）" "64" "$rc1"
ck "O1 且不产出文件" "no" "$([ -e "$out1" ] && echo yes || echo no)"
ck "O1 报错写明必须显式指定目标" "yes" "$(grep -q '必须显式指定录制目标' "$TMP/notarget.log" && echo yes || echo no)"

# O2 --help
"$BIN" --help >"$TMP/help.log" 2>&1
ck "O2 --help 退出码 0" "0" "$?"
ck "O2 --help 写明不内置默认目标" "yes" "$(grep -q '本工具不内置默认目标' "$TMP/help.log" && echo yes || echo no)"

# O3 数字静音必须判失败，且要报告覆盖范围（不是只报一个峰值就下结论）
sil="$TMP/silent.mp4"
ffmpeg -hide_banner -v error -f lavfi -i "color=c=black:s=64x64:r=30:d=5" \
  -f lavfi -i "anullsrc=channel_layout=stereo:sample_rate=48000" -t 5 \
  -c:v libx264 -pix_fmt yuv420p -c:a aac -y "$sil" >/dev/null 2>&1
"$RS" verify "$sil" --expect av >"$TMP/sil.log" 2>&1
ck "O3 数字静音判失败（退出码 2）" "2" "$?"
ck "O3 报告逐秒覆盖范围" "yes" "$(grep -q '逐秒检查覆盖' "$TMP/sil.log" && echo yes || echo no)"
ck "O3 打印逐秒有声桶占比" "yes" "$(grep -q '有声桶：' "$TMP/sil.log" && echo yes || echo no)"

# O4 只有 1 帧的视频 → 结构化失败
one="$TMP/one-frame.mp4"
ffmpeg -hide_banner -v error -f lavfi -i "color=c=black:s=64x64:r=1:d=1" \
  -f lavfi -i "sine=frequency=440:duration=1" -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest -y "$one" >/dev/null 2>&1
frames=$(ffprobe -v error -select_streams v:0 -count_frames -show_entries stream=nb_read_frames -of csv=p=0 "$one")
"$RS" verify "$one" --expect av >"$TMP/one.log" 2>&1
ck "O4 1 帧视频被判失败（退出码 2，实际帧数 ${frames}）" "2" "$?"
ck "O4 失败原因写明只有 1 帧" "yes" "$(grep -q '只有 1 帧' "$TMP/one.log" && echo yes || echo no)"

# O5 模式不匹配：有信号的纯音频，用 --expect av 必须失败
aonly="$TMP/audio-only.m4a"
ffmpeg -hide_banner -v error -f lavfi -i "sine=frequency=440:duration=3" \
  -c:a aac -y "$aonly" >/dev/null 2>&1
ck "O5 纯音频用 --expect audio 通过" "0" "$("$RS" verify "$aonly" --expect audio >"$TMP/a_ok.log" 2>&1; echo $?)"
ck "O5 同一文件 --expect av 失败（模式不匹配）" "2" "$("$RS" verify "$aonly" --expect av >"$TMP/a_av.log" 2>&1; echo $?)"
ck "O5 失败原因写明没有视频轨" "yes" "$(grep -q '没有视频轨' "$TMP/a_av.log" && echo yes || echo no)"

# O6 record-game.sh 的用法闸门（不需要权限）
out6="$TMP/preflight-noenv.log"
( unset GAME_BUNDLE_ID; "$RS" preflight >"$out6" 2>&1 )
ck "O6 无 GAME_BUNDLE_ID 时 preflight 拒绝（退出码 64）" "64" "$?"
ck "O6 拒绝原因写明缺少录制目标" "yes" "$(grep -q '缺少录制目标' "$out6" && echo yes || echo no)"

# O7 **默认不覆盖**：--out 命中已存在文件时必须失败关闭。
#    关键点：这个闸门在 `let rec = Recorder(...)` / `Task { start() }` **之前**执行，
#    所以它不需要屏幕录制权限就能验 —— 一个安全属性不该等到有权限才被测。
out7="$TMP/existing.mp4"
printf 'do not clobber me\n' > "$out7"
sha7_before=$(shasum -a 256 "$out7" | cut -d' ' -f1)
"$BIN" --bundle-id com.example.any --out "$out7" --duration 1 --quiet >"$TMP/ovw.log" 2>&1
ck "O7 --out 已存在时默认拒绝覆盖（退出码 3）" "3" "$?"
ck "O7 原文件字节未被改动" "$sha7_before" "$(shasum -a 256 "$out7" | cut -d' ' -f1)"
ck "O7 报错写明退出码 3 与两条出路" "yes" \
   "$(grep -q '退出码 3' "$TMP/ovw.log" && grep -q -- '--overwrite' "$TMP/ovw.log" && echo yes || echo no)"

# O8 同一个「默认不覆盖」也适用于 --log，而不是只管 --out
out8="$TMP/logconflict.mp4"; log8="$TMP/tee.log"
: > "$log8"
"$BIN" --bundle-id com.example.any --out "$out8" --log "$log8" --duration 1 --quiet \
  >"$TMP/logconflict.log" 2>&1
ck "O8 --log 已存在时默认拒绝覆盖（退出码 3）" "3" "$?"
ck "O8 且不产出 --out 文件" "no" "$([ -e "$out8" ] && echo yes || echo no)"

if [ -z "$TARGET" ]; then
  echo
  echo "═══ 权限组：未设置 GAME_BUNDLE_ID → 跳过（这些用例需要屏幕录制权限 + 真在出声的目标 app）═══"
  for c in "P1 目标没命中的失败路径" "P2 --overwrite 明确给出后才真覆盖" "P3 实录音频 verify 通过" \
           "P4 --no-video 音频模式" "P5 时间基准与状态自洽" "P6 --log tee 不自我膨胀" \
           "P7 早停有界且有终态"; do skipping "$c"; done
  echo
  echo "═══ 结果：通过 ${pass}，失败 ${fail}，跳过 ${skip} ═══"
  [ "$fail" -eq 0 ] || exit 1
  rm -rf "$TMP"
  exit 0
fi

echo
echo "═══ 权限组（目标 GAME_BUNDLE_ID=${TARGET}）═══"

# P1 显式目标没命中 → 必须失败，且不能产出文件、不能回退
outP1="$TMP/miss.mp4"
"$BIN" --bundle-id "$TARGET" --app-name "__no_such_app__" --out "$outP1" --duration 2 --quiet \
  >"$TMP/miss.log" 2>&1
ck "P1 显式目标没命中即失败（退出码 2）" "2" "$?"
ck "P1 且不产出文件" "no" "$([ -e "$outP1" ] && echo yes || echo no)"
ck "P1 报错说明不回退" "yes" "$(grep -q '拒绝改抓别的应用' "$TMP/miss.log" && echo yes || echo no)"

# P2/P3 覆盖闸门本身已在离线组 O7/O8 验过（它在申请权限之前执行）。
#        这里只验「显式给了 --overwrite 之后，真能重录一次」这一半。
outP2="$TMP/existing.mp4"
"$RS" start --out "$outP2" --duration 2 >/dev/null 2>&1
if [ -f "$outP2" ]; then
  sha_before=$(shasum -a 256 "$outP2" | cut -d' ' -f1); size_before=$(stat -f%z "$outP2")
  "$RS" start --out "$outP2" --duration 2 >"$TMP/clobber.log" 2>&1
  ck "P2 已存在默认拒绝覆盖（退出码 2）" "2" "$?"
  ck "P2 原文件字节不变" "$sha_before/$size_before" "$(shasum -a 256 "$outP2" | cut -d' ' -f1)/$(stat -f%z "$outP2")"
  "$BIN" --bundle-id "$TARGET" --out "$outP2" --duration 2 --overwrite --quiet >/dev/null 2>&1
  ck "P2 --overwrite 允许覆盖（退出码 0）" "0" "$?"
  sha_after=$(shasum -a 256 "$outP2" | cut -d' ' -f1)
  ck "P2 覆盖后内容确实变了" "changed" "$([ "$sha_before" != "$sha_after" ] && echo changed || echo same)"
else
  skipping "P2 首次录制失败，覆盖语义无法验证"
fi

# P3 实录音频产物（--no-video）用 --expect audio 校验
outP3="$TMP/novideo.m4a"
"$RS" start --out "$outP3" --duration 3 --no-video >"$TMP/startP3.log" 2>&1
ck "P3 start --no-video 自动按音频模式校验并通过" "0" "$?"
ck "P3 走的是 expect=audio" "yes" "$(grep -q 'expect=audio' "$TMP/startP3.log" && echo yes || echo no)"

# P4/P5 时间基准与状态自洽
rP5="$TMP/pts.mp4"; rP5j="$TMP/pts.metrics.json"; rP5s="$TMP/pts.status.json"
"$BIN" --bundle-id "$TARGET" --out "$rP5" --json "$rP5j" --status "$rP5s" --duration 4 --quiet >/dev/null 2>&1
ck "P5 录制成功（退出码 0）" "0" "$?"
read -r rP5ok rP5v < <(python3 -c "
import json;d=json.load(open('$rP5j'));v=d['video']
last=v['last_pts_rel']; first=v['first_pts_rel']; frames=v['frames']
ok = (0 <= first <= 2) and (first <= last <= 6) and frames > 0
print('yes' if ok else 'no', f'first={first:.3f} last={last:.3f} frames={frames}')
")
ck "P5 last_pts_rel 是相对时间（${rP5v}）" "yes" "$rP5ok"
ck "P5 status.state=done" "done" "$(python3 -c "import json;print(json.load(open('$rP5s'))['state'])")"
ck "P5 status.exit=0" "0" "$(python3 -c "import json;print(json.load(open('$rP5s'))['exit'])")"
ck "P5 writer_status=completed" "completed" "$(python3 -c "import json;print(json.load(open('$rP5j'))['writer_status'])")"
ck "P5 文件里确实有 1 条音轨" "1" "$(python3 -c "import json;print(json.load(open('$rP5j'))['file_tracks']['audio'])")"
ck "P5 文件里确实有 1 条视频轨" "1" "$(python3 -c "import json;print(json.load(open('$rP5j'))['file_tracks']['video'])")"
ck "P5 verdict 以 file_tracks 为依据" "file_tracks" "$(python3 -c "import json;print(json.load(open('$rP5j'))['verdict']['audio_track_present_basis'])")"

# P6 --log tee 不能自我反馈（原实现把日志写回同一管道 → 无限增长）
rP6="$TMP/tee.mp4"; rP6log="$TMP/tee.log"
"$BIN" --bundle-id "$TARGET" --out "$rP6" --duration 3 --log "$rP6log" >/dev/null 2>&1
sz=$(stat -f%z "$rP6log" 2>/dev/null || echo 0)
ck "P6 日志文件没有爆炸式增长（${sz} 字节 < 200KB）" "yes" "$([ "$sz" -lt 204800 ] && echo yes || echo no)"
ck "P6 日志确实收到了内容" "yes" "$(grep -q 'gameavrec' "$rP6log" && echo yes || echo no)"

# P7 早停（SIGTERM 可能早于 startCapture 完成）：必须有界、必须有终态、不得报成功
rP7="$TMP/abort.mp4"; rP7j="$TMP/abort.metrics.json"; rP7s="$TMP/abort.status.json"
"$BIN" --bundle-id "$TARGET" --out "$rP7" --json "$rP7j" --status "$rP7s" --duration 0 --quiet \
  >/dev/null 2>&1 &
pP7=$!; sleep 0.15; kill -TERM "$pP7" 2>/dev/null
exited=no
for _ in $(seq 1 24); do kill -0 "$pP7" 2>/dev/null || { exited=yes; break; }; sleep 0.25; done  # 上限 6s
killed=no
if [ "$exited" = no ]; then kill -KILL "$pP7" 2>/dev/null; killed=yes; fi
wait "$pP7" 2>/dev/null; rP7rc=$?
ck "P7 早停后 6s 内有界退出（killed=${killed}，rc=${rP7rc}）" "yes" "$exited"
term=no; { [ -f "$rP7s" ] || [ -f "$rP7j" ]; } && term=yes
ck "P7 早停也必须留下终态（status 或 sidecar）" "yes" "$term"
if [ "$term" = yes ]; then
  ck "P7 零轨道时状态必须自洽（非 done）" "yes" "$(python3 - "$rP7j" "$rP7s" <<'PYCHK'
import json, os, sys
m = json.load(open(sys.argv[1])) if os.path.exists(sys.argv[1]) else {}
s = json.load(open(sys.argv[2])) if os.path.exists(sys.argv[2]) else {}
a = (m.get("file_tracks", {}) or {}).get("audio") or 0
state = s.get("state", m.get("state"))
ok = (a > 0 and state == "done") or (a == 0 and state in ("failed", "unverified"))
print("yes" if ok else "no(a=%s,state=%s)" % (a, state))
PYCHK
)"
  ck "P7 退出码与状态一致（录到轨道=0，否则非 0）" "yes" "$(python3 - "$rP7j" "$rP7rc" <<'PYRC'
import json, os, sys
m = json.load(open(sys.argv[1])) if os.path.exists(sys.argv[1]) else {}
a = (m.get("file_tracks", {}) or {}).get("audio") or 0
rc = int(sys.argv[2])
print("yes" if ((a > 0 and rc == 0) or (a == 0 and rc != 0)) else "no(a=%s,rc=%s)" % (a, rc))
PYRC
)"
fi

echo
echo "═══ 结果：通过 ${pass}，失败 ${fail}，跳过 ${skip} ═══"
[ "$fail" -eq 0 ] || exit 1
rm -rf "$TMP"
