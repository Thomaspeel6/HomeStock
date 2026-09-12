import SwiftUI

/// Shopping and Use soon: short, actionable lists where every row shows the
/// reasoning behind its guess and a correction is one click.
struct ItemListView: View {
    @Environment(Backend.self) private var backend
    let pane: Pane
    let items: [Item]
    let empty: String
    let emptySymbol: String

    @State private var note = ""

    var body: some View {
        Group {
            if items.isEmpty {
                ContentUnavailableView(empty, systemImage: emptySymbol)
            } else {
                List {
                    ForEach(items) { item in
                        ItemRow(item: item, actions: actions)
                            .listRowSeparator(.visible)
                    }
                }
                .listStyle(.inset)
                .alternatingRowBackgrounds()
            }
        }
        .navigationTitle(pane.rawValue)
        .navigationSubtitle(subtitle)
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button { Task { await backend.refresh() } } label: {
                    Label("Refresh", systemImage: "arrow.clockwise")
                }
            }
        }
        .safeAreaInset(edge: .bottom) { quickAdd }
    }

    private var subtitle: String {
        guard let h = backend.state?.health else { return "" }
        return "\(h.items) items · \(h.receiptLines) receipt lines"
    }

    private var actions: [(String, String)] {
        pane == .soon ? [("out", "Used it"), ("binned", "Binned it")]
                         : [("have", "Already have it")]
    }

    private var quickAdd: some View {
        HStack(spacing: 8) {
            Image(systemName: "plus.circle").foregroundStyle(.secondary)
            TextField("Add what you bought — 2 milk, bread, 6 eggs", text: $note)
                .textFieldStyle(.plain)
                .onSubmit {
                    let text = note.trimmingCharacters(in: .whitespaces)
                    guard !text.isEmpty else { return }
                    note = ""
                    Task { await backend.addNote(text) }
                }
            if let pending = backend.state?.pendingCaptures, pending > 0 {
                Text("\(pending) waiting to be read")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .background(.bar)
    }
}

struct ItemRow: View {
    @Environment(Backend.self) private var backend
    let item: Item
    let actions: [(String, String)]
    @State private var hovering = false

    var body: some View {
        HStack(spacing: 14) {
            VStack(alignment: .leading, spacing: 2) {
                Text(item.name).font(.body.weight(.medium))
                Text(item.reasoning).font(.caption).foregroundStyle(.secondary)
            }
            Spacer(minLength: 12)

            if let position = item.cyclePosition {
                CycleGauge(position: position)
                    .help("\(Int(position * 100))% through its usual repurchase cycle")
            }
            Text(figure)
                .font(.callout.weight(.semibold).monospacedDigit())
                .foregroundStyle(overdue ? .red : .secondary)
                .frame(minWidth: 46, alignment: .trailing)

            ForEach(actions, id: \.0) { action, label in
                Button(label) { Task { await backend.act(action, on: item.name) } }
                    .buttonStyle(.borderless)
                    .controlSize(.small)
            }
            .opacity(hovering ? 1 : 0.55)
        }
        .padding(.vertical, 5)
        .onHover { hovering = $0 }
    }

    private var overdue: Bool {
        (item.daysOver ?? 0) > 0 || item.confirmed == true || (item.daysLeft ?? 1) <= 0
    }

    private var figure: String {
        if let left = item.daysLeft { return left <= 0 ? "today" : "in \(left)d" }
        if item.confirmed == true { return "now" }
        guard let over = item.daysOver else { return "—" }
        return over > 0 ? "+\(over)d" : "in \(-over)d"
    }
}

/// Where an item sits in its own repurchase cycle. The track runs to one and a
/// half cycles so the notch at two thirds is "due" and overshoot is visible
/// past it, rather than merely asserted.
struct CycleGauge: View {
    let position: Double
    private let cycles = 1.5

    var body: some View {
        GeometryReader { geo in
            let width = geo.size.width
            ZStack(alignment: .leading) {
                Capsule().fill(.quaternary)
                Capsule()
                    .fill(position > 1 ? Color.red : Color.secondary)
                    .frame(width: width * min(position, cycles) / cycles)
                Rectangle()
                    .fill(.background)
                    .frame(width: 1.5)
                    .offset(x: width / cycles)
            }
        }
        .frame(width: 84, height: 5)
    }
}

/// Reference, not a to-do list: a real table, sortable, dense enough to scan.
struct HouseView: View {
    @Environment(Backend.self) private var backend
    let items: [Item]
    @State private var order = [KeyPathComparator(\Item.name)]

    var body: some View {
        Table(items.sorted(using: order), sortOrder: $order) {
            TableColumn("Item", value: \.name) { item in
                Text(item.name).font(.body.weight(.medium))
            }
            .width(min: 150, ideal: 220)

            TableColumn("Reasoning") { item in
                Text(item.reasoning).foregroundStyle(.secondary)
            }
            .width(min: 160, ideal: 240)

            TableColumn("Confidence") { item in
                Text(item.confidence?.capitalized ?? "—").foregroundStyle(.secondary)
            }
            .width(90)

            TableColumn("Due") { item in
                Text(item.daysOver.map { $0 > 0 ? "+\($0)d" : "in \(-$0)d" } ?? "—")
                    .monospacedDigit()
            }
            .width(66)

            TableColumn("") { item in
                HStack(spacing: 6) {
                    Button("Still have it") { Task { await backend.act("have", on: item.name) } }
                    Button("Out of it") { Task { await backend.act("out", on: item.name) } }
                }
                .buttonStyle(.borderless)
                .controlSize(.small)
            }
            .width(min: 150, ideal: 160)
        }
        .navigationTitle("In the house")
        .navigationSubtitle("\(items.count) items · estimates, not facts")
    }
}
