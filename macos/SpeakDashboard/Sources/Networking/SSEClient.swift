import Foundation
import SwiftUI

enum ConnectionStatus: Equatable {
    case disconnected
    case connecting
    case connected

    var color: Color { switch self { case .connected: .green; case .connecting: .yellow; case .disconnected: .red } }
    var label: String { switch self { case .connected: "Connected"; case .connecting: "Connecting"; case .disconnected: "Disconnected" } }
}

final class SSEClient: NSObject, URLSessionDataDelegate, @unchecked Sendable {
    typealias EventHandler = (String, Data) -> Void

    private let url: URL
    private let onEvent: EventHandler
    private let onStatusChange: (ConnectionStatus) -> Void

    private var session: URLSession?
    private var task: URLSessionDataTask?
    private var buffer = Data()
    private var retryCount = 0
    private var isRunning = false
    private let maxBackoff: TimeInterval = 30

    init(url: URL, onEvent: @escaping EventHandler, onStatusChange: @escaping (ConnectionStatus) -> Void) {
        self.url = url
        self.onEvent = onEvent
        self.onStatusChange = onStatusChange
        super.init()
    }

    func connect() {
        guard !isRunning else { return }
        isRunning = true
        startConnection()
    }

    func disconnect() {
        isRunning = false
        task?.cancel()
        task = nil
        session?.invalidateAndCancel()
        session = nil
        buffer = Data()
        onStatusChange(.disconnected)
    }

    private func startConnection() {
        guard isRunning else { return }
        onStatusChange(.connecting)

        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = .infinity
        config.timeoutIntervalForResource = .infinity
        session = URLSession(configuration: config, delegate: self, delegateQueue: .main)

        var request = URLRequest(url: url)
        request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        request.setValue("no-cache", forHTTPHeaderField: "Cache-Control")
        task = session?.dataTask(with: request)
        task?.resume()
    }

    private func scheduleReconnect() {
        guard isRunning else { return }
        onStatusChange(.disconnected)
        let delay = min(pow(2.0, Double(retryCount)), maxBackoff)
        retryCount += 1
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
            guard let self, self.isRunning else { return }
            self.buffer = Data()
            self.startConnection()
        }
    }

    // MARK: - URLSessionDataDelegate

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive response: URLResponse,
                    completionHandler: @escaping (URLSession.ResponseDisposition) -> Void) {
        if let http = response as? HTTPURLResponse, http.statusCode == 200 {
            onStatusChange(.connected)
            completionHandler(.allow)
        } else {
            completionHandler(.cancel)
            scheduleReconnect()
        }
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        buffer.append(data)
        processBuffer()
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        if isRunning {
            scheduleReconnect()
        }
    }

    // MARK: - SSE Parsing

    private func processBuffer() {
        guard let text = String(data: buffer, encoding: .utf8) else { return }

        // Split on double newline to find complete messages
        let parts = text.components(separatedBy: "\n\n")
        guard parts.count > 1 else { return }

        // Keep the last incomplete part in the buffer
        let lastPart = parts.last ?? ""
        buffer = Data(lastPart.utf8)

        // Process all complete messages
        for i in 0..<(parts.count - 1) {
            let message = parts[i]
            parseSSEMessage(message)
        }
    }

    private func parseSSEMessage(_ message: String) {
        var eventName = "message"
        var dataLines: [String] = []

        for line in message.split(separator: "\n", omittingEmptySubsequences: false) {
            let lineStr = String(line)
            if lineStr.hasPrefix("event:") {
                eventName = lineStr.dropFirst(6).trimmingCharacters(in: .whitespaces)
            } else if lineStr.hasPrefix("data:") {
                dataLines.append(String(lineStr.dropFirst(5)).trimmingCharacters(in: .init(charactersIn: " ")))
            }
        }

        guard !dataLines.isEmpty else { return }
        let joined = dataLines.joined(separator: "\n")
        guard let data = joined.data(using: .utf8) else { return }

        // Reset backoff on successful state event (initial handshake)
        if eventName == "state" {
            retryCount = 0
        }

        onEvent(eventName, data)
    }
}
