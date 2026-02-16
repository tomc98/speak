import SwiftUI

enum DashboardTab: String, CaseIterable {
    case nowPlaying = "Now Playing"
    case queue = "Queue"
    case history = "History"
    case voices = "Voices"

    var icon: String {
        switch self {
        case .nowPlaying: "waveform"
        case .queue: "list.bullet"
        case .history: "clock"
        case .voices: "person.2"
        }
    }
}

struct PopoverRootView: View {
    let viewModel: DashboardViewModel

    @State private var selectedTab: DashboardTab = .nowPlaying

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            tabContent
        }
        .frame(width: 360, height: 520)
        .background(.ultraThinMaterial)
    }

    private var header: some View {
        HStack {
            Text("Speak")
                .font(.headline)

            Spacer()

            ConnectionStatusView(status: viewModel.connectionStatus)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
    }

    private var tabPicker: some View {
        Picker("Tab", selection: $selectedTab) {
            ForEach(DashboardTab.allCases, id: \.self) { tab in
                Label(tab.rawValue, systemImage: tab.icon)
                    .tag(tab)
            }
        }
        .pickerStyle(.segmented)
        .labelsHidden()
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
    }

    @ViewBuilder
    private var tabContent: some View {
        VStack(spacing: 0) {
            tabPicker
            Divider()

            switch selectedTab {
            case .nowPlaying:
                NowPlayingView(viewModel: viewModel)
            case .queue:
                QueuePanelView(viewModel: viewModel)
            case .history:
                HistoryPanelView(viewModel: viewModel)
            case .voices:
                VoiceRosterView(viewModel: viewModel)
            }
        }
    }
}
