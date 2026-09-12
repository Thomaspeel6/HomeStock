// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "HomeStock",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(
            name: "HomeStock",
            path: "Sources/HomeStock",
            swiftSettings: [.swiftLanguageMode(.v5)]
        )
    ]
)
