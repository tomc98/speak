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
    private var live = false

    let startDelay: TimeInterval = 0.08

    /// `live` starts the engine on an empty envelope that later appends extend;
    /// `offset` is how much of the utterance already played (reconnect).
    func start(voiceName: String, envelope: [Float], chunkMs: Int = 50,
               live: Bool = false, offset: TimeInterval = 0) {
        stop()
        guard live || !envelope.isEmpty else { return }
        self.envelope = envelope
        self.live = live
        self.chunkMs = chunkMs
        self.activeVoice = voiceName
        self.smoothedAmp = 0
        self.lastTickTime = 0
        self.openMs = 0
        self.closingUntil = 0
        self.startTime = ProcessInfo.processInfo.systemUptime + startDelay - offset
        self.isActive = true
        startTimer()
    }

    /// Extends the envelope without touching the clock. A gap zero-fills; an
    /// overlap overwrites idempotently.
    func appendEnvelope(seq: Int, values: [Float]) {
        guard live, seq >= 0, !values.isEmpty else { return }
        if envelope.count < seq {
            envelope.append(contentsOf: repeatElement(0, count: seq - envelope.count))
        }
        for (i, value) in values.enumerated() {
            let index = seq + i
            if index < envelope.count {
                envelope[index] = value
            } else {
                envelope.append(value)
            }
        }
    }

    /// Swaps in the calibrated envelope, again leaving the clock alone. An
    /// empty envelope means the calibration decode failed, not silence — the
    /// accumulated appends stay.
    func replaceEnvelope(_ values: [Float], chunkMs: Int? = nil) {
        guard live, !values.isEmpty else { return }
        envelope = values
        if let chunkMs { self.chunkMs = chunkMs }
    }

    func stop() {
        stopTimer()
        paused = false
        pauseTime = 0
        live = false
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
        guard !paused, live || !envelope.isEmpty else { return }
        paused = true
        pauseTime = ProcessInfo.processInfo.systemUptime
        stopTimer()
        amplitude = 0
    }

    func resume() {
        guard paused, live || !envelope.isEmpty else { return }
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
            // Live playback idles past the end of the envelope rather than
            // stopping, so a later append resumes on the same clock.
            amplitude = 0
            smoothedAmp = 0
            if !live { stop() }
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
