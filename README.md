# agent-capture

录制**一个明确指定的窗口或应用**（画面与音频可分开指定），并给出**可核对**的录像完整性结论。

```
capability → targets → preflight → start → status → stop → verify → report
```

它是 [`window-recording`](SKILL.md) skill 背后的工具层，也是
[`agent-gameplay-studio`](https://github.com/enderzcx/agent-gameplay-studio)
的 `gameplay-production` 总控所用的采集端。

---

## 三条不会妥协的规则

1. **没有默认目标。** 不给选择器 = 用法错误。绝不"那就录整屏吧"。
2. **给了目标但没命中 = 失败。** 绝不回退去录别的 app 或窗口。
3. **未知不报通过。** 拿不到证据的结论一律 `unknown`。

`verified_level` 只有 `recorded` 才表示"真的录过"；编译通过、mock 通过都不算。

---

## 安装

```bash
git clone https://github.com/enderzcx/agent-capture.git
cd agent-capture

# 看计划，不写任何文件
./install.sh --dry-run

# 安装（默认 $SKILL_HOME=~/.agents/skills，$TOOLS_HOME=~/.local/share）
./install.sh --yes

# 自定义位置
./install.sh --skill-home ~/.dsh/skills --tools-home ~/.local/share --yes
```

安装器行为：

- **钉版本快照**：工具装进 `$TOOLS_HOME/agent-capture/<版本>/`，
  `$SKILL_HOME/window-recording` 是指向该快照的软链 —— "装的到底是哪一版"永远可查；
- **只动自有入口**：不碰任何别的 skill、不碰全局配置、不碰任何既有插件凭据；
- **冲突即停**：目标已存在但**不是本工具装的**（没有 manifest）→ 拒绝，不覆盖别人的东西；
- **先备份再替换**：旧入口移进 `$TOOLS_HOME/agent-capture/.backups/`；
- **回读校验**：装完立刻读回 `SKILL.md` 与 CLI，失败即报错。

卸载 / 回滚：

```bash
rm -rf ~/.agents/skills/window-recording ~/.local/share/agent-capture/<版本>
# 回滚到某个备份
mv ~/.agents/skills/window-recording /tmp/x
mv ~/.local/share/agent-capture/.backups/window-recording.<时间戳> ~/.agents/skills/window-recording
```

> 录制器是**本机构建产物**，不进版本库。安装后需要自己编一次：
> ```bash
> ~/.agents/skills/window-recording/tools/macos/build.sh
> ```

---

## 用法

```bash
CAP=./scripts/agent_capture.py     # 或安装后的 <快照>/scripts/agent_capture.py

# 1) 这个后端能做什么、验证到哪一层
python3 $CAP capability

# 2) 现在有哪些可选目标（应用 + 在屏窗口，含所属 app）
python3 $CAP targets

# 3) 采集前必做预检（会用 live 事实把选择器解析成确定身份再比对）
python3 $CAP preflight --video-app com.example.Game --video-window 12345 --audio-app com.example.Game

# 4) 开始录（立刻返回；worker 在后台推进状态）
python3 $CAP start \
  --video-app com.example.Game --video-window 12345 --audio-app com.example.Game \
  --job-dir ./run --run-id game-01 --out ./run/game-01.mp4 --duration 120 --expect av

# 5) 查状态 / 停止 / 复核
python3 $CAP status --job-dir ./run --run-id game-01
python3 $CAP stop   --job-dir ./run --run-id game-01
python3 $CAP verify --job-dir ./run --run-id game-01

# 6) 产出给 gameplay-production 用的规范化 report（只读证据，不手填结论）
python3 $CAP report --job-dir ./run --run-id game-01 \
  --production-run-id prod-01 --out ./run/game-01.report.json
```

### 选择器语义

| 想录什么 | 怎么给 |
|---|---|
| 某个 app 的全部画面（macOS） | `--video-app <bundle id 或名字>` |
| **只录某一个窗口** | `--video-window <窗口id>`；按标题则**必须**同时给 `--video-app` |
| 只要画面、不要声音 | `--audio-mode none` |
| 音频跟随画面 app（默认） | 不用给 |

- `--video-window` 给**数字**按窗口 id 精确匹配；给文字按标题匹配。
- **画面与音频必须指向同一个 app**：ScreenCaptureKit 的音频过滤跟随画面 filter，
  一条流做不到"录 A 窗口的画面 + 只要 B app 的声音" → 这种情况**直接报错**。

---

## 平台能力（如实标注）

| 能力 | macOS 后端 | Windows 后端 |
|---|---|---|
| 画面：单 app | ✅ 已实现 | ❌ 未实现（只做单窗口） |
| 画面：单窗口 | ✅ `desktopIndependentWindow` | ✅ 已实现（协议层验证；**未实机**） |
| 音频：单 app | ✅ 已实现 | ✅ 已实现（**未实机**） |
| 音频：单窗口 | ❌ 框架无此粒度 | ❌ 同 |
| 整屏 / 整机混音 / 麦克风 | ❌ **本后端未实现**，会被拒 | ❌ 同 |

- macOS `verified_level = compile_only`（真实采集证据见任务交接目录）。
- **Windows `verified_level = none`**：`tools/windows/` 的 obs-websocket 后端是
  **实验性**的，本机无 Windows 实机、也无授权凭据 → **没有证明能录到画面或声音**。
  mock 只证明协议与状态机，**不把 mock 当实机**。

---

## 测试

```bash
./tests/run_offline_tests.sh
```

**不需要任何权限、显示器、目标 app 或网络。** 覆盖：目标解析/防误录/跨后端能力边界、
状态机/并发/原子写/CAS/版本绑定、worker 反例（令牌/期望轨道/提前退出/组合判定/清理）、
CLI 分发与退出码、capture report 的"无证据不许 true"。

真实采集（需要屏幕录制权限）**不在这里**，它单独跑，且**不会**被算作通过。

录制器自己的离线回归：

```bash
GAME_BUNDLE_ID=<你的目标 app bundle id> ./tools/macos/tests/regression.sh
```

---

## "有效开始"是什么意思

进程起来了**不等于**录到了东西。四个里程碑分开报：

| 里程碑 | 含义 |
|---|---|
| `capture_initialized` | `startCapture` 返回 |
| `first_video_frame` | **真的收到第一帧画面** ← 与上一条合起来才算"有效开始" |
| `audio_path_has_data` | 音频通路真的有采样到达 |
| `audio_signal_observed` | 音频**实际超过静音门槛** |

`audio_signal_observed` 为假**不是失败**：游戏开局前本来就是静音的。
把它当失败条件会**死锁等一个永远不来的信号**。

---

## verify 证明了什么、没证明什么

**证明**：音轨存在且逐秒有超过门槛的信号；帧在持续到达**且抽帧内容真的在变**
（"PTS 在走"不等于"画面在变"）；容器级音视频时长差。

**不证明**：音画同步、画面内容质量、"这段声音确实是目标应用发出的"。

---

## 边界

- 锁面向**本地文件系统**（APFS/ext4/NTFS）；网络文件系统未验证。
- 录制产物、run 状态、媒体**都不进版本库**（见 `.gitignore`）。
- 本仓库只放**通用代码、合成 fixtures 与文档**：不含任何原片、会话、密钥、字体或私人路径。

许可证：MIT。
