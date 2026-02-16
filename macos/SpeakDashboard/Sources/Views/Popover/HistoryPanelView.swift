import SwiftUI

struct HistoryPanelView: View {
    let viewModel: DashboardViewModel

    @State private var expandedId: String?
    @State private var voiceFilter: String?
    @State private var channelFilter: String?

    private var filteredEntries: [HistoryEntry] {
        viewModel.historyEntries.filter { entry in
            if let voiceFilter, entry.voice != voiceFilter { return false }
            if let channelFilter, entry.channel != channelFilter { return false }
            return true
        }
    }

    var body: some View {
        VStack(spacing: 0) {
            filterChips
                .padding(.horizontal, 12)
                .padding(.vertical, 8)

            Divider()

            if filteredEntries.isEmpty {
                Spacer()
                Text("No history")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                Spacer()
            } else {
                ScrollView {
                    LazyVStack(spacing: 0) {
                        ForEach(filteredEntries) { entry in
                            historyRow(entry)
                            Divider().padding(.leading, 12)
                        }

                        Button("Load more") {
                            Task { await viewModel.loadMoreHistory() }
                        }
                        .buttonStyle(.plain)
                        .font(.caption)
                        .foregroundStyle(.blue)
                        .padding(8)
                    }
                }
            }
        }
    }

    private func historyRow(_ entry: HistoryEntry) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 8) {
                Circle()
                    .fill(viewModel.voiceColor(for: entry.voice))
                    .frame(width: 8, height: 8)

                Text(entry.voice)
                    .font(.caption)
                    .fontWeight(.medium)

                Text(entry.text)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(expandedId == entry.id ? nil : 1)

                Spacer()

                Text(relativeTime(entry.date))
                    .font(.caption2)
                    .foregroundStyle(.tertiary)

                Button {
                    Task { await viewModel.replay(id: entry.id) }
                } label: {
                    Image(systemName: "play.circle")
                        .font(.caption)
                }
                .glassEffect(.regular.interactive())
            }

            if expandedId == entry.id {
                Text(entry.text)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .padding(.leading, 16)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
        .contentShape(Rectangle())
        .onTapGesture {
            withAnimation(.easeInOut(duration: 0.15)) {
                expandedId = expandedId == entry.id ? nil : entry.id
            }
        }
    }

    private var filterChips: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 6) {
                let voices = Array(Set(viewModel.historyEntries.map(\.voice))).sorted()
                ForEach(voices, id: \.self) { voice in
                    chipButton(voice, isActive: voiceFilter == voice) {
                        voiceFilter = voiceFilter == voice ? nil : voice
                    }
                }

                let channels = Array(Set(viewModel.historyEntries.compactMap(\.channel))).sorted()
                ForEach(channels, id: \.self) { channel in
                    chipButton("#\(channel)", isActive: channelFilter == channel) {
                        channelFilter = channelFilter == channel ? nil : channel
                    }
                }
            }
        }
    }

    private func chipButton(_ label: String, isActive: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(label)
                .font(.caption2)
                .padding(.horizontal, 8)
                .padding(.vertical, 4)
                .glassEffect(
                    isActive ? .regular.tint(.accentColor) : .regular,
                    in: Capsule()
                )
        }
        .buttonStyle(.plain)
    }

    private func relativeTime(_ date: Date) -> String {
        let seconds = Int(-date.timeIntervalSinceNow)
        if seconds < 60 { return "now" }
        if seconds < 3600 { return "\(seconds / 60)m ago" }
        if seconds < 86400 { return "\(seconds / 3600)h ago" }
        return "\(seconds / 86400)d ago"
    }
}
