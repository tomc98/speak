import SwiftUI

struct QueuePanelView: View {
    let viewModel: DashboardViewModel

    var body: some View {
        VStack(spacing: 0) {
            if !viewModel.playback.channelPaused.isEmpty || !viewModel.uniqueChannels.isEmpty {
                channelPauseToggles
                    .padding(.horizontal, 12)
                    .padding(.vertical, 8)
                Divider()
            }

            if viewModel.queueItems.isEmpty {
                Spacer()
                Text("Queue is empty")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                Spacer()
            } else {
                List(viewModel.queueItems) { item in
                    HStack(spacing: 8) {
                        Circle()
                            .fill(viewModel.voiceColor(for: item.voice))
                            .frame(width: 8, height: 8)

                        Text(item.voice)
                            .font(.caption)
                            .fontWeight(.medium)
                            .frame(width: 60, alignment: .leading)

                        Text(item.text)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)

                        Spacer()

                        if item.isPlaying {
                            Image(systemName: "speaker.wave.2.fill")
                                .font(.caption2)
                                .foregroundStyle(.blue)
                        }
                    }
                    .padding(.vertical, 2)
                }
                .listStyle(.plain)
            }

            if !viewModel.queueItems.isEmpty {
                Divider()
                Button("Clear Queue") {
                    Task { await viewModel.clearQueue() }
                }
                .font(.caption)
                .glassEffect(.regular.tint(.red), in: Capsule())
                .padding(8)
            }
        }
    }

    private var channelPauseToggles: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 6) {
                ForEach(viewModel.uniqueChannels, id: \.self) { channel in
                    let isPaused = viewModel.playback.channelPaused.contains(channel)
                    Button {
                        Task {
                            if isPaused {
                                await viewModel.resumeChannel(channel)
                            } else {
                                await viewModel.pauseChannel(channel)
                            }
                        }
                    } label: {
                        HStack(spacing: 4) {
                            Image(systemName: isPaused ? "pause.circle.fill" : "play.circle.fill")
                                .font(.caption2)
                            Text(channel)
                                .font(.caption2)
                        }
                        .padding(.horizontal, 8)
                        .padding(.vertical, 4)
                        .glassEffect(
                            isPaused ? .regular.tint(.orange) : .regular.tint(.green),
                            in: Capsule()
                        )
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }
}
