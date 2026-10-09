import AppKit

final class Fixture: NSObject, NSApplicationDelegate, NSTextFieldDelegate {
    let stateDirectory: URL
    var window: NSWindow!
    var field: NSTextField!
    var status: NSTextField!
    var download: NSButton!
    var lastDownload: String = ""

    init(stateDirectory: URL) { self.stateDirectory = stateDirectory }

    func applicationDidFinishLaunching(_ notification: Notification) {
        window = NSWindow(contentRect: NSRect(x: 120, y: 120, width: 620, height: 280),
                          styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
        window.title = "AI Secretary Self-Owned Test Fixture"
        let content = window.contentView!
        let label = NSTextField(labelWithString: "Local UI verification: type text, then create a synthetic download.")
        label.frame = NSRect(x: 24, y: 220, width: 570, height: 25)
        content.addSubview(label)
        field = NSTextField(frame: NSRect(x: 24, y: 160, width: 570, height: 34))
        field.placeholderString = "Unicode input"
        field.setAccessibilityIdentifier("fixture-input")
        field.delegate = self
        content.addSubview(field)
        download = NSButton(title: "Download Synthetic File", target: self, action: #selector(downloadFile))
        download.frame = NSRect(x: 24, y: 100, width: 220, height: 34)
        download.setAccessibilityIdentifier("fixture-download")
        content.addSubview(download)
        for (identifier, text, y) in [("fixture-account", "fixture-account", 112.0),
                                      ("fixture-conversation", "fixture-conversation", 85.0),
                                      ("fixture-item", "Attachment: fixture-download.txt", 140.0)] {
            let evidence = NSTextField(labelWithString: text)
            evidence.setAccessibilityIdentifier(identifier)
            evidence.frame = NSRect(x: identifier == "fixture-item" ? 24 : 280, y: y, width: 310, height: 20)
            content.addSubview(evidence)
        }
        status = NSTextField(labelWithString: "No download yet")
        status.setAccessibilityIdentifier("fixture-status")
        status.frame = NSRect(x: 24, y: 35, width: 570, height: 40)
        content.addSubview(status)
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        window.makeFirstResponder(field)
        record()
        Timer.scheduledTimer(withTimeInterval: 0.2, repeats: true) { [weak self] _ in self?.record() }
    }

    func controlTextDidChange(_ notification: Notification) { record() }

    @objc func downloadFile() {
        let file = stateDirectory.appendingPathComponent("fixture-download.txt")
        do {
            try Data("Synthetic local download created by AI Secretary fixture.\n".utf8).write(to: file, options: .atomic)
            lastDownload = file.path
            status.stringValue = "Downloaded: fixture-download.txt"
        } catch { status.stringValue = error.localizedDescription }
        record()
    }

    func record() {
        guard window != nil else { return }
        let top = NSScreen.screens.first?.frame.maxY ?? 0
        let frame = window.frame
        let point = field.convert(field.bounds, to: nil)
        let screen = window.convertToScreen(point)
        let button = window.convertToScreen(download.convert(download.bounds, to: nil))
        let values: [String: Any] = ["pid": getpid(), "window_id": window.windowNumber,
            "geometry": ["x": frame.minX, "y": top - frame.maxY, "width": frame.width, "height": frame.height],
            "text": field.stringValue, "download_path": lastDownload,
            "textfield_point": ["x": screen.midX, "y": top - screen.midY],
            "download_point": ["x": button.midX, "y": top - button.midY],
            "observed_at": Date().timeIntervalSince1970]
        if let data = try? JSONSerialization.data(withJSONObject: values, options: [.sortedKeys]) {
            try? data.write(to: stateDirectory.appendingPathComponent("readback.json"), options: .atomic)
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}

@main
struct TestWindow {
    static func main() {
        guard CommandLine.arguments.count == 3, CommandLine.arguments[1] == "--state-dir",
              CommandLine.arguments[2].hasPrefix("/") else {
            fputs("usage: TestWindow --state-dir /absolute/fixture-state\n", stderr)
            exit(2)
        }
        let directory = URL(fileURLWithPath: CommandLine.arguments[2], isDirectory: true)
        do {
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
                                                    attributes: [.posixPermissions: 0o700])
        } catch { fputs("could not create fixture state\n", stderr); exit(2) }
        let app = NSApplication.shared
        app.setActivationPolicy(.regular)
        let fixture = Fixture(stateDirectory: directory)
        app.delegate = fixture
        app.run()
        withExtendedLifetime(fixture) {}
    }
}
