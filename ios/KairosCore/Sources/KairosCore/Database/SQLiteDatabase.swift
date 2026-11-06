import Foundation
#if canImport(SQLite3)
import SQLite3

/// Minimal SQLite3 C-bridge. Its surface (execute / run / query / transaction) maps 1:1 onto
/// GRDB's `DatabaseQueue` (`write { try $0.execute(...) }`, `Row.fetchAll`), so swapping in
/// GRDB.swift later means changing this one file, not the store.
///
/// Thread safety: one connection, serialised by a lock (SQLITE_OPEN_FULLMUTEX as well). Reads from
/// the UI are short indexed queries, and sync applies its writes in one transaction per page.
public final class SQLiteDatabase: @unchecked Sendable {
    public enum Value: Equatable, Sendable {
        case null, int(Int64), double(Double), text(String), blob(Data)
    }

    public struct Row: Sendable {
        public let columns: [String: Value]
        public func int(_ k: String) -> Int64? {
            switch columns[k] {
            case .int(let v)?: return v
            case .double(let v)?: return Int64(v)
            default: return nil
            }
        }
        public func double(_ k: String) -> Double? {
            switch columns[k] {
            case .double(let v)?: return v
            case .int(let v)?: return Double(v)
            default: return nil
            }
        }
        public func string(_ k: String) -> String? {
            if case .text(let v)? = columns[k] { return v }
            return nil
        }
        public func data(_ k: String) -> Data? {
            switch columns[k] {
            case .blob(let v)?: return v
            case .text(let v)?: return Data(v.utf8)
            default: return nil
            }
        }
        public func bool(_ k: String) -> Bool { (int(k) ?? 0) != 0 }
    }

    public struct Failure: Error, CustomStringConvertible {
        public let code: Int32
        public let message: String
        public var description: String { "SQLite \(code): \(message)" }
    }

    private var handle: OpaquePointer?
    private let lock = NSRecursiveLock()
    private static let transient = unsafeBitCast(-1, to: sqlite3_destructor_type.self)

    /// `path` = nil opens a private in-memory database (tests). `readOnly` leaves the file
    /// untouched (no WAL switch): map packs, foreign files.
    public init(path: String?, readOnly: Bool = false) throws {
        let flags = readOnly ? (SQLITE_OPEN_READONLY | SQLITE_OPEN_FULLMUTEX)
                             : (SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE | SQLITE_OPEN_FULLMUTEX)
        let rc = sqlite3_open_v2(path ?? ":memory:", &handle, flags, nil)
        guard rc == SQLITE_OK else {
            let msg = handle.map { String(cString: sqlite3_errmsg($0)) } ?? "open failed"
            sqlite3_close(handle)
            throw Failure(code: rc, message: msg)
        }
        sqlite3_busy_timeout(handle, 5000)
        if !readOnly {
            try execute("PRAGMA journal_mode = WAL; PRAGMA synchronous = NORMAL; PRAGMA foreign_keys = ON;")
        }
    }

    deinit { sqlite3_close_v2(handle) }

    private func fail(_ rc: Int32) -> Failure {
        Failure(code: rc, message: String(cString: sqlite3_errmsg(handle)))
    }

    public func execute(_ sql: String) throws {
        lock.lock(); defer { lock.unlock() }
        var err: UnsafeMutablePointer<CChar>?
        let rc = sqlite3_exec(handle, sql, nil, nil, &err)
        if rc != SQLITE_OK {
            let msg = err.map { String(cString: $0) } ?? "exec failed"
            sqlite3_free(err)
            throw Failure(code: rc, message: msg)
        }
    }

    private func prepare(_ sql: String, _ args: [Value]) throws -> OpaquePointer? {
        var stmt: OpaquePointer?
        let rc = sqlite3_prepare_v2(handle, sql, -1, &stmt, nil)
        guard rc == SQLITE_OK else { throw fail(rc) }
        for (i, v) in args.enumerated() {
            let idx = Int32(i + 1)
            let brc: Int32
            switch v {
            case .null: brc = sqlite3_bind_null(stmt, idx)
            case .int(let x): brc = sqlite3_bind_int64(stmt, idx, x)
            case .double(let x): brc = sqlite3_bind_double(stmt, idx, x)
            case .text(let x): brc = sqlite3_bind_text(stmt, idx, x, -1, Self.transient)
            case .blob(let x):
                brc = x.withUnsafeBytes { p in
                    sqlite3_bind_blob(stmt, idx, p.baseAddress, Int32(x.count), Self.transient)
                }
            }
            if brc != SQLITE_OK {
                sqlite3_finalize(stmt)
                throw fail(brc)
            }
        }
        return stmt
    }

    /// One statement that returns no rows.
    public func run(_ sql: String, _ args: [Value] = []) throws {
        lock.lock(); defer { lock.unlock() }
        let stmt = try prepare(sql, args)
        defer { sqlite3_finalize(stmt) }
        let rc = sqlite3_step(stmt)
        guard rc == SQLITE_DONE || rc == SQLITE_ROW else { throw fail(rc) }
    }

    public func query(_ sql: String, _ args: [Value] = []) throws -> [Row] {
        lock.lock(); defer { lock.unlock() }
        let stmt = try prepare(sql, args)
        defer { sqlite3_finalize(stmt) }
        var rows: [Row] = []
        let n = sqlite3_column_count(stmt)
        let names = (0..<n).map { String(cString: sqlite3_column_name(stmt, $0)) }
        while true {
            let rc = sqlite3_step(stmt)
            if rc == SQLITE_DONE { break }
            guard rc == SQLITE_ROW else { throw fail(rc) }
            var cols: [String: Value] = [:]
            for i in 0..<n {
                let v: Value
                switch sqlite3_column_type(stmt, i) {
                case SQLITE_INTEGER: v = .int(sqlite3_column_int64(stmt, i))
                case SQLITE_FLOAT: v = .double(sqlite3_column_double(stmt, i))
                case SQLITE_TEXT:
                    if let t = sqlite3_column_text(stmt, i) { v = .text(String(cString: t)) } else { v = .null }
                case SQLITE_BLOB:
                    let len = Int(sqlite3_column_bytes(stmt, i))
                    if let p = sqlite3_column_blob(stmt, i), len > 0 { v = .blob(Data(bytes: p, count: len)) }
                    else { v = .blob(Data()) }
                default: v = .null
                }
                cols[names[Int(i)]] = v
            }
            rows.append(Row(columns: cols))
        }
        return rows
    }

    public func scalarInt(_ sql: String, _ args: [Value] = []) throws -> Int64? {
        try query(sql, args).first?.columns.values.first.flatMap { v -> Int64? in
            if case .int(let x) = v { return x }
            return nil
        }
    }

    /// BEGIN IMMEDIATE … COMMIT, rolled back when `body` throws.
    public func transaction<T>(_ body: () throws -> T) throws -> T {
        lock.lock(); defer { lock.unlock() }
        try execute("BEGIN IMMEDIATE")
        do {
            let out = try body()
            try execute("COMMIT")
            return out
        } catch {
            try? execute("ROLLBACK")
            throw error
        }
    }
}

extension SQLiteDatabase.Value {
    public init(_ v: Int64?) { self = v.map { .int($0) } ?? .null }
    public init(_ v: Double?) { self = v.map { .double($0) } ?? .null }
    public init(_ v: String?) { self = v.map { .text($0) } ?? .null }
    public init(_ v: Bool) { self = .int(v ? 1 : 0) }
}
#endif
