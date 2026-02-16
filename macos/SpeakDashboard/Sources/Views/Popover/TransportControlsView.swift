import SwiftUI

struct TransportControlsView: View {
    let viewModel: DashboardViewModel

    @State private var isDragging = false
    @State private var dragValue: Double = 0

    private var elapsed: Double {
        isDragging ? dragValue : viewModel.playback.elapsed
    }

    private var total: Double {
        viewModel.playback.totalDuration ?? viewModel.playback.duration ?? 0
    }

    private var remaining: Double {
        max(0, total - elapsed)
    }

    private var progress: Double {
        total > 0 ? min(elapsed / total, 1) : 0
    }

    var body: some View {
        VStack(spacing: 8) {
            // Smooth scrubber
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    // Track
                    Capsule()
                        .fill(.clear)
                        .frame(height: 4)
                        .glassEffect(.clear, in: Capsule())

                    // Fill
                    Capsule()
                        .fill(Color.accentColor)
                        .frame(width: max(0, geo.size.width * progress), height: 4)
                        .glassEffect(.regular.tint(.accentColor), in: Capsule())
                        .animation(.linear(duration: isDragging ? 0 : 1.0 / 30.0), value: progress)

                    // Thumb
                    Circle()
                        .fill(Color.accentColor)
                        .frame(width: 10, height: 10)
                        .offset(x: max(0, geo.size.width * progress - 5))
                        .animation(.linear(duration: isDragging ? 0 : 1.0 / 30.0), value: progress)
                }
                .frame(height: 10)
                .contentShape(Rectangle())
                .gesture(
                    DragGesture(minimumDistance: 0)
                        .onChanged { value in
                            isDragging = true
                            let fraction = max(0, min(value.location.x / geo.size.width, 1))
                            dragValue = fraction * total
                        }
                        .onEnded { value in
                            let fraction = max(0, min(value.location.x / geo.size.width, 1))
                            let seekTo = fraction * total
                            isDragging = false
                            Task { await viewModel.seek(offset: seekTo) }
                        }
                )
            }
            .frame(height: 10)

            HStack {
                Text(formatTime(elapsed))
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
                Spacer()
                Text("-\(formatTime(remaining))")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
            }

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

                Button {
                    Task { await viewModel.skip() }
                } label: {
                    Image(systemName: "forward.fill")
                        .font(.title2)
                }
                .padding(8)
                .glassEffect(.regular.interactive())
            }
        }
        .padding(.horizontal, 16)
    }

    private func formatTime(_ seconds: Double) -> String {
        let m = Int(seconds) / 60
        let s = Int(seconds) % 60
        return String(format: "%d:%02d", m, s)
    }
}
