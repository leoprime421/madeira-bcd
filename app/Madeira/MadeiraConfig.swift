import Foundation

/// ml1095: ONE configuration file for every runtime switch: Documents/madeira.cfg
///
///     # comments start with #
///     key = value        (whitespace trimmed; the value runs to the end of the
///                         line, so it may contain '='; the last line wins)
///
/// Keys are the old per-file names without "madeira-" and ".txt"
/// (swap-mb, vram-mb, pool, totalphys, wx, ...). Environment exports are
/// "env.NAME = value". DXMT options are one line, "dxmt = a=b;c=d".
///
/// The native side reads the same file through build/madeira_cfg.h with the
/// same rules. When madeira.cfg is ABSENT the legacy one-value-per-file layout
/// still works; when it is PRESENT the legacy files are ignored, so the one
/// file is the single source of truth. `migrateLegacy()` writes madeira.cfg
/// from whatever legacy files exist, once, so they can then be deleted.
enum MadeiraConfig {
    static let fileName = "madeira.cfg"

    /// Every switch that used to live in its own Documents/madeira-<key>.txt.
    static let legacyKeys = [
        "swap-mb", "swap-canary", "vram-mb", "pool", "totalphys", "inproc-sync", "wx",
        "mono-bridge", "ctx-frame", "tf-trace", "usd-time", "real-suspend", "mono-suspend",
        "arena", "arena-mb", "arena-test", "fexfail", "remote", "remote-batch", "d3d12",
        "apicensus", "shadow", "census", "wxprobe", "args", "valley-args",
        "jumbo-mb", "jumbo-keep-mb", "iat-noexec", "vmwatch", "no-local-read", "vsps-fill",
        "d3d9",
    ]

    static var documents: URL? {
        FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first
    }
    static var url: URL? { documents?.appendingPathComponent(fileName) }
    static var present: Bool { url.map { FileManager.default.fileExists(atPath: $0.path) } ?? false }

    /// All key/value pairs of madeira.cfg (empty when the file is absent).
    static func all() -> [String: String] {
        guard let u = url, let text = try? String(contentsOf: u, encoding: .utf8) else { return [:] }
        var out: [String: String] = [:]
        for raw in text.split(omittingEmptySubsequences: false, whereSeparator: { $0 == "\n" || $0 == "\r\n" }) {
            let line = raw.trimmingCharacters(in: .whitespaces)
            if line.isEmpty || line.hasPrefix("#") { continue }
            guard let eq = line.firstIndex(of: "=") else { continue }
            let k = line[..<eq].trimmingCharacters(in: .whitespaces)
            let v = line[line.index(after: eq)...].trimmingCharacters(in: .whitespaces)
            if !k.isEmpty { out[k] = v }
        }
        return out
    }

    /// madeira-bcd: `key` from the running game's own file ($MADEIRA_CFG_GAME,
    /// GameProfiles.swift), or nil. The native reader lets it win over
    /// madeira.cfg; Swift callers that merge (dxmt) read both.
    static func gameValue(_ key: String) -> String? {
        guard let p = getenv("MADEIRA_CFG_GAME"), case let path = String(cString: p), !path.isEmpty else { return nil }
        return GameProfile.values(ofFile: URL(fileURLWithPath: path))[key].flatMap { $0.isEmpty ? nil : $0 }
    }

    /// Runtime guardrails that are required by one title regardless of a stale
    /// global setting. They are intentionally applied only at read time: the
    /// user's madeira.cfg is never rewritten.
    private static func launchAdjusted(_ key: String, _ value: String?) -> String? {
        guard key == "pool" else { return value }

        let dockApp = getenv("MADEIRA_DOCK_APPID").map { String(cString: $0) }
        let directApp = getenv("MADEIRA_STEAM_APPID").map { String(cString: $0) }
        guard dockApp == "1293830" || directApp == "1293830" else { return value }

        /* FH4 Build 124: ForzaWebHelper/libcef is spawned late, after the game
         * has already consumed most of a 512 MB executable pool. The gpu-process
         * then hits JIT-pool EXHAUSTED and core dependencies (dxgi/user32/gdi32)
         * fail with STATUS_NO_MEMORY, leaving the Microsoft login black.
         * 896 MB is Madeira's historically proven Steam/CEF pool size; pool
         * splitting changes placement only and cannot increase a 512 MB total. */
        let trimmed = value?.trimmingCharacters(in: .whitespacesAndNewlines)
        if let trimmed, let mb = Int(trimmed), mb >= 896, mb <= 1152 {
            /* Build 124 also proves the direct official-EXE path still enables
             * the old experimental W^X fast path (ml1157), then dies in an
             * unhandled C0000005. Force the safe default for FH4; a later
             * env.MADEIRA_WX line from madeira.cfg/game config can still opt in. */
            setenv("MADEIRA_WX", "0", 1)
            return trimmed
        }
        setenv("MADEIRA_WX", "0", 1)
        return "896"
    }

    /// The value for `key`, trimmed, or nil when unset. Falls back to the legacy
    /// file ONLY when madeira.cfg does not exist.
    static func get(_ key: String) -> String? {
        if present { return launchAdjusted(key, all()[key]) }
        guard let d = documents,
              let txt = try? String(contentsOf: d.appendingPathComponent("madeira-\(key).txt"), encoding: .utf8)
        else { return launchAdjusted(key, nil) }
        return launchAdjusted(key, txt.trimmingCharacters(in: .whitespacesAndNewlines))
    }

    static func bool(_ key: String, default dflt: Bool = false) -> Bool {
        guard let v = get(key) else { return dflt }
        return ["1", "on", "true", "yes"].contains(v)
    }

    /// Set or remove one key in madeira.cfg (Settings). Comments and every
    /// other line are kept; earlier lines for the key are dropped and the new
    /// value is appended; a nil value removes the key. Without madeira.cfg the
    /// legacy files are migrated first, so writing one key never hides the
    /// switches that still live in madeira-*.txt files.
    @discardableResult
    static func set(_ key: String, _ value: String?) -> Bool {
        guard let u = url else { return false }
        if !present { migrateLegacy(log: { _ in }) }
        let text = (try? String(contentsOf: u, encoding: .utf8)) ?? ""
        var lines = text.split(omittingEmptySubsequences: false, whereSeparator: { $0 == "\n" || $0 == "\r\n" }).map(String.init)
        if lines.last == "" { lines.removeLast() }
        lines.removeAll { line in
            let t = line.trimmingCharacters(in: .whitespaces)
            guard !t.hasPrefix("#"), let eq = t.firstIndex(of: "=") else { return false }
            return t[..<eq].trimmingCharacters(in: .whitespaces) == key
        }
        if let value { lines.append("\(key) = \(value)") }
        let out = lines.joined(separator: "\n") + (lines.isEmpty ? "" : "\n")
        do { try out.write(to: u, atomically: true, encoding: .utf8); return true } catch { return false }
    }

    /// An app-side switch spelled like the native ones: `env.NAME = value` in
    /// madeira.cfg, else the process environment, else `fallback`. Any value
    /// other than "0" means on.
    static func flag(_ name: String, fallback: Bool = true) -> Bool {
        if present, let v = all()["env." + name] { return v != "0" }
        return getenv(name).map { String(cString: $0) != "0" } ?? fallback
    }

    /// One-time migration: with no madeira.cfg and at least one legacy file,
    /// write madeira.cfg from them. Legacy files are left in place (ignored from
    /// now on) so nothing is destroyed; the log names them so they can be deleted.
    /// Returns the keys that were migrated.
    @discardableResult
    static func migrateLegacy(log: (String) -> Void) -> [String] {
        guard let d = documents, let u = url, !present else { return [] }
        var lines = ["# Madeira configuration (ml1095): one file for every switch.",
                     "# key = value; lines starting with # are comments; the last line wins.",
                     "# Keys are the old file names without 'madeira-' and '.txt'.",
                     "# Environment exports: env.NAME = value. DXMT options: dxmt = a=b;c=d.",
                     ""]
        var migrated: [String] = []
        for key in legacyKeys {
            guard let txt = try? String(contentsOf: d.appendingPathComponent("madeira-\(key).txt"), encoding: .utf8) else { continue }
            let v = txt.trimmingCharacters(in: .whitespacesAndNewlines)
            lines.append("\(key) = \(v)")
            migrated.append(key)
        }
        if let txt = try? String(contentsOf: d.appendingPathComponent("madeira-dxmt.txt"), encoding: .utf8) {
            let parts = txt.split(whereSeparator: { $0 == "\n" || $0 == "\r\n" })
                .map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty && !$0.hasPrefix("#") }
            if !parts.isEmpty { lines.append("dxmt = " + parts.joined(separator: ";")); migrated.append("dxmt") }
        }
        if let txt = try? String(contentsOf: d.appendingPathComponent("madeira-env.txt"), encoding: .utf8) {
            for raw in txt.split(whereSeparator: { $0 == "\n" || $0 == "\r\n" }) {
                let line = raw.trimmingCharacters(in: .whitespaces)
                guard !line.isEmpty, !line.hasPrefix("#"), let eq = line.firstIndex(of: "="), eq != line.startIndex else { continue }
                lines.append("env.\(line[..<eq]) = \(line[line.index(after: eq)...])")
            }
            migrated.append("env")
        }
        guard !migrated.isEmpty else { return [] }
        do {
            try (lines.joined(separator: "\n") + "\n").write(to: u, atomically: true, encoding: .utf8)
            log("madeira.cfg written from legacy files (\(migrated.joined(separator: ", "))); the madeira-*.txt files are now ignored and can be deleted")
        } catch {
            log("madeira.cfg could not be written: \(error)")
            return []
        }
        return migrated
    }

    /// Once madeira.cfg exists the legacy files are dead weight: remove every
    /// known switch file (never logs, traces or the input map). Idempotent.
    /// Returns the names removed.
    @discardableResult
    static func deleteLegacyFiles(log: (String) -> Void) -> [String] {
        guard present, let d = documents else { return [] }
        var removed: [String] = []
        for name in legacyKeys.map({ "madeira-\($0).txt" }) + ["madeira-env.txt", "madeira-dxmt.txt"] {
            let u = d.appendingPathComponent(name)
            guard FileManager.default.fileExists(atPath: u.path) else { continue }
            do { try FileManager.default.removeItem(at: u); removed.append(name) }
            catch { log("could not remove \(name): \(error)") }
        }
        if !removed.isEmpty { log("removed legacy config files (madeira.cfg is the one file now): " + removed.joined(separator: ", ")) }
        return removed
    }
}
