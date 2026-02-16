import AppKit
import SwiftUI

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    private var floatingPanel: FloatingPanelController?
    let viewModel = DashboardViewModel()

    func applicationDidFinishLaunching(_ notification: Notification) {
        floatingPanel = FloatingPanelController(viewModel: viewModel)

        viewModel.onPlaybackChanged = { [weak self] isActive in
            DispatchQueue.main.async {
                self?.updateFloatingPanel(isActive: isActive)
            }
        }

        viewModel.connect()
    }

    private func updateFloatingPanel(isActive: Bool) {
        guard let panel = floatingPanel else { return }
        if isActive {
            if !panel.isVisible {
                panel.positionOnScreen()
                panel.orderFront(nil)
            }
        } else if viewModel.queueItems.isEmpty {
            panel.orderOut(nil)
        }
    }
}
