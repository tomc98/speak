import SwiftUI

struct FloatingHeadsView: View {
    let viewModel: DashboardViewModel

    @Namespace private var orbitalNamespace

    private let orbitRadius: CGFloat = 70
    private let thumbnailSize: CGFloat = 40
    private let arcStart: Double = 210
    private let arcEnd: Double = 330

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
                    .offset(y: -16)

                    // Orbiting queue thumbnails
                    let queued = Array(viewModel.queueItems.filter { !$0.isPlaying }.prefix(5))
                    ForEach(Array(queued.enumerated()), id: \.element.id) { index, item in
                        let angle = orbitAngle(index: index, total: queued.count)

                        PortraitView(
                            voiceName: item.voice,
                            amplitude: 0,
                            size: thumbnailSize,
                            voiceColor: viewModel.voiceColor(for: item.voice),
                            portraitManager: viewModel.portraitManager
                        )
                        .glassEffectID(item.id, in: orbitalNamespace)
                        .offset(
                            x: cos(angle) * orbitRadius,
                            y: sin(angle) * orbitRadius + 10
                        )
                        .transition(.scale.combined(with: .opacity))
                        .animation(.spring(response: 0.5, dampingFraction: 0.7), value: queued.count)
                    }
                }
            }
        }
        .frame(width: 240, height: 260)
        .animation(.spring(response: 0.4, dampingFraction: 0.8), value: viewModel.queueItems.map(\.id))
    }

    private func orbitAngle(index: Int, total: Int) -> Double {
        guard total > 0 else { return 0 }
        let span = arcEnd - arcStart
        let step = total > 1 ? span / Double(total - 1) : 0
        let degrees = arcStart + step * Double(index)
        return degrees * .pi / 180
    }
}
