import CoreGraphics
import Foundation
import os.log
import PeekabooFoundation

public enum WindowMovementAdjustment: Sendable {
    case unchanged(CGPoint)
    case adjusted(CGPoint, delta: CGPoint)
    case stale(String)
}

public protocol WindowTrackingProviding: AnyObject, Sendable {
    @MainActor func windowBounds(for windowID: CGWindowID) -> CGRect?
    @MainActor func windowOwnerProcessIdentifier(for windowID: CGWindowID) -> pid_t?
    @MainActor func refreshWindow(for windowID: CGWindowID)
}

extension WindowTrackingProviding {
    @MainActor
    public func windowOwnerProcessIdentifier(for _: CGWindowID) -> pid_t? {
        nil
    }

    @MainActor
    public func refreshWindow(for _: CGWindowID) {}
}

@MainActor
public enum WindowMovementTracking {
    private struct CurrentWindow {
        let bounds: CGRect
        let ownerProcessIdentifier: pid_t?
    }

    private static let logger = Logger(subsystem: "boo.peekaboo.core", category: "WindowMovementTracking")
    private static let identityService = WindowIdentityService()
    private static let toleratedSizeJitter: CGFloat = 4

    public weak static var provider: (any WindowTrackingProviding)?

    public static func adjustPoint(
        _ point: CGPoint,
        snapshot: UIAutomationSnapshot) -> WindowMovementAdjustment
    {
        guard let windowID = snapshot.windowID else {
            return .unchanged(point)
        }

        guard let currentWindow = self.currentWindow(for: windowID) else {
            let identity = self.windowIdentityDescription(snapshot: snapshot, windowID: windowID)
            return .stale(
                """
                Snapshot window is no longer available (\(identity)). \
                Run 'peekaboo see' again before targeting elements from this snapshot.
                """)
        }

        if let expectedProcessIdentifier = snapshot.applicationProcessId,
           let actualProcessIdentifier = currentWindow.ownerProcessIdentifier,
           expectedProcessIdentifier != actualProcessIdentifier
        {
            let identity = self.windowIdentityDescription(snapshot: snapshot, windowID: windowID)
            return .stale(
                """
                Snapshot window now belongs to PID \(actualProcessIdentifier), not PID \(expectedProcessIdentifier) \
                (\(identity)). Run 'peekaboo see' again before targeting elements from this snapshot.
                """)
        }

        guard let snapshotBounds = snapshot.windowBounds else {
            return .unchanged(point)
        }

        let currentBounds = currentWindow.bounds

        if self.sizeChangedMeaningfully(from: snapshotBounds.size, to: currentBounds.size) {
            let identity = self.windowIdentityDescription(snapshot: snapshot, windowID: windowID)
            let message = """
            Snapshot window changed size (\(identity)). \
            Previous bounds: \(snapshotBounds); current bounds: \(currentBounds). \
            Run 'peekaboo see' again before targeting elements from this snapshot.
            """
            return .stale(message)
        }

        let delta = CGPoint(
            x: currentBounds.origin.x - snapshotBounds.origin.x,
            y: currentBounds.origin.y - snapshotBounds.origin.y)

        guard delta != .zero else {
            return .unchanged(point)
        }

        let adjusted = CGPoint(x: point.x + delta.x, y: point.y + delta.y)
        self.logger.debug("Adjusted point for moved window dx=\(delta.x) dy=\(delta.y)")
        return .adjusted(adjusted, delta: delta)
    }

    public static func adjustFrame(
        _ frame: CGRect,
        snapshot: UIAutomationSnapshot) -> WindowMovementAdjustment
    {
        let point = CGPoint(x: frame.midX, y: frame.midY)
        return self.adjustPoint(point, snapshot: snapshot)
    }

    public static func adjustPoint(
        _ point: CGPoint,
        snapshotId: String?,
        snapshots: any SnapshotManagerProtocol) async throws -> CGPoint
    {
        guard let snapshotId,
              let snapshot = try? await snapshots.getUIAutomationSnapshot(snapshotId: snapshotId)
        else {
            return point
        }

        switch self.adjustPoint(point, snapshot: snapshot) {
        case let .unchanged(original):
            return original
        case let .adjusted(adjusted, _):
            return adjusted
        case let .stale(message):
            throw PeekabooError.snapshotStale(message)
        }
    }

    private static func currentWindow(for windowID: CGWindowID) -> CurrentWindow? {
        if let provider = self.provider {
            if let currentWindow = self.currentWindow(for: windowID, from: provider) {
                return currentWindow
            }

            // The installed tracker polls visible windows, so off-Space windows may be absent from its cache.
            provider.refreshWindow(for: windowID)
            if let currentWindow = self.currentWindow(for: windowID, from: provider) {
                return currentWindow
            }
        }

        guard let info = self.identityService.getWindowInfo(windowID: windowID) else { return nil }
        return CurrentWindow(
            bounds: info.bounds,
            ownerProcessIdentifier: info.ownerPID > 0 ? info.ownerPID : nil)
    }

    private static func currentWindow(
        for windowID: CGWindowID,
        from provider: any WindowTrackingProviding) -> CurrentWindow?
    {
        guard let bounds = provider.windowBounds(for: windowID) else { return nil }
        return CurrentWindow(
            bounds: bounds,
            ownerProcessIdentifier: provider.windowOwnerProcessIdentifier(for: windowID))
    }

    private static func sizeChangedMeaningfully(from snapshotSize: CGSize, to currentSize: CGSize) -> Bool {
        abs(currentSize.width - snapshotSize.width) > self.toleratedSizeJitter ||
            abs(currentSize.height - snapshotSize.height) > self.toleratedSizeJitter
    }

    private static func windowIdentityDescription(
        snapshot: UIAutomationSnapshot,
        windowID: CGWindowID) -> String
    {
        var parts = ["windowID: \(windowID)"]
        if let applicationName = snapshot.applicationName {
            parts.append("app: \(applicationName)")
        }
        if let applicationBundleId = snapshot.applicationBundleId {
            parts.append("bundle: \(applicationBundleId)")
        }
        if let windowTitle = snapshot.windowTitle {
            parts.append("title: \(windowTitle)")
        }
        return parts.joined(separator: ", ")
    }
}
