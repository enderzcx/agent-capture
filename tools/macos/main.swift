// GameAVRec — 最小 app 级「游戏原声 + 画面」录制器（macOS ScreenCaptureKit）
//
// 目的：只抓目标游戏的音频（app 过滤），不碰麦克风、不收其他应用音频；
//       同时抓该 app 的画面，输出带音轨的 mp4 + 分段音频指标 sidecar。
//
// 关键 API：
//   SCContentFilter(display:includingApplications:exceptingWindows:)  → 按 app 过滤
//   SCStreamConfiguration.capturesAudio = true / captureMicrophone = false
//   AVAssetWriter（video h264 + audio AAC）→ 自己控制，便于统计真实信号
//
// 用法：
//   GameAVRec --probe
//   GameAVRec --out x.mp4 --duration 20 [--json x.metrics.json] [--no-video]

import Foundation
import AVFoundation
import CoreMedia
import CoreGraphics
import CoreAudio
import AppKit
import ScreenCaptureKit

// MARK: - 小工具

func logErr(_ s: String) {
    FileHandle.standardError.write(("[gameavrec] " + s + "\n").data(using: .utf8)!)
}

let ISO8601: ISO8601DateFormatter = {
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return f
}()

func jsonString(_ obj: [String: Any]) -> String {
    guard let d = try? JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys, .prettyPrinted]),
          let s = String(data: d, encoding: .utf8) else { return "{}" }
    return s
}

func writeFile(_ path: String, _ text: String) {
    do {
        let url = URL(fileURLWithPath: path)
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(),
                                                withIntermediateDirectories: true)
        try text.write(to: url, atomically: true, encoding: .utf8)
    } catch {
        logErr("写入 \(path) 失败: \(error)")
    }
}

// MARK: - 参数

struct Options {
    var out = ""
    var json = ""
    var statusFile = ""
    var logFile = ""
    // 刻意留空：本工具**不内置任何默认目标**。抓错 app 会静默录到别的东西，
    // 所以调用方必须显式给出 --bundle-id / --app-name / --pid 之一。
    var bundleId = ""
    var bundleIdSet = false
    var appName = ""
    var pid: Int = 0
    var fps = 30
    var duration = 0.0
    var maxWidth = 1920
    var maxSeconds = 3600.0
    var cursor = false
    var noVideo = false
    // `--no-audio`：**真的**不录音频。
    // 只在 worker 层"不要求声音"是不够的 —— 原生这里若照旧 capturesAudio=true
    // 并挂上音频输入/输出，就会**意外收到同一个 app 其它窗口的声音**，
    // 而调用方以为自己关掉了音频。必须 cfg / writer / output / 元数据四处一致关闭。
    var noAudio = false
    // 期望的窗口标题（pin）。
    // 窗口 ID 是稳定的，但**窗口里显示什么会变**：浏览器同一个窗口切个标签，
    // ID 不变、内容全变。实测踩到过一次 —— 探到的是本地 live viewer，
    // 起录时那个窗口已经切成了别的页面，于是"录到了完全不相干的东西"。
    // 给了这个参数就在命中窗口后**核对标题**，不一致直接失败。
    var expectWindowTitle = ""
    var probe = false
    var videoBitrate = 12_000_000
    var quiet = false
    var overwrite = false
    var focusLog = ""
    var focusInterval = 0.5
    // —— 单窗口粒度（--window / --window-title）——
    // 用 SCContentFilter(desktopIndependentWindow:) 真正按窗口过滤，
    // 而不是靠 sourceRect 裁剪近似（窗口移动/缩放时裁剪会漂）。
    var windowId = 0
    var windowTitle = ""
    // 期望的音频所属 app（bundle id）。用于**校验**，不是用来切换音源：
    // ScreenCaptureKit 的音频过滤跟随画面 filter，做不到"画面 A 窗口 + 只要 B app 的声音"。
    // 给了且与画面 app 不一致 → 直接报错，绝不静默只满足其中一个。
    var audioBundleId = ""
    // 窗口身份追踪日志：记录目标窗口是否还在、标题是否变了（默认沿用 focusLog 路径）
    var windowTrackLog = ""
    // 实时进度文件：录制过程中持续更新，让调用方区分
    //   ① 采集初始化（startCapture 返回）
    //   ② 有效首帧（真的收到第一帧画面）
    //   ③ 音频通路有数据（真的收到第一个音频采样）
    //   ④ 音频实际有信号（某个桶超过静音门槛）
    // 这四件事必须分开：进程起来了不等于录到了东西；游戏开局前没声音
    // 不等于采集失败，**不能**因此死锁等一个永远不来的信号。
    var liveStatus = ""
    // 本 run 的令牌：写进实时进度文件。调用方据此确认"这个文件是**这一次**录制的"，
    // 而不是上一次残留（残留会让 worker 把"没开始"读成"已开始"）。
    var liveToken = ""

    static func parse(_ argv: [String]) throws -> Options {
        var o = Options()
        var i = 1
        func value(_ name: String) throws -> String {
            i += 1
            guard i < argv.count else { throw CliError.bad("\(name) 缺参数值") }
            return argv[i]
        }
        while i < argv.count {
            switch argv[i] {
            case "--out": o.out = try value("--out")
            case "--json": o.json = try value("--json")
            case "--status": o.statusFile = try value("--status")
            case "--log": o.logFile = try value("--log")
            case "--bundle-id": o.bundleId = try value("--bundle-id"); o.bundleIdSet = true
            case "--app-name": o.appName = try value("--app-name")
            case "--pid": o.pid = Int(try value("--pid")) ?? 0
            case "--fps": o.fps = Int(try value("--fps")) ?? 30
            case "--duration": o.duration = Double(try value("--duration")) ?? 0
            case "--max-width": o.maxWidth = Int(try value("--max-width")) ?? 1920
            case "--max-seconds": o.maxSeconds = Double(try value("--max-seconds")) ?? 3600
            case "--video-bitrate": o.videoBitrate = Int(try value("--video-bitrate")) ?? 12_000_000
            case "--cursor": o.cursor = true
            case "--no-video": o.noVideo = true
            case "--no-audio": o.noAudio = true
            case "--expect-window-title": o.expectWindowTitle = try value("--expect-window-title")
            case "--probe": o.probe = true
            case "--quiet": o.quiet = true
            case "--overwrite": o.overwrite = true
            case "--focus-log": o.focusLog = try value("--focus-log")
            case "--focus-interval": o.focusInterval = Double(try value("--focus-interval")) ?? 0.5
            case "--window": o.windowId = Int(try value("--window")) ?? 0
            case "--window-title": o.windowTitle = try value("--window-title")
            case "--audio-bundle-id": o.audioBundleId = try value("--audio-bundle-id")
            case "--window-track-log": o.windowTrackLog = try value("--window-track-log")
            case "--live-status": o.liveStatus = try value("--live-status")
            case "--live-token": o.liveToken = try value("--live-token")
            case "--help", "-h":
                print("""
                GameAVRec — app 级游戏原声音视频录制（ScreenCaptureKit）

                  --probe                     只探测：权限/显示器/目标 app/窗口，输出 JSON 后退出
                  --out <path.mp4>            输出视频（**默认拒绝覆盖已存在文件**）
                  --json <path.json>          分段音频/画面指标 sidecar（同样默认拒绝覆盖）
                  --status <path.json>        结束时写状态文件（供 open -g 启动的调用方等待）
                  --log <path.log>            把日志同时写入文件
                  --overwrite                 允许覆盖上面这些已存在的文件（默认不允许）
                  --bundle-id <id>            目标 app bundle id
                  --app-name <name>           目标 app 名称
                  --pid <pid>                 目标进程 pid
                                              三个选择器是"与"关系；给了却没命中就报错退出，不回退去抓别的 app。
                                              **至少要给一个**：本工具不内置默认目标，
                  --fps <n>                   帧率，默认 30
                  --duration <sec>            录制时长，0 = 等到 SIGINT/SIGTERM
                  --max-width <px>            输出视频最大宽度，默认 1920
                  --cursor                    把系统光标画进画面（默认不画）
                  --no-video                  只录音频（该模式的产物请用 verify --expect audio 校验）
                  --expect-window-title <t>   起录前核对命中窗口的标题必须等于 <t>。
                                              **窗口 ID 稳定但内容会变**（浏览器切标签），
                                              不一致直接失败，避免静默录到别的东西。
                  --no-audio                  **真的**不录音频：产物没有音频轨，
                                              cfg/writer/stream/元数据四处一致关闭。
                                              与 --no-video 不能同时给（那样什么都没录）。
                  --focus-log <path.jsonl>    录制期间每 0.5s 记一行：前台 app / 游��窗口是否在屏 + 位置
                                              注意：记的是"前台 app"，不等于游戏窗口 key 状态
                  --focus-interval <sec>      前台采样间隔，默认 0.5
                  --window <windowID>         只录**这一个窗口**（SCContentFilter(desktopIndependentWindow:)）
                  --window-title <title>      按标题选窗口（建议同时给 --bundle-id 把范围钉死到一个 app）
                                              与 --window 二选一；给了就进入窗口粒度，不再按 app 全量抓
                  --audio-bundle-id <id>      期望的音频所属 app；与画面 app 不一致时**直接报错**
                                              （ScreenCaptureKit 音频跟随画面 filter，无法只收另一个 app）
                  --window-track-log <path>   记录目标窗口存活/标题变化（默认与 --focus-log 同路径）
                  --live-status <path>        录制过程中**持续更新**的进度文件（可反复覆盖，不受
                                              --overwrite 约束）。区分：采集初始化 / 有效首帧 /
                                              音频通路有数据 / 音频实际有信号。
                  --live-token <token>        写进实时进度文件的本次 run 令牌。调用方据此确认
                                              读到的是**这一次**的进度，而不是上一次的残留文件。
                  --quiet                     不打印逐条进度
                """)
                exit(0)
            default:
                throw CliError.bad("未知参数: \(argv[i])")
            }
            i += 1
        }
        return o
    }
}

enum CliError: Error, CustomStringConvertible {
    case bad(String)
    case usage(String)
    case permission(String)
    case runtime(String)
    var description: String {
        switch self {
        case .bad(let s): return "参数错误: " + s
        case .usage(let s): return "用法错误: " + s
        case .permission(let s): return "权限问题: " + s
        case .runtime(let s): return "运行时错误: " + s
        }
    }
}

/// 窗口选择失败的原因。单独一个类型是为了让 `Result` 的 failure 能带**可读的**中文原因
/// （String 本身不是 Error），调用方直接 `"\(err)"` 就能拿到给人看的说明。
struct WindowMatchError: Error, CustomStringConvertible {
    let message: String
    init(_ m: String) { message = m }
    var description: String { message }
}

// MARK: - 录制器

final class Recorder: NSObject, SCStreamOutput, SCStreamDelegate, @unchecked Sendable {
    let opts: Options
    private let queue = DispatchQueue(label: "gameavrec.capture")
    private var stream: SCStream?
    private var writer: AVAssetWriter?
    private var videoInput: AVAssetWriterInput?
    private var audioInput: AVAssetWriterInput?
    private var sessionStarted = false
    private var sessionStartPTS = 0.0
    private var stopping = false
    private var closed = false
    /// 采集是否真正跑起来（startCapture 返回后为 true）
    private var captureLive = false
    /// 是否收到过停止请求（可能早于采集启动）
    private var stopRequested = false
    /// 是否在拿到任何采样前就被停止（此时 finishWriting 可能永不回调）
    private var abortedBeforeSamples = false
    private var forcedCloseReason: String? = nil

    let finished = DispatchSemaphore(value: 0)
    private(set) var targetDescription: [String: Any] = [:]

    // video metrics
    private var videoFrames = 0
    private var firstVideoPTS = -1.0
    private var lastVideoPTS = -1.0
    private var maxVideoGap = 0.0
    private var stallsOver500ms = 0
    private var droppedVideoAppends = 0
    private var videoAppendsOK = 0

    // audio metrics
    private var audioBuffers = 0
    private var audioSamples = 0
    private var firstAudioPTS = -1.0
    private var lastAudioPTS = -1.0
    private var peak: Double = 0
    private var sumSq = 0.0
    private var sumN = 0.0
    private var buckets: [Int: (peak: Double, sumSq: Double, n: Double)] = [:]
    private let bucketSize = 0.5
    private var audioFormatDesc = ""
    private var droppedAudioAppends = 0
    private var audioAppendsOK = 0
    /// close() 决定的真实退出码（0 成功 / 1 失败 / 2 无法核实）
    private(set) var finalExitCode: Int32 = 1

    private var startedAt = Date()
    private var endedAt: Date?
    private var startError: String?

    // 焦点/窗口时间线
    private let focusQueue = DispatchQueue(label: "gameavrec.focus")
    private var focusTimer: DispatchSourceTimer?
    private var focusHandle: FileHandle?
    private var focusSamples = 0
    private var gameFrontmostSamples = 0
    // 目标窗口身份追踪（窗口粒度下才有意义）：
    // 采样目标窗口是否还在、标题是否变了。这两个是**独立**的失败模式：
    // 窗口还在但标题变了 = 可能切到了同 app 的另一个界面（如菜单/结算页）；
    // 窗口没了 = 画面已经开始录不到东西了。
    private var targetWindowID: Int = 0
    private var targetWindowTitleInitial: String = ""
    private var targetWindowMissingSamples = 0
    // ���时进度（见 Options.liveStatus 的说明）
    private var liveInitializedAt: Double = -1
    private var liveFirstVideoAt: Double = -1
    private var liveFirstAudioAt: Double = -1
    private var liveFirstSignalAt: Double = -1
    private var liveLastWrite = 0.0
    private var targetWindowTitleChanges: [[String: Any]] = []
    private var lastSeenTitle: String = ""

    init(opts: Options) { self.opts = opts }

    // MARK: 焦点/窗口时间线

    /// 记录「前台是哪个 app」「游戏窗口是否还在屏上、在哪」，用来给 A/B/A 对照分段。
    /// 只用 NSWorkspace + CGWindowList，不需要无障碍权限。
    private func startFocusLog(gamePid: Int, gameBundle: String) {
        guard !opts.focusLog.isEmpty else { return }
        let url = URL(fileURLWithPath: opts.focusLog)
        try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        // 入口已做过"拒绝覆盖"校验；这里只在文件不存在时创建，避免把已有证据清零
        if !FileManager.default.fileExists(atPath: opts.focusLog) {
            FileManager.default.createFile(atPath: opts.focusLog, contents: nil)
        }
        focusHandle = FileHandle(forWritingAtPath: opts.focusLog)
        let t = DispatchSource.makeTimerSource(queue: focusQueue)
        t.schedule(deadline: .now(), repeating: opts.focusInterval)
        t.setEventHandler { [weak self] in
            guard let self = self else { return }
            let now = Date()
            let front = NSWorkspace.shared.frontmostApplication
            let frontBundle = front?.bundleIdentifier ?? ""
            let isGameFront = (frontBundle == gameBundle) || (front?.processIdentifier == pid_t(gamePid))
            var gameWindows: [[String: Any]] = []
            var gameOnScreen = false
            if let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] {
                for w in list {
                    guard let owner = w[kCGWindowOwnerPID as String] as? Int, owner == gamePid else { continue }
                    let b = w[kCGWindowBounds as String] as? [String: Any] ?? [:]
                    gameOnScreen = true
                    gameWindows.append([
                        "window_number": w[kCGWindowNumber as String] as? Int ?? -1,
                        "layer": w[kCGWindowLayer as String] as? Int ?? -1,
                        "x": b["X"] as? Double ?? 0, "y": b["Y"] as? Double ?? 0,
                        "w": b["Width"] as? Double ?? 0, "h": b["Height"] as? Double ?? 0,
                    ])
                }
            }
            self.focusSamples += 1
            if isGameFront { self.gameFrontmostSamples += 1 }

            // —— 目标窗口身份追踪（仅窗口粒度）——
            // 用 CGWindowList 再采一次，专门看**那一个** windowID 还在不在、标题变没变。
            // 注意：这里只观察，不激活、不置前、不改动目标窗口。
            var targetPresent: Any = NSNull()
            var targetTitleNow: Any = NSNull()
            if self.targetWindowID > 0 {
                var found = false
                if let wl = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements],
                                                       kCGNullWindowID) as? [[String: Any]] {
                    for w in wl {
                        guard (w[kCGWindowNumber as String] as? Int) == self.targetWindowID else { continue }
                        found = true
                        let t = w[kCGWindowName as String] as? String ?? ""
                        targetPresent = true
                        targetTitleNow = t
                        if self.lastSeenTitle.isEmpty {
                            self.lastSeenTitle = t
                        } else if t != self.lastSeenTitle {
                            // 标题变化单独记一条：这不是"失败"，但它让"这段画面录的是哪一屏"变得不确定
                            self.targetWindowTitleChanges.append([
                                "t_rel": now.timeIntervalSince(self.startedAt),
                                "from": self.lastSeenTitle,
                                "to": t,
                            ])
                            self.lastSeenTitle = t
                        }
                        break
                    }
                }
                if !found {
                    self.targetWindowMissingSamples += 1
                    targetPresent = false
                }
            }

            let line: [String: Any] = [
                "wall": ISO8601.string(from: now),
                "t_rel": now.timeIntervalSince(self.startedAt),
                "frontmost_name": front?.localizedName ?? "",
                "frontmost_bundle": frontBundle,
                "game_is_frontmost": isGameFront,
                "game_window_on_screen": gameOnScreen,
                "game_windows": gameWindows,
                "target_window_id": self.targetWindowID > 0 ? self.targetWindowID : NSNull(),
                "target_window_present": targetPresent,
                "target_window_title": targetTitleNow,
            ]
            if let d = try? JSONSerialization.data(withJSONObject: line, options: [.sortedKeys]),
               let s = String(data: d, encoding: .utf8) {
                self.focusHandle?.write((s + "\n").data(using: .utf8)!)
            }
        }
        t.resume()
        focusTimer = t
    }


    // MARK: 实时进度文件

    /// 把"采集到哪一步了"写进一个**持续更新**的小文件。
    ///
    /// 为什么需要它：调用方要判断"录制是否**有效开始**"才能开局。
    /// 只看"进程起来了"是不够的（进程活着但一帧都没收到是完全可能的）。
    /// 四个里程碑分开报，其中音频只报"通路有没有数据"，**不**把
    /// "还没有声音"当成失败 —— 游戏开局前本来就是静音的，等它会死锁。
    private func writeLiveStatus(force: Bool = false) {
        guard !opts.liveStatus.isEmpty else { return }
        let now = Date().timeIntervalSince(startedAt)
        // 节流：只在里程碑变化或每 1s 写一次，避免高频 IO 干扰采集
        if !force && (now - liveLastWrite) < 1.0 { return }
        liveLastWrite = now
        let obj: [String: Any] = [
            "tool": "GameAVRec",
            // 令牌让调用方分辨"这一次"与"上一次残留"：没有它，一个陈旧文件
            // 会被读成 effectively_started=true，把"没开始"误判成"已开始"。
            "run_token": opts.liveToken,
            "recorder_pid": Int(ProcessInfo.processInfo.processIdentifier),
            "wall": ISO8601.string(from: Date()),
            "t_rel": now,
            // ① 采集初始化：startCapture 返回（还没证明收到了东西）
            "capture_initialized": liveInitializedAt >= 0,
            "capture_initialized_at": liveInitializedAt >= 0 ? liveInitializedAt : NSNull(),
            // ② 有效首帧：真的收到第一帧画面 —— 这才是"录到了东西"
            "first_video_frame": liveFirstVideoAt >= 0,
            "first_video_frame_at": liveFirstVideoAt >= 0 ? liveFirstVideoAt : NSNull(),
            // ③ 音频通路有数据（配置正确且真的有采样到达）
            "audio_path_has_data": liveFirstAudioAt >= 0,
            "audio_first_sample_at": liveFirstAudioAt >= 0 ? liveFirstAudioAt : NSNull(),
            // ④ 音频**实际有信号**（超过静音门槛）。没信号 ≠ 失败。
            "audio_signal_observed": liveFirstSignalAt >= 0,
            "audio_first_signal_at": liveFirstSignalAt >= 0 ? liveFirstSignalAt : NSNull(),
            "video_frames": videoFrames,
            "audio_buffers": audioBuffers,
            "stopping": stopping || stopRequested,
            // 调用方据此判断"有效开始"：初始化 + 首帧都到位
            "effectively_started": liveInitializedAt >= 0 && liveFirstVideoAt >= 0,
        ]
        writeFile(opts.liveStatus, jsonString(obj))
    }

    private func stopFocusLog() {
        focusTimer?.cancel()
        focusTimer = nil
        try? focusHandle?.close()
        focusHandle = nil
    }

    // MARK: 发现目标

    private static func pickDisplay(_ content: SCShareableContent, app: SCRunningApplication) -> SCDisplay? {
        let wins = content.windows.filter { $0.owningApplication?.bundleIdentifier == app.bundleIdentifier && $0.isOnScreen }
        for w in wins {
            if let d = content.displays.first(where: { $0.frame.intersects(w.frame) }) { return d }
        }
        return content.displays.first
    }

    static func probe(_ opts: Options) async throws -> [String: Any] {
        var out: [String: Any] = [:]
        out["cg_preflight_screen_capture"] = CGPreflightScreenCaptureAccess()
        let content: SCShareableContent
        do {
            content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
        } catch {
            out["shareable_content_error"] = "\(error)"
            out["hint"] = "未取得「屏幕与系统音频录制」权限：系统设置 → 隐私与安全性 → 屏幕与系统音频录制，勾选运行本程序的 app"
            return out
        }
        out["displays"] = content.displays.map { ["displayID": $0.displayID, "width": $0.width, "height": $0.height,
                                                  "x": $0.frame.origin.x, "y": $0.frame.origin.y,
                                                  "w": $0.frame.width, "h": $0.frame.height] }
        var apps: [[String: Any]] = []
        for a in content.applications {
            let n = content.windows.filter { $0.owningApplication?.bundleIdentifier == a.bundleIdentifier }.count
            apps.append(["name": a.applicationName, "bundle_id": a.bundleIdentifier, "pid": a.processID, "windows": n])
        }
        out["applications"] = apps.sorted { ($0["name"] as? String ?? "") < ($1["name"] as? String ?? "") }
        // 全部在屏窗口 + 所属 pid/bundle：调用方要用它把"窗口 id"反查成"哪个 app"，
        // 从而在**采集之前**确认画面与音频指向同一个 app（防误录）。
        // 只报观察到的，不做任何猜测。
        out["windows"] = content.windows
            .filter { $0.isOnScreen }
            .map { w -> [String: Any] in
                ["windowID": w.windowID, "title": w.title ?? "",
                 "on_screen": w.isOnScreen,
                 "owner_pid": Int(w.owningApplication?.processID ?? 0),
                 "owner_bundle_id": w.owningApplication?.bundleIdentifier ?? "",
                 "owner_name": w.owningApplication?.applicationName ?? "",
                 "layer": w.windowLayer,
                 "x": w.frame.origin.x, "y": w.frame.origin.y,
                 "w": w.frame.width, "h": w.frame.height]
            }
        out["selector"] = selectorDescription(opts)
        if let target = matchApp(content, opts) {
            out["match"] = ["name": target.applicationName, "bundle_id": target.bundleIdentifier, "pid": target.processID]
            out["match_windows"] = content.windows
                .filter { $0.owningApplication?.bundleIdentifier == target.bundleIdentifier }
                .map { ["title": $0.title ?? "", "windowID": $0.windowID, "on_screen": $0.isOnScreen,
                        "x": $0.frame.origin.x, "y": $0.frame.origin.y, "w": $0.frame.width, "h": $0.frame.height] }
        } else {
            out["match"] = NSNull()
            out["hint"] = "显式目标没命中：selector = \(selectorDescription(opts)) —— 不会回退去抓别的 app；确认目标是否在运行"
        }
        return out
    }

    /// 目标选择器是**合取**语义：给了哪个就按哪个筛，多个都给就都要满足。
    /// **本工具没有默认目标**：三个都不给 = 调用方用法错误，直接失败。
    /// 指定的目标没命中 = 返回 nil（调用方必须失败），**绝不**回退去抓别的 app。
    static func selectorDescription(_ opts: Options) -> String {
        var parts: [String] = []
        if opts.pid > 0 { parts.append("--pid \(opts.pid)") }
        if opts.bundleIdSet { parts.append("--bundle-id \(opts.bundleId)") }
        if !opts.appName.isEmpty { parts.append("--app-name \(opts.appName)") }
        if parts.isEmpty { return "未指定目标" }
        return parts.joined(separator: " AND ")
    }

    /// 必须显式给出选择器：本工具**没有**内置默认目标，也**不**回退去抓别的 app。
    private static func matchApp(_ content: SCShareableContent, _ opts: Options) -> SCRunningApplication? {
        let explicit = opts.pid > 0 || opts.bundleIdSet || !opts.appName.isEmpty
        guard explicit else { return nil }
        var candidates = content.applications
        if opts.pid > 0 { candidates = candidates.filter { $0.processID == opts.pid } }
        if opts.bundleIdSet { candidates = candidates.filter { $0.bundleIdentifier == opts.bundleId } }
        if !opts.appName.isEmpty { candidates = candidates.filter { $0.applicationName == opts.appName } }
        return candidates.first
    }

    /// 是否处于**窗口粒度**（而不是 app 粒度）。
    static func windowMode(_ opts: Options) -> Bool {
        return opts.windowId > 0 || !opts.windowTitle.isEmpty
    }

    /// 找目标窗口。命中不了返回 nil —— 调用方必须失败，**绝不**退化成抓整个 app。
    ///
    /// 选择规则（保守优先）：
    ///   1. 先按 app 选择器（bundle/name/pid）把范围缩到一个 app（若给了）；
    ///   2. 再按 windowID 精确命中，或按标题匹配；
    ///   3. 只在**在屏**窗口里选（离屏窗口拿不到帧，选了也没用）；
    ///   4. 标题匹配有多个时**报错而不是随便挑**（挑错窗口 = 录到别的东西）。
    static func matchWindow(_ content: SCShareableContent, _ opts: Options)
        -> Result<SCWindow, WindowMatchError> {
        var cands = content.windows.filter { $0.owningApplication != nil }

        // 1) app 范围
        if opts.pid > 0 {
            cands = cands.filter { Int($0.owningApplication?.processID ?? 0) == opts.pid }
        }
        if opts.bundleIdSet {
            cands = cands.filter { $0.owningApplication?.bundleIdentifier == opts.bundleId }
        }
        if !opts.appName.isEmpty {
            cands = cands.filter { $0.owningApplication?.applicationName == opts.appName }
        }

        // 2) 窗口精确命中
        if opts.windowId > 0 {
            let hit = cands.filter { Int($0.windowID) == opts.windowId }
            guard let w = hit.first else {
                return .failure(WindowMatchError("按 windowID=\(opts.windowId) 没找到窗口"
                    + (opts.bundleIdSet ? "（在 app \(opts.bundleId) 范围内）" : "")
                    + "；不会退化成抓整个 app 或别的窗口"))
            }
            return .success(w)
        }

        // 3) 标题匹配：只比在屏窗口
        let onScreen = cands.filter { $0.isOnScreen }
        let titleMatches = onScreen.filter { ($0.title ?? "") == opts.windowTitle }
        if titleMatches.count == 1 { return .success(titleMatches[0]) }
        if titleMatches.count > 1 {
            // 同名窗口挑错就是录错东西：报错并让调用方改用 --window <id>
            let ids = titleMatches.map { String($0.windowID) }.joined(separator: ", ")
            return .failure(WindowMatchError("标题 \"\(opts.windowTitle)\" 命中 \(titleMatches.count) 个窗口（id: \(ids)）："
                + "无法确定要哪一个。请改用 --window <windowID> 精确指定。"))
        }
        // 没命中：给出可用窗口，方便修正，但仍然失败
        let avail = onScreen.prefix(8).map { w -> String in
            "  #\(w.windowID) \"\(w.title ?? "")\" [\(w.owningApplication?.applicationName ?? "?")]"
        }.joined(separator: "\n")
        return .failure(WindowMatchError("按标题 \"\(opts.windowTitle)\" 没找到在屏窗口"
            + (opts.bundleIdSet ? "（app \(opts.bundleId) 范围内）" : "")
            + "。当前可选在屏窗口：\n" + (avail.isEmpty ? "  （无）" : avail)))
    }

    /// 校验音频目标：ScreenCaptureKit 的音频过滤跟随画面 filter，
    /// **做不到**"画面 A 窗口 + 只要 B app 的声音"。给了不一致的音频 app 就报错，
    /// 而不是静默只满足画面（那等于冒称支持）。
    static func checkAudioTarget(_ opts: Options, videoApp: SCRunningApplication)
        -> String? {
        guard !opts.audioBundleId.isEmpty else { return nil }
        if opts.audioBundleId != videoApp.bundleIdentifier {
            return "音频目标 app (\(opts.audioBundleId)) 与画面所属 app "
                + "(\(videoApp.bundleIdentifier)) 不一致：ScreenCaptureKit 的音频过滤跟随画面 "
                + "filter，一条流无法只收另一个 app 的声音。请让两者一致，或分两次录制后合成。"
        }
        return nil
    }

    // MARK: 启动

    func start() async throws {
        startedAt = Date()
        // 用法错误在要权限之前就报掉：没目标就没必要去申请屏幕录制权限。
        // 窗口粒度下，--window/--window-title 本身就是选择器，所以不再强制 app 选择器。
        let hasAppSelector = opts.pid > 0 || opts.bundleIdSet || !opts.appName.isEmpty
        // 两个都关 = 什么都不录：明确拒绝，而不是产出一个空壳文件
        guard !(opts.noVideo && opts.noAudio) else {
            throw CliError.usage("--no-video 与 --no-audio 不能同时给："
                                 + "那样画面和声音都没有，这不是一次录制。")
        }
        guard hasAppSelector || Recorder.windowMode(opts) else {
            throw CliError.usage(
                "必须显式指定录制目标：--bundle-id <id> / --app-name <name> / --pid <pid>，"
                + "或用 --window <id> / --window-title <title> 指定单个窗口。\n" +
                "本工具不内置任何默认目标，也不会回退去抓别的 app 或整屏。")
        }
        // 只给标题不给 app 是危险的：标题会变、也可能重名。
        if !hasAppSelector && opts.windowTitle.isEmpty && opts.windowId > 0 {
            // windowID 是精确的，可以单独使用
        } else if !hasAppSelector && !opts.windowTitle.isEmpty {
            throw CliError.usage(
                "只给 --window-title 而不给 app 限定（--bundle-id/--app-name/--pid）不安全：\n" +
                "窗口标题会变、也可能重名，可能录到别的 app 的同名窗口。\n" +
                "请加一个 app 选择器把范围钉死，或改用 --window <windowID> 精确指定。")
        }
        let content: SCShareableContent
        do {
            content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
        } catch {
            throw CliError.permission(
                "SCShareableContent 失败: \(error)\n" +
                "→ 需要「屏幕与系统音频录制」权限。请到 系统设置 → 隐私与安全性 → 屏幕与系统音频录制，\n" +
                "  勾选（必要时点 + 添加）本程序所在 app bundle，然后重跑。")
        }

        // —— 决定画面 filter：窗口粒度优先，否则 app 粒度 ——
        let filter: SCContentFilter
        let app: SCRunningApplication
        var matchedWindow: SCWindow? = nil
        if Recorder.windowMode(opts) {
            switch Recorder.matchWindow(content, opts) {
            case .failure(let msg):
                throw CliError.runtime("目标窗口未命中，拒绝改抓别的窗口/app：\(msg)")
            case .success(let w):
                // pin 核对：窗口 ID 稳定 ≠ 内容稳定
                if !opts.expectWindowTitle.isEmpty {
                    let actual = w.title ?? ""
                    if actual != opts.expectWindowTitle {
                        throw CliError.runtime(
                            "目标窗口 #\(w.windowID) 的标题已变："
                            + "期望 \(opts.expectWindowTitle.debugDescription)，"
                            + "实际 \(actual.debugDescription)。\n"
                            + "窗口 ID 不变但内容可能整个换掉（例如浏览器切了标签），"
                            + "继续录会录到不相干的东西。请重新预检确认目标。")
                    }
                }
                guard let owner = w.owningApplication else {
                    throw CliError.runtime("命中窗口 #\(w.windowID) 但拿不到所属 app，无法确定音频范围")
                }
                matchedWindow = w
                app = owner
                // 窗口粒度：**真正**按窗口过滤，不用 sourceRect 近似
                filter = SCContentFilter(desktopIndependentWindow: w)
            }
        } else {
            guard let a = Recorder.matchApp(content, opts) else {
                throw CliError.runtime("目标未命中，拒绝改抓别的应用：selector = \(Recorder.selectorDescription(opts))")
            }
            app = a
            guard let display = Recorder.pickDisplay(content, app: app) else {
                throw CliError.runtime("没有可用显示器")
            }
            filter = SCContentFilter(display: display, including: [app], exceptingWindows: [])
        }

        // 音频目标校验：不一致就报错，不静默只满足画面
        if let audioProblem = Recorder.checkAudioTarget(opts, videoApp: app) {
            throw CliError.usage(audioProblem)
        }

        // 显示器的用途：① 兜底尺寸 ② 状态里的 display_id。
        // 窗口粒度下它只是参考（窗口可能跨屏），拿不到也不算致命；
        // app 粒度下上面已经强制要求过它存在。
        let display = Recorder.pickDisplay(content, app: app)
        let scale = Double(filter.pointPixelScale)

        // 游戏常有多个不可见的辅助窗口，filter.contentRect 会退化成整屏（四周大片黑边）。
        // 这里用窗口服务器里「最大的一块在屏 app 窗口」当裁剪区，录出来紧贴游戏窗口。
        //
        // **窗口粒度下不做这个裁剪**：desktopIndependentWindow 的 contentRect 已经就是
        // 那一个窗口，再叠一层 sourceRect 裁剪只会在窗口移动/缩放时把画面切歪。
        var sourceRect: CGRect? = nil
        if matchedWindow == nil,
           let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] {
            var best: CGRect = .zero
            for win in list {
                guard let owner = win[kCGWindowOwnerPID as String] as? Int, owner == Int(app.processID) else { continue }
                guard (win[kCGWindowLayer as String] as? Int ?? 0) == 0 else { continue }
                guard let b = win[kCGWindowBounds as String] as? [String: Any] else { continue }
                let r = CGRect(x: b["X"] as? Double ?? 0, y: b["Y"] as? Double ?? 0,
                               width: b["Width"] as? Double ?? 0, height: b["Height"] as? Double ?? 0)
                if r.width * r.height > best.width * best.height { best = r }
            }
            // 只在这个窗口确实落在显示器内、且比整块内容区更小时才裁剪。
            // display 可能为 nil（无显示器/取不到）——那就**不裁剪**，保持完整内容区，
            // 而不是拿一个不确定的矩形去切画面。
            if best.width >= 64, best.height >= 64,
               (display?.frame.contains(best) ?? false),
               best.width * best.height < filter.contentRect.width * filter.contentRect.height * 0.95 {
                sourceRect = best
            }
        }

        let baseRect = sourceRect ?? filter.contentRect
        var w = Int((Double(baseRect.width) * scale).rounded())
        var h = Int((Double(baseRect.height) * scale).rounded())
        if w < 2 || h < 2 {
            // 兜底：拿 filter 的内容区尺寸，其次显示器尺寸。
            // 都拿不到就报错——不能猜一个尺寸然后录出歪画面。
            let fw = Int(filter.contentRect.width * scale)
            let fh = Int(filter.contentRect.height * scale)
            if fw >= 2 && fh >= 2 {
                w = fw; h = fh
            } else if let d = display, d.width >= 2, d.height >= 2 {
                w = Int(d.width); h = Int(d.height)
            } else {
                throw CliError.runtime("无法确定采集尺寸（filter 内容区与显示器都不可用）")
            }
        }
        if w > opts.maxWidth {
            let r = Double(opts.maxWidth) / Double(w)
            w = opts.maxWidth
            h = Int((Double(h) * r).rounded())
        }
        w -= (w % 2); h -= (h % 2)
        w = max(w, 2); h = max(h, 2)

        let cfg = SCStreamConfiguration()
        cfg.width = w
        cfg.height = h
        if let sr = sourceRect {
            cfg.sourceRect = sr
            // sourceRect 是相对 filter 内容原点的坐标，单显示器下内容原点即 (0,0)
            cfg.sourceRect.origin.x -= filter.contentRect.origin.x
            cfg.sourceRect.origin.y -= filter.contentRect.origin.y
        }
        cfg.minimumFrameInterval = CMTime(value: 1, timescale: CMTimeScale(opts.fps))
        cfg.queueDepth = 6
        cfg.pixelFormat = kCVPixelFormatType_32BGRA
        cfg.scalesToFit = false
        cfg.showsCursor = opts.cursor
        cfg.colorSpaceName = CGColorSpace.sRGB
        // —— 音频：只要系统里这个 app 的音频，不要麦克风 ——
        // `--no-audio` 时**真的不抓**：不能只在调用方那层"不要求声音"，
        // 否则这里照旧 capturesAudio=true 会**意外收到同一 app 其它窗口的声音**。
        let audioEnabled = !opts.noAudio
        cfg.capturesAudio = audioEnabled
        cfg.sampleRate = 48_000
        cfg.channelCount = 2
        cfg.excludesCurrentProcessAudio = true
        cfg.captureMicrophone = false

        targetDescription = ["name": app.applicationName, "bundle_id": app.bundleIdentifier,
                             "pid": app.processID, "display_id": display?.displayID ?? 0,
                             "capture_width": w, "capture_height": h, "fps": opts.fps,
                             "source_rect": sourceRect.map { ["x": $0.origin.x, "y": $0.origin.y, "w": $0.width, "h": $0.height] } ?? NSNull(),
                             // 如实记录用的是哪种 filter：窗口粒度 vs app 粒度。
                             // 下游据此判断"画面范围到底是一个窗口还是一个 app"。
                             "video_granularity": matchedWindow != nil ? "window" : "app",
                             "filter": matchedWindow != nil
                                ? "desktopIndependentWindow(#\(matchedWindow!.windowID))"
                                : "display + includingApplications([\(app.applicationName)])",
                             "window": matchedWindow.map { w -> [String: Any] in
                                 ["windowID": w.windowID, "title": w.title ?? "",
                                  "on_screen": w.isOnScreen,
                                  "x": w.frame.origin.x, "y": w.frame.origin.y,
                                  "w": w.frame.width, "h": w.frame.height]
                             } ?? NSNull(),
                             // 音频粒度恒为 app：ScreenCaptureKit 没有窗口级音频
                             "audio_granularity": "app",
                             "audio_app_bundle_id": app.bundleIdentifier,
                             "audio_granularity_note":
                                matchedWindow != nil
                                ? "画面是单个窗口，但音频是该 app 级的：可能含该 app 其它窗口的声音"
                                : "画面与音频都是 app 级",
                             "selector": Recorder.selectorDescription(opts),
                             "captures_audio": audioEnabled,
                             "capture_microphone": false,
                             "sample_rate": audioEnabled ? 48_000 : NSNull(),
                             "channels": audioEnabled ? 2 : NSNull(),
                             "excludes_current_process_audio": true]

        let url = URL(fileURLWithPath: opts.out)
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(),
                                                withIntermediateDirectories: true)
        // 不静默删除已有素材；存在即报错（除非显式 --overwrite）
        if FileManager.default.fileExists(atPath: url.path) {
            if opts.overwrite {
                try FileManager.default.removeItem(at: url)
            } else {
                throw CliError.usage("输出已存在，拒绝覆盖：\(url.path)（要覆盖请显式加 --overwrite，或换新路径）")
            }
        }
        let writer = try AVAssetWriter(outputURL: url, fileType: .mp4)
        self.writer = writer

        if !opts.noVideo {
            let vSettings: [String: Any] = [
                AVVideoCodecKey: AVVideoCodecType.h264,
                AVVideoWidthKey: w,
                AVVideoHeightKey: h,
                AVVideoCompressionPropertiesKey: [
                    AVVideoAverageBitRateKey: opts.videoBitrate,
                    AVVideoExpectedSourceFrameRateKey: opts.fps,
                    AVVideoMaxKeyFrameIntervalKey: opts.fps * 2,
                    AVVideoProfileLevelKey: AVVideoProfileLevelH264HighAutoLevel,
                ],
            ]
            let vi = AVAssetWriterInput(mediaType: .video, outputSettings: vSettings)
            vi.expectsMediaDataInRealTime = true
            if writer.canAdd(vi) { writer.add(vi); videoInput = vi }
            else { throw CliError.runtime("AVAssetWriter 无法添加视频轨") }
        }

        if audioEnabled {
            let aSettings: [String: Any] = [
                AVFormatIDKey: kAudioFormatMPEG4AAC,
                AVSampleRateKey: 48_000,
                AVNumberOfChannelsKey: 2,
                AVEncoderBitRateKey: 192_000,
            ]
            let ai = AVAssetWriterInput(mediaType: .audio, outputSettings: aSettings)
            ai.expectsMediaDataInRealTime = true
            if writer.canAdd(ai) { writer.add(ai); audioInput = ai }
            else { throw CliError.runtime("AVAssetWriter 无法添加音频轨") }
        }

        guard writer.startWriting() else {
            throw CliError.runtime("AVAssetWriter.startWriting 失败: \(writer.error?.localizedDescription ?? "unknown")")
        }

        let stream = SCStream(filter: filter, configuration: cfg, delegate: self)
        try stream.addStreamOutput(self, type: .screen, sampleHandlerQueue: queue)
        if audioEnabled {
            try stream.addStreamOutput(self, type: .audio, sampleHandlerQueue: queue)
        }
        self.stream = stream
        try await stream.startCapture()
        queue.sync {
            self.captureLive = true
            if self.liveInitializedAt < 0 {
                self.liveInitializedAt = Date().timeIntervalSince(self.startedAt)
            }
            self.writeLiveStatus(force: true)
        }
        // 目标窗口身份要在起录前就钉好，采样线程才能从一开始就追踪它
        if let mw = matchedWindow {
            queue.sync {
                self.targetWindowID = Int(mw.windowID)
                self.targetWindowTitleInitial = mw.title ?? ""
                self.lastSeenTitle = mw.title ?? ""
            }
        }
        startFocusLog(gamePid: Int(app.processID), gameBundle: app.bundleIdentifier)
        // 停止请求可能早于采集启动：采集一起来就在这里串行收尾（同一线程，不与 writer 竞争）
        if queue.sync(execute: { self.stopRequested && !self.closed }) {
            if let s = self.stream { try? await s.stopCapture() }
            queue.sync { self.abortBeforeSamples(reason: "stop-requested-during-start") }
        }

        if !opts.quiet {
            logErr("开始录制 → \(opts.out) [\(w)x\(h) @\(opts.fps)] app=\(app.applicationName)/\(app.bundleIdentifier) pid=\(app.processID) audio=on mic=off")
        }

        if opts.duration > 0 {
            queue.asyncAfter(deadline: .now() + opts.duration) { self.requestStop(reason: "duration") }
        }
        if opts.maxSeconds > 0 {
            queue.asyncAfter(deadline: .now() + opts.maxSeconds) { self.requestStop(reason: "max_seconds") }
        }
    }

    // MARK: 采样

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard sampleBuffer.isValid, CMSampleBufferDataIsReady(sampleBuffer) else { return }
        let pts = CMTimeGetSeconds(CMSampleBufferGetPresentationTimeStamp(sampleBuffer))
        guard pts.isFinite else { return }

        if !sessionStarted {
            sessionStarted = true
            sessionStartPTS = pts
            writer?.startSession(atSourceTime: CMSampleBufferGetPresentationTimeStamp(sampleBuffer))
        }

        switch type {
        case .screen:
            guard !opts.noVideo, let vi = videoInput else { return }
            if liveFirstVideoAt < 0 {
                liveFirstVideoAt = pts - sessionStartPTS
                writeLiveStatus(force: true)
            }
            // 统一时间基准：首/末 PTS 都相对 sessionStartPTS（绝对 host PTS 有 75 万秒量级，不能混用）
            let vRel = pts - sessionStartPTS
            if firstVideoPTS < 0 { firstVideoPTS = vRel }
            if lastVideoPTS >= 0 {
                let gap = vRel - lastVideoPTS
                if gap > maxVideoGap { maxVideoGap = gap }
                if gap > 0.5 { stallsOver500ms += 1 }
            }
            lastVideoPTS = vRel
            videoFrames += 1
            if vi.isReadyForMoreMediaData {
                if vi.append(sampleBuffer) { videoAppendsOK += 1 } else { droppedVideoAppends += 1 }
            } else {
                droppedVideoAppends += 1
            }

        case .audio:
            guard let ai = audioInput else { return }
            if liveFirstAudioAt < 0 {
                liveFirstAudioAt = pts - sessionStartPTS
                writeLiveStatus(force: true)
            }
            if firstAudioPTS < 0 { firstAudioPTS = pts - sessionStartPTS }
            lastAudioPTS = pts - sessionStartPTS
            audioBuffers += 1
            measure(sampleBuffer, pts: pts)
            if ai.isReadyForMoreMediaData {
                if ai.append(sampleBuffer) { audioAppendsOK += 1 } else { droppedAudioAppends += 1 }
            } else {
                droppedAudioAppends += 1
            }

        default:
            break
        }
        // 周期性刷新（内部按 1s 节流）：里程碑之外也让调用方看到实时帧数，
        // 否则 video_frames 会一直停在最后一次里程碑的数值上（看起来像卡住）。
        writeLiveStatus()
    }

    /// 计算真实信号：峰值 / RMS，并按 0.5s 分桶，用来判断「有音轨但全是静音」
    private func measure(_ sb: CMSampleBuffer, pts: Double) {
        guard let fd = CMSampleBufferGetFormatDescription(sb),
              let asbdPtr = CMAudioFormatDescriptionGetStreamBasicDescription(fd) else { return }
        let asbd = asbdPtr.pointee
        if audioFormatDesc.isEmpty {
            let isFloat = (asbd.mFormatFlags & kAudioFormatFlagIsFloat) != 0
            let layout = (asbd.mFormatFlags & kAudioFormatFlagIsNonInterleaved) != 0 ? "non-interleaved" : "interleaved"
            audioFormatDesc = "\(asbd.mSampleRate)Hz ch=\(asbd.mChannelsPerFrame) bits=\(asbd.mBitsPerChannel) \(isFloat ? "float" : "int") \(layout)"
        }
        let chCount = max(1, Int(asbd.mChannelsPerFrame))
        let ablPtr = AudioBufferList.allocate(maximumBuffers: chCount)
        defer { free(ablPtr.unsafeMutablePointer) }
        var blockBuffer: CMBlockBuffer?
        let st = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sb, bufferListSizeNeededOut: nil,
            bufferListOut: ablPtr.unsafeMutablePointer,
            bufferListSize: AudioBufferList.sizeInBytes(maximumBuffers: chCount),
            blockBufferAllocator: kCFAllocatorDefault,
            blockBufferMemoryAllocator: kCFAllocatorDefault,
            flags: 0, blockBufferOut: &blockBuffer)
        guard st == noErr else { return }

        let isFloat = (asbd.mFormatFlags & kAudioFormatFlagIsFloat) != 0
        let bits = Int(asbd.mBitsPerChannel)
        let abl = UnsafeMutableAudioBufferListPointer(ablPtr.unsafeMutablePointer)

        var localPeak = 0.0
        var localSq = 0.0
        var localN = 0.0

        for buf in abl {
            guard let mData = buf.mData else { continue }
            let byteCount = Int(buf.mDataByteSize)
            if isFloat && bits == 32 {
                let n = byteCount / 4
                let p = mData.assumingMemoryBound(to: Float.self)
                for i in 0..<n {
                    let v = Double(p[i])
                    let a = abs(v)
                    if a > localPeak { localPeak = a }
                    localSq += v * v
                }
                localN += Double(n)
            } else if !isFloat && bits == 16 {
                let n = byteCount / 2
                let p = mData.assumingMemoryBound(to: Int16.self)
                for i in 0..<n {
                    let v = Double(p[i]) / 32768.0
                    let a = abs(v)
                    if a > localPeak { localPeak = a }
                    localSq += v * v
                }
                localN += Double(n)
            } else if !isFloat && bits == 32 {
                let n = byteCount / 4
                let p = mData.assumingMemoryBound(to: Int32.self)
                for i in 0..<n {
                    let v = Double(p[i]) / 2147483648.0
                    let a = abs(v)
                    if a > localPeak { localPeak = a }
                    localSq += v * v
                }
                localN += Double(n)
            }
        }

        audioSamples += Int(localN) / chCount
        if localPeak > peak { peak = localPeak }
        sumSq += localSq
        sumN += localN

        // ④ 音频**实际有信号**：第一次超过静音门槛时记一笔。
        // 注意：**没信号不算失败**。游戏开局前 / 静音场景本来就是 0，
        // 调用方若把它当失败条件，就会死锁等一个永远不来的信号。
        if liveFirstSignalAt < 0 && dbfs(localPeak) > -66.0 {
            liveFirstSignalAt = pts - sessionStartPTS
            writeLiveStatus(force: true)
        }

        let rel = pts - sessionStartPTS
        let key = Int(floor(rel / bucketSize))
        var b = buckets[key] ?? (0, 0, 0)
        if localPeak > b.peak { b.peak = localPeak }
        b.sumSq += localSq
        b.n += localN
        buckets[key] = b
    }

    // MARK: 停止

    func requestStop(reason: String) {
        stopRequested = true
        queue.async {
            guard !self.stopping, !self.closed else { return }

            // 采集还没起来（startCapture 未返回）：**不要在这里碰 writer** ——
            // start() 可能正在同一时刻 startWriting()，并发 cancelWriting 会让
            // AVFoundation 抛 ObjC 异常直接 abort。这里只记录请求：
            //   · start() 起来后会自己走 abort 路径给终态；
            //   · 启动卡死则由 main 的 30s 启动上限给 failed 终态。
            if !self.captureLive {
                if !self.opts.quiet { logErr("收到停止请求（\(reason)），采集尚未启动：交给启动流程收尾") }
                return
            }

            self.stopping = true
            if !self.opts.quiet { logErr("停止录制（\(reason)）") }

            // 采集在跑但还没拿到任何采样：writer 没 startSession，
            // finishWriting 回调可能永远不来 → 取消写入并立刻给终态。
            if !self.sessionStarted {
                self.abortBeforeSamples(reason: reason)
                self.stream?.stopCapture { _ in }
                return
            }

            if let s = self.stream {
                s.stopCapture { err in
                    if let err = err { logErr("stopCapture 报错: \(err)") }
                    self.queue.async { self.finishUp() }
                }
            } else {
                self.finishUp()
            }
        }
    }

    /// 在拿到任何采样前停止：取消写入并立刻给失败终态（finishWriting 此时可能永不回调）
    private func abortBeforeSamples(reason: String) {
        guard !closed, !stopping else { return }
        stopping = true
        abortedBeforeSamples = true
        if !opts.quiet { logErr("在采集到任何采样之前停止（\(reason)）：取消写入并给失败终态") }
        videoInput?.markAsFinished()
        audioInput?.markAsFinished()
        writer?.cancelWriting()
        close(sessionStartPTS: 0)
    }

    /// 正常收尾：标记输入结束 + finishWriting，并挂一个兜底，保证一定有终态
    private func finishUp() {
        guard !closed else { return }
        videoInput?.markAsFinished()
        audioInput?.markAsFinished()
        writer?.finishWriting { self.close(sessionStartPTS: self.sessionStartPTS) }
        queue.asyncAfter(deadline: .now() + 5) { [weak self] in
            guard let self = self, !self.closed else { return }
            logErr("finishWriting 5s 未回调：强制收尾（文件可能不完整）")
            self.forcedCloseReason = "finish_writing_timeout"
            self.close(sessionStartPTS: self.sessionStartPTS)
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        logErr("SCStream 异常停止: \(error)")
        startError = "\(error)"
        queue.async {
            guard !self.stopping, !self.closed else { return }
            self.stopping = true
            if self.sessionStarted { self.finishUp() } else {
                self.abortedBeforeSamples = true
                self.writer?.cancelWriting()
                self.close(sessionStartPTS: 0)
            }
        }
    }

    private func close(sessionStartPTS: Double) {
        guard !closed else { return }
        closed = true
        endedAt = Date()
        stopFocusLog()

        // ---- 真实成功/失败判定：writer 状态 + stream 错误 + 文件里是否真有轨道 ----
        let wStatus = writer?.status ?? .unknown
        let writerErrorText: String? = writer?.error.map { "\($0)" }
        var fileAudioTracks: Int? = nil
        var fileVideoTracks: Int? = nil
        var trackProbeError: String? = nil
        if wStatus == .completed, let url = writer?.outputURL {
            let asset = AVURLAsset(url: url)
            let sem = DispatchSemaphore(value: 0)
            var na = 0, nv = 0, perr: String? = nil
            Task {
                do {
                    na = try await asset.loadTracks(withMediaType: .audio).count
                    nv = try await asset.loadTracks(withMediaType: .video).count
                } catch { perr = "\(error)" }
                sem.signal()
            }
            sem.wait()
            if perr == nil { fileAudioTracks = na; fileVideoTracks = nv } else { trackProbeError = perr }
        }
        let tracksVerified = (fileAudioTracks != nil)
        let audioInFile = tracksVerified ? (fileAudioTracks! > 0) : false
        let videoInFile = tracksVerified ? (fileVideoTracks! > 0) : false
        let needVideo = !opts.noVideo
        var problems: [String] = []
        if wStatus != .completed { problems.append("writer.status=\(wStatus.rawValue)（非 completed）") }
        if let we = writerErrorText { problems.append("writer.error=\(we)") }
        if let se = startError { problems.append("stream_error=\(se)") }
        if abortedBeforeSamples { problems.append("在采集到任何采样之前就被停止（没有可交付内容）") }
        if let fr = forcedCloseReason { problems.append("收尾异常：\(fr)") }
        if tracksVerified {
            // `--no-audio` 时没有音轨是**预期**的，不是缺陷
            if !opts.noAudio && !audioInFile { problems.append("产出文件里没有音频轨") }
            if needVideo && !videoInFile { problems.append("产出文件里没有视频轨") }
        }
        // 目标窗口在录制期间消失过：这是**真实**的采集缺陷，必须进 problems，
        // 而不是只在 focus 日志里留一行没人看的记录。
        //
        // 标题变化则进 **advisories**，不进 problems。
        // 这两件事必须分开：problems 决定退出码与"录像是否完整"的结论；
        // 而标题变化（例如国际象棋的"白方走棋 ↔ 黑方走棋"）是完全正常的现象，
        // 把它算成失败会把一次**好**的录制判成 fail。
        // 反过来说，"好"要如实说好，和"未知不能报通过"是同一个纪律的两面。
        var advisories: [String] = []
        if targetWindowID > 0 {
            if targetWindowMissingSamples > 0 {
                problems.append("目标窗口在录制期间有 \(targetWindowMissingSamples)/\(focusSamples) "
                    + "个采样时刻不在屏上：该段时间的画面内容不可信（窗口可能已关闭/最小化）")
            }
            if !targetWindowTitleChanges.isEmpty {
                advisories.append("目标窗口标题在录制期间变化 \(targetWindowTitleChanges.count) 次："
                    + "该段画面可能跨了不同界面（审片时需知道；不影响录像完整性）")
            }
        }

        var bucketArr: [[String: Any]] = []
        for k in buckets.keys.sorted() {
            let b = buckets[k]!
            let rms = b.n > 0 ? sqrt(b.sumSq / b.n) : 0
            bucketArr.append([
                "t_start": Double(k) * bucketSize,
                "t_end": Double(k + 1) * bucketSize,
                "peak_dbfs": dbfs(b.peak),
                "rms_dbfs": dbfs(rms),
            ])
        }
        let overallRMS = sumN > 0 ? sqrt(sumSq / sumN) : 0
        let silentBuckets = bucketArr.filter { ($0["peak_dbfs"] as? Double ?? -160) < -60 }.count

        var out: [String: Any] = [
            "tool": "GameAVRec",
            "out": opts.out,
            "started_at": ISO8601.string(from: startedAt),
            "ended_at": ISO8601.string(from: endedAt ?? Date()),
            "wall_seconds": (endedAt ?? Date()).timeIntervalSince(startedAt),
            "time_basis": "所有 *_pts_rel 均相对本次 session 起点（第一个采样的 PTS）；绝对 host PTS 不外泄",
            "writer_status": Recorder.statusName(wStatus),
            "writer_status_raw": wStatus.rawValue,
            "writer_error": writerErrorText ?? NSNull(),
            "file_tracks": [
                "verified": tracksVerified,
                "audio": fileAudioTracks.map { $0 as Any } ?? NSNull(),
                "video": fileVideoTracks.map { $0 as Any } ?? NSNull(),
                "probe_error": trackProbeError ?? NSNull(),
            ],
            "appends_ok": ["video": videoAppendsOK, "audio": audioAppendsOK],
            "target": targetDescription,
            "video": [
                "enabled": !opts.noVideo,
                "frames": videoFrames,
                "first_pts_rel": firstVideoPTS,
                "last_pts_rel": lastVideoPTS,
                "max_gap_s": maxVideoGap,
                "stalls_over_500ms": stallsOver500ms,
                "dropped_appends": droppedVideoAppends,
                "appends_ok": videoAppendsOK,
            ],
            "audio": [
                "enabled": !opts.noAudio,
                "capture_requested": !opts.noAudio,
                "note": opts.noAudio
                    ? "本次用 --no-audio：cfg.capturesAudio=false，未挂音频输入/输出，产物**没有**音频轨"
                    : "按所属 app 抓取音频（粒度是 app，不是单窗口）",
                "buffers": audioBuffers,
                "samples_per_channel": audioSamples,
                "source_format": audioFormatDesc,
                "first_pts_rel": firstAudioPTS,
                "last_pts_rel": lastAudioPTS,
                "peak_dbfs": dbfs(peak),
                "rms_dbfs": dbfs(overallRMS),
                "silent_buckets": silentBuckets,
                "buckets_total": bucketArr.count,
                "dropped_appends": droppedAudioAppends,
                "appends_ok": audioAppendsOK,
                "buckets": bucketArr,
            ],
            "av_start_delta_s": (firstVideoPTS >= 0 && firstAudioPTS >= 0) ? (firstVideoPTS - firstAudioPTS) : NSNull(),
            "focus": [
                "log": opts.focusLog,
                "samples": focusSamples,
                "frontmost_game_samples": gameFrontmostSamples,
                "frontmost_game_ratio": focusSamples > 0 ? Double(gameFrontmostSamples) / Double(focusSamples) : NSNull(),
            ],
            // 目标窗口身份：窗口粒度下才有内容。**只报实测到的**，
            // 没有追踪就不给结论（不是"没问题"）。
            "target_window_tracking": targetWindowID > 0 ? [
                "window_id": targetWindowID,
                "initial_title": targetWindowTitleInitial,
                "samples": focusSamples,
                "missing_samples": targetWindowMissingSamples,
                "present_ratio": focusSamples > 0
                    ? Double(focusSamples - targetWindowMissingSamples) / Double(focusSamples)
                    : NSNull(),
                "title_changes": targetWindowTitleChanges,
                "title_change_count": targetWindowTitleChanges.count,
                "note": "present_ratio < 1 表示有采样时刻目标窗口不在屏上（画面当时可能已静止/丢失）；"
                      + "title_changes 非空表示录制期间标题变过，说明该段画面可能跨了不同界面。",
            ] : NSNull(),
            "stream_error": startError ?? NSNull(),
            // problems 决定退出码与"录像是否完整"；advisories 只供审片参考，**不影响结论**。
            "problems": problems,
            "advisories": advisories,
        ]

        // 结论性判定：轨道是否真的写进文件（查不到就标 unverified，不用 buffers>0 冒充）
        var verdict: [String: Any] = [:]
        if opts.noAudio {
            // 本次**没要**音频：不能声称有音轨，也不该被读成"应该有一条却没有"。
            verdict["audio_track_present"] = NSNull()
            verdict["audio_track_present_basis"] = "not_requested"
            verdict["audio_requested"] = false
        } else {
            verdict["audio_track_present"] = tracksVerified ? audioInFile : NSNull()
            verdict["audio_track_present_basis"] = tracksVerified ? "file_tracks" : "unverified"
            verdict["audio_requested"] = true
        }
        verdict["audio_buffers_seen"] = audioBuffers
        verdict["audio_appends_ok"] = audioAppendsOK
        verdict["audio_has_signal"] = peak > 0.0005   // 约 -66 dBFS
        verdict["audio_peak_dbfs"] = dbfs(peak)
        verdict["video_track_present"] = tracksVerified ? videoInFile : NSNull()
        verdict["video_frames_present"] = videoFrames > 0
        verdict["video_frames_arriving"] = (maxVideoGap < 2.0)
        verdict["tracks_verified"] = tracksVerified
        out["verdict"] = verdict

        let text = jsonString(out)
        let jsonPath = opts.json.isEmpty ? (opts.out + ".metrics.json") : opts.json
        writeFile(jsonPath, text)

        // 只有真的成功才写 done/exit=0；否则如实传播失败
        let state: String
        if !problems.isEmpty {
            state = "failed"; finalExitCode = 1
        } else if !tracksVerified {
            state = "unverified"; finalExitCode = 2
            problems.append("完成写入但无法核实产出轨道（\(trackProbeError ?? "unknown")）")
        } else {
            state = "done"; finalExitCode = 0
        }
        if !opts.statusFile.isEmpty {
            writeFile(opts.statusFile, jsonString(["state": state, "exit": Int(finalExitCode),
                                                   "metrics": jsonPath, "out": opts.out,
                                                   "writer_status": Recorder.statusName(wStatus),
                                                   "problems": problems,
                                                   "advisories": advisories, "verdict": verdict]))
        }
        if !opts.quiet {
            print(jsonString(["ok": finalExitCode == 0, "state": state, "exit": Int(finalExitCode),
                              "out": opts.out, "metrics": jsonPath, "problems": problems,
                              "advisories": advisories,
                              "audio_buffers": audioBuffers, "audio_appends_ok": audioAppendsOK,
                              "video_frames": videoFrames, "video_appends_ok": videoAppendsOK,
                              "file_audio_tracks": fileAudioTracks.map { $0 as Any } ?? NSNull(),
                              "file_video_tracks": fileVideoTracks.map { $0 as Any } ?? NSNull(),
                              "peak_dbfs": dbfs(peak), "verdict": verdict]))
        }
        finished.signal()
    }

    /// AVAssetWriter.Status 的可读名字（rawValue 数字不利于人读）
    static func statusName(_ s: AVAssetWriter.Status) -> String {
        switch s {
        case .unknown: return "unknown"
        case .writing: return "writing"
        case .completed: return "completed"
        case .failed: return "failed"
        case .cancelled: return "cancelled"
        @unknown default: return "unknown(\(s.rawValue))"
        }
    }

    private func dbfs(_ linear: Double) -> Double {
        if linear <= 0 { return -160.0 }
        return max(-160.0, 20.0 * log10(linear))
    }
}

// MARK: - main

/// 必须活到进程结束：Pipe / FileHandle 一旦被 ARC 释放，stderr 写端就成了孤儿，
/// 之后任何一次日志写入都会 SIGPIPE 把录制器悄悄干掉（连 sidecar 都不会写）。
var gLogPipe: Pipe?
var gLogFileHandle: FileHandle?
/// 录制器还没构造出来时，信号先记在钩子上，避免早期 SIGTERM 被吞掉后进程不再响应停止
var gStopHook: ((String) -> Void)?

do {
    let opts = try Options.parse(CommandLine.arguments)

    // 建立与窗口服务器的连接 —— **必须在任何 SCContentFilter 之前**。
    //
    // 为什么：`SCContentFilter(desktopIndependentWindow:)` 内部要走 CoreGraphics 的
    // 窗口服务器路径。一个从终端直接启动的**裸可执行文件**默认没有这条连接，
    // 于是会在 CGInitialization.c 里撞上 `CGS_REQUIRE_INIT` 断言直接 SIGABRT
    // （实测：app 级采集正常出 190 帧，窗口级采集当场 abort）。
    // 触碰一次 NSApplication.shared 就会完成 AppKit/CG 初始化，代价可忽略。
    //
    // 这一条对 app 级采集无害，对窗口级采集是必需的。
    _ = NSApplication.shared

    // 信号源第一件事就装好（后面才创建 Recorder）
    signal(SIGINT, SIG_IGN)
    signal(SIGTERM, SIG_IGN)
    let sigint = DispatchSource.makeSignalSource(signal: SIGINT, queue: .global())
    let sigterm = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .global())
    sigint.setEventHandler { gStopHook?("SIGINT") }
    sigterm.setEventHandler { gStopHook?("SIGTERM") }
    sigint.resume()
    sigterm.resume()

    // 会写的文件一律默认不覆盖（--log 也追加入已有文件而不是清零）
    if !opts.logFile.isEmpty, FileManager.default.fileExists(atPath: opts.logFile), !opts.overwrite {
        logErr("拒绝覆盖已存在的日志文件（退出码 3）：\(opts.logFile)（加 --overwrite 或换路径）")
        exit(3)
    }

    if !opts.logFile.isEmpty {
        // 简单 tee：把 stderr 也写文件（供 open -g 启动时排查）
        let path = opts.logFile
        if !FileManager.default.fileExists(atPath: path) {
            FileManager.default.createFile(atPath: path, contents: nil)
        }
        if let fh = FileHandle(forWritingAtPath: path) {
            fh.seekToEndOfFile()
            gLogFileHandle = fh        // 保活
            // 用管道把 stderr 复制一份；**先把原 stderr 存成 dup fd**，
            // 否则 handler 里再写标准错误 = 写回同一条管道 → 自我反馈、日志无限增长。
            let savedStderr = dup(STDERR_FILENO)
            let pipe = Pipe()
            gLogPipe = pipe            // 保活（否则 handler 随 Pipe 一起被释放 → SIGPIPE）
            dup2(pipe.fileHandleForWriting.fileDescriptor, STDERR_FILENO)
            pipe.fileHandleForReading.readabilityHandler = { h in
                let d = h.availableData
                if d.isEmpty { return }
                fh.write(d)                      // 落盘
                if savedStderr >= 0 {            // 回显到**原始**终端，不回流管道
                    d.withUnsafeBytes { raw in
                        if let base = raw.baseAddress { _ = write(savedStderr, base, d.count) }
                    }
                }
            }
        }
    }

    if opts.probe {
        let sem = DispatchSemaphore(value: 0)
        var payload: [String: Any] = [:]
        Task {
            do { payload = try await Recorder.probe(opts) }
            catch { payload = ["error": "\(error)"] }
            sem.signal()
        }
        sem.wait()
        print(jsonString(payload))
        exit(0)
    }

    guard !opts.out.isEmpty else {
        logErr("缺少 --out")
        exit(64)
    }

    // 入口处一次性校验所有会写的文件：默认不改写任何已存在的素材/证据（退出码 3）
    let metricsPath = opts.json.isEmpty ? (opts.out + ".metrics.json") : opts.json
    var conflicts: [String] = []
    for p in [opts.out, metricsPath, opts.focusLog, opts.statusFile] where !p.isEmpty {
        if FileManager.default.fileExists(atPath: p) { conflicts.append(p) }
    }
    if !conflicts.isEmpty && !opts.overwrite {
        logErr("拒绝覆盖已存在的文件（退出码 3）：\n  " + conflicts.joined(separator: "\n  ") +
               "\n→ 换一个新路径，或显式加 --overwrite")
        exit(3)
    }

    let rec = Recorder(opts: opts)

    gStopHook = { reason in rec.requestStop(reason: reason) }

    let startup = DispatchSemaphore(value: 0)
    var failure: String?
    Task {
        do { try await rec.start() }
        catch { failure = "\(error)" }
        startup.signal()
    }
    // 启动有界：SCShareableContent / startCapture 卡住时也必须给终态
    if startup.wait(timeout: .now() + 30) == .timedOut {
        let msg = "启动超时（30s）：startCapture 未返回，按失败收尾"
        logErr(msg)
        if !opts.statusFile.isEmpty {
            writeFile(opts.statusFile, jsonString(["state": "failed", "exit": 4, "error": "startup_timeout"]))
        }
        exit(4)
    }

    if let failure = failure {
        logErr(failure)
        // 退出码分工：64 = 用法错误（与参数解析失败一致，也和 record-game.sh 的约定对齐），
        // 2 = 其它启动失败，3 = 预留「拒绝覆盖」。用法错误**不能**复用 3，否则会被误读成"拒绝覆盖"。
        let code = failure.hasPrefix("用法错误") ? 64 : 2
        if !opts.statusFile.isEmpty {
            writeFile(opts.statusFile, jsonString(["state": "failed", "exit": code, "error": failure]))
        }
        exit(Int32(code))
    }

    // 运行 + 收尾有界：到期还没终态就如实报失败，绝不无限等
    let budget: Double = opts.duration > 0 ? opts.duration : max(opts.maxSeconds, 1)
    let hardDeadline = Date().addingTimeInterval(budget + 30)
    while rec.finished.wait(timeout: .now() + 0.5) == .timedOut {
        if Date() > hardDeadline {
            let msg = "收尾超时（\(Int(budget + 30))s 内没有终态）：按失败退出"
            logErr(msg)
            if !opts.statusFile.isEmpty {
                writeFile(opts.statusFile, jsonString(["state": "failed", "exit": 4,
                                                       "error": "no_terminal_state_timeout"]))
            }
            exit(4)
        }
    }
    exit(rec.finalExitCode)
} catch {
    logErr("\(error)")
    exit(64)
}
