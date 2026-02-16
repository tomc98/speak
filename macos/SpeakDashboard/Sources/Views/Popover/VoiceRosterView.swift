import SwiftUI

struct VoiceRosterView: View {
    let viewModel: DashboardViewModel

    private let columns = [
        GridItem(.flexible(), spacing: 12),
        GridItem(.flexible(), spacing: 12),
    ]

    var body: some View {
        if viewModel.voices.isEmpty {
            VStack {
                Spacer()
                Text("No voices loaded")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                Spacer()
            }
            .frame(maxWidth: .infinity)
        } else {
            ScrollView {
                LazyVGrid(columns: columns, spacing: 10) {
                    ForEach(viewModel.voices) { voice in
                        voiceCell(voice)
                    }
                }
                .padding(12)
            }
        }
    }

    private func voiceCell(_ voice: Voice) -> some View {
        HStack(spacing: 10) {
            Circle()
                .fill(voice.swiftUIColor)
                .frame(width: 28, height: 28)
                .overlay {
                    Text(String(voice.name.prefix(1)).uppercased())
                        .font(.caption2.bold())
                        .foregroundStyle(.white)
                }

            VStack(alignment: .leading, spacing: 2) {
                Text(voice.name)
                    .font(.caption)
                    .fontWeight(.medium)
                Text(voice.style)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }

            Spacer()
        }
        .padding(8)
        .background(Color.secondary.opacity(0.08))
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }
}
