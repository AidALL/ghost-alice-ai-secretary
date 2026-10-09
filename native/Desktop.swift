import AppKit
import ApplicationServices
import CryptoKit
import Darwin
import ScreenCaptureKit

var effectStarted = false
var activeAuthorization: DispatchAuthorization?
var backendOutcome: [String: Any]?
var effectStatusOverride: String?
let maximumObservationAge: Double = 10
let coordinateSpace = "quartz_global_top_left"

func number(_ object: [String: Any], _ name: String) throws -> Double {
    guard let value = object[name] as? NSNumber,
          CFGetTypeID(value) != CFBooleanGetTypeID(), value.doubleValue.isFinite else {
        try fail("invalid_request", "\(name) must be a finite number")
    }
    return value.doubleValue
}

func integer(_ object: [String: Any], _ name: String) throws -> Int {
    let value = try number(object, name)
    guard value.rounded() == value, value >= 0, value <= Double(Int32.max) else {
        try fail("invalid_request", "\(name) must be a nonnegative integer")
    }
    return Int(value)
}

func geometry(_ rectangle: CGRect) -> [String: Double] {
    ["x": rectangle.minX, "y": rectangle.minY, "width": rectangle.width, "height": rectangle.height]
}

func rectangle(_ values: [String: Any]) throws -> CGRect {
    CGRect(x: try number(values, "x"), y: try number(values, "y"),
           width: try number(values, "width"), height: try number(values, "height"))
}

func windows() -> [[String: Any]] {
    (CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID)
        as? [[String: Any]]) ?? []
}

func windowRectangle(_ window: [String: Any]) -> CGRect? {
    guard let bounds = window[kCGWindowBounds as String] as? NSDictionary else { return nil }
    return CGRect(dictionaryRepresentation: bounds)
}

func windowJSON(_ window: [String: Any]) -> [String: Any] {
    var value: [String: Any] = [
        "window_id": window[kCGWindowNumber as String] ?? 0,
        "pid": window[kCGWindowOwnerPID as String] ?? 0,
        "process_name": window[kCGWindowOwnerName as String] ?? "",
        "title": window[kCGWindowName as String] ?? "",
        "layer": window[kCGWindowLayer as String] ?? 0,
        "coordinate_space": coordinateSpace]
    if let rect = windowRectangle(window) { value["geometry"] = geometry(rect) }
    return value
}

func processStart(_ pid: pid_t) throws -> String {
    var info = proc_bsdinfo()
    let received = proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &info, Int32(MemoryLayout<proc_bsdinfo>.size))
    guard received == MemoryLayout<proc_bsdinfo>.size else {
        try fail("process_unavailable", "cannot establish process start identity")
    }
    return "\(info.pbi_start_tvsec).\(String(format: "%06llu", info.pbi_start_tvusec))"
}

func selectedWindow(_ pid: pid_t, _ windowID: Int?) throws -> [String: Any] {
    guard let found = windows().first(where: { entry in
        (entry[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value == pid
        && (entry[kCGWindowLayer as String] as? NSNumber)?.intValue == 0
        && (windowID == nil || (entry[kCGWindowNumber as String] as? NSNumber)?.intValue == windowID)
    }), let rect = windowRectangle(found), rect.width > 0, rect.height > 0 else {
        try fail("window_unavailable", "selected process/window has no visible normal window")
    }
    return found
}

func key(_ request: [String: Any]) throws -> SymmetricKey {
    let defaultPath = NSHomeDirectory() + "/Library/Application Support/AI_Secretary/native-observation.key"
    let path = request["key_path"] as? String ?? defaultPath
    guard path.hasPrefix("/") else { try fail("invalid_request", "key_path must be absolute") }
    let parent = URL(fileURLWithPath: path).deletingLastPathComponent()
    try FileManager.default.createDirectory(at: parent, withIntermediateDirectories: true,
                                            attributes: [.posixPermissions: 0o700])
    let created = Darwin.open(path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0o600)
    if created >= 0 {
        var generator = SystemRandomNumberGenerator()
        let bytes = (0..<32).map { _ in UInt8.random(in: .min ... .max, using: &generator) }
        let written = bytes.withUnsafeBytes { Darwin.write(created, $0.baseAddress, $0.count) }
        Darwin.close(created)
        guard written == 32 else { try fail("key_unavailable", "could not initialize observation key") }
    } else if errno != EEXIST {
        try fail("key_unavailable", "could not open private observation key")
    }
    let descriptor = Darwin.open(path, O_RDONLY | O_NOFOLLOW)
    guard descriptor >= 0 else { try fail("key_unavailable", "could not read observation key") }
    defer { Darwin.close(descriptor) }
    var metadata = stat()
    guard fstat(descriptor, &metadata) == 0,
          metadata.st_uid == getuid(), metadata.st_mode & 0o077 == 0,
          metadata.st_mode & S_IFMT == S_IFREG, metadata.st_size == 32 else {
        try fail("key_unavailable", "observation key must be a private regular 32-byte file")
    }
    var bytes = [UInt8](repeating: 0, count: 32)
    let received = bytes.withUnsafeMutableBytes { Darwin.read(descriptor, $0.baseAddress, $0.count) }
    guard received == 32 else { try fail("key_unavailable", "could not read complete observation key") }
    return SymmetricKey(data: bytes)
}

func signingData(_ observation: [String: Any]) throws -> Data {
    var values = observation
    values.removeValue(forKey: "token")
    return try JSONSerialization.data(withJSONObject: values, options: [.sortedKeys])
}

func observation(_ request: [String: Any], pid: pid_t, processGeneration: String,
                 window: [String: Any], selectors: [String: Any]?) throws -> [String: Any] {
    var values: [String: Any] = ["pid": pid, "process_start": processGeneration,
        "window_id": window[kCGWindowNumber as String] ?? 0,
        "geometry": geometry(windowRectangle(window)!), "observed_at": Date().timeIntervalSince1970,
        "coordinate_space": coordinateSpace]
    values["backend"] = nativeBackend
    if let selectors { values["selectors"] = selectors }
    if let activeAuthorization { values["ownership"] = activeAuthorization.observationBinding }
    let signature = HMAC<SHA256>.authenticationCode(for: try signingData(values), using: try key(request))
    values["token"] = signature.map { String(format: "%02x", $0) }.joined()
    return values
}

@MainActor
func observeWindowTransaction(
    windowID: Int?, select: (Int?) throws -> [String: Any], processGeneration: () throws -> String,
    collect: ([String: Any], String) async throws -> (result: [String: Any], selectors: [String: Any]?),
    publish: ([String: Any], String, [String: Any], [String: Any]?) throws -> [String: Any]) async throws -> [String: Any] {
    let initialGeneration = try processGeneration()
    let window = try select(windowID)
    guard let initialID = (window[kCGWindowNumber as String] as? NSNumber)?.intValue, initialID > 0 else {
        try fail("snapshot_inconsistent", "initial window identity is absent")
    }
    let collected = try await collect(window, initialGeneration)
    let current = try select(initialID)
    guard (current[kCGWindowNumber as String] as? NSNumber)?.intValue == initialID,
          (current[kCGWindowOwnerPID as String] as? NSNumber)?.intValue == (window[kCGWindowOwnerPID as String] as? NSNumber)?.intValue,
          let before = windowRectangle(window), let after = windowRectangle(current), sameRectangle(before, after),
          let described = collected.result["window"] as? [String: Any],
          (described["window_id"] as? NSNumber)?.intValue == initialID else {
        try fail("snapshot_inconsistent", "window identity or geometry changed during observation; observe again")
    }
    if let captured = collected.result["screenshot"] as? [String: Any],
       (captured["window_id"] as? NSNumber)?.intValue != initialID {
        try fail("snapshot_inconsistent", "capture does not belong to the initial window")
    }
    guard try processGeneration() == initialGeneration else {
        try fail("process_mismatch", "process generation changed during observation; observe again")
    }
    return try publish(current, initialGeneration, collected.result, collected.selectors)
}

func sameRectangle(_ lhs: CGRect, _ rhs: CGRect) -> Bool {
    abs(lhs.minX - rhs.minX) < 0.5 && abs(lhs.minY - rhs.minY) < 0.5
        && abs(lhs.width - rhs.width) < 0.5 && abs(lhs.height - rhs.height) < 0.5
}

func guardObservation(_ request: [String: Any]) throws -> (pid_t, [String: Any], CGRect) {
    guard let expected = request["expected"] as? [String: Any],
          let token = expected["token"] as? String, token.count == 64,
          expected["coordinate_space"] as? String == coordinateSpace,
          let expectedGeometry = expected["geometry"] as? [String: Any] else {
        try fail("observation_required", "effect requires a complete signed expected observation")
    }
    let signature = stride(from: 0, to: token.count, by: 2).compactMap { offset -> UInt8? in
        let start = token.index(token.startIndex, offsetBy: offset)
        let end = token.index(start, offsetBy: 2)
        return UInt8(token[start..<end], radix: 16)
    }
    guard signature.count == 32,
          HMAC<SHA256>.isValidAuthenticationCode(signature, authenticating: try signingData(expected),
                                                using: try key(request)) else {
        try fail("observation_invalid", "observation signature did not verify")
    }
    guard expected["backend"] as? String == nativeBackend,
          let binding = expected["ownership"] as? [String: Any], let activeAuthorization,
          NSDictionary(dictionary: binding).isEqual(to: activeAuthorization.observationBinding) else {
        try fail("observation_ownership_mismatch", "dispatch observation belongs to another backend, run, attempt or desktop fence")
    }
    if let selectors = expected["selectors"] as? [String: Any] { _ = try validateSelectors(selectors) }
    let age = Date().timeIntervalSince1970 - (try number(expected, "observed_at"))
    guard age >= 0, age <= maximumObservationAge else {
        try fail("stale_observation", "observation is older than 10 seconds or from the future")
    }
    let pid = pid_t(try integer(expected, "pid"))
    guard pid > 0, expected["process_start"] as? String == (try processStart(pid)) else {
        try fail("process_mismatch", "process start identity changed")
    }
    let window = try selectedWindow(pid, integer(expected, "window_id"))
    let rect = windowRectangle(window)!
    guard sameRectangle(rect, try rectangle(expectedGeometry)) else {
        try fail("window_mismatch", "window moved or resized since observation")
    }
    return (pid, window, rect)
}

func attribute(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
    var value: CFTypeRef?
    return AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success ? value : nil
}

func axRectangle(_ element: AXUIElement) -> CGRect? {
    guard let position = attribute(element, kAXPositionAttribute),
          let size = attribute(element, kAXSizeAttribute),
          CFGetTypeID(position) == AXValueGetTypeID(), CFGetTypeID(size) == AXValueGetTypeID() else { return nil }
    var point = CGPoint.zero
    var dimensions = CGSize.zero
    guard AXValueGetValue(position as! AXValue, .cgPoint, &point),
          AXValueGetValue(size as! AXValue, .cgSize, &dimensions) else { return nil }
    return CGRect(origin: point, size: dimensions)
}

struct AXWindowCandidate {
    let windowID: Int?
    let bounds: CGRect?
}

func chooseAXWindowIndex(_ candidates: [AXWindowCandidate], windowID: Int, bounds: CGRect) throws -> Int {
    let matches = candidates.indices.filter { candidates[$0].windowID == windowID }
    guard !matches.isEmpty else { try fail("ax_window_unavailable", "no AX window maps to the observed window ID") }
    guard matches.count == 1 else { try fail("ax_window_ambiguous", "multiple AX windows map to the observed window ID") }
    let index = matches[0]
    guard candidates[index].bounds.map({ sameRectangle($0, bounds) }) == true else {
        try fail("ax_window_unavailable", "exact AX window bounds differ from the observed window")
    }
    return index
}

func boundedAXString(_ value: String) -> (value: String, incomplete: Bool) {
    value.utf8.count <= 4096 ? (value, false) : ("", true)
}

func extractAXTree<Node>(_ root: Node, readChildren: (Node) -> [Node]?, readNode: (Node) -> [String: Any]) -> [String: Any] {
    var queue: [(Node, Int, Int?)] = [(root, 0, nil)]
    var nodes = [[String: Any]]()
    var incomplete = false
    while !queue.isEmpty && nodes.count < 250 {
        let (element, depth, parent) = queue.removeFirst()
        var node = readNode(element)
        node["id"] = nodes.count
        node["depth"] = depth
        if let parent { node["parent"] = parent }
        let parentID = nodes.count
        nodes.append(node)
        guard let children = readChildren(element) else { incomplete = true; continue }
        if depth >= 6 {
            if !children.isEmpty { incomplete = true }
            continue
        }
        let capacity = max(0, 250 - nodes.count - queue.count)
        let admitted = min(children.count, 250, capacity)
        if admitted < children.count { incomplete = true }
        queue.append(contentsOf: children.prefix(admitted).map { ($0, depth + 1, parentID) })
    }
    return ["available": true, "nodes": nodes, "truncated": incomplete || !queue.isEmpty]
}

typealias AXWindowIDFunction = @convention(c) (AXUIElement, UnsafeMutablePointer<CGWindowID>) -> Int32
let axWindowIDSymbol = dlsym(UnsafeMutableRawPointer(bitPattern: -2), "_AXUIElementGetWindow")
var axWindowIDBindingAvailable: Bool { axWindowIDSymbol != nil }

func axWindowID(_ element: AXUIElement) -> Int? {
    guard let symbol = axWindowIDSymbol else { return nil }
    let resolve = unsafeBitCast(symbol, to: AXWindowIDFunction.self)
    var windowID: CGWindowID = 0
    return resolve(element, &windowID) == AXError.success.rawValue && windowID > 0 ? Int(windowID) : nil
}

func axChildren(_ element: AXUIElement) -> [AXUIElement]? {
    var value: CFTypeRef?
    let result = AXUIElementCopyAttributeValue(element, kAXChildrenAttribute as CFString, &value)
    switch result {
    case .success: return value as? [AXUIElement]
    case .attributeUnsupported, .noValue: return []
    default: return nil // A failed/timed-out branch is incomplete, not an empty subtree.
    }
}

func axWindow(_ pid: pid_t, _ rect: CGRect, windowID: Int) throws -> AXUIElement {
    guard AXIsProcessTrusted() else { try fail("accessibility_denied", "Accessibility permission is not granted") }
    guard axWindowIDBindingAvailable else { try fail("ax_window_binding_unavailable", "exact AX window-ID binding is unavailable") }
    let app = AXUIElementCreateApplication(pid)
    AXUIElementSetMessagingTimeout(app, 0.5)
    guard let entries = attribute(app, kAXWindowsAttribute) as? [AXUIElement] else {
        try fail("ax_window_unavailable", "could not enumerate Accessibility windows")
    }
    let candidates = entries.map { AXWindowCandidate(windowID: axWindowID($0), bounds: axRectangle($0)) }
    return entries[try chooseAXWindowIndex(candidates, windowID: windowID, bounds: rect)]
}

func axTree(_ root: AXUIElement) -> [String: Any] {
    extractAXTree(root, readChildren: axChildren, readNode: { element in
        var node: [String: Any] = [:]
        for (name, output) in [(kAXRoleAttribute, "role"), (kAXIdentifierAttribute, "identifier"), (kAXTitleAttribute, "title"),
                               (kAXDescriptionAttribute, "description"), (kAXValueAttribute, "value"),
                               (kAXEnabledAttribute, "enabled")] {
            if let value = attribute(element, name) as? String {
                let bounded = boundedAXString(value)
                node[output] = bounded.value
                if bounded.incomplete { node["selector_incomplete"] = true }
            }
            else if let value = attribute(element, name) as? NSNumber { node[output] = value }
        }
        if let rect = axRectangle(element) { node["geometry"] = geometry(rect) }
        var actions: CFArray?
        if AXUIElementCopyActionNames(element, &actions) == .success { node["actions"] = actions as? [String] ?? [] }
        return node
    })
}

func requireKeyboardTarget(_ pid: pid_t, _ rect: CGRect, windowID: Int) throws {
    guard AXIsProcessTrusted() else { try fail("accessibility_denied", "Accessibility permission is not granted") }
    guard NSWorkspace.shared.frontmostApplication?.processIdentifier == pid else {
        try fail("focus_mismatch", "observed application is not frontmost")
    }
    let app = AXUIElementCreateApplication(pid)
    AXUIElementSetMessagingTimeout(app, 0.5)
    guard let focused = attribute(app, kAXFocusedWindowAttribute),
          CFGetTypeID(focused) == AXUIElementGetTypeID(),
          axWindowID(focused as! AXUIElement) == windowID,
          let current = axRectangle(focused as! AXUIElement), sameRectangle(current, rect) else {
        try fail("focus_mismatch", "focused window differs from observed window")
    }
}

func requirePoint(_ point: CGPoint, _ rect: CGRect, _ window: [String: Any]) throws {
    guard AXIsProcessTrusted() else { try fail("accessibility_denied", "Accessibility permission is not granted") }
    guard rect.contains(point) else { try fail("point_outside_window", "input point is outside observed window") }
    let covering = windows().first { entry in
        (entry[kCGWindowAlpha as String] as? NSNumber)?.doubleValue ?? 1 > 0
            && (windowRectangle(entry)?.contains(point) ?? false)
    }
    guard (covering?[kCGWindowNumber as String] as? NSNumber)?.uint32Value
            == (window[kCGWindowNumber as String] as? NSNumber)?.uint32Value else {
        try fail("window_occluded", "another window covers the input point")
    }
}

func post(_ event: CGEvent?, pid: pid_t? = nil) throws {
    guard let event else { try fail("event_unavailable", "could not create input event") }
    if !effectStarted { try activeAuthorization?.validateCurrent() }
    effectStarted = true
    if let pid { event.postToPid(pid) }
    else { event.post(tap: .cghidEventTap) }
    usleep(10000)
}

func saveScreenshot(_ data: Data, path: String) throws {
    guard path.hasPrefix("/") else { try fail("invalid_request", "screenshot_path must be absolute") }
    let descriptor = Darwin.open(path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0o600)
    guard descriptor >= 0 else { try fail("screenshot_write_failed", "screenshot path must be a writable new file") }
    defer { Darwin.close(descriptor) }
    try data.withUnsafeBytes { bytes in
        var offset = 0
        while offset < bytes.count {
            let count = Darwin.write(descriptor, bytes.baseAddress!.advanced(by: offset), bytes.count - offset)
            if count < 0 && errno == EINTR { continue }
            guard count > 0 else { try fail("screenshot_write_failed", "could not write complete screenshot") }
            offset += count
        }
    }
}

final class CaptureResult {
    private let lock = NSLock()
    private var result: Result<Data, Error>?
    func complete(_ value: Result<Data, Error>) { lock.lock(); result = value; lock.unlock() }
    func current() -> Result<Data, Error>? { lock.lock(); defer { lock.unlock() }; return result }
}

func screenshot(_ windowID: Int, pid: pid_t, processGeneration: String) throws -> Data {
    guard CGPreflightScreenCaptureAccess() else { try fail("screen_recording_denied", "Screen Recording permission is not granted") }
    let receiver = CaptureResult()
    SCShareableContent.getExcludingDesktopWindows(false, onScreenWindowsOnly: true) { content, error in
        guard let selected = content?.windows.first(where: { $0.windowID == windowID }),
              selected.owningApplication?.processID == pid else {
            receiver.complete(.failure(error ?? DesktopError(code: "screenshot_unavailable", message: "window is not capturable")))
            return
        }
        do {
            guard try processStart(pid) == processGeneration else {
                try fail("process_mismatch", "capture target process generation changed")
            }
        } catch { receiver.complete(.failure(error)); return }
        let configuration = SCStreamConfiguration()
        configuration.width = max(1, Int(selected.frame.width * 2))
        configuration.height = max(1, Int(selected.frame.height * 2))
        configuration.showsCursor = false
        SCScreenshotManager.captureImage(contentFilter: SCContentFilter(desktopIndependentWindow: selected),
                                        configuration: configuration) { image, error in
            if let image, let data = NSBitmapImageRep(cgImage: image).representation(using: .png, properties: [:]) {
                receiver.complete(.success(data))
            } else {
                receiver.complete(.failure(error ?? DesktopError(code: "screenshot_unavailable", message: "could not encode screenshot")))
            }
        }
    }
    let deadline = Date().addingTimeInterval(8)
    while receiver.current() == nil && Date() < deadline {
        RunLoop.current.run(until: Date().addingTimeInterval(0.01))
    }
    guard let outcome = receiver.current() else { try fail("screenshot_timeout", "screenshot timed out") }
    return try outcome.get()
}

@MainActor
func execute(_ request: [String: Any]) async throws -> [String: Any] {
    guard let command = request["command"] as? String else { try fail("invalid_request", "command is required") }
    if let selected = request["backend"] as? String, selected != nativeBackend {
        try fail("backend_mismatch", "selected backend is not the running helper; no fallback was attempted")
    }
    let isEffect = ["focus", "click", "drag", "scroll", "type", "hotkey", "open"].contains(command)
    if let envelope = request["_authorization"] {
        activeAuthorization = try DispatchAuthorization(envelope, key: key(request))
        try activeAuthorization!.validateCurrent()
    } else if isEffect {
        try fail("authorization_required", "effects require server-signed local authority and inherited dispatch ownership")
    }
    switch command {
    case "status":
        return ["accessibility": AXIsProcessTrusted(), "screen_recording": CGPreflightScreenCaptureAccess(),
                "permissions_requested": false, "observation_max_age_seconds": maximumObservationAge,
                "coordinate_space": coordinateSpace, "native_pid": getpid(), "observed_at": Date().timeIntervalSince1970,
                "backend": nativeBackend, "backend_version": nativeBackendVersion,
                "capabilities": axWindowIDBindingAvailable ? nativeCapabilities : nativeCapabilities.filter { !["selector_click", "focus", "type", "hotkey"].contains($0) },
                "ax_window_id_binding_available": axWindowIDBindingAvailable, "authorization_contract": "local-sqlite-v1",
                "selector_schema": selectorSchema, "actual_input_qualified": false]
    case "apps":
        return ["apps": NSWorkspace.shared.runningApplications.map { app -> [String: Any] in
            var value: [String: Any] = ["pid": app.processIdentifier, "name": app.localizedName ?? "",
                "bundle_id": app.bundleIdentifier ?? "", "active": app.isActive]
            value["process_start"] = try? processStart(app.processIdentifier)
            return value
        }]
    case "windows":
        let pid = request["pid"] == nil ? nil : try integer(request, "pid")
        return ["windows": windows().filter {
            pid == nil || ($0[kCGWindowOwnerPID as String] as? NSNumber)?.intValue == pid
        }.map(windowJSON)]
    case "observe":
        let pid = pid_t(try integer(request, "pid"))
        guard pid > 0 else { try fail("invalid_request", "pid must be positive") }
        let windowID = request["window_id"] == nil ? nil : try integer(request, "window_id")
        return try await observeWindowTransaction(windowID: windowID,
          select: { try selectedWindow(pid, $0) }, processGeneration: { try processStart(pid) }, collect: { window, generation in
        var result: [String: Any] = ["window": windowJSON(window)]
        var selectors: [String: Any]?
        if request["include_ax"] as? Bool ?? true {
            do {
                let inspection = try await inspectSelectedWindow(pid: pid, window: window)
                result["ax"] = inspection.ax
                selectors = inspection.selectors
            }
            catch let error as DesktopError { result["ax"] = ["available": false, "reason": error.code] }
        }
        if request["screenshot_path"] != nil || request["screenshot_base64"] as? Bool == true {
            let data = try screenshot((window[kCGWindowNumber as String] as! NSNumber).intValue,
                                      pid: pid, processGeneration: generation)
            var image: [String: Any] = ["format": "png", "bytes": data.count,
                                      "window_id": window[kCGWindowNumber as String]!, "process_start": generation]
            if let path = request["screenshot_path"] as? String {
                try saveScreenshot(data, path: path)
                image["path"] = path
            }
            if request["screenshot_base64"] as? Bool == true {
                guard data.count <= 4 * 1024 * 1024 else { try fail("output_limit", "base64 screenshot exceeds 4 MiB") }
                image["base64"] = data.base64EncodedString()
            }
            result["screenshot"] = image
        }
        return (result, selectors)
        }, publish: { current, generation, collected, selectors in
        var result = collected
        try activeAuthorization?.validateCurrent()
        result["observation"] = try observation(request, pid: pid, processGeneration: generation,
                                                window: current, selectors: selectors)
        return result
        })
    case "focus", "click", "drag", "scroll", "type", "hotkey", "open":
        let (pid, window, rect) = try guardObservation(request)
        #if SECRETARY_PEEKABOO
        if ["click", "type", "hotkey"].contains(command) {
            return try await executePeekaboo(request, pid: pid, window: window, bounds: rect)
        }
        if !["focus", "open"].contains(command) { try fail("backend_unsupported", "selected backend does not implement this effect") }
        #endif
        switch command {
        case "focus":
            let ax = try axWindow(pid, rect, windowID: (window[kCGWindowNumber as String] as! NSNumber).intValue)
            guard let app = NSRunningApplication(processIdentifier: pid) else { try fail("process_unavailable", "application exited") }
            try activeAuthorization!.validateCurrent()
            effectStarted = true
            guard app.activate(options: []), AXUIElementPerformAction(ax, kAXRaiseAction as CFString) == .success else {
                try fail("focus_unconfirmed", "application focus could not be confirmed")
            }
            try await Task.sleep(nanoseconds: 150_000_000)
            try requireKeyboardTarget(pid, rect, windowID: (window[kCGWindowNumber as String] as! NSNumber).intValue)
        case "click":
            let point = try await selectedClickPoint(request, pid: pid, window: window)
            try requirePoint(point, rect, window)
            let buttonName = request["button"] as? String ?? "left"
            guard ["left", "right"].contains(buttonName) else { try fail("invalid_request", "button must be left or right") }
            let button: CGMouseButton = buttonName == "right" ? .right : .left
            let count = request["count"] == nil ? 1 : try integer(request, "count")
            guard (1...2).contains(count) else { try fail("invalid_request", "count must be one or two") }
            for index in 1...count {
                let down = CGEvent(mouseEventSource: nil, mouseType: button == .left ? .leftMouseDown : .rightMouseDown, mouseCursorPosition: point, mouseButton: button)
                let up = CGEvent(mouseEventSource: nil, mouseType: button == .left ? .leftMouseUp : .rightMouseUp, mouseCursorPosition: point, mouseButton: button)
                down?.setIntegerValueField(.mouseEventClickState, value: Int64(index))
                up?.setIntegerValueField(.mouseEventClickState, value: Int64(index))
                try post(down)
                try post(up)
                usleep(50000)
            }
        case "drag":
            let start = CGPoint(x: try number(request, "from_x"), y: try number(request, "from_y"))
            let end = CGPoint(x: try number(request, "to_x"), y: try number(request, "to_y"))
            let duration = request["duration"] == nil ? 0.3 : try number(request, "duration")
            guard (0.05...2).contains(duration) else { try fail("invalid_request", "drag duration must be 0.05 to 2 seconds") }
            try requirePoint(start, rect, window)
            try requirePoint(end, rect, window)
            try post(CGEvent(mouseEventSource: nil, mouseType: .leftMouseDown, mouseCursorPosition: start, mouseButton: .left))
            for index in 1...20 {
                let fraction = Double(index) / 20
                let point = CGPoint(x: start.x + (end.x - start.x) * fraction, y: start.y + (end.y - start.y) * fraction)
                try post(CGEvent(mouseEventSource: nil, mouseType: .leftMouseDragged, mouseCursorPosition: point, mouseButton: .left))
                usleep(useconds_t(duration / 20 * 1_000_000))
            }
            try post(CGEvent(mouseEventSource: nil, mouseType: .leftMouseUp, mouseCursorPosition: end, mouseButton: .left))
        case "scroll":
            let point = CGPoint(x: try number(request, "x"), y: try number(request, "y"))
            try requirePoint(point, rect, window)
            let dy = try number(request, "delta_y")
            let dx = request["delta_x"] == nil ? 0 : try number(request, "delta_x")
            guard abs(dy) <= 10000, abs(dx) <= 10000 else { try fail("invalid_request", "scroll deltas exceed bounds") }
            let event = CGEvent(scrollWheelEvent2Source: nil, units: .pixel, wheelCount: 2, wheel1: Int32(dy), wheel2: Int32(dx), wheel3: 0)
            event?.location = point
            try post(event)
        case "type":
            guard let text = request["text"] as? String, text.utf16.count <= 10000 else { try fail("invalid_request", "text must be a string of at most 10000 UTF-16 units") }
            try requireKeyboardTarget(pid, rect, windowID: (window[kCGWindowNumber as String] as! NSNumber).intValue)
            let units = Array(text.utf16)
            let source = CGEventSource(stateID: .privateState)
            let down = CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: true)
            units.withUnsafeBufferPointer { down?.keyboardSetUnicodeString(stringLength: units.count, unicodeString: $0.baseAddress) }
            try post(down, pid: pid)
            try post(CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: false), pid: pid)
        case "hotkey":
            let keys: [String: CGKeyCode] = ["a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "9": 25, "7": 26, "8": 28, "0": 29, "o": 31, "u": 32, "i": 34, "p": 35, "return": 36, "enter": 36, "l": 37, "j": 38, "k": 40, "n": 45, "m": 46, "tab": 48, "space": 49, "delete": 51, "escape": 53, "left": 123, "right": 124, "down": 125, "up": 126, "home": 115, "end": 119, "pageup": 116, "pagedown": 121]
            guard let name = request["key"] as? String, let code = keys[name.lowercased()] else { try fail("invalid_request", "unsupported hotkey key") }
            let modifiers = request["modifiers"] as? [String] ?? []
            let flagMap: [String: CGEventFlags] = ["command": .maskCommand, "shift": .maskShift, "option": .maskAlternate, "control": .maskControl]
            var flags = CGEventFlags()
            for modifier in modifiers {
                guard let flag = flagMap[modifier] else { try fail("invalid_request", "unsupported modifier") }
                flags.insert(flag)
            }
            try requireKeyboardTarget(pid, rect, windowID: (window[kCGWindowNumber as String] as! NSNumber).intValue)
            for down in [true, false] {
                let event = CGEvent(keyboardEventSource: CGEventSource(stateID: .hidSystemState), virtualKey: code, keyDown: down)
                event?.flags = flags
                try post(event, pid: pid)
            }
        case "open":
            let url: URL
            if let path = request["path"] as? String {
                guard path.hasPrefix("/"), FileManager.default.fileExists(atPath: path), request["url"] == nil else { try fail("invalid_request", "path must be one existing absolute path") }
                url = URL(fileURLWithPath: path)
            } else if let string = request["url"] as? String, let parsed = URL(string: string),
                      ["https", "http"].contains(parsed.scheme?.lowercased() ?? ""), parsed.host != nil {
                url = parsed
            } else { try fail("invalid_request", "open requires path or http(s) URL") }
            try activeAuthorization!.validateCurrent()
            effectStarted = true
            guard NSWorkspace.shared.open(url) else { try fail("open_unconfirmed", "workspace did not confirm open request") }
        default: break
        }
        return ["dispatched": true, "verified": false, "pid": pid, "window_id": window[kCGWindowNumber as String]!, "dispatched_at": Date().timeIntervalSince1970]
    default:
        try fail("invalid_request", "unknown command")
    }
}

@main
struct Desktop {
    @MainActor
    static func main() async {
        var response: [String: Any]
        do {
            var input = Data()
            while true {
                let chunk = FileHandle.standardInput.readData(ofLength: 16384)
                if chunk.isEmpty { break }
                input.append(chunk)
                guard input.count <= 1024 * 1024 else { try fail("invalid_request", "request exceeds 1 MiB") }
            }
            guard let request = try JSONSerialization.jsonObject(with: input) as? [String: Any] else {
                try fail("invalid_request", "request must be a JSON object")
            }
            let result = try await execute(request)
            response = ["ok": true, "result": result, "effect_status": effectStatusOverride ?? (effectStarted ? "dispatched" : "not_started")]
        } catch let error as DesktopError {
            response = ["ok": false, "error": ["code": error.code, "message": error.message], "effect_status": effectStatusOverride ?? (effectStarted ? "unknown" : "not_started")]
        } catch {
            response = ["ok": false, "error": ["code": "native_error", "message": String(error.localizedDescription.prefix(512))], "effect_status": effectStarted ? "unknown" : "not_started"]
        }
        if let backendOutcome { response["backend_outcome"] = backendOutcome }
        // Keep inherited ownership and its duplicate alive until async work is quiescent.
        withExtendedLifetime(activeAuthorization) {}
        if let data = try? JSONSerialization.data(withJSONObject: response, options: [.sortedKeys]) {
            FileHandle.standardOutput.write(data + Data([10]))
        }
    }
}
