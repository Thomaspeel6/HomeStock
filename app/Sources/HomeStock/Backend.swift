import Foundation
import Observation

/// Owns the Python engine: spawns it, learns where it landed, and talks to it.
///
/// The engine is the same code the MCP server runs, bundled inside the .app and
/// started on a loopback port the OS picks. The app never reads homestock.db
/// itself — one writer, one set of rules, no second implementation of anything.
@MainActor
@Observable
final class Backend {
    enum Status: Equatable {
        case starting, ready, failed(String)
    }

    private(set) var status: Status = .starting
    private(set) var state: KitchenState?
    private(set) var providers: [Provider] = []

    private(set) var lanMode = false
    private(set) var pairing: Pairing?

    private var process: Process?
    private var port: Int = 0
    private var token: String = ""
    private let session = URLSession(configuration: .ephemeral)

    // MARK: - Lifecycle

    func start() async {
        guard process == nil else { return }
        do {
            try await spawn()
            status = .ready
            await refresh()
            await loadProviders()
        } catch {
            status = .failed(error.localizedDescription)
        }
    }

    func stop() {
        process?.terminate()
        process = nil
    }

    /// Phone capture means binding the network rather than loopback, and the
    /// listening socket cannot be rebound in place — so the engine is restarted
    /// with --lan. It is off by default and every launch starts off again:
    /// leaving a household's shopping exposed on a cafe wifi is the one
    /// mistake here with real consequences.
    func setLAN(_ on: Bool) async {
        guard on != lanMode else { return }
        stop()
        lanMode = on
        pairing = nil
        status = .starting
        await start()
        if on { await loadPairing() }
    }

    func loadPairing() async {
        pairing = try? await get("/api/pairing", as: Pairing.self)
    }

    struct Pairing: Decodable, Equatable {
        var code: String
        var url: String
        var expiresIn: Int
        enum CodingKeys: String, CodingKey {
            case code, url
            case expiresIn = "expires_in"
        }
        /// What the QR encodes: the address plus the code, so the phone pairs
        /// by pointing a camera instead of someone reading six digits out loud.
        var pairingURL: String {
            url.hasSuffix("/") ? "\(url)?code=\(code)" : "\(url)/?code=\(code)"
        }
    }

    private func spawn() async throws {
        guard let python = Self.pythonPath else {
            throw Failure("HomeStock needs Python 3.11 or newer, and could not find it. "
                          + "Install it from python.org or with Homebrew, then reopen HomeStock.")
        }
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: python)
        var args = ["-m", "homestock.ui_main", "--port", "0", "--handshake"]
        if lanMode { args.append("--lan") }
        proc.arguments = args
        var env = ProcessInfo.processInfo.environment
        env["PYTHONPATH"] = Self.enginePath
        env["PYTHONUNBUFFERED"] = "1"
        env["HOMESTOCK_DB"] = Self.databasePath.path
        proc.environment = env

        let pipe = Pipe()
        let errors = Pipe()
        proc.standardOutput = pipe
        proc.standardError = errors
        try proc.run()
        self.process = proc

        // The handshake is the only contract between the two halves: the engine
        // prints one JSON line naming its port and write token, then serves.
        let handle = pipe.fileHandleForReading
        let deadline = Date().addingTimeInterval(20)
        var buffer = Data()
        while Date() < deadline {
            let chunk = handle.availableData
            if chunk.isEmpty { try await Task.sleep(nanoseconds: 50_000_000); continue }
            buffer.append(chunk)
            guard let text = String(data: buffer, encoding: .utf8) else { continue }
            for line in text.split(separator: "\n") {
                guard let data = line.data(using: .utf8),
                      let hs = try? JSONDecoder().decode(Handshake.self, from: data)
                else { continue }
                port = hs.port
                token = hs.token
                return
            }
        }
        // Whatever Python said on the way down is the only useful thing here,
        // so it goes in front of the user rather than into a null device.
        proc.terminate()
        let detail = String(data: errors.fileHandleForReading.availableData, encoding: .utf8)?
            .split(separator: "\n").suffix(3).joined(separator: " ")
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        throw Failure(detail.isEmpty
            ? "The HomeStock engine did not start, and said nothing about why. Interpreter: \(python)"
            : "The HomeStock engine did not start: \(detail)")
    }

    private struct Handshake: Decodable { var port: Int; var token: String }

    struct Failure: LocalizedError {
        let message: String
        init(_ m: String) { message = m }
        var errorDescription: String? { message }
    }

    // MARK: - Paths

    /// In a built .app the engine travels inside Resources; in `swift run` it is
    /// the repo checkout two levels up. Both are supported so the app is
    /// runnable from Xcode without a packaging step.
    static var enginePath: String {
        if let bundled = Bundle.main.resourceURL?.appendingPathComponent("engine"),
           FileManager.default.fileExists(atPath: bundled.appendingPathComponent("homestock").path) {
            return bundled.path
        }
        // `swift run` from app/ — the engine is the checkout one level up.
        let cwd = FileManager.default.currentDirectoryPath
        return cwd.hasSuffix("/app") ? String(cwd.dropLast(4)) : cwd
    }

    /// The engine needs Python 3.11 or newer. macOS still ships 3.9 at
    /// /usr/bin/python3, so picking the first interpreter on disk would find an
    /// interpreter that cannot parse the engine and fail with a SyntaxError
    /// nobody could act on. Each candidate is asked its version instead.
    static let minimumPython = (3, 11)

    static var pythonPath: String? {
        // The bundled runtime first, always. The dependencies inside the app
        // are compiled wheels built for *this* interpreter version, so running
        // the engine on the user's Homebrew Python loads a pydantic_core built
        // for a different ABI and dies at import. A system interpreter is only
        // a fallback for `swift run` during development.
        if let bundled = Bundle.main.resourceURL?
            .appendingPathComponent("python/bin/python3").path,
           FileManager.default.isExecutableFile(atPath: bundled) {
            return bundled
        }
        let candidates = [
            "/opt/homebrew/bin/python3",
            "/usr/local/bin/python3",
            "/Library/Frameworks/Python.framework/Versions/Current/bin/python3",
            "/usr/bin/python3",
        ]
        return candidates.first { isUsable($0) }
    }

    private static func isUsable(_ path: String) -> Bool {
        guard FileManager.default.isExecutableFile(atPath: path) else { return false }
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: path)
        proc.arguments = ["-c", "import sys; print(sys.version_info[0], sys.version_info[1])"]
        let pipe = Pipe()
        proc.standardOutput = pipe
        proc.standardError = FileHandle.nullDevice
        guard (try? proc.run()) != nil else { return false }
        proc.waitUntilExit()
        let out = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        let parts = out.split(separator: " ").compactMap { Int($0.trimmingCharacters(in: .whitespacesAndNewlines)) }
        guard parts.count == 2 else { return false }
        return (parts[0], parts[1]) >= minimumPython
    }

    /// The user's data, in the standard place, not inside the app bundle —
    /// so deleting the app never deletes a kitchen.
    static var databasePath: URL {
        let dir = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("HomeStock", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir.appendingPathComponent("homestock.db")
    }

    // MARK: - Requests

    private func url(_ path: String) -> URL {
        URL(string: "http://127.0.0.1:\(port)\(path)")!
    }

    private func get<T: Decodable>(_ path: String, as: T.Type) async throws -> T {
        let (data, _) = try await session.data(from: url(path))
        return try JSONDecoder().decode(T.self, from: data)
    }

    @discardableResult
    private func post(_ path: String, _ body: [String: Any]) async throws -> Data {
        var req = URLRequest(url: url(path))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "content-type")
        // The write token proves the request came from this app rather than
        // from a page the user happens to have open.
        req.setValue(token, forHTTPHeaderField: "x-homestock-token")
        req.httpBody = try JSONSerialization.data(withJSONObject: body)
        let (data, response) = try await session.data(for: req)
        if let http = response as? HTTPURLResponse, http.statusCode >= 400 {
            let detail = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["error"] as? String
            throw Failure(detail ?? "The engine returned \(http.statusCode).")
        }
        return data
    }

    // MARK: - Actions

    /// Offered in Settings as a list. For Ollama the answer depends on what the
    /// user has pulled, so no hardcoded default can be right.
    func models(for provider: Provider, key: String?) async -> [String] {
        struct Wrapper: Decodable { var models: [String] }
        var path = "/api/models?provider=\(provider.id)"
        if let key, !key.isEmpty,
           let escaped = key.addingPercentEncoding(withAllowedCharacters: .alphanumerics) {
            path += "&api_key=\(escaped)"
        }
        return (try? await get(path, as: Wrapper.self))?.models ?? []
    }

    func refresh() async {
        state = try? await get("/api/state", as: KitchenState.self)
    }

    private func loadProviders() async {
        struct Wrapper: Decodable { var providers: [Provider] }
        providers = (try? await get("/api/providers", as: Wrapper.self))?.providers ?? []
    }

    func act(_ action: String, on item: String) async {
        _ = try? await post("/api/action", ["item": item, "action": action])
        await refresh()
    }

    func addNote(_ text: String) async {
        _ = try? await post("/api/capture", ["kind": "note", "text": text, "device": "mac"])
        await refresh()
    }

    func addBarcode(_ code: String) async throws {
        _ = try await post("/api/capture", ["kind": "barcode", "text": code, "device": "mac"])
        await refresh()
    }

    /// A photographed receipt from the Mac: same inbox the phone writes to, so
    /// there is one reading path rather than one per device.
    func addPhoto(_ file: URL) async throws {
        let data = try Data(contentsOf: file)
        let mime = switch file.pathExtension.lowercased() {
            case "png": "image/png"
            case "webp": "image/webp"
            case "heic": "image/heic"
            default: "image/jpeg"
        }
        let dataURL = "data:\(mime);base64,\(data.base64EncodedString())"
        _ = try await post("/api/capture",
                           ["kind": "receipt_photo", "data_url": dataURL, "device": "mac"])
        await refresh()
    }

    func undo(_ eventIDs: [Int]) async {
        _ = try? await post("/api/undo", ["event_ids": eventIDs])
        await refresh()
    }

    func send(history: [Message], provider: Provider, model: String, key: String?,
              allowWrites: Bool) async throws -> ChatReply {
        var body: [String: Any] = [
            "allow_writes": allowWrites,
            "provider": provider.id,
            "model": model.isEmpty ? provider.defaultModel : model,
            "messages": history.filter { $0.role != .failure }.map {
                ["role": $0.role == .user ? "user" : "assistant", "content": $0.text]
            },
        ]
        if let key, !key.isEmpty { body["api_key"] = key }
        let data = try await post("/api/chat", body)
        return try JSONDecoder().decode(ChatReply.self, from: data)
    }
}
