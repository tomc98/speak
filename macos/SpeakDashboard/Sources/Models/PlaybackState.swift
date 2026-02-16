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
            startTimer()
        }
        queuedCount = data.queued ?? 0
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
        let total = totalDuration ?? duration ?? 0
        elapsed = min(offset + Date().timeIntervalSince(startedAt), total)
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
    let priority: Bool?

    enum CodingKeys: String, CodingKey {
        case id, voice, type, text, duration
        case totalDuration = "total_duration"
        case offset, segments, envelope
        case chunkMs = "chunk_ms"
        case queued, channel, priority
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

    enum CodingKeys: String, CodingKey {
        case playing, queued, total, items, paused
        case channelPaused = "channel_paused"
        case recentHistory = "recent_history"
    }
}

struct HistoryResponse: Codable {
    let entries: [HistoryEntry]
    let total: Int
}
