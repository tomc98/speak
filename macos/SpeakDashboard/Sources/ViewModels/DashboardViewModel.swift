import AppKit
import Foundation
import SwiftUI

@Observable
@MainActor
final class DashboardViewModel {
    var playback = PlaybackState()
    var lipSync = LipSyncEngine()
    var portraitManager = PortraitManager()
    var connectionStatus: ConnectionStatus = .disconnected
    var voices: [Voice] = []
    var queueItems: [QueueItem] = []
    var historyEntries: [HistoryEntry] = []

    var onPlaybackChanged: ((Bool) -> Void)?

    private var sseClient: SSEClient?
    private let api = DaemonAPI()
    private let decoder = JSONDecoder()

    var uniqueChannels: [String] {
        let channels = Set(
            queueItems.compactMap(\.channel) + historyEntries.compactMap(\.channel)
        )
        return channels.sorted()
    }

    func voiceColor(for name: String) -> Color {
        voices.first(where: { $0.name == name })?.swiftUIColor ?? .blue
    }

    // MARK: - Connection

    func connect() {
        let port = DaemonAPI.defaultPort
        let url = URL(string: "http://127.0.0.1:\(port)/events")!
        sseClient = SSEClient(url: url, onEvent: { [weak self] event, data in
            guard let self else { return }
            Task { @MainActor in
                self.handleSSEEvent(event: event, data: data)
            }
        }, onStatusChange: { [weak self] status in
            guard let self else { return }
            Task { @MainActor in
                self.connectionStatus = status
                if status == .connected {
                    await self.loadVoices()
                }
            }
        })
        sseClient?.connect()
    }

    func disconnect() {
        sseClient?.disconnect()
    }

    // MARK: - SSE Event Handling

    private func handleSSEEvent(event: String, data: Data) {
        switch event {
        case "state":
            handleStateEvent(data)
        case "voice_active":
            handleVoiceActiveEvent(data)
        case "pause_state":
            handlePauseStateEvent(data)
        case "history_update":
            handleHistoryUpdateEvent(data)
        default:
            break
        }
    }

    private func handleStateEvent(_ data: Data) {
        guard let state = try? decoder.decode(QueueStatusResponse.self, from: data) else { return }
        queueItems = state.items
        playback.globalPaused = state.paused
        playback.channelPaused = state.channelPaused
        if let history = state.recentHistory {
            historyEntries = history
        }
        let isActive = state.playing || state.queued > 0
        onPlaybackChanged?(isActive)
    }

    private func handleVoiceActiveEvent(_ data: Data) {
        guard let event = try? decoder.decode(VoiceActiveEvent.self, from: data) else { return }
        let wasPlaying = playback.isPlaying
        playback.updateFromVoiceActive(event)

        if playback.isPlaying, let voice = event.voice {
            lipSync.start(
                voiceName: voice,
                envelope: event.envelope ?? [],
                chunkMs: event.chunkMs ?? 50
            )
        } else {
            lipSync.stop()
        }

        // Update queue count
        playback.queuedCount = event.queued ?? 0

        let isActive = playback.isPlaying || playback.queuedCount > 0
        let wasActive = wasPlaying
        if isActive != wasActive {
            onPlaybackChanged?(isActive)
        }
    }

    private func handlePauseStateEvent(_ data: Data) {
        guard let event = try? decoder.decode(PauseStateEvent.self, from: data) else { return }
        playback.updateFromPauseState(event)

        if event.globalPaused {
            lipSync.pause()
        } else {
            lipSync.resume()
        }
    }

    private func handleHistoryUpdateEvent(_ data: Data) {
        guard let entry = try? decoder.decode(HistoryEntry.self, from: data) else { return }
        historyEntries.insert(entry, at: 0)
        if historyEntries.count > 200 {
            historyEntries = Array(historyEntries.prefix(200))
        }
    }

    // MARK: - Actions

    func pause() async {
        try? await api.pause()
    }

    func resume() async {
        try? await api.resume()
    }

    func skip() async {
        try? await api.skip()
    }

    func seek(offset: Double) async {
        try? await api.seek(offset: offset)
    }

    func replay(id: String) async {
        try? await api.replay(id: id)
    }

    func clearQueue() async {
        try? await api.clearQueue()
    }

    func pauseChannel(_ channel: String) async {
        try? await api.pause(channel: channel)
    }

    func resumeChannel(_ channel: String) async {
        try? await api.resume(channel: channel)
    }

    func loadMoreHistory() async {
        let offset = historyEntries.count
        guard let response = try? await api.fetchHistory(limit: 50, offset: offset) else { return }
        historyEntries.append(contentsOf: response.entries)
    }

    private func loadVoices() async {
        guard voices.isEmpty else { return }
        if let fetched = try? await api.fetchVoices() {
            voices = fetched
        }
    }
}
