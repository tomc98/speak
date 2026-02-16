import SwiftUI

struct FloatingPortraitView: View {
    let voiceName: String
    let amplitude: Float
    let voiceColor: Color
    let portraitManager: PortraitManager

    private let portraitSize: CGFloat = 120

    var body: some View {
        TimelineView(.animation) { timeline in
            let time = timeline.date.timeIntervalSinceReferenceDate
            let amp = Double(amplitude)

            ZStack {
                // 1. Aurora halo — subtle glow behind portrait
                auroraHalo(time: time, amp: amp)

                // 2. Breathing glow rings
                glowRings(time: time, amp: amp)

                // 3. Portrait
                PortraitView(
                    voiceName: voiceName,
                    amplitude: amplitude,
                    size: portraitSize,
                    voiceColor: voiceColor,
                    portraitManager: portraitManager
                )
            }
            .scaleEffect(
                x: 1.0 + amp * 0.04 + sin(time * 2.7) * amp * 0.02,
                y: 1.0 + amp * 0.04 + cos(time * 3.1) * amp * 0.02
            )
            .rotationEffect(.radians(sin(time * 1.9) * amp * 0.03))
            .offset(
                x: sin(time * 2.3) * amp * 2,
                y: sin(time * 1.2) * 2 + cos(time * 1.7) * amp * 1.5
            )
            .shadow(color: voiceColor.opacity(0.15 + amp * 0.2), radius: 4 + amp * 6)
            .animation(.interactiveSpring(response: 0.3, dampingFraction: 0.7), value: amplitude)
        }
    }

    // MARK: - Aurora Halo

    @ViewBuilder
    private func auroraHalo(time: Double, amp: Double) -> some View {
        let rotation = time * 0.03 + amp * 0.5
        let opacity = amp * 0.5

        AngularGradient(
            colors: [
                voiceColor,
                voiceColor.hueShift(by: 0.33),
                voiceColor.hueShift(by: 0.66),
                voiceColor,
            ],
            center: .center,
            angle: .radians(rotation)
        )
        .frame(width: portraitSize + 48, height: portraitSize + 48)
        .mask(
            RadialGradient(
                colors: [.white, .white.opacity(0.6), .clear],
                center: .center,
                startRadius: portraitSize * 0.2,
                endRadius: portraitSize * 0.55
            )
        )
        .blur(radius: 12)
        .opacity(opacity)
    }

    // MARK: - Breathing Glow Rings

    @ViewBuilder
    private func glowRings(time: Double, amp: Double) -> some View {
        Canvas { context, size in
            guard amp > 0.05 else { return }
            let center = CGPoint(x: size.width / 2, y: size.height / 2)
            let baseRadius = portraitSize / 2

            for i in 0..<2 {
                let phase = Double(i) * 1.0
                let breath = sin(time * 1.8 + phase) * 0.5 + 0.5
                let expansion = amp * 8 * breath + Double(i) * 5
                let radius = baseRadius + 4 + expansion
                let ringOpacity = max(0, (amp - 0.05) * 0.4 * breath)

                if ringOpacity > 0.02 {
                        let rect = CGRect(
                            x: center.x - radius,
                            y: center.y - radius,
                            width: radius * 2,
                            height: radius * 2
                        )
                        let path = Circle().path(in: rect)
                        context.stroke(
                            path,
                            with: .color(voiceColor.opacity(ringOpacity)),
                            lineWidth: 2.0
                        )
                    }
                }

                // Single ripple on loud bursts
                if amp > 0.65 {
                    let ripplePhase = fmod(time * 2.5, 3.0)
                    let rippleProgress = ripplePhase / 3.0
                    let rippleRadius = baseRadius + 8 + rippleProgress * 30
                    let rippleOpacity = (1.0 - rippleProgress) * amp * 0.25

                    if rippleOpacity > 0.02 {
                        let rect = CGRect(
                            x: center.x - rippleRadius,
                            y: center.y - rippleRadius,
                            width: rippleRadius * 2,
                            height: rippleRadius * 2
                        )
                        let path = Circle().path(in: rect)
                        context.stroke(
                            path,
                            with: .color(voiceColor.opacity(rippleOpacity)),
                            lineWidth: 2.0
                        )
                    }
                }
        }
        .frame(width: portraitSize + 60, height: portraitSize + 60)
        .blur(radius: 3)
        .allowsHitTesting(false)
    }
}

// MARK: - Color Hue Shift

extension Color {
    func hueShift(by amount: Double) -> Color {
        let nsColor = NSColor(self)
        var hue: CGFloat = 0
        var sat: CGFloat = 0
        var bri: CGFloat = 0
        var alpha: CGFloat = 0
        let converted = nsColor.usingColorSpace(.sRGB) ?? nsColor
        converted.getHue(&hue, saturation: &sat, brightness: &bri, alpha: &alpha)
        return Color(hue: fmod(hue + amount, 1.0), saturation: Double(sat), brightness: Double(bri), opacity: Double(alpha))
    }
}
