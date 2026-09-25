#!/usr/bin/env bash
# 录制预检 + 启动 + 停止后探测（目标应用原声，app 级过滤，不碰麦克风/其他应用）
# 录制目标由环境变量 GAME_BUNDLE_ID 指定（例如 Chrome 是 com.google.Chrome）。
# **本脚本不内置任何默认目标**：抓错 app 会静默录到别的东西，所以必须显式给。
#
#   GAME_BUNDLE_ID=<your.app.bundle.id> ./record-game.sh preflight
#       只检：屏幕与系统音频录制权限 / 目标 app 是否命中 / 窗口是否在屏 / 抓取参数。
#
#   GAME_BUNDLE_ID=<id> ./record-game.sh start --out x.mp4 [--duration 60] [--no-video] [--focus-log x.jsonl] [--overwrite]
#       先 preflight，再录，再自动 verify。
#       **默认不覆盖**已存在的 mp4/metrics/focus（录制器退出码 3 = 拒绝覆盖）。
#       --no-video 只录音频，此时自动用 verify --expect audio 校验。
#
#   ./record-game.sh verify <file> [--expect av|audio|auto]
#       停止后探测。**只声称它真正测到的**：
#         · 音轨存在 + 单遍逐秒峰值（全覆盖，不分段截断）+ 有声桶占比
#         · 视频帧是否持续到达（PTS 间隔）**和**抽帧内容是否真的在变（md5）
#         · 容器级音视频时长差
#       **不**证明：音画同步（未做动作级核验）、画面内容质量、"这段声音是不是目标应用发出的"。
#
# 退出码：0 = 通过；2 = 未通过；3 = 拒绝覆盖；64 = 用法错误
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$HERE/build/GameAVRec.app"
BIN="$APP/Contents/MacOS/GameAVRec"
BUNDLE_ID="${GAME_BUNDLE_ID:-}"
# 有真实信号的判定线（约 -66 dBFS）；低于它 = 该秒只有静音
SIGNAL_FLOOR_DBFS="-66"
# 判"持续有声"的逐秒覆盖率门槛（低于它只报占比，不称"持续"）
VOICED_RATIO_MIN="0.90"

die() { printf '\033[31m✗ %s\033[0m\n' "$1" >&2; exit 2; }
usage_die() { printf '\033[31m用法错误: %s\033[0m\n' "$1" >&2; exit 64; }

[ -x "$BIN" ] || die "没编译：先跑 $HERE/build.sh"

cmd="${1:-}"; shift || true

# preflight/start 必须知道抓哪个 app；verify 只看已有文件，不需要目标。
case "$cmd" in
  preflight|start)
    [ -n "$BUNDLE_ID" ] || usage_die "缺少录制目标：先设 GAME_BUNDLE_ID=<目标 app 的 bundle id>（例如 com.google.Chrome）"
    ;;
esac

case "$cmd" in
  preflight)
    echo "═══ 目标应用原声录制预检 ═══"
    probe=$("$BIN" --probe --bundle-id "$BUNDLE_ID" 2>/dev/null) || die "probe 失败"
    python3 - "$probe" <<'PY'
import json,sys
d=json.loads(sys.argv[1])
if not d.get("cg_preflight_screen_capture"):
    print("✗ 没有屏幕录制权限"); sys.exit(2)
print("✓ 屏幕与系统音频录制权限：已具备")
print(f"✓ 目标选择器：{d.get('selector')}")
m=d.get("match")
if not m:
    # 显式目标没命中时，录制器会直接失败，不会回退抓别的 app
    print("✗ 目标未命中（不会回退抓别的 app）：" + str(d.get("hint") or "")); sys.exit(2)
print(f"✓ 命中目标：{m['name']} / {m['bundle_id']} pid={m['pid']}")
ws=d.get("match_windows") or []
on=[w for w in ws if w.get("on_screen")]
if not on:
    print("✗ 目标窗口不在屏上：app 级画面捕获拿不到帧（音频仍取决于游戏是否输出）"); sys.exit(2)
w=on[0]
print(f"✓ 在屏窗口：#{w['windowID']} {int(w['w'])}x{int(w['h'])} @({int(w['x'])},{int(w['y'])})")
print("✓ 录制参数：capturesAudio=true, captureMicrophone=false, app 过滤=只收该 app")
PY
    ;;

  start)
    out=""; dur="0"; novideo=""; focus=""; extra=()
    while [ $# -gt 0 ]; do
      case "$1" in
        --out) out="$2"; shift 2 ;;
        --duration) dur="$2"; shift 2 ;;
        --no-video) novideo="--no-video"; shift ;;
        --focus-log) focus="$2"; shift 2 ;;
        *) extra+=("$1"); shift ;;
      esac
    done
    [ -n "$out" ] || usage_die "缺少 --out"
    "$0" preflight >/dev/null || die "预检未通过，先修再录"
    args=(--bundle-id "$BUNDLE_ID" --out "$out" --json "${out%.*}.metrics.json" --duration "$dur" $novideo)
    [ -n "$focus" ] && args+=(--focus-log "$focus")
    "$BIN" "${args[@]}" "${extra[@]+"${extra[@]}"}"
    rc=$?
    if [ $rc -eq 3 ]; then die "拒绝覆盖已存在的素材/证据（换新路径，或显式 --overwrite）"; fi
    [ $rc -eq 0 ] || die "录制进程退出码 $rc"
    if [ -n "$novideo" ]; then "$0" verify "$out" --expect audio; else "$0" verify "$out" --expect av; fi
    ;;

  verify)
    f=""; expect="av"
    while [ $# -gt 0 ]; do
      case "$1" in
        --expect) expect="$2"; shift 2 ;;
        *) f="$1"; shift ;;
      esac
    done
    [ -n "$f" ] || usage_die "verify 需要文件路径"
    [ -f "$f" ] || die "文件不存在：$f"
    case "$expect" in av|audio|auto) ;; *) usage_die "--expect 只接受 av|audio|auto" ;; esac
    echo "═══ 停止后探测：${f}（expect=${expect}）═══"
    python3 - "$f" "$SIGNAL_FLOOR_DBFS" "$expect" "$VOICED_RATIO_MIN" <<'PY'
import json, re, subprocess, sys, hashlib
path, floor, expect, ratio_min = sys.argv[1], float(sys.argv[2]), sys.argv[3], float(sys.argv[4])

def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)
def run_bytes(cmd):
    return subprocess.run(cmd, capture_output=True).stdout
def ffprobe(args):
    return run(["ffprobe", "-v", "error"] + args + [path]).stdout

info = json.loads(ffprobe(["-show_entries",
    "stream=index,codec_type,codec_name,sample_rate,channels,duration,start_time",
    "-show_entries", "format=duration", "-of", "json"]) or "{}")
st = info.get("streams", [])
v = [s for s in st if s["codec_type"] == "video"]
a = [s for s in st if s["codec_type"] == "audio"]
fmt_dur = float(info.get("format", {}).get("duration") or 0)
fail, notes = [], []
print(f"  容器时长 {fmt_dur:.2f}s；视频轨 {len(v)}，音频轨 {len(a)}")

if not a:
    fail.append("没有音频轨")
if expect == "av" and not v:
    fail.append("expect=av 但没有视频轨（只录音频的产物请用 --expect audio）")
if expect == "audio" and v:
    notes.append("expect=audio 但文件里有视频轨：只按音频校验，视频未检查")
if expect == "auto" and not v:
    notes.append("auto：无视频轨，按音频模式校验")
need_video = (expect == "av") or (expect == "auto" and bool(v))

# ---------- 音频：单遍、逐秒、全覆盖 ----------
if a:
    print(f"  音频：{a[0].get('codec_name')} {a[0].get('sample_rate')}Hz {a[0].get('channels')}ch "
          f"start={a[0].get('start_time')} duration={a[0].get('duration')}")
    af = ("asetnsamples=n=48000,astats=metadata=1:reset=1,"
          "ametadata=mode=print:key=lavfi.astats.Overall.Peak_level:file=-")
    out = run(["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-af", af, "-f", "null", "-"]).stdout
    buckets, t = [], None
    for line in out.splitlines():
        m = re.search(r"pts_time:([\d.]+)", line)
        if m:
            t = float(m.group(1)); continue
        m = re.search(r"Peak_level=(-?[\d.]+|-inf)", line)
        if m and t is not None:
            buckets.append((t, float("-inf") if m.group(1) == "-inf" else float(m.group(1))))
            t = None
    if not buckets:
        fail.append("拿不到逐秒音频峰值（没有可解码的音频样本）")
    else:
        audio_dur = float(a[0].get("duration") or fmt_dur or 0)
        print(f"  逐秒检查覆盖：桶 0–{buckets[-1][0]:.0f}s，共 {len(buckets)} 个桶（音频时长 {audio_dur:.2f}s，末桶含补零）")
        voiced = [b for b in buckets if b[1] > floor]
        ratio = len(voiced) / len(buckets)
        pks = [b[1] for b in buckets if b[1] != float("-inf")]
        print(f"  逐秒峰值：最好 {max(pks):.1f} dBFS / 最差 {min(pks):.1f} dBFS（门槛 {floor} dBFS）"
              if pks else f"  逐秒峰值：全部 -inf（数字静音）")
        print(f"  有声桶：{len(voiced)}/{len(buckets)}（{ratio*100:.0f}%）")
        runs, cur = [], []
        for b in buckets:
            if b[1] > floor:
                cur.append(b[0])
            elif cur:
                runs.append(cur); cur = []
        if cur: runs.append(cur)
        if runs:
            print("  有声桶区间：" + ", ".join(f"{r[0]:.0f}-{r[-1]+1:.0f}s" for r in runs))
        if not voiced:
            fail.append(f"整段没有超过 {floor} dBFS 的秒（数字静音）——游戏当时没输出声音？")
        elif ratio < ratio_min:
            notes.append(f"有声占比 {ratio*100:.0f}% < {ratio_min*100:.0f}%：只能说「部分时段有信号」，"
                         f"不能称「逐秒持续有声」")
        else:
            print(f"  ✓ 有声占比 ≥ {ratio_min*100:.0f}%：可称「逐秒持续有信号」（是不是游戏声仍需交叉核验）")

# ---------- 视频：帧是否持续到达 + 内容是否真的在变 ----------
if need_video and v:
    vdur = float(v[0].get("duration") or fmt_dur or 0)
    raw = ffprobe(["-select_streams", "v:0", "-show_entries", "frame=pts_time", "-of", "csv=p=0"])
    ts = []
    for line in raw.splitlines():
        f0 = line.strip().rstrip(",").split(",")[0].strip()
        if not f0 or f0 == "N/A":
            continue
        try:
            ts.append(float(f0))
        except ValueError:
            pass
    if not ts:
        fail.append("视频没有帧")
    elif len(ts) == 1:
        fail.append("视频只有 1 帧：无法判断帧间隔与画面是否在变（结构化失败）")
    else:
        gaps = [b - x for x, b in zip(ts, ts[1:])]
        print(f"  视频：{len(ts)} 帧，最大帧间隔 {max(gaps):.3f}s（仅说明帧在持续到达）")
        if max(gaps) > 2.0:
            fail.append(f"帧到达有 {max(gaps):.1f}s 空档（采集卡住或窗口不在屏）")
        # 真正的"画面新鲜"：抽样比 md5
        n = min(12, max(3, int(vdur // 5) or 3))
        step = max(vdur / n, 0.5)
        times = [round(i * step, 2) for i in range(n) if i * step < max(vdur - 0.05, 0.1)]
        md5s = []
        for t0 in times:
            data = run_bytes(["ffmpeg", "-v", "error", "-ss", str(t0), "-i", path,
                              "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"])
            if data:
                md5s.append(hashlib.md5(data).hexdigest())
        if len(md5s) <= 1:
            fail.append("抽帧失败或只有一帧可比，无法判断画面内容是否在变")
        else:
            distinct = len(set(md5s))
            print(f"  画面内容抽样：{len(md5s)} 帧中 {distinct} 帧互不相同（抽帧点 {times}）")
            if distinct <= 1:
                fail.append("抽帧内容完全相同：画面疑似冻结（PTS 连续 ≠ 画面在变）")
            elif distinct < len(md5s):
                notes.append(f"{len(md5s)-distinct} 处抽帧重复（静态 UI 也可能如此，仅供参考）")

# ---------- 音视频容器级对照 ----------
if v and a:
    dv = float(v[0].get("duration") or 0)
    da = float(a[0].get("duration") or 0)
    if dv and da:
        print(f"  容器级：视频 {dv:.2f}s / 音频 {da:.2f}s，差 {abs(dv-da):.2f}s")
        if abs(dv-da) > 1.0:
            fail.append("音视频时长差 > 1s")
    print("  音画同步：**未验证**（本脚本只比容器时长与起止时间戳，未做动作级同步核验）")

for n in notes:
    print("  ! " + n)
if fail:
    print("\n✗ 未通过：" + "；".join(fail))
    sys.exit(2)
print("\n✓ 通过（仅限本脚本检查范围）：音轨存在且有超过门槛的信号；帧在持续到达且画面内容在变")
print("  本脚本不证明：音画同步、画面内容质量、『这段声音是游戏发出的』")
PY
    ;;

  *)
    sed -n '2,24p' "$0"
    exit 64
    ;;
esac
