import XCTest
import PeekabooAutomationKit
import PeekabooFoundation
@testable import SecretaryPeekaboo

final class BackendContractTests: XCTestCase {
    @MainActor
    func testConfirmedInputRequiresMatchingExactTarget() throws {
        let bounds = CGRect(x: -100, y: 20, width: 100, height: 60)
        let identity = WindowMutationIdentity(windowID: 123, ownerProcessIdentifier: 456,
                                             ownerProcessStartIdentity: 789, capturedBounds: bounds)
        let target = DesktopTargetIdentity(exactWindow: try .init(identity: identity, bounds: bounds))
        let outcome = DesktopActionOutcome.confirmedChange(delivery: .init(mechanism: .windowTargetedEvents, mode: .background))
        let result = UIAutomationActionResult(payload: (), outcome: outcome, targetIdentity: target)
        effectStarted = true
        effectStatusOverride = nil
        try recordPeekabooOutcome(result, expected: identity, bounds: bounds)
        XCTAssertEqual(effectStatusOverride, "dispatched")
        let other = WindowMutationIdentity(windowID: 124, ownerProcessIdentifier: 456,
                                          ownerProcessStartIdentity: 789, capturedBounds: bounds)
        XCTAssertThrowsError(try recordPeekabooOutcome(result, expected: other, bounds: bounds))
        XCTAssertEqual(effectStatusOverride, "unknown")
    }

    @MainActor
    func testRefusedOutcomeIsNotStarted() {
        effectStarted = true
        effectStatusOverride = nil
        let outcome = DesktopActionOutcome.refused(reason: .runtimeIncompatible)
        XCTAssertThrowsError(try recordPeekabooOutcome(UIAutomationActionResult(payload: (), outcome: outcome)))
        XCTAssertEqual(effectStatusOverride, "not_started")
        XCTAssertFalse(effectStarted)
    }

    @MainActor
    func testConfirmedInputWithoutTargetReceiptRemainsUnknown() {
        effectStarted = true
        effectStatusOverride = nil
        let outcome = DesktopActionOutcome.confirmedChange(delivery: .init(mechanism: .windowTargetedEvents, mode: .background))
        XCTAssertThrowsError(try recordPeekabooOutcome(UIAutomationActionResult(payload: (), outcome: outcome)))
        XCTAssertEqual(effectStatusOverride, "unknown")
        XCTAssertTrue(effectStarted)
    }

    @MainActor
    func testUnconfirmedDispatchStatesRemainUnknown() {
        let delivery = DesktopActionOutcome.Delivery(mechanism: .windowTargetedEvents, mode: .background)
        for outcome in [DesktopActionOutcome.partial(delivery: delivery),
                        .dispatchedUnverified(delivery: delivery, evidence: .deliveryAccepted),
                        .suspectedNoop(delivery: delivery), .indeterminate(evidence: .completionUnknown)] {
            effectStarted = true
            effectStatusOverride = nil
            XCTAssertThrowsError(try recordPeekabooOutcome(UIAutomationActionResult(payload: (), outcome: outcome)))
            XCTAssertEqual(effectStatusOverride, "unknown")
            XCTAssertEqual(backendOutcome?["state"] as? String, outcome.state.rawValue)
        }
    }
}
