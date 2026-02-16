import AppKit
import SwiftUI

final class FloatingPanelController: NSPanel {
    init(viewModel: DashboardViewModel) {
        super.init(
            contentRect: NSRect(x: 0, y: 0, width: 160, height: 300),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: true
        )

        level = .floating
        isOpaque = false
        backgroundColor = .clear
        hasShadow = true
        isMovableByWindowBackground = true
        collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        hidesOnDeactivate = false

        let hostingView = NSHostingView(rootView: FloatingHeadsView(viewModel: viewModel))
        contentView = hostingView
    }

    func positionOnScreen() {
        guard let screen = NSScreen.main else { return }
        let visibleFrame = screen.visibleFrame
        let panelFrame = frame
        let x = visibleFrame.minX + 16
        let y = visibleFrame.maxY - panelFrame.height - 16
        setFrameOrigin(NSPoint(x: x, y: y))
    }

    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
}
