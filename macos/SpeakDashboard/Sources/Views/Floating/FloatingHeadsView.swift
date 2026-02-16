import SwiftUI

struct FloatingHeadsView: View {
    let viewModel: DashboardViewModel

    var body: some View {
        VStack(spacing: 8) {
            if let voice = viewModel.playback.currentVoice {
                // Active speaker — large portrait
                VStack(spacing: 4) {
                    PortraitView(
                        voiceName: voice,
                        amplitude: viewModel.lipSync.amplitude,
                        size: 120,
                        voiceColor: viewModel.voiceColor(for: voice),
                        portraitManager: viewModel.portraitManager
                    )
                    Text(voice)
                        .font(.caption.bold())
                        .foregroundStyle(.white)
                        .shadow(color: .black.opacity(0.8), radius: 2)
                }
            }

            // Queued thumbnails — up to 5
            let queued = viewModel.queueItems.filter { !$0.isPlaying }.prefix(5)
            if !queued.isEmpty {
                Divider()
                    .frame(width: 80)
                    .opacity(0.5)

                ForEach(Array(queued)) { item in
                    PortraitView(
                        voiceName: item.voice,
                        amplitude: 0,
                        size: 48,
                        voiceColor: viewModel.voiceColor(for: item.voice),
                        portraitManager: viewModel.portraitManager
                    )
                }
            }
        }
        .padding(8)
    }
}
