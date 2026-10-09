// swift-tools-version: 6.2
import PackageDescription

// Optional product only. scripts/build_native.sh builds builtin without SwiftPM.
let package = Package(
    name: "SecretaryNative",
    platforms: [.macOS(.v14)],
    products: [.executable(name: "desktop-secretary-peekaboo", targets: ["SecretaryPeekaboo"])],
    dependencies: [
        .package(path: "Vendor/Peekaboo"),
        .package(url: "https://github.com/apple/swift-log.git", exact: "1.15.1"),
        .package(url: "https://github.com/apple/swift-algorithms", exact: "1.2.1"),
        .package(url: "https://github.com/apple/swift-numerics", exact: "1.1.1"),
    ],
    targets: [.executableTarget(name: "SecretaryPeekaboo", dependencies: [
        .product(name: "PeekabooAutomationKit", package: "Peekaboo"),
        .product(name: "PeekabooFoundation", package: "Peekaboo"),
    ], path: ".", exclude: ["Vendor", "Notices", "upstream", "Tests", "TestWindow.swift", "verify_sources.py", "ABI.md", "THIRD_PARTY_NOTICES.md", "UPSTREAM.json"],
       sources: ["Desktop.swift", "NativeContract.swift", "PeekabooBackend.swift"],
       swiftSettings: [.define("SECRETARY_PEEKABOO")], linkerSettings: [.linkedLibrary("sqlite3")]),
        .testTarget(name: "NativeBackendTests", dependencies: ["SecretaryPeekaboo"], path: "Tests")],
    swiftLanguageModes: [.v5])
