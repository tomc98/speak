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

    var index: Int {
        Self.allCases.firstIndex(of: self) ?? 0
    }
}

struct PopoverRootView: View {
    let viewModel: DashboardViewModel

    @State private var selectedTab: DashboardTab = .nowPlaying
    @State private var navigatingForward = true
    @Namespace private var tabNamespace

    var body: some View {
        GlassEffectContainer {
            VStack(spacing: 0) {
                header
                tabPicker
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
    }

    private var tabPicker: some View {
        GlassEffectContainer(spacing: 0) {
            HStack(spacing: 0) {
                ForEach(DashboardTab.allCases, id: \.self) { tab in
                    let isSelected = selectedTab == tab
                    Button {
                        guard tab != selectedTab else { return }
                        navigatingForward = tab.index > selectedTab.index
                        withAnimation(.spring(duration: 0.45, bounce: 0.15)) {
                            selectedTab = tab
                        }
                    } label: {
                        Image(systemName: isSelected ? tab.icon : tab.icon)
                            .font(.system(size: 14, weight: isSelected ? .semibold : .regular))
                            .symbolEffect(.bounce, value: isSelected)
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 8)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(isSelected ? .primary : .secondary)
                    .glassEffect(
                        isSelected ? .regular.tint(.accentColor).interactive() : .clear,
                        in: Capsule()
                    )
                    .glassEffectID(tab.rawValue, in: tabNamespace)
                    .help(tab.rawValue)
                }
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
    }

    @ViewBuilder
    private var tabContent: some View {
        ZStack {
            tabView(for: selectedTab)
                .id(selectedTab)
                .transition(
                    .asymmetric(
                        insertion: .move(edge: navigatingForward ? .trailing : .leading)
                            .combined(with: .opacity)
                            .combined(with: .scale(scale: 0.92)),
                        removal: .move(edge: navigatingForward ? .leading : .trailing)
                            .combined(with: .opacity)
                            .combined(with: .scale(scale: 0.92))
                    )
                )
        }
        .clipped()
        .animation(.spring(duration: 0.5, bounce: 0.12), value: selectedTab)
    }

    @ViewBuilder
    private func tabView(for tab: DashboardTab) -> some View {
        switch tab {
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
