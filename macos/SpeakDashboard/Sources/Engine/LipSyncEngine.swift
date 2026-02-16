import Foundation

@Observable
@MainActor
final class LipSyncEngine {

    private(set) var amplitude: Float = 0
    private(set) var activeVoice: String?
    private(set) var isActive = false

    private var envelope: [Float] = []
    private var chunkMs: Int = 50
    private var startTime: TimeInterval = 0
    private var smoothedAmp: Float = 0
    private var lastTickTime: TimeInterval = 0
    private var openMs: Double = 0
    private var closingUntil: TimeInterval = 0
    private var timer: Timer?
    private var paused = false
    private var pauseTime: TimeInterval = 0

    let startDelay: TimeInterval = 0.08

    func start(voiceName: String, envelope: [Float], chunkMs: Int = 50) {
        stop()
        guard !envelope.isEmpty else { return }
        self.envelope = envelope
        self.chunkMs = chunkMs
        self.activeVoice = voiceName
        self.smoothedAmp = 0
        self.lastTickTime = 0
        self.openMs = 0
        self.closingUntil = 0
        self.startTime = ProcessInfo.processInfo.systemUptime + startDelay
        self.isActive = true
        startTimer()
    }

    func stop() {
        stopTimer()
        paused = false
        pauseTime = 0
        activeVoice = nil
        envelope = []
        smoothedAmp = 0
        amplitude = 0
        isActive = false
        openMs = 0
        closingUntil = 0
        lastTickTime = 0
    }

    func pause() {
        guard !paused, !envelope.isEmpty else { return }
        paused = true
        pauseTime = ProcessInfo.processInfo.systemUptime
        stopTimer()
        amplitude = 0
    }

    func resume() {
        guard paused, !envelope.isEmpty else { return }
        let pauseDuration = ProcessInfo.processInfo.systemUptime - pauseTime
        startTime += pauseDuration
        paused = false
        pauseTime = 0
        lastTickTime = 0
        startTimer()
    }

    // MARK: - Private

    private func startTimer() {
        timer = Timer.scheduledTimer(withTimeInterval: 1.0 / 60.0, repeats: true) { [weak self] _ in
            Task { @MainActor in
                self?.tick()
            }
        }
    }

    private func stopTimer() {
        timer?.invalidate()
        timer = nil
    }

    private func tick() {
        let now = ProcessInfo.processInfo.systemUptime
        let elapsed = now - startTime

        if elapsed < 0 { return }

        let dt: Double
        if lastTickTime > 0 {
            dt = (now - lastTickTime) * 1000
        } else {
            dt = 16
        }
        lastTickTime = now

        let chunkSec = Double(chunkMs) / 1000.0
        let idx = Int(floor(elapsed / chunkSec))

        if idx >= envelope.count {
            amplitude = 0
            stop()
            return
        }

        let frac = Float((elapsed / chunkSec) - Double(idx))
        let a = envelope[idx]
        let nextIdx = min(idx + 1, envelope.count - 1)
        let b = envelope[nextIdx]
        let rawAmp = a + (b - a) * frac

        let alpha: Float = rawAmp > smoothedAmp ? 0.4 : 0.15
        smoothedAmp += (rawAmp - smoothedAmp) * alpha

        var finalAmp = smoothedAmp

        if finalAmp > 0.2 {
            openMs += dt
        } else {
            openMs = 0
        }

        if openMs > 350 && closingUntil == 0 {
            closingUntil = now + 0.12
            openMs = 0
        }

        if closingUntil > 0 {
            if now < closingUntil {
                let progress = 1.0 - (closingUntil - now) / 0.12
                finalAmp *= Float(1.0 - 0.85 * sin(progress * .pi))
            } else {
                closingUntil = 0
            }
        }

        amplitude = max(0, min(1, finalAmp))
    }
}
