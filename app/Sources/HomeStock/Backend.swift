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

    private func spawn() async throws {
        guard let python = Self.pythonPath else {
            throw Failure("HomeStock needs Python 3.11 or newer, and could not find it. "
                          + "Install it from python.org or with Homebrew, then reopen HomeStock.")
        }
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: python)
        proc.arguments = ["-m", "homestock.ui_main", "--port", "0", "--handshake"]
        var env = ProcessInfo.processInfo.environment
        env["PYTHONPATH"] = Self.enginePath
        env["PYTHONUNBUFFERED"] = "1"
        env["HOMESTOCK_DB"] = Self.databasePath.path
        proc.environment = env

        let pipe = Pipe()
        proc.standardOutput = pipe
        proc.standardError = FileHandle.nullDevice
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
        throw Failure("The HomeStock engine did not start. Check that Python 3.11 or newer is installed.")
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
        return FileManager.default.currentDirectoryPath
    }

    /// The engine needs Python 3.11 or newer. macOS still ships 3.9 at
    /// /usr/bin/python3, so picking the first interpreter on disk would find an
    /// interpreter that cannot parse the engine and fail with a SyntaxError
    /// nobody could act on. Each candidate is asked its version instead.
    static let minimumPython = (3, 11)

    static var pythonPath: String? {
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

    func send(history: [Message], provider: Provider, model: String, key: String?) async throws -> ChatReply {
        var body: [String: Any] = [
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
