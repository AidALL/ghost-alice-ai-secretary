import AppKit
import Foundation

#if SECRETARY_PEEKABOO
import PeekabooAutomationKit
import PeekabooFoundation
let nativeBackend = "peekaboo"
let nativeBackendVersion = "4.6.0"
let nativeCapabilities = ["apps", "windows", "observe", "screenshot", "focus", "click", "selector_click", "type", "hotkey", "open"]
#else
let nativeBackend = "builtin"
let nativeBackendVersion = "1"
let nativeCapabilities = ["apps", "windows", "observe", "screenshot", "focus", "click", "selector_click", "drag", "scroll", "type", "hotkey", "open"]
#endif

struct SelectedInspection {
    let ax: [String: Any]
    let selectors: [String: Any]
}

@MainActor
func inspectSelectedWindow(pid: pid_t, window: [String: Any]) async throws -> SelectedInspection {
    let bounds = windowRectangle(window)!
    #if SECRETARY_PEEKABOO
    let identity = try peekabooIdentity(pid: pid, window: window)
    let context = WindowContext(
        applicationProcessId: pid, applicationProcessStartIdentity: identity.ownerProcessStartIdentity,
        windowID: identity.windowID, windowBounds: bounds, windowMutationIdentity: identity,
        shouldFocusWebContent: false, includeMenuBarElements: false,
        traversalBudget: AXTraversalBudget(maxDepth: 6, maxElementCount: 250, maxChildrenPerNode: 250),
        requiresFreshAccessibilityTree: true, accessibilityTimeoutSeconds: 0.5,
        allowApplicationScopedAccessibilityFallback: false)
    let detection = try await ElementDetectionService().inspectElements(snapshotId: nil, windowContext: context)
    guard !detection.metadata.isApplicationScopedAccessibilityFallback,
          let receipt = detection.metadata.windowContext?.windowMutationIdentity,
          receipt.windowID == identity.windowID,
          receipt.ownerProcessIdentifier == pid,
          receipt.ownerProcessStartIdentity == identity.ownerProcessStartIdentity else {
        try fail("selector_unavailable", "AX inspection did not retain the exact window receipt")
    }
    let incomplete = detection.metadata.truncationInfo != nil
    let candidates = detection.elements.all.compactMap { element -> [String: Any]? in
        // Menu and OCR origin cannot acquire authority from a selector reference.
        guard element.attributes["source"] == nil, !element.isOCRSemanticEvidence,
              element.bounds.width > 0, element.bounds.height > 0,
              let role = element.attributes["role"], !role.hasPrefix("AXMenu") else { return nil }
        return ["role": role, "identifier": element.attributes["identifier"] ?? "",
                "label": element.label ?? "", "geometry": geometry(element.bounds),
                "enabled": element.knownIsEnabled == true, "actionable": element.isActionable,
                "visible": bounds.contains(element.bounds)]
    }
    let selectors = try selectorSidecar(candidates, truncated: incomplete)
    return SelectedInspection(ax: ["available": true, "nodes": selectors["refs"]!,
                                   "truncated": selectors["truncated"]!], selectors: selectors)
    #else
    let tree = axTree(try axWindow(pid, bounds, windowID: (window[kCGWindowNumber as String] as! NSNumber).intValue))
    var incomplete = tree["truncated"] as? Bool ?? true
    let candidates = (tree["nodes"] as? [[String: Any]] ?? []).compactMap { node -> [String: Any]? in
        guard let role = node["role"] as? String, !role.hasPrefix("AXMenu"),
              let shape = node["geometry"] as? [String: Any], let rect = try? rectangle(shape),
              rect.width > 0, rect.height > 0 else { return nil }
        if node["selector_incomplete"] as? Bool == true { incomplete = true; return nil }
        let actions = node["actions"] as? [String] ?? []
        let actionableRoles = ["AXButton", "AXTextField", "AXTextArea", "AXLink", "AXCheckBox", "AXRadioButton", "AXSlider"]
        return ["role": role, "identifier": node["identifier"] as? String ?? "",
                "label": node["title"] as? String ?? node["description"] as? String ?? "",
                "geometry": shape, "enabled": node["enabled"] as? Bool == true,
                "actionable": !actions.isEmpty || actionableRoles.contains(role), "visible": bounds.contains(rect)]
    }
    return SelectedInspection(ax: tree, selectors: try selectorSidecar(candidates, truncated: incomplete))
    #endif
}

@MainActor
func selectedClickPoint(_ request: [String: Any], pid: pid_t, window: [String: Any]) async throws -> CGPoint {
    if let name = request["element_ref"] as? String {
        guard request["x"] == nil, request["y"] == nil,
              let expected = request["expected"] as? [String: Any],
              let selectors = expected["selectors"] as? [String: Any] else {
            try fail("selector_invalid", "selector click requires signed evidence and excludes caller coordinates")
        }
        let fresh = try await inspectSelectedWindow(pid: pid, window: window)
        let point = try corroborateSelector(name, expected: selectors, fresh: fresh.selectors)
        _ = try guardObservation(request) // Recheck age/process/window after async AX traversal.
        return point
    }
    guard request["element_ref"] == nil else { try fail("selector_invalid", "element_ref must be an opaque string") }
    return CGPoint(x: try number(request, "x"), y: try number(request, "y"))
}

#if SECRETARY_PEEKABOO
func peekabooIdentity(pid: pid_t, window: [String: Any]) throws -> WindowMutationIdentity {
    let start = try processStart(pid).split(separator: ".")
    guard start.count == 2, let seconds = UInt64(start[0]), let micros = UInt64(start[1]),
          seconds <= (UInt64.max - micros) / 1_000_000 else {
        try fail("process_mismatch", "process start generation could not be translated exactly")
    }
    return WindowMutationIdentity(windowID: (window[kCGWindowNumber as String] as! NSNumber).intValue,
        ownerProcessIdentifier: pid, ownerProcessStartIdentity: seconds * 1_000_000 + micros,
        capturedBounds: windowRectangle(window), isMinimized: false)
}

@MainActor
final class SecretaryPeekabooBridge {
    let service: UIAutomationService
    init(ownedRoot: URL) throws {
        try FileManager.default.createDirectory(at: ownedRoot, withIntermediateDirectories: true,
                                               attributes: [.posixPermissions: 0o700])
        var metadata = stat()
        guard lstat(ownedRoot.path, &metadata) == 0, metadata.st_uid == getuid(),
              metadata.st_mode & S_IFMT == S_IFDIR, metadata.st_mode & 0o777 == 0o700 else {
            try fail("native_state_invalid", "native coordination root must be an owned private directory")
        }
        let lanes = DesktopOperationLaneCoordinator(coordinationRootURL: ownedRoot)
        service = UIAutomationService(snapshotManager: InMemorySnapshotManager(options: .init(
            snapshotValidityWindow: 10, maxSnapshots: 1, deleteArtifactsOnCleanup: false, copyArtifactsOnStore: false)),
            inputPolicy: UIInputPolicy(defaultStrategy: .synthOnly), feedbackClient: NoopAutomationFeedbackClient(),
            operationLaneCoordinator: lanes)
    }
}

func encodedObject<T: Encodable>(_ value: T) throws -> [String: Any] {
    try JSONSerialization.jsonObject(with: JSONEncoder().encode(value)) as! [String: Any]
}

func recordPeekabooOutcome<T>(_ result: UIAutomationActionResult<T>, expected: WindowMutationIdentity? = nil,
                             bounds: CGRect? = nil) throws {
    guard let outcome = result.outcome else { try fail("backend_unconfirmed", "selected backend returned no dispatch evidence") }
    backendOutcome = try encodedObject(outcome)
    effectStarted = outcome.dispatchState.mutationDispatched
    if effectStarted {
        guard let window = result.targetIdentity?.exactWindow,
              expected.map({ $0.windowID == window.identity.windowID &&
                  $0.ownerProcessIdentifier == window.identity.ownerProcessIdentifier &&
                  $0.ownerProcessStartIdentity == window.identity.ownerProcessStartIdentity }) ?? true,
              bounds.map({ $0 == window.bounds }) ?? true else {
            effectStatusOverride = "unknown"
            try fail("backend_target_unconfirmed", "dispatched input did not retain the exact target receipt")
        }
    }
    if let target = result.targetIdentity {
        var receipt: [String: Any] = ["pid": target.processIdentity.processIdentifier,
            "process_start_decimal": String(target.processIdentity.processStartIdentity)]
        if let window = target.exactWindow {
            receipt["window_identity"] = try encodedObject(window.identity)
            receipt["geometry"] = geometry(window.bounds)
        }
        backendOutcome!["target_identity"] = receipt
    }
    switch outcome.state {
    case .confirmedChange, .confirmedNoChange:
        effectStatusOverride = effectStarted ? "dispatched" : "not_started"
    case .refused:
        effectStatusOverride = "not_started"
        try fail("backend_refused", "selected backend refused the exact-window operation")
    default:
        effectStatusOverride = "unknown"
        try fail("backend_unconfirmed", "selected backend input result requires reconciliation")
    }
}

@MainActor
func executePeekaboo(_ request: [String: Any], pid: pid_t, window: [String: Any], bounds: CGRect) async throws -> [String: Any] {
    let authority = activeAuthorization!
    let root = URL(fileURLWithPath: authority.payload["db_path"] as! String).deletingLastPathComponent()
        .appendingPathComponent("native-coordination", isDirectory: true)
    let bridge = try SecretaryPeekabooBridge(ownedRoot: root)
    let identity = try peekabooIdentity(pid: pid, window: window)
    do {
        switch request["command"] as! String {
        case "click":
            let point = try await selectedClickPoint(request, pid: pid, window: window)
            try requirePoint(point, bounds, window)
            let button = request["button"] as? String ?? "left"
            let count = request["count"] == nil ? 1 : try integer(request,"count")
            guard ["left", "right"].contains(button), (1...2).contains(count), !(button == "right" && count == 2) else {
                try fail("invalid_request", "selected backend accepts single left/right or double left click")
            }
            try authority.validateCurrent()
            effectStarted = true // An untyped failure after crossing the service boundary is uncertain.
            let result = try await bridge.service.clickWithOutcome(target: .coordinates(point),
                clickType: button == "right" ? .right : (count == 2 ? .double : .single), snapshotId: nil,
                windowEvidence: ExactWindowClickEvidence(identity: identity, bounds: bounds),
                allowsAccessibilityValueDelivery: false)
            try recordPeekabooOutcome(result, expected: identity, bounds: bounds)
        case "type":
            guard let text = request["text"] as? String, text.utf16.count <= 10000 else {
                try fail("invalid_request", "text must contain at most 10000 UTF-16 units")
            }
            try requireKeyboardTarget(pid, bounds, windowID: identity.windowID)
            try authority.validateCurrent()
            effectStarted = true
            try recordPeekabooOutcome(try await bridge.service.typeActionsWithOutcome([.text(text)],
                cadence: .fixed(milliseconds: 0), snapshotId: nil,
                expectedWindowIdentity: identity, expectedWindowBounds: bounds), expected: identity, bounds: bounds)
        case "hotkey":
            let supported = Set("abcdefghijklmnopqrstuvwxyz0123456789".map(String.init) + ["return", "enter", "tab", "space", "delete", "escape", "left", "right", "down", "up", "home", "end", "pageup", "pagedown"])
            guard let key = request["key"] as? String, supported.contains(key.lowercased()) else {
                try fail("invalid_request", "unsupported hotkey key")
            }
            let modifiers = request["modifiers"] as? [String] ?? []
            guard modifiers.count <= 4, Set(modifiers).count == modifiers.count,
                  modifiers.allSatisfy({ ["command", "shift", "option", "control"].contains($0) }) else {
                try fail("invalid_request", "unsupported or duplicate hotkey modifiers")
            }
            try requireKeyboardTarget(pid, bounds, windowID: identity.windowID)
            try authority.validateCurrent()
            effectStarted = true
            try recordPeekabooOutcome(try await bridge.service.hotkeyWithOutcome(
                keys: (modifiers + [key.lowercased()]).joined(separator: "+"), holdDuration: 0,
                expectedWindowIdentity: identity, expectedWindowBounds: bounds), expected: identity, bounds: bounds)
        default: try fail("backend_unsupported", "unsupported selected backend effect")
        }
    } catch let failure as DesktopActionFailure {
        backendOutcome = try encodedObject(failure.outcome)
        effectStarted = failure.outcome.dispatchState.mutationDispatched
        effectStatusOverride = effectStarted ? "unknown" : "not_started"
        try fail("backend_unconfirmed", String(failure.message.prefix(512)))
    }
    return ["dispatched": effectStarted, "verified": false, "backend": nativeBackend,
            "pid": pid, "window_id": identity.windowID, "dispatched_at": Date().timeIntervalSince1970]
}
#endif
