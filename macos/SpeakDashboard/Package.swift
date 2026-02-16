// swift-tools-version: 5.10
import PackageDescription
import Foundation

let packageDir = URL(fileURLWithPath: #filePath).deletingLastPathComponent().path

let package = Package(
    name: "SpeakDashboard",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(
            name: "SpeakDashboard",
            path: "Sources",
            resources: [.process("Resources")],
            linkerSettings: [
                .unsafeFlags(["-Xlinker", "-sectcreate",
                              "-Xlinker", "__TEXT",
                              "-Xlinker", "__info_plist",
                              "-Xlinker", "\(packageDir)/Info.plist"])
            ]
        ),
    ]
)
