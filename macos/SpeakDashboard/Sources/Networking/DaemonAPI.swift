import Foundation

struct DaemonError: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

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
        // New envelope shape: {"voices": [...]}
        if let env = try? JSONDecoder().decode(VoicesEnvelope.self, from: data) {
            return env.voices
        }
        // Backward-compat: bare array
        return try JSONDecoder().decode([Voice].self, from: data)
    }

    func createVoice(name: String, id: String, color: String, style: String, kind: String?) async throws {
        var body: [String: Any] = [
            "name": name,
            "id": id,
            "color": color,
            "style": style,
        ]
        if let kind, !kind.isEmpty { body["kind"] = kind }
        try await request("voices", method: "POST", body: body)
    }

    func updateVoice(currentName: String, patchJSON: Data) async throws {
        try await requestRaw("voices/\(currentName)", method: "PATCH", bodyData: patchJSON)
    }

    func deleteVoice(name: String) async throws {
        try await request("voices/\(name)", method: "DELETE", body: nil)
    }

    // MARK: - Config

    func getConfig() async throws -> DaemonConfig {
        let (data, _) = try await URLSession.shared.data(from: baseURL.appendingPathComponent("config"))
        return try JSONDecoder().decode(DaemonConfig.self, from: data)
    }

    @discardableResult
    func setConfig(model: String) async throws -> DaemonConfig {
        let data = try await request("config", method: "POST", body: ["model": model])
        return try JSONDecoder().decode(DaemonConfig.self, from: data)
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

    @discardableResult
    private func request(_ path: String, method: String, body: [String: Any]?) async throws -> Data {
        let data: Data? = try body.map { try JSONSerialization.data(withJSONObject: $0) }
        return try await requestRaw(path, method: method, bodyData: data)
    }

    @discardableResult
    private func requestRaw(_ path: String, method: String, bodyData: Data?) async throws -> Data {
        var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false)!
        components.path = "/" + path
        guard let url = components.url else {
            throw DaemonError(message: "Invalid URL for \(path)")
        }
        var request = URLRequest(url: url)
        request.httpMethod = method
        if let bodyData {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = bodyData
        }
        let (data, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw DaemonError(message: "No HTTP response")
        }
        if !(200..<300).contains(http.statusCode) {
            if let env = try? JSONDecoder().decode(ErrorEnvelope.self, from: data) {
                throw DaemonError(message: env.error)
            }
            let snippet = String(data: data, encoding: .utf8).map { $0.prefix(200) } ?? ""
            throw DaemonError(message: "HTTP \(http.statusCode): \(snippet)")
        }
        return data
    }

    private func channelBody(_ channel: String?) -> [String: Any]? {
        channel.map { ["channel": $0] }
    }
}

private struct VoicesEnvelope: Decodable {
    let voices: [Voice]
}

private struct ErrorEnvelope: Decodable {
    let error: String
}
