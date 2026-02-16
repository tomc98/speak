import AppKit
import SwiftUI

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    private var statusItem: NSStatusItem!
    private var popover: NSPopover!
    private var floatingPanel: FloatingPanelController?
    private let viewModel = DashboardViewModel()

    func applicationDidFinishLaunching(_ notification: Notification) {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        if let button = statusItem.button {
            button.image = makeMenuBarIcon()
            button.action = #selector(togglePopover)
            button.target = self
        }

        let rootView = PopoverRootView(viewModel: viewModel)
        let hostingController = NSHostingController(rootView: rootView)

        popover = NSPopover()
        popover.contentSize = NSSize(width: 360, height: 520)
        popover.behavior = .transient
        popover.contentViewController = hostingController

        floatingPanel = FloatingPanelController(viewModel: viewModel)

        viewModel.onPlaybackChanged = { [weak self] isActive in
            DispatchQueue.main.async {
                self?.updateFloatingPanel(isActive: isActive)
            }
        }

        viewModel.connect()
    }

    @objc private func togglePopover() {
        guard let button = statusItem.button else { return }
        if popover.isShown {
            popover.performClose(nil)
        } else {
            popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
            NSApp.activate(ignoringOtherApps: true)
        }
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

    private func makeMenuBarIcon() -> NSImage {
        let image = NSImage(size: NSSize(width: 18, height: 18), flipped: false) { rect in
            NSColor.labelColor.setFill()
            // Waveform bars
            let barWidth: CGFloat = 2
            let gap: CGFloat = 2
            let heights: [CGFloat] = [6, 12, 8, 14, 6]
            let totalWidth = CGFloat(heights.count) * barWidth + CGFloat(heights.count - 1) * gap
            var x = (rect.width - totalWidth) / 2
            for h in heights {
                let y = (rect.height - h) / 2
                let bar = NSRect(x: x, y: y, width: barWidth, height: h)
                NSBezierPath(roundedRect: bar, xRadius: 1, yRadius: 1).fill()
                x += barWidth + gap
            }
            return true
        }
        image.isTemplate = true
        return image
    }
}
