import XCTest
import AppKit
import AXorcist
import enum AXorcist.SpecialKey
import PeekabooFoundation
@testable import PeekabooAutomationKit

@MainActor
private final class LegacyPointerProbe: SyntheticInputDriving {
    var calls = 0
    func click(at: CGPoint, button: MouseButton, count: Int, target: ExactWindowPointerTarget) async throws -> DesktopActionOutcome {
        calls += 1
        return .dispatchedUnverified(delivery: .init(mechanism: .accessibilityValue, mode: .background), evidence: .deliveryAccepted)
    }
    func click(at: CGPoint, button: MouseButton, count: Int) throws -> DesktopActionOutcome { throw ProbeError.unexpected }
    func click(at: CGPoint, button: MouseButton, count: Int, targetProcessIdentifier: pid_t) async throws -> DesktopActionOutcome { throw ProbeError.unexpected }
    func click(at: CGPoint, button: MouseButton, count: Int, targetProcessIdentifier: pid_t, targetWindowID: CGWindowID?) async throws -> DesktopActionOutcome { throw ProbeError.unexpected }
    func move(to: CGPoint) throws { throw ProbeError.unexpected }
    func currentLocation() -> CGPoint? { nil }
    func pressHold(at: CGPoint, button: MouseButton, duration: TimeInterval) async throws { throw ProbeError.unexpected }
    func scroll(deltaX: Double, deltaY: Double, at: CGPoint?) throws { throw ProbeError.unexpected }
    func type(_ text: String, delayPerCharacter: TimeInterval) throws { throw ProbeError.unexpected }
    func tapKey(_ key: SpecialKey, modifiers: CGEventFlags) throws { throw ProbeError.unexpected }
    func hotkey(keys: [String], holdDuration: TimeInterval) throws { throw ProbeError.unexpected }
}

private enum ProbeError: Error { case unexpected }

final class PointerDeliveryTests: XCTestCase {
    @MainActor
    private func exercise(allowsAX: Bool, failRouted: Bool = false) async throws -> (DesktopActionOutcome?, Int, Int) {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let bounds = CGRect(x: -100, y: 20, width: 100, height: 60)
        let identity = WindowMutationIdentity(windowID: 123, ownerProcessIdentifier: 456,
                                             ownerProcessStartIdentity: 789, capturedBounds: bounds)
        let legacy = LegacyPointerProbe()
        var routed = 0
        let service = ClickService(snapshotManager: InMemorySnapshotManager(),
            inputPolicy: UIInputPolicy(defaultStrategy: .synthOnly), syntheticInputDriver: legacy,
            exactWindowIdentityValidator: { candidate, shape in candidate == identity && shape == bounds },
            processStartIdentityProvider: { _ in 789 },
            desktopOperationExecutor: DesktopOperationExecutor(laneCoordinator: .init(coordinationRootURL: root)),
            exactWindowPointerExecutor: { point, button, count, target in
                routed += 1
                XCTAssertEqual(point, CGPoint(x: -50, y: 40))
                XCTAssertEqual(button, .left)
                XCTAssertEqual(count, 1)
                XCTAssertEqual(target.identity, identity)
                XCTAssertEqual(target.bounds, bounds)
                if failRouted {
                    throw DesktopActionFailure.dispatchedUnverified(
                        delivery: .init(mechanism: .windowTargetedEvents, mode: .background),
                        evidence: .deliveryAccepted, message: "synthetic interrupted dispatch", hint: "observe")
                }
                return .dispatchedUnverified(delivery: .init(mechanism: .windowTargetedEvents, mode: .background), evidence: .deliveryAccepted)
            })
        do {
            let result = try await service.click(target: .coordinates(CGPoint(x: -50, y: 40)), clickType: .single,
                snapshotId: nil, automationTarget: .exactWindow(.init(identity: identity, bounds: bounds)),
                validatesProcessIdentity: true, allowsAccessibilityValueDelivery: allowsAX)
            if failRouted { XCTFail("routed failure must propagate without another delivery") }
            return (result.outcome, legacy.calls, routed)
        } catch let error as DesktopActionFailure {
            guard failRouted else { throw error }
            XCTAssertEqual(error.outcome.state, .dispatchedUnverified)
            XCTAssertEqual(error.outcome.delivery?.mechanism, .windowTargetedEvents)
            return (nil, legacy.calls, routed)
        }
    }

    @MainActor
    func testFalseAccessibilityFlagReachesOnlyExactWindowPointerEvents() async throws {
        let (outcome, legacy, routed) = try await exercise(allowsAX: false)
        XCTAssertEqual(legacy, 0)
        XCTAssertEqual(routed, 1)
        XCTAssertEqual(outcome?.delivery?.mechanism, .windowTargetedEvents)
        XCTAssertEqual(outcome?.state, .dispatchedUnverified)
    }

    @MainActor
    func testAllowedAccessibilityRoutePreservesUpstreamBehavior() async throws {
        let (outcome, legacy, routed) = try await exercise(allowsAX: true)
        XCTAssertEqual(legacy, 1)
        XCTAssertEqual(routed, 0)
        XCTAssertEqual(outcome?.delivery?.mechanism, .accessibilityValue)
    }

    @MainActor
    func testRoutedPartialDeliveryNeverRetriesAccessibilityOrGlobalInput() async throws {
        let (_, legacy, routed) = try await exercise(allowsAX: false, failRouted: true)
        XCTAssertEqual(legacy, 0)
        XCTAssertEqual(routed, 1)
    }

    @MainActor
    func testSynthOnlyUnicodeTypingUsesProcessEventsWithoutAXWrites() throws {
        var driver = TargetedTypeInputDriver()
        var writes = 0
        var typed = ""
        driver.insertText = { _, _, _, _, _ in writes += 1; return true }
        driver.typeCharacter = { character, pid in XCTAssertEqual(pid, 456); typed.append(character) }
        for character in "한글 世界 😀" {
            let result = try driver.character(character, processIdentifier: 456, strategy: .synthOnly,
                keyboardDelivery: .init(mechanism: .processTargetedEvents, mode: .background))
            XCTAssertEqual(result.delivery?.mechanism, .processTargetedEvents)
        }
        XCTAssertEqual(writes, 0)
        XCTAssertEqual(typed, "한글 世界 😀")
    }
}
