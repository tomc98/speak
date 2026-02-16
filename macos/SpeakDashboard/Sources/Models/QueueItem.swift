import Foundation

struct QueueItem: Codable, Identifiable {
    let id: String
    let position: Int
    let status: String
    let voice: String
    let text: String
    let channel: String?
    let priority: Bool

    var isPlaying: Bool { status == "playing" }
}
