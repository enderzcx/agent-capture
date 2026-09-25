// fixture_app — 采集层的测试夹具（**不是游戏，也不冒充游戏**）。
//
// 用途：给 ScreenCaptureKit 采集层提供**完全可控**的目标，用来验证四件事：
//   1. 动态窗口：窗口会移动/缩放（`--move`），画面必须跟着更新而不是冻结；
//   2. 原声：持续播放一个**指定频率**的正弦音，录制里必须能测到这个频率；
//   3. 遮挡/失焦：把别的 app 拉到前台时，目标窗口仍应被正常采集；
//   4. 目标隔离：同时跑两个夹具（不同频率），录 A 时**不应**出现 B 的频率。
//
// 第 4 条是真正有分量的验证：它靠**频域测量**判定，而不是靠"看起来没问题"。
//
// 编译：  swiftc -O fixture_app.swift -o fixture_app
// 运行：  ./fixture_app --freq 440 --color 255,80,80 --title FIXTURE-A --duration 30
//         ./fixture_app --freq 1200 --color 80,120,255 --title FIXTURE-B --duration 30 --move

import AppKit
import AVFoundation

struct Opts {
    var freq: Double = 440
    var color: (Double, Double, Double) = (1, 0.3, 0.3)
    var title = "FIXTURE"
    var duration: Double = 20
    var move = false
    var gain: Double = 0.25
}

func parseOpts() -> Opts {
    var o = Opts()
    var i = 1
    let a = CommandLine.arguments
    func val() -> String { i += 1; return i < a.count ? a[i] : "" }
    while i < a.count {
        switch a[i] {
        case "--freq": o.freq = Double(val()) ?? o.freq
        case "--color":
            let p = val().split(separator: ",").compactMap { Double($0) }
            if p.count == 3 { o.color = (p[0] / 255.0, p[1] / 255.0, p[2] / 255.0) }
        case "--title": o.title = val()
        case "--duration": o.duration = Double(val()) ?? o.duration
        case "--gain": o.gain = Double(val()) ?? o.gain
        case "--move": o.move = true
        default: break
        }
        i += 1
    }
    return o
}

let opts = parseOpts()

// ---------------------------------------------------------------------------
// 画面：一个持续变化的视图。
// 变化必须**明显且可度量**：移动的色带 + 每秒变化的计数块 + 旋转指针。
// 静态画面会让"画面是否新鲜"的检查失去意义（PTS 在走但内容没变）。
// ---------------------------------------------------------------------------
final class FixtureView: NSView {
    var tick: Double = 0
    override var isFlipped: Bool { return true }

    override func draw(_ dirtyRect: NSRect) {
        let b = bounds
        NSColor.black.setFill()
        b.fill()

        // 背景色带：随时间水平移动
        let bands = 8
        for k in 0..<bands {
            let phase = (tick * 0.6 + Double(k) / Double(bands)).truncatingRemainder(dividingBy: 1.0)
            let x = phase * Double(b.width) - 60
            let r = NSRect(x: x, y: Double(k) * Double(b.height) / Double(bands),
                           width: 60, height: Double(b.height) / Double(bands))
            NSColor(calibratedRed: opts.color.0,
                    green: opts.color.1 * (0.4 + 0.6 * Double(k) / Double(bands)),
                    blue: opts.color.2,
                    alpha: 0.85).setFill()
            r.fill()
        }

        // 旋转指针：证明画面在连续变化
        let cx = Double(b.width) / 2, cy = Double(b.height) / 2
        let rad = min(cx, cy) * 0.7
        let ang = tick * 2.4
        let path = NSBezierPath()
        path.move(to: NSPoint(x: cx, y: cy))
        path.line(to: NSPoint(x: cx + cos(ang) * rad, y: cy + sin(ang) * rad))
        path.lineWidth = 6
        NSColor.white.setStroke()
        path.stroke()

        // 大字：标题 + 频率 + 计时，方便人工核对"录的是哪一个"
        let text = "\(opts.title)\n\(Int(opts.freq)) Hz\nt=\(String(format: "%.1f", tick))s"
        let attrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.monospacedSystemFont(ofSize: 28, weight: .bold),
            .foregroundColor: NSColor.white,
        ]
        text.draw(at: NSPoint(x: 16, y: 16), withAttributes: attrs)
    }
}

// ---------------------------------------------------------------------------
// 声音：指定频率的正弦音，持续播放。
// 用 AVAudioEngine 而不是 NSSound：频率完全可控，便于事后做频域判定。
// ---------------------------------------------------------------------------
final class Tone {
    private let engine = AVAudioEngine()
    private var src: AVAudioSourceNode?
    private var phase: Double = 0

    func start(freq: Double, gain: Double, sampleRate: Double = 48000) {
        let inc = 2.0 * Double.pi * freq / sampleRate
        let g = Float(gain)
        let node = AVAudioSourceNode { [weak self] _, _, frameCount, abl -> OSStatus in
            guard let self = self else { return noErr }
            let bufs = UnsafeMutableAudioBufferListPointer(abl)
            for buf in bufs {
                let p = buf.mData!.assumingMemoryBound(to: Float.self)
                for i in 0..<Int(frameCount) {
                    p[i] = Float(sin(self.phase)) * g
                    self.phase += inc
                    if self.phase > 2 * Double.pi { self.phase -= 2 * Double.pi }
                }
            }
            return noErr
        }
        src = node
        let fmt = AVAudioFormat(standardFormatWithSampleRate: sampleRate, channels: 2)!
        engine.attach(node)
        engine.connect(node, to: engine.mainMixerNode, format: fmt)
        try? engine.start()
    }
}

// ---------------------------------------------------------------------------
// 应用
// ---------------------------------------------------------------------------
let app = NSApplication.shared
app.setActivationPolicy(.regular)

let win = NSWindow(contentRect: NSRect(x: 120, y: 120, width: 640, height: 420),
                   styleMask: [.titled, .closable, .resizable],
                   backing: .buffered, defer: false)
win.title = opts.title
let view = FixtureView(frame: win.contentRect(forFrameRect: win.frame))
win.contentView = view
win.makeKeyAndOrderFront(nil)

let tone = Tone()
tone.start(freq: opts.freq, gain: opts.gain)

let start = Date()
let timer = Timer.scheduledTimer(withTimeInterval: 1.0 / 30.0, repeats: true) { t in
    let el = Date().timeIntervalSince(start)
    view.tick = el
    view.needsDisplay = true

    // 动态窗口：让窗口自己移动 + 轻微缩放，验证采集是否跟随
    if opts.move {
        let x = 120 + sin(el * 0.5) * 160
        let y = 120 + cos(el * 0.4) * 90
        let w = 640 + sin(el * 0.7) * 80
        let h = 420 + cos(el * 0.6) * 60
        win.setFrame(NSRect(x: x, y: y, width: w, height: h), display: true)
    }
    if el >= opts.duration {
        t.invalidate()
        print("FIXTURE_DONE \(opts.title) \(String(format: "%.2f", el))s")
        fflush(stdout)
        NSApp.terminate(nil)
    }
}
RunLoop.main.add(timer, forMode: .common)
print("FIXTURE_READY \(opts.title) freq=\(Int(opts.freq)) move=\(opts.move)")
fflush(stdout)
app.run()
