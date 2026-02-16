import SwiftUI

struct ConnectionStatusView: View {
    let status: ConnectionStatus

    var body: some View {
        HStack(spacing: 6) {
            Circle()
                .fill(status.color)
                .frame(width: 8, height: 8)

            Text(status.label)
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
    }
}
