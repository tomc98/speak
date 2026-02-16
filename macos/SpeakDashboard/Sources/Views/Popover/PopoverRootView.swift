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

// MARK: - Carousel Transition

struct CarouselTransition: Transition {
    let forward: Bool

    func body(content: Content, phase: TransitionPhase) -> some View {
        let sign: CGFloat = switch phase {
        case .willAppear: forward ? 1 : -1
        case .didDisappear: forward ? -1 : 1
        case .identity: 0
        }
        let progress: CGFloat = phase == .identity ? 0 : 1

        content
            .offset(x: progress * sign * 360)
            .scaleEffect(1 - progress * 0.18)
            .rotation3DEffect(
                .degrees(Double(-sign * 18) * progress),
                axis: (x: 0, y: 1, z: 0),
                perspective: 0.4
            )
            .opacity(1 - progress)
    }
}

// MARK: - Root View

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
                        withAnimation(.spring(duration: 0.5, bounce: 0.18)) {
                            selectedTab = tab
                        }
                    } label: {
                        Image(systemName: tab.icon)
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
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .id(selectedTab)
                .transition(CarouselTransition(forward: navigatingForward))
        }
        .clipped()
        .animation(.spring(duration: 0.55, bounce: 0.15), value: selectedTab)
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
