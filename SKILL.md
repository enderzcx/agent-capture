---
name: window-recording
description: "录制一个**明确指定**的窗口或应用（画面与音频可分开指定），并给出可核对的录像完整性结论：targets/preflight/start/status/stop/verify。Use when 需要把某个 app 或某个窗口的画面连同它自己的声音录下来（游戏录屏、复现问题、留证据），尤其是要后台录制、不抢前台、不希望录到别的应用声音时。Not for 整屏/整机混音录制（本后端未实现，会被拒）、麦克风录制（未实现）、视频剪辑与后期（交给 gameplay-postproduction）、以及游戏对局决策质量评测（与录制无关）。"
license: MIT
compatibility: 需要 Python 3.9+（纯标准库）与 macOS 15+；采集依赖仓库内 tools/macos 的 ScreenCaptureKit 录制器（用 build.sh 自行编译），并需要「屏幕与系统音频录制」权限。verify 需要 ffprobe/ffmpeg（不在 PATH 里也能找到常见安装位置）。
metadata:
  short-description: 指定窗口/应用的音视频录制与完整性核对
  sunny_skill_type: wrapper
  external_tool: agent-capture
---

# Window Recording

把一个**明确指定**的窗口或应用录下来，并如实说明"到底录到了什么"。

它是**对 `agent-capture` CLI 的包装**：规范与口径在这里，采集与判定在 CLI 里。
本 skill 刻意保持轻量 —— 录制状态本身由 CLI 的 JSON 输出承载，
**不在这里再造一套模板**。

## Boundary

**Own：** 目标选择语义（画面/音频可不同粒度）、录制生命周期
（targets → preflight → start → status → stop → verify）、
"有效开始"的判定、录像完整性的**实测**结论。

**Not own：**
- 剪辑、合成、字幕、解说、审片 → `gameplay-postproduction`
- 整屏 / 整机混音 / 麦克风 → **本后端未实现，直接拒绝**（不静默降级）
- 游戏对局质量评测 → 与录制完全无关

## 三条不会妥协的规则

1. **没有默认目标。** 不给选择器 = 用法错误。绝不"那就录整屏吧"。
2. **给了目标但没命中 = 失败。** 绝不回退去录别的 app 或窗口。
3. **未知不报通过。** 拿不到证据的结论一律 `unknown`。
   `verified_level` 只有 `recorded` 才表示"真的录过"；编译通过、mock 通过都不算。

## 用法

先确认这台机器上这个后端能做什么：

```bash
python3 scripts/agent_capture.py capability
```

看当前有哪些可选目标（应用 + 在屏窗口，含所属 app）：

```bash
python3 scripts/agent_capture.py targets
```

**采集前必须预检**（它会用 live 事实把选择器解析成确定身份再比对）：

```bash
python3 scripts/agent_capture.py preflight \
  --video-app com.example.Game --video-window 12345 --audio-app com.example.Game
```

真正开始录（立刻返回；worker 在后台推进状态）：

```bash
python3 scripts/agent_capture.py start \
  --video-app com.example.Game --video-window 12345 --audio-app com.example.Game \
  --job-dir ./run --run-id game-01 --out ./run/game-01.mp4 --duration 120 --expect av
```

查状态 / 停止 / 复核：

```bash
python3 scripts/agent_capture.py status --job-dir ./run --run-id game-01
python3 scripts/agent_capture.py stop   --job-dir ./run --run-id game-01
python3 scripts/agent_capture.py verify --job-dir ./run --run-id game-01
```

## 选择器语义（重要）

| 想录什么 | 怎么给 |
|---|---|
| 某个 app 的全部画面 | `--video-app <bundle id 或名字>` |
| **只录某一个窗口** | `--video-window <窗口id>`（精确）；按标题则**必须**同时给 `--video-app` |
| 只要画面、不要声音 | `--audio-mode none` |
| 音频跟随画面 app（默认） | 不用给；或显式 `--audio-app <同一个 app>` |

- `--video-window` 给**数字**按窗口 id 精确匹配；给文字按标题匹配，
  此时必须用 `--video-app` 把范围钉死（标题会变、也可能重名）。
- **画面与音频必须指向同一个 app**。ScreenCaptureKit 的音频过滤跟随画面 filter，
  一条流做不到"录 A 窗口的画面 + 只要 B app 的声音" → 这种情况**直接报错**，
  不会静默只满足其中一个。

## 各平台的真实能力（不要用局部实现断言 OS 能力）

| 能力 | macOS 后端 | Windows 后端 |
|---|---|---|
| 画面：单 app | ✅ 已实现 | ❌ **未实现**（只做单窗口，不靠改名声称支持） |
| 画面：单窗口 | ✅ 已实现（`desktopIndependentWindow`） | ✅ 已实现（协议层已验证；**未实机**） |
| 音频：单 app | ✅ 已实现 | ✅ 已实现（**未实机**；粒度是 process/app，不是窗口独占） |
| 音频：单窗口 | ❌ 框架本身没有这个粒度 | ❌ 同 |
| 整屏 / 整机混音 / 麦克风 | ❌ **本后端未实现**，会被拒 | ❌ 同（且**拒绝**接入全局音/麦克风） |

Windows 的 `verified_level` 恒为 `none` 直到有实机证据 ——
mock 只证明协议与状态机，**不证明能录到画面或声音**。

## "有效开始"是什么意思

进程起来了**不等于**录到了东西���CLI 把四件事分开报：

| 里程碑 | 含义 |
|---|---|
| `capture_initialized` | `startCapture` 返回（采集初始化） |
| `first_video_frame` | **真的收到第一帧画面** ← 与上一条合起来才算"有效开始" |
| `audio_path_has_data` | 音频通路真的有采样到达 |
| `audio_signal_observed` | 音频**实际超过静音门槛** |

`audio_signal_observed` 为假**不是失败**：游戏开局前本来就是静音的。
把它当失败条件会**死锁等一个永远不来的信号**。状态只在
`capture_initialized && first_video_frame` 都为真后才推到 `running`；
超时则判 `failed`，**不会**假装在录。

**Windows 后端的可观测性更弱，必须如实报 `unknown`**：obs-websocket 不传帧、
也没有音频采样，所以 `first_video_frame` 与 `audio_signal_observed` 在 Windows 上
**拿不到**。`outputBytes` 在涨**绝不**等于"录到了画面"——它只证明输出在写。
不要把这两个里程碑用"StartRecord 成功 + 字节在涨"顶替。

## 结论怎么读

`status` 返回的 `summary` 里三个结论**互相独立**，不互相背书：

```json
{"capture_integrity": "pass", "verify_result": "pass",
 "game_result": "unknown", "postproduction_ready": "unknown",
 "all_known": false}
```

- `capture_integrity`：录像本身完整吗（轨道存在、帧在到达、音频有信号）
- `verify_result`：停止后探测的结论
- `game_result` / `postproduction_ready`：**录制层不知道**，所以是 `unknown`
  —— 这正是"未知不报通过"

## 停止是**请求**，不是杀进程

`stop` 写一个带 nonce 的停止请求；worker 核对 nonce 与发起者身份后，
才对录制器发 SIGTERM（录制器自己有界收尾）。
**不用旧 PID 直接杀** —— PID 会被复用，而且无法证明请求来源。

## verify 证明了什么、没证明什么

证明：音轨存在且逐秒有超过门槛的信号；帧在持续到达**且抽帧内容真的在变**
（"PTS 在走"不等于"画面在变"）；容器级音视频时长差。

**不**证明：音画同步（未做动作级核验）、画面内容质量、
"这段声音确实是目标应用发出的"（需要交叉核验，例如窄带能量测量）。
