import Foundation

struct HistoryEntry: Codable, Identifiable {
    let id: String
    let voice: String
    let text: String
    let channel: String?
    let timestamp: Double
    let duration: Double?
    let type: String
    let failed: Bool

    var date: Date { Date(timeIntervalSince1970: timestamp) }
}
