import Foundation
import CryptoKit
import Darwin
import SQLite3

struct DesktopError: Error {
    let code: String
    let message: String
}

func fail(_ code: String, _ message: String) throws -> Never {
    throw DesktopError(code: code, message: message)
}

let selectorSchema = "secretary-selectors-v1"
let maximumSelectorBytes = 256 * 1024
let maximumSelectorRefs = 128
let selectorFields: Set<String> = ["ref", "role", "identifier", "label", "geometry", "enabled", "actionable", "visible"]

func isJSONBoolean(_ value: Any?) -> Bool {
    guard let value = value as? NSNumber else { return false }
    return CFGetTypeID(value) == CFBooleanGetTypeID()
}

func validateSelectors(_ sidecar: [String: Any]) throws -> [[String: Any]] {
    guard Set(sidecar.keys) == ["schema", "refs", "truncated"],
          sidecar["schema"] as? String == selectorSchema,
          isJSONBoolean(sidecar["truncated"]),
          let refs = sidecar["refs"] as? [[String: Any]], refs.count <= maximumSelectorRefs,
          (try JSONSerialization.data(withJSONObject: sidecar)).count <= maximumSelectorBytes else {
        try fail("selector_invalid", "selector evidence has an unknown schema or exceeds its bounds")
    }
    var identifiers = Set<String>()
    for ref in refs {
        guard Set(ref.keys) == selectorFields,
              let name = ref["ref"] as? String, !name.isEmpty,
              identifiers.insert(name).inserted,
              ["ref", "role", "identifier", "label"].allSatisfy({ field in
                  (ref[field] as? String).map { $0.utf8.count <= 4096 } ?? false
              }), ["enabled", "actionable", "visible"].allSatisfy({ isJSONBoolean(ref[$0]) }),
              let shape = ref["geometry"] as? [String: Any], Set(shape.keys) == ["x", "y", "width", "height"] else {
            try fail("selector_invalid", "selector fields are malformed or ambiguous")
        }
        let bounds = try rectangle(shape)
        guard bounds.width > 0, bounds.height > 0 else {
            try fail("selector_invalid", "selector bounds must have positive area")
        }
    }
    return refs
}

func selectorSidecar(_ candidates: [[String: Any]], truncated: Bool) throws -> [String: Any] {
    var incomplete = truncated || candidates.count > maximumSelectorRefs
    var refs = [[String: Any]]()
    for var value in candidates.prefix(maximumSelectorRefs) {
        guard ["role", "identifier", "label"].allSatisfy({
            (value[$0] as? String).map { $0.utf8.count <= 4096 } ?? false
        }) else { incomplete = true; continue }
        value["ref"] = UUID().uuidString
        refs.append(value)
    }
    var result: [String: Any] = ["schema": selectorSchema, "refs": refs, "truncated": incomplete]
    // Never truncate strings into misleading exact evidence. Reduce the set and
    // mark it incomplete; selectors from incomplete observations cannot act.
    while (try JSONSerialization.data(withJSONObject: result)).count > maximumSelectorBytes && !refs.isEmpty {
        refs.removeLast()
        result = ["schema": selectorSchema, "refs": refs, "truncated": true]
    }
    _ = try validateSelectors(result)
    return result
}

func corroborateSelector(_ name: String, expected: [String: Any], fresh: [String: Any]) throws -> CGPoint {
    let old = try validateSelectors(expected)
    let current = try validateSelectors(fresh)
    guard expected["truncated"] as? Bool == false, fresh["truncated"] as? Bool == false,
          let wanted = old.first(where: { $0["ref"] as? String == name }) else {
        try fail("selector_unavailable", "selector observation is incomplete or reference is absent")
    }
    let matches = try current.filter { candidate in
        guard ["role", "identifier", "label"].allSatisfy({ candidate[$0] as? String == wanted[$0] as? String }),
              let before = wanted["geometry"] as? [String: Any],
              let now = candidate["geometry"] as? [String: Any] else { return false }
        return try rectangle(before) == rectangle(now)
    }
    guard matches.count == 1, let match = matches.first,
          ["enabled", "actionable", "visible"].allSatisfy({ wanted[$0] as? Bool == true && match[$0] as? Bool == true }),
          let shape = match["geometry"] as? [String: Any] else {
        try fail("selector_mismatch", "fresh exact selector is missing, changed, duplicate or unavailable")
    }
    let bounds = try rectangle(shape)
    return CGPoint(x: bounds.midX, y: bounds.midY)
}

let authorizationFields: Set<String> = ["db_path", "run_id", "session", "admission_id", "task_id", "input_event_id", "lifecycle_id", "epoch", "run_fence", "operation_id", "attempt_id", "lease_owner", "lease_fence", "lease_boot_id", "dispatch_lock_fd"]

func openAuthorityDatabase(_ path: String) throws -> OpaquePointer {
    var database: OpaquePointer?
    // The real store uses WAL. Apple's SQLite must be able to initialize its
    // coordination files when the last store connection removed them. Never
    // CREATE a database; immediately restrict SQL to reads before its snapshot.
    guard sqlite3_open_v2(path, &database, SQLITE_OPEN_READWRITE | SQLITE_OPEN_NOMUTEX, nil) == SQLITE_OK,
          let database else {
        if let database { sqlite3_close(database) }
        try fail("authorization_invalid", "authority database could not be read")
    }
    guard sqlite3_exec(database, "PRAGMA query_only=ON", nil, nil, nil) == SQLITE_OK else {
        sqlite3_close(database)
        try fail("authorization_invalid", "authority read-only policy could not be established")
    }
    var policy: OpaquePointer?
    let prepared = sqlite3_prepare_v2(database, "PRAGMA query_only", -1, &policy, nil)
    let enforced = prepared == SQLITE_OK && sqlite3_step(policy) == SQLITE_ROW && sqlite3_column_int(policy, 0) == 1
    sqlite3_finalize(policy)
    guard enforced else {
        sqlite3_close(database)
        try fail("authorization_invalid", "authority read-only policy did not verify")
    }
    return database
}

final class DispatchAuthorization {
    let payload: [String: Any]
    private let retainedLock: Int32

    init(_ envelope: Any, key: SymmetricKey) throws {
        guard let value = envelope as? [String: Any], Set(value.keys) == ["payload", "token"],
              let encoded = value["payload"] as? String, encoded.utf8.count <= 24 * 1024,
              let bytes = Data(base64Encoded: encoded), bytes.count <= 16 * 1024,
              let token = value["token"] as? String, token.count == 64,
              token.allSatisfy({ $0.isHexDigit && !$0.isUppercase }) else {
            try fail("authorization_invalid", "private authorization envelope is malformed")
        }
        let signature = stride(from: 0, to: 64, by: 2).compactMap { offset -> UInt8? in
            let start = token.index(token.startIndex, offsetBy: offset)
            return UInt8(token[start..<token.index(start, offsetBy: 2)], radix: 16)
        }
        guard HMAC<SHA256>.isValidAuthenticationCode(signature, authenticating: bytes, using: key),
              let values = try JSONSerialization.jsonObject(with: bytes) as? [String: Any],
              Set(values.keys) == authorizationFields else {
            try fail("authorization_invalid", "private authorization signature or fields did not verify")
        }
        for name in authorizationFields.subtracting(["epoch", "run_fence", "lease_fence", "dispatch_lock_fd"]) {
            guard let string = values[name] as? String, !string.isEmpty, string.utf8.count <= 4096 else {
                try fail("authorization_invalid", "private authorization identity is incomplete")
            }
        }
        for name in ["epoch", "run_fence", "lease_fence", "dispatch_lock_fd"] {
            guard let number = values[name] as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
                  number.doubleValue.isFinite, number.doubleValue.rounded() == number.doubleValue,
                  number.int64Value > 0, number.int64Value <= (name == "dispatch_lock_fd" ? Int64(Int32.max) : 9_007_199_254_740_991) else {
                try fail("authorization_invalid", "private authorization counters are invalid")
            }
        }
        guard (values["db_path"] as! String).hasPrefix("/") else {
            try fail("authorization_invalid", "authority database path must be absolute")
        }
        payload = values
        retainedLock = dup((values["dispatch_lock_fd"] as! NSNumber).int32Value)
        guard retainedLock >= 0 else { try fail("authorization_invalid", "inherited dispatch lock descriptor is absent") }
        try validateLock()
    }

    deinit { close(retainedLock) } // Never LOCK_UN: the parent retains ownership through reaping.

    var observationBinding: [String: Any] {
        payload.filter { !["db_path", "lease_boot_id", "dispatch_lock_fd"].contains($0.key) }
    }

    func validateLock() throws {
        var metadata = stat()
        var path = [CChar](repeating: 0, count: Int(MAXPATHLEN))
        guard fstat(retainedLock, &metadata) == 0, metadata.st_uid == getuid(),
              metadata.st_mode & S_IFMT == S_IFREG, metadata.st_mode & 0o777 == 0o600,
              fcntl(retainedLock, F_GETPATH, &path) == 0 else {
            try fail("authorization_invalid", "dispatch descriptor must identify an owned private regular lock")
        }
        let lockPath = String(cString: path)
        let parent = URL(fileURLWithPath: payload["db_path"] as! String).deletingLastPathComponent().standardizedFileURL
        var parentMetadata = stat()
        guard URL(fileURLWithPath: lockPath).standardizedFileURL == parent.appendingPathComponent("desktop-dispatch.lock"),
              lstat(parent.path, &parentMetadata) == 0, parentMetadata.st_uid == getuid(),
              parentMetadata.st_mode & S_IFMT == S_IFDIR, parentMetadata.st_mode & 0o777 == 0o700 else {
            try fail("authorization_invalid", "dispatch lock is outside the authority database directory")
        }
        let challenger = open(lockPath, O_RDWR | O_NOFOLLOW)
        guard challenger >= 0 else { try fail("authorization_invalid", "dispatch lock path is unavailable") }
        defer { close(challenger) }
        var current = stat()
        guard fstat(challenger, &current) == 0, current.st_dev == metadata.st_dev, current.st_ino == metadata.st_ino else {
            try fail("authorization_invalid", "dispatch lock pathname no longer identifies inherited inode")
        }
        let acquired = flock(challenger, LOCK_EX | LOCK_NB)
        guard acquired != 0, errno == EWOULDBLOCK else {
            // Closing challenger releases only its independently acquired lock.
            try fail("authorization_invalid", "exclusive dispatch lock is not retained")
        }
    }

    func validateCurrent() throws {
        try validateLock()
        // SQLite has no authority to create a database here. The server selects
        // an existing private file and this helper only reads a short snapshot.
        let path = payload["db_path"] as! String
        let descriptor = open(path, O_RDONLY | O_NOFOLLOW)
        guard descriptor >= 0 else { try fail("authorization_invalid", "authority database is unavailable") }
        defer { close(descriptor) }
        var metadata = stat()
        guard fstat(descriptor, &metadata) == 0, metadata.st_uid == getuid(), metadata.st_mode & 0o077 == 0,
              metadata.st_mode & S_IFMT == S_IFREG else { try fail("authorization_invalid", "authority database is not private") }
        let database = try openAuthorityDatabase(path)
        defer { sqlite3_close(database) }
        sqlite3_busy_timeout(database, 250)
        guard sqlite3_exec(database, "BEGIN", nil, nil, nil) == SQLITE_OK else {
            try fail("authorization_invalid", "authority snapshot could not be opened")
        }
        defer { sqlite3_exec(database, "ROLLBACK", nil, nil, nil) }
        let sql = """
        SELECT c.session,c.admission_id,c.task_id,c.input_event_id,c.lifecycle_id,c.epoch,c.run_fence,c.state,
               l.epoch,l.enabled,l.status,o.task_id,o.state,o.attempt_id,d.owner,d.fence,d.boot_id,d.expires,c.desktop_fence
        FROM execution_contexts c JOIN execution_lifecycle l ON l.lifecycle_id=c.lifecycle_id
        JOIN execution_admissions a ON a.session=c.session AND a.admission_id=c.admission_id
          AND a.task_id=c.task_id AND a.input_event_id=c.input_event_id
        JOIN execution_operations b ON b.run_id=c.run_id
        JOIN operations o ON o.operation_id=b.operation_id AND o.attempt_id=b.attempt_id
        JOIN desktop_lease d ON d.singleton=1 WHERE o.operation_id=? AND c.run_id=?
        """
        var statement: OpaquePointer?
        guard sqlite3_prepare_v2(database, sql, -1, &statement, nil) == SQLITE_OK else {
            let code = sqlite3_errcode(database)
            let extended = sqlite3_extended_errcode(database)
            // sqlite3_errstr is SQLite's bounded static description; unlike
            // errmsg it cannot include database paths or caller identities.
            let reason = String(cString: sqlite3_errstr(code)).prefix(128)
            try fail("authorization_invalid", "authority schema is unavailable (SQLite \(code)/\(extended): \(reason))")
        }
        defer { sqlite3_finalize(statement) }
        let transient = unsafeBitCast(-1, to: sqlite3_destructor_type.self)
        sqlite3_bind_text(statement, 1, payload["operation_id"] as! String, -1, transient)
        sqlite3_bind_text(statement, 2, payload["run_id"] as! String, -1, transient)
        guard sqlite3_step(statement) == SQLITE_ROW else { try fail("authorization_revoked", "current admission, run or operation binding is absent") }
        func string(_ index: Int32) -> String { sqlite3_column_text(statement, index).map { String(cString: $0) } ?? "" }
        func equal(_ index: Int32, _ key: String) -> Bool { string(index) == payload[key] as? String }
        let epoch = (payload["epoch"] as! NSNumber).int64Value
        let fence = (payload["lease_fence"] as! NSNumber).int64Value
        var timebase = mach_timebase_info_data_t()
        guard mach_timebase_info(&timebase) == KERN_SUCCESS else {
            try fail("authorization_invalid", "monotonic clock is unavailable")
        }
        // CPython on macOS uses mach_absolute_time, which excludes sleep.
        // CLOCK_MONOTONIC includes a different offset on this host and cannot
        // be compared to the lease's Python time.monotonic() value.
        let monotonic = Double(mach_absolute_time()) * Double(timebase.numer) / Double(timebase.denom) / 1_000_000_000
        guard equal(0,"session"), equal(1,"admission_id"), equal(2,"task_id"), equal(3,"input_event_id"), equal(4,"lifecycle_id"),
              sqlite3_column_int64(statement,5) == epoch,
              sqlite3_column_int64(statement,6) == (payload["run_fence"] as! NSNumber).int64Value,
              string(7) == "Active", sqlite3_column_int64(statement,8) == epoch,
              sqlite3_column_int(statement,9) == 1, string(10) == "Running", equal(11,"task_id"),
              string(12) == "Applying", equal(13,"attempt_id"), equal(14,"lease_owner"),
              sqlite3_column_int64(statement,15) == fence, equal(16,"lease_boot_id"),
              sqlite3_column_double(statement,17) > monotonic, sqlite3_column_int64(statement,18) == fence else {
            try fail("authorization_revoked", "lifecycle, run, attempt or desktop ownership changed before acceptance")
        }
        try validateCurrentBoot(payload["lease_boot_id"] as! String)
        try validateCollection(database)
    }

    private func validateCollection(_ database: OpaquePointer?) throws {
        let sql = """
        SELECT r.source_id,r.source_revision,r.run_fence,r.lifecycle_id,r.epoch,r.state,
               s.revoked,s.current_revision,s.active_run_id,s.run_fence,
               g.source_id,g.source_revision,g.session,g.admission_id,g.task_id,g.input_event_id,
               g.scope_digest,g.expires_at,g.revoked,a.scope_digest,
               s.unresolved_retrieval_id,t.run_id,t.operation_id,t.state
        FROM authority_runs r LEFT JOIN source_guard s ON s.source_id=r.source_id
        LEFT JOIN collection_grants g ON g.grant_id=r.grant_id
        LEFT JOIN authority_sources a ON a.source_id=r.source_id AND a.revision=r.source_revision
        LEFT JOIN authority_retrievals t ON t.retrieval_id=s.unresolved_retrieval_id WHERE r.run_id=?
        """
        var statement: OpaquePointer?
        guard sqlite3_prepare_v2(database, sql, -1, &statement, nil) == SQLITE_OK else {
            try fail("authorization_invalid", "collection authority schema is unavailable")
        }
        defer { sqlite3_finalize(statement) }
        let transient = unsafeBitCast(-1, to: sqlite3_destructor_type.self)
        sqlite3_bind_text(statement,1,payload["run_id"] as! String,-1,transient)
        let step = sqlite3_step(statement)
        if step == SQLITE_DONE { return } // A generic foreground context has no collection authority row.
        guard step == SQLITE_ROW else { try fail("authorization_invalid", "collection authority could not be read") }
        func string(_ index: Int32) -> String { sqlite3_column_text(statement,index).map { String(cString:$0) } ?? "" }
        func equal(_ index: Int32, _ key: String) -> Bool { string(index) == payload[key] as? String }
        let revision = sqlite3_column_int64(statement,1)
        let runFence = (payload["run_fence"] as! NSNumber).int64Value
        guard !string(0).isEmpty, revision > 0, sqlite3_column_int64(statement,2) == runFence,
              equal(3,"lifecycle_id"), sqlite3_column_int64(statement,4) == (payload["epoch"] as! NSNumber).int64Value,
              ["Running","Pending"].contains(string(5)), sqlite3_column_type(statement,6) != SQLITE_NULL,
              sqlite3_column_int(statement,6) == 0, sqlite3_column_int64(statement,7) == revision,
              equal(8,"run_id"), sqlite3_column_int64(statement,9) == runFence,
              string(10) == string(0), sqlite3_column_int64(statement,11) == revision,
              equal(12,"session"), equal(13,"admission_id"), equal(14,"task_id"), equal(15,"input_event_id"),
              !string(16).isEmpty, string(16) == string(19),
              sqlite3_column_double(statement,17) > Date().timeIntervalSince1970,
              sqlite3_column_type(statement,18) != SQLITE_NULL, sqlite3_column_int(statement,18) == 0 else {
            try fail("authorization_revoked", "collection grant, scope, source revision or run changed before acceptance")
        }
        if sqlite3_column_type(statement,20) != SQLITE_NULL {
            guard equal(21,"run_id"), equal(22,"operation_id"), ["Applying","Unknown","Pending"].contains(string(23)) else {
                try fail("authorization_revoked", "another unresolved retrieval owns this collection source")
            }
        }
    }
}

func validateCurrentBoot(_ receipt: String) throws {
    var boot = timeval()
    var size = MemoryLayout<timeval>.size
    guard sysctlbyname("kern.boottime", &boot, &size, nil, 0) == 0,
          receipt.hasPrefix("{ sec = \(boot.tv_sec), usec = \(boot.tv_usec) }") else {
        try fail("authorization_revoked", "desktop lease boot identity is stale")
    }
}
