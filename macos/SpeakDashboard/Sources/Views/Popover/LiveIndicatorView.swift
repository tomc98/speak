import SwiftUI

/// Shown while an entry plays live: collection has not finished, so there is no
/// total to scrub against and the only honest readout is "still arriving".
struct LiveIndicatorView: View {
    let voiceColor: Color
    let paused: Bool

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var pulsing = false

    private var animating: Bool { !paused && !reduceMotion }

    var body: some View {
        HStack(spacing: 5) {
            Circle()
                .fill(voiceColor)
                .frame(width: 6, height: 6)
                .opacity(pulsing ? 0.35 : 1)
                .scaleEffect(pulsing ? 0.75 : 1)

            Text(paused ? "paused" : "streaming")
                .font(.caption)
                .foregroundStyle(.tertiary)
                .textCase(.uppercase)
        }
        .animation(animating ? .easeInOut(duration: 0.75).repeatForever(autoreverses: true) : .default,
                   value: pulsing)
        .onAppear { pulsing = animating }
        .onChange(of: animating) { _, isAnimating in pulsing = isAnimating }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(paused ? "Streaming, paused" : "Streaming")
    }
}
