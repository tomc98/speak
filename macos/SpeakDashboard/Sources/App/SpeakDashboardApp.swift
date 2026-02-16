import SwiftUI

@main
struct SpeakDashboardApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate

    var body: some Scene {
        MenuBarExtra("Speak", systemImage: "waveform") {
            PopoverRootView(viewModel: appDelegate.viewModel)
        }
        .menuBarExtraStyle(.window)
    }
}
