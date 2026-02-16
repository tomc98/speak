import Foundation

struct DaemonAPI: Sendable {
    let baseURL: URL

    init(port: Int = Self.defaultPort) {
        baseURL = URL(string: "http://127.0.0.1:\(port)")!
    }

    static var defaultPort: Int {
        if let env = ProcessInfo.processInfo.environment["SPEAK_PORT"], let p = Int(env) { return p }
        return 7865
    }

    // MARK: - Queue Control

    func pause(channel: String? = nil) async throws {
        try await post("queue/pause", body: channelBody(channel))
    }

    func resume(channel: String? = nil) async throws {
        try await post("queue/resume", body: channelBody(channel))
    }

    func skip() async throws {
        try await post("queue/skip")
    }

    func seek(offset: Double) async throws {
        try await post("queue/seek", body: ["offset": offset])
    }

    func clearQueue(channel: String? = nil) async throws {
        try await post("queue/clear", body: channelBody(channel))
    }

    // MARK: - History

    func replay(id: String) async throws {
        try await post("history/replay", body: ["id": id])
    }

    func fetchHistory(limit: Int = 50, offset: Int = 0, channel: String? = nil) async throws -> HistoryResponse {
        var components = URLComponents(url: baseURL.appendingPathComponent("history"), resolvingAgainstBaseURL: false)!
        var queryItems = [
            URLQueryItem(name: "limit", value: "\(limit)"),
            URLQueryItem(name: "offset", value: "\(offset)"),
        ]
        if let channel { queryItems.append(URLQueryItem(name: "channel", value: channel)) }
        components.queryItems = queryItems
        let (data, _) = try await URLSession.shared.data(from: components.url!)
        return try JSONDecoder().decode(HistoryResponse.self, from: data)
    }

    // MARK: - Voices

    func fetchVoices() async throws -> [Voice] {
        let (data, _) = try await URLSession.shared.data(from: baseURL.appendingPathComponent("voices"))
        return try JSONDecoder().decode([Voice].self, from: data)
    }

    // MARK: - Queue Status

    func fetchQueueStatus(channel: String? = nil) async throws -> QueueStatusResponse {
        var components = URLComponents(url: baseURL.appendingPathComponent("queue"), resolvingAgainstBaseURL: false)!
        if let channel {
            components.queryItems = [URLQueryItem(name: "channel", value: channel)]
        }
        let (data, _) = try await URLSession.shared.data(from: components.url!)
        return try JSONDecoder().decode(QueueStatusResponse.self, from: data)
    }

    // MARK: - Private

    @discardableResult
    private func post(_ path: String, body: [String: Any]? = nil) async throws -> Data {
        var request = URLRequest(url: baseURL.appendingPathComponent(path))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if let body {
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
        } else {
            request.httpBody = Data("{}".utf8)
        }
        let (data, _) = try await URLSession.shared.data(for: request)
        return data
    }

    private func channelBody(_ channel: String?) -> [String: Any]? {
        channel.map { ["channel": $0] }
    }
}
