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
        GlassEffectContainer {
            VStack(spacing: 0) {
                header
                Divider()
                tabContent
            }
            .frame(width: 360, height: 520)
            .glassEffect(.clear, in: RoundedRectangle(cornerRadius: 12))
        }
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
        .glassEffect(in: RoundedRectangle(cornerRadius: 8))
    }

    private var tabPicker: some View {
        HStack(spacing: 4) {
            ForEach(DashboardTab.allCases, id: \.self) { tab in
                Button {
                    selectedTab = tab
                } label: {
                    Label(tab.rawValue, systemImage: tab.icon)
                        .font(.caption)
                        .padding(.horizontal, 8)
                        .padding(.vertical, 6)
                }
                .glassEffect(
                    selectedTab == tab ? .regular.tint(.accentColor) : .regular,
                    in: Capsule()
                )
            }
        }
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
