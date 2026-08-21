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
                if viewModel.workerStopped {
                    workerStoppedBanner
                }
                tabPicker
                tabContent
            }
            .frame(width: 360, height: 520)
            .glassEffect(.clear, in: RoundedRectangle(cornerRadius: 12))
        }
    }

    private var header: some View {
        HStack(spacing: 8) {
            Text("Speak")
                .font(.headline)

            Spacer()

            modelPicker

            ConnectionStatusView(status: viewModel.connectionStatus)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
    }

    private var modelPicker: some View {
        Picker("Model", selection: modelBinding) {
            ForEach(viewModel.availableModels, id: \.self) { model in
                Text(Self.modelLabel(model))
                    .tag(model)
                    // SwiftUI has no per-segment disable, so an unreachable model
                    // is dimmed and refused by setModel rather than hidden — the
                    // roster staying stable is what makes the tooltip make sense.
                    .foregroundStyle(viewModel.modelIsAvailable(model) ? .primary : .tertiary)
            }
        }
        .pickerStyle(.segmented)
        .labelsHidden()
        .frame(width: 108)
        .disabled(viewModel.connectionStatus != .connected)
        .help(modelPickerHelp)
    }

    private var modelPickerHelp: String {
        guard viewModel.connectionStatus == .connected else {
            return "Disconnected — model cannot be changed"
        }
        if !viewModel.streamingEnabled {
            return "Conversational needs the streaming engine — SPEAK_STREAMING=0 forces the legacy eleven_v3 path"
        }
        return "Synthesis model: \(viewModel.currentModel ?? "unknown")"
    }

    private var modelBinding: Binding<String> {
        Binding(
            get: { viewModel.currentModel ?? viewModel.availableModels.first ?? "" },
            set: { model in Task { await viewModel.setModel(model) } }
        )
    }

    static func modelLabel(_ model: String) -> String {
        switch model {
        case "eleven_v3": "v3"
        case "eleven_v3_conversational": "Conv"
        default: model
        }
    }

    private var workerStoppedBanner: some View {
        HStack(spacing: 6) {
            Image(systemName: "exclamationmark.triangle.fill")
                .foregroundStyle(.orange)
            Text("Daemon playback stopped — restart required")
                .font(.caption)
                .foregroundStyle(.secondary)
            Spacer()
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 6)
        .background(Color.orange.opacity(0.12))
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
