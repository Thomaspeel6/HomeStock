import SwiftUI

@main
struct HomeStockApp: App {
    @State private var backend = Backend()

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(backend)
                .task { await backend.start() }
                .onDisappear { backend.stop() }
                .frame(minWidth: 880, minHeight: 560)
        }
        .windowToolbarStyle(.unified)
        .commands {
            CommandGroup(after: .newItem) {
                Button("Refresh") { Task { await backend.refresh() } }
                    .keyboardShortcut("r")
            }
        }

        // A real Settings window on ⌘, rather than a tab pretending to be one.
        Settings {
            SettingsView().environment(backend)
        }
    }
}

enum Pane: String, CaseIterable, Identifiable {
    case shopping = "Shopping"
    case soon = "Use soon"
    case house = "In the house"
    case chat = "Chat"

    var id: String { rawValue }

    var symbol: String {
        switch self {
        case .shopping: "cart"
        case .soon: "clock.badge.exclamationmark"
        case .house: "cabinet"
        case .chat: "bubble.left.and.text.bubble.right"
        }
    }
}

struct RootView: View {
    @Environment(Backend.self) private var backend
    @State private var selection: Pane = .shopping

    var body: some View {
        NavigationSplitView {
            List(selection: $selection) {
                ForEach(Pane.allCases) { pane in
                    Label(pane.rawValue, systemImage: pane.symbol)
                        .badge(count(for: pane))
                        .tag(pane)
                }
            }
            .listStyle(.sidebar)
            .navigationSplitViewColumnWidth(min: 190, ideal: 205, max: 260)
            .safeAreaInset(edge: .bottom) { EngineStatusBar() }
        } detail: {
            switch selection {
            case .shopping:
                ItemListView(pane: .shopping, items: backend.state?.order ?? [],
                             empty: "Nothing looks due.",
                             emptySymbol: "checkmark.circle")
            case .soon:
                ItemListView(pane: .soon, items: backend.state?.expiring ?? [],
                             empty: "Nothing about to go off.",
                             emptySymbol: "leaf")
            case .house:
                HouseView(items: backend.state?.shelf ?? [])
            case .chat:
                ChatView()
            }
        }
    }

    private func count(for pane: Pane) -> Int {
        switch pane {
        case .shopping: backend.state?.order.count ?? 0
        case .soon: backend.state?.expiring.count ?? 0
        case .house: backend.state?.shelf.count ?? 0
        case .chat: 0
        }
    }
}

/// Ingestion failing silently looks exactly like a quiet week, so the state of
/// the engine is always on screen rather than discoverable.
struct EngineStatusBar: View {
    @Environment(Backend.self) private var backend

    var body: some View {
        HStack(spacing: 7) {
            switch backend.status {
            case .starting:
                ProgressView().controlSize(.small)
                Text("Starting…")
            case .ready:
                Image(systemName: stale ? "exclamationmark.triangle.fill" : "circle.fill")
                    .foregroundStyle(stale ? .orange : .green)
                    .font(.system(size: stale ? 10 : 7))
                Text(stale ? "Receipts out of date" : "Up to date")
            case .failed(let message):
                Image(systemName: "xmark.octagon.fill").foregroundStyle(.red).font(.system(size: 10))
                Text(message).lineLimit(2)
            }
            Spacer()
        }
        .font(.caption)
        .foregroundStyle(.secondary)
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
    }

    private var stale: Bool { backend.state?.health.stale ?? false }
}
