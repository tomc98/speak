import SwiftUI

struct FloatingHeadsView: View {
    let viewModel: DashboardViewModel

    private let orbitRadius: CGFloat = 70
    private let thumbnailSize: CGFloat = 40
    private let orbitYOffset: CGFloat = 24
    private let arcStart: Double = 30
    private let arcEnd: Double = 150

    var body: some View {
        GlassEffectContainer(spacing: 20) {
            ZStack {
                if let voice = viewModel.playback.currentVoice {
                    // Active speaker with all effects
                    VStack(spacing: 2) {
                        FloatingPortraitView(
                            voiceName: voice,
                            amplitude: viewModel.lipSync.amplitude,
                            voiceColor: viewModel.voiceColor(for: voice),
                            portraitManager: viewModel.portraitManager
                        )

                        Text(voice)
                            .font(.caption.bold())
                            .foregroundStyle(.white)
                            .padding(.horizontal, 8)
                            .padding(.vertical, 2)
                            .glassEffect(.clear.tint(viewModel.voiceColor(for: voice)), in: Capsule())
                    }
                    .id(voice)
                    .transition(.opacity.combined(with: .scale(scale: 0.92)))
                    .offset(y: -16)
                    .zIndex(10)

                    // Orbiting queue thumbnails
                    let queued = Array(viewModel.queueItems.filter { !$0.isPlaying }.prefix(5))
                    ForEach(Array(queued.enumerated()), id: \.element.id) { index, item in
                        QueueBubbleView(
                            item: item,
                            index: index,
                            total: queued.count,
                            thumbnailSize: thumbnailSize,
                            orbitRadius: orbitRadius,
                            orbitYOffset: orbitYOffset,
                            angle: orbitAngle(index: index, total: queued.count),
                            voiceColor: viewModel.voiceColor(for: item.voice),
                            portraitManager: viewModel.portraitManager
                        )
                        .zIndex(Double(5 - index))
                    }
                }
            }
        }
        .frame(width: 240, height: 260)
        .animation(.spring(response: 0.5, dampingFraction: 0.75), value: viewModel.queueItems.map(\.id))
        .animation(.easeInOut(duration: 0.4), value: viewModel.playback.currentVoice)
    }

    private func orbitAngle(index: Int, total: Int) -> Double {
        guard total > 0 else { return 0 }
        if total == 1 {
            return ((arcStart + arcEnd) / 2) * .pi / 180
        }
        let span = arcEnd - arcStart
        let step = span / Double(total - 1)
        let degrees = arcStart + step * Double(index)
        return degrees * .pi / 180
    }
}

// MARK: - Queue Bubble

struct QueueBubbleView: View {
    let item: QueueItem
    let index: Int
    let total: Int
    let thumbnailSize: CGFloat
    let orbitRadius: CGFloat
    let orbitYOffset: CGFloat
    let angle: Double
    let voiceColor: Color
    let portraitManager: PortraitManager

    var body: some View {
        TimelineView(.animation) { timeline in
            let time = timeline.date.timeIntervalSinceReferenceDate
            let phase = Double(index) * 1.7
            let bobX = sin(time * 0.9 + phase) * 2.0
            let bobY = cos(time * 0.7 + phase * 0.6) * 1.5

            PortraitView(
                voiceName: item.voice,
                amplitude: 0,
                size: thumbnailSize,
                voiceColor: voiceColor,
                portraitManager: portraitManager
            )
            .shadow(color: voiceColor.opacity(0.3), radius: 4)
            .scaleEffect(index == 0 ? 1.05 : 1.0)
            .offset(
                x: cos(angle) * orbitRadius + bobX,
                y: sin(angle) * orbitRadius + orbitYOffset + bobY
            )
        }
        .transition(
            .asymmetric(
                insertion: .scale(scale: 0.1)
                    .combined(with: .opacity)
                    .combined(with: .offset(y: 30)),
                removal: .scale(scale: 1.4)
                    .combined(with: .opacity)
                    .combined(with: .offset(y: -60))
            )
        )
    }
}
