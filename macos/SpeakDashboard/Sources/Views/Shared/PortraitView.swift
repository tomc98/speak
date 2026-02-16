import SwiftUI

struct PortraitView: View {
    let voiceName: String
    let amplitude: Float
    let size: CGFloat
    let voiceColor: Color
    let portraitManager: PortraitManager

    @State private var frames: PortraitFrames?

    private var slightOpacity: Double {
        if amplitude < 0.03 { return 0 }
        if amplitude < 0.15 { return Double(amplitude - 0.03) / 0.12 }
        if amplitude < 0.35 { return 1.0 - Double(amplitude - 0.15) / 0.20 }
        return 0
    }

    private var openOpacity: Double {
        if amplitude < 0.15 { return 0 }
        if amplitude < 0.35 { return Double(amplitude - 0.15) / 0.20 }
        return 1
    }

    var body: some View {
        ZStack {
            if let frames {
                Image(nsImage: frames.closed)
                    .resizable()
                    .aspectRatio(contentMode: .fill)

                Image(nsImage: frames.slight)
                    .resizable()
                    .aspectRatio(contentMode: .fill)
                    .opacity(slightOpacity)

                Image(nsImage: frames.open)
                    .resizable()
                    .aspectRatio(contentMode: .fill)
                    .opacity(openOpacity)
            } else {
                Circle()
                    .fill(voiceColor.opacity(0.3))
                    .overlay {
                        Text(String(voiceName.prefix(1)).uppercased())
                            .font(.system(size: size * 0.4, weight: .bold))
                            .foregroundStyle(voiceColor)
                    }
            }
        }
        .frame(width: size, height: size)
        .clipShape(Circle())
        .overlay {
            if amplitude > 0 {
                Circle()
                    .stroke(voiceColor.opacity(0.6), lineWidth: 2)
                    .shadow(color: voiceColor.opacity(0.4), radius: 6)
            }
        }
        .animation(.easeOut(duration: 0.05), value: amplitude)
        .task {
            frames = await portraitManager.frames(for: voiceName)
        }
    }
}
