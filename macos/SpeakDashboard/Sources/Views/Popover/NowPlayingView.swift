import SwiftUI

struct NowPlayingView: View {
    let viewModel: DashboardViewModel

    var body: some View {
        VStack(spacing: 12) {
            if viewModel.playback.isPlaying, let voice = viewModel.playback.currentVoice {
                PortraitView(
                    voiceName: voice,
                    amplitude: viewModel.lipSync.amplitude,
                    size: 100,
                    voiceColor: viewModel.voiceColor(for: voice),
                    portraitManager: viewModel.portraitManager
                )
                .background {
                    Circle()
                        .fill(.clear)
                        .glassEffect(.clear, in: Circle())
                        .frame(width: 110, height: 110)
                        .allowsHitTesting(false)
                }

                Text(voice)
                    .font(.headline)
                    .foregroundStyle(.primary)

                if let text = viewModel.playback.currentText {
                    Text(text)
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                        .multilineTextAlignment(.center)
                        .frame(maxWidth: .infinity)
                        .padding(.horizontal, 16)
                }

                TransportControlsView(viewModel: viewModel)
                    .padding(.top, 4)
            } else {
                Spacer()
                Image(systemName: "waveform")
                    .font(.system(size: 40))
                    .foregroundStyle(.tertiary)
                Text(viewModel.playback.globalPaused ? "Paused — queuing audio" : "Idle — no audio playing")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                Spacer()

                HStack(spacing: 24) {
                    Button {
                        Task {
                            if viewModel.playback.globalPaused {
                                await viewModel.resume()
                            } else {
                                await viewModel.pause()
                            }
                        }
                    } label: {
                        Image(systemName: viewModel.playback.globalPaused ? "play.fill" : "pause.fill")
                            .font(.title2)
                    }
                    .padding(8)
                    .glassEffect(.regular.interactive())
                }
                .padding(.bottom, 8)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding()
    }
}
