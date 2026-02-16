import SwiftUI

struct ConnectionStatusView: View {
    let status: ConnectionStatus

    var body: some View {
        Circle()
            .fill(status.color)
            .frame(width: 8, height: 8)
            .shadow(color: status.color.opacity(0.6), radius: 4)
            .animation(.easeInOut(duration: 0.4), value: status)
            .help(status.label)
    }
}
