import Foundation

@Observable
@MainActor
final class PlaybackState {
    var isPlaying = false
    var currentVoice: String?
    var currentText: String?
    var currentId: String?
    var currentType: String = "idle"
    var duration: Double?
    var totalDuration: Double?
    var offset: Double = 0
    var elapsed: Double = 0
    var envelope: [Float] = []
    var chunkMs: Int = 50
    var queuedCount: Int = 0
    var channel: String?
    var session: String?
    var isLive = false
    var epoch: String?

    var globalPaused = false
    var channelPaused: [String] = []

    private var playbackStartedAt: Date?
    private var elapsedTimer: Timer?

    func updateFromVoiceActive(_ data: VoiceActiveEvent) {
        stopTimer()
        if data.type == "idle" {
            isPlaying = false
            currentVoice = nil
            currentText = nil
            currentId = nil
            currentType = "idle"
            duration = nil
            totalDuration = nil
            offset = 0
            elapsed = 0
            envelope = []
            channel = nil
            session = nil
            isLive = false
            epoch = nil
        } else {
            isPlaying = true
            currentVoice = data.voice
            currentText = data.text
            currentId = data.id
            currentType = data.type ?? "speak"
            duration = data.duration
            totalDuration = data.totalDuration
            offset = data.offset ?? 0
            elapsed = data.offset ?? 0
            envelope = data.envelope ?? []
            chunkMs = data.chunkMs ?? 50
            channel = data.channel
            session = data.session
            isLive = data.live ?? false
            epoch = data.epoch
            startTimer()
        }
        queuedCount = data.queued ?? 0
    }

    /// Collection finished mid-playback: totals become known and the envelope is
    /// replaced by the calibrated one. The elapsed clock keeps running.
    func applyVoiceUpdate(_ data: VoiceUpdateEvent) {
        duration = data.duration
        totalDuration = data.totalDuration
        chunkMs = data.chunkMs ?? chunkMs
        if let envelope = data.envelope { self.envelope = envelope }
    }

    /// Connected mid-playback: rebuild from the state snapshot's now_playing.
    func applyNowPlaying(_ data: NowPlaying, item: QueueItem) {
        stopTimer()
        isPlaying = true
        currentVoice = item.voice
        currentText = item.text
        currentId = data.id
        currentType = "speak"
        channel = item.channel
        session = item.session
        duration = data.duration
        totalDuration = data.totalDuration
        chunkMs = data.chunkMs ?? 50
        envelope = data.envelopeSoFar ?? []
        isLive = data.live
        epoch = data.epoch
        offset = data.elapsedEstimate ?? 0
        elapsed = offset
        startTimer()
    }

    private func startTimer() {
        playbackStartedAt = Date()
        elapsedTimer = Timer.scheduledTimer(withTimeInterval: 1.0 / 30.0, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in
                self?.tickElapsed()
            }
        }
    }

    private func stopTimer() {
        elapsedTimer?.invalidate()
        elapsedTimer = nil
        playbackStartedAt = nil
    }

    private func tickElapsed() {
        guard let startedAt = playbackStartedAt, !globalPaused else { return }
        let raw = offset + Date().timeIntervalSince(startedAt)
        // While the total is unknown (live playback before voice_update) there is
        // nothing to clamp against — clamping to 0 would pin the clock.
        if let total = totalDuration ?? duration, total > 0 {
            elapsed = min(raw, total)
        } else {
            elapsed = raw
        }
    }

    func updateFromPauseState(_ data: PauseStateEvent) {
        globalPaused = data.globalPaused
        channelPaused = data.channelPaused
    }
}

struct VoiceActiveEvent: Codable {
    let id: String?
    let voice: String?
    let type: String?
    let text: String?
    let duration: Double?
    let totalDuration: Double?
    let offset: Double?
    let segments: [DialogueSegment]?
    let envelope: [Float]?
    let chunkMs: Int?
    let queued: Int?
    let channel: String?
    let session: String?
    let priority: Bool?
    let live: Bool?
    let epoch: String?

    enum CodingKeys: String, CodingKey {
        case id, voice, type, text, duration
        case totalDuration = "total_duration"
        case offset, segments, envelope
        case chunkMs = "chunk_ms"
        case queued, channel, session, priority
        case live, epoch
    }
}

struct EnvelopeAppendEvent: Codable {
    let id: String
    let epoch: String
    let seq: Int
    let values: [Float]
    let chunkMs: Int?

    enum CodingKeys: String, CodingKey {
        case id, epoch, seq, values
        case chunkMs = "chunk_ms"
    }
}

struct VoiceUpdateEvent: Codable {
    let id: String
    let epoch: String
    let duration: Double?
    let totalDuration: Double?
    let envelope: [Float]?
    let chunkMs: Int?
    let segments: [DialogueSegment]?

    enum CodingKeys: String, CodingKey {
        case id, epoch, duration
        case totalDuration = "total_duration"
        case envelope
        case chunkMs = "chunk_ms"
        case segments
    }
}

struct NowPlaying: Codable {
    let id: String
    let live: Bool
    let phase: String?
    let epoch: String?
    let elapsedEstimate: Double?
    let duration: Double?
    let totalDuration: Double?
    let envelopeSoFar: [Float]?
    let seq: Int?
    let chunkMs: Int?

    enum CodingKeys: String, CodingKey {
        case id, live, phase, epoch
        case elapsedEstimate = "elapsed_estimate"
        case duration
        case totalDuration = "total_duration"
        case envelopeSoFar = "envelope_so_far"
        case seq
        case chunkMs = "chunk_ms"
    }
}

struct DialogueSegment: Codable {
    let voice: String
    let text: String
    let chars: Int
    let start: Double?
    let end: Double?
}

struct PauseStateEvent: Codable {
    let globalPaused: Bool
    let channelPaused: [String]

    enum CodingKeys: String, CodingKey {
        case globalPaused = "global_paused"
        case channelPaused = "channel_paused"
    }
}

struct QueueStatusResponse: Codable {
    let playing: Bool
    let queued: Int
    let total: Int
    let items: [QueueItem]
    let paused: Bool
    let channelPaused: [String]
    let recentHistory: [HistoryEntry]?
    let nowPlaying: NowPlaying?

    enum CodingKeys: String, CodingKey {
        case playing, queued, total, items, paused
        case channelPaused = "channel_paused"
        case recentHistory = "recent_history"
        case nowPlaying = "now_playing"
    }
}

struct HistoryResponse: Codable {
    let entries: [HistoryEntry]
    let total: Int
}
