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
    private var liveId: String?
    private var liveEpoch: String?
    private var isQueueRefreshInFlight = false
    private var queueRefreshPending = false
    private var queuePollTimer: Timer?

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
                } else {
                    // Nothing will ever tell us the utterance ended: the
                    // lip-sync loop idles rather than self-terminating, and the
                    // elapsed clock has no total to clamp against.
                    self.clearPlayback()
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
        case "envelope_append":
            handleEnvelopeAppendEvent(data)
        case "voice_update":
            handleVoiceUpdateEvent(data)
        case "pause_state":
            handlePauseStateEvent(data)
        case "history_update":
            handleHistoryUpdateEvent(data)
        case "voices_updated":
            handleVoicesUpdatedEvent(data)
        default:
            // Some daemon builds deliver events as a generic "message" with
            // the event name inside the JSON payload. Fall back to sniffing.
            if event == "message", let type = try? decoder.decode(EventTypeProbe.self, from: data).type {
                if type == "voices_updated" {
                    handleVoicesUpdatedEvent(data)
                }
            }
        }
    }

    private func handleStateEvent(_ data: Data) {
        guard let state = try? decoder.decode(QueueStatusResponse.self, from: data) else { return }
        applyQueueStatus(state)
        restoreLivePlayback(state)
        let isActive = state.playing || state.queued > 0
        onPlaybackChanged?(isActive)
    }

    /// Connected mid-live-playback: rebuild the clock, the envelope decoded so
    /// far and the lip-sync engine from the snapshot. epoch, elapsed_estimate
    /// and envelope_so_far are null while the entry is still collecting or
    /// starting — nothing to restore then.
    private func restoreLivePlayback(_ state: QueueStatusResponse) {
        guard let nowPlaying = state.nowPlaying, nowPlaying.live, nowPlaying.epoch != nil,
              let item = state.items.first(where: { $0.isPlaying }) else {
            // Idle, file mode, or still collecting — no live generation to
            // adopt, and anything left over from before the reconnect must not
            // survive. A state event only ever arrives on a fresh connection,
            // so nothing legitimate is running at this point.
            liveId = nil
            liveEpoch = nil
            lipSync.stop()
            if !state.playing { playback.clear() }
            return
        }

        liveId = nowPlaying.id
        liveEpoch = nowPlaying.epoch
        playback.applyNowPlaying(nowPlaying, item: item)
        lipSync.start(
            voiceName: item.voice,
            envelope: [],
            chunkMs: nowPlaying.chunkMs ?? 50,
            live: true,
            offset: nowPlaying.elapsedEstimate ?? 0
        )
        lipSync.appendEnvelope(seq: nowPlaying.seq ?? 0, values: nowPlaying.envelopeSoFar ?? [])
    }

    /// Only voice_active and the state snapshot establish the active generation;
    /// envelope_append / voice_update are discarded unless (id, epoch) matches.
    private func matchesLiveGeneration(id: String, epoch: String) -> Bool {
        id == liveId && epoch == liveEpoch
    }

    private func handleEnvelopeAppendEvent(_ data: Data) {
        guard let event = try? decoder.decode(EnvelopeAppendEvent.self, from: data),
              matchesLiveGeneration(id: event.id, epoch: event.epoch) else { return }
        lipSync.appendEnvelope(seq: event.seq, values: event.values)
    }

    private func handleVoiceUpdateEvent(_ data: Data) {
        guard let event = try? decoder.decode(VoiceUpdateEvent.self, from: data),
              matchesLiveGeneration(id: event.id, epoch: event.epoch) else { return }
        playback.applyVoiceUpdate(event)
        if let envelope = event.envelope, !envelope.isEmpty {
            lipSync.replaceEnvelope(envelope, chunkMs: event.chunkMs)
        }
    }

    /// The daemon went away: stop every clock and drop the live generation.
    private func clearPlayback() {
        liveId = nil
        liveEpoch = nil
        lipSync.stop()
        playback.clear()
        updateQueuePolling(isActive: false)
        onPlaybackChanged?(false)
    }

    private func handleVoiceActiveEvent(_ data: Data) {
        guard let event = try? decoder.decode(VoiceActiveEvent.self, from: data) else { return }
        let wasActive = playback.isPlaying || playback.queuedCount > 0
        let previousQueuedCount = playback.queuedCount
        let previousCurrentId = playback.currentId
        playback.updateFromVoiceActive(event)

        if playback.isPlaying, let voice = event.voice {
            let isLive = event.live ?? false
            liveId = isLive ? event.id : nil
            liveEpoch = isLive ? event.epoch : nil
            lipSync.start(
                voiceName: voice,
                envelope: event.envelope ?? [],
                chunkMs: event.chunkMs ?? 50,
                live: isLive
            )
        } else {
            liveId = nil
            liveEpoch = nil
            lipSync.stop()
        }

        // Update queue count
        playback.queuedCount = event.queued ?? 0

        let shouldRefreshQueue = previousQueuedCount != playback.queuedCount ||
            previousCurrentId != playback.currentId ||
            (playback.queuedCount > 0 && queueItems.isEmpty) ||
            (event.type == "idle" && !queueItems.isEmpty)
        if shouldRefreshQueue {
            requestQueueRefresh()
        }

        let isActive = playback.isPlaying || playback.queuedCount > 0
        if isActive != wasActive {
            onPlaybackChanged?(isActive)
        }
        updateQueuePolling(isActive: isActive)
    }

    private func updateQueuePolling(isActive: Bool) {
        if isActive && queuePollTimer == nil {
            queuePollTimer = Timer.scheduledTimer(withTimeInterval: 2.0, repeats: true) { [weak self] _ in
                Task { @MainActor [weak self] in
                    self?.requestQueueRefresh()
                }
            }
        } else if !isActive {
            queuePollTimer?.invalidate()
            queuePollTimer = nil
        }
    }

    private func applyQueueStatus(_ state: QueueStatusResponse) {
        queueItems = state.items
        playback.queuedCount = state.queued
        playback.globalPaused = state.paused
        playback.channelPaused = state.channelPaused
        if let history = state.recentHistory {
            historyEntries = history
        }
    }

    private func requestQueueRefresh() {
        if isQueueRefreshInFlight {
            queueRefreshPending = true
            return
        }

        isQueueRefreshInFlight = true
        Task { [weak self] in
            await self?.refreshQueueStatus()
        }
    }

    private func refreshQueueStatus() async {
        defer {
            isQueueRefreshInFlight = false
            if queueRefreshPending {
                queueRefreshPending = false
                requestQueueRefresh()
            }
        }

        guard let state = try? await api.fetchQueueStatus() else { return }
        applyQueueStatus(state)
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

    func refreshVoices() async {
        if let fetched = try? await api.fetchVoices() {
            voices = fetched
        }
    }

    // MARK: - Voice CRUD

    func createVoice(name: String, id: String, color: String, style: String, kind: String?) async throws {
        try await api.createVoice(name: name, id: id, color: color, style: style, kind: kind)
        await refreshVoices()
    }

    func updateVoice(currentName: String, patch: [String: Any]) async throws {
        let data = try JSONSerialization.data(withJSONObject: patch)
        try await api.updateVoice(currentName: currentName, patchJSON: data)
        await refreshVoices()
    }

    func deleteVoice(name: String) async throws {
        try await api.deleteVoice(name: name)
        portraitManager.invalidate(voiceName: name)
        await refreshVoices()
    }

    // MARK: - voices_updated handling

    private func handleVoicesUpdatedEvent(_ data: Data) {
        let event = try? decoder.decode(VoicesUpdatedEvent.self, from: data)
        let reason = event?.reason ?? ""
        let name = event?.name

        if reason == "portrait", let name {
            portraitManager.invalidate(voiceName: name)
        } else if let name {
            // Created / updated / deleted: best to invalidate just the affected
            // voice, but fall back to a full flush if the event is vague.
            portraitManager.invalidate(voiceName: name)
        } else {
            portraitManager.invalidateAll()
        }

        Task { [weak self] in
            await self?.refreshVoices()
        }
    }
}

private struct VoicesUpdatedEvent: Decodable {
    let type: String?
    let reason: String?
    let name: String?
}

private struct EventTypeProbe: Decodable {
    let type: String?
}
