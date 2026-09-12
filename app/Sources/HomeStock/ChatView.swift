import SwiftUI

struct ChatView: View {
    @Environment(Backend.self) private var backend
    @AppStorage("provider") private var providerID = "ollama"
    @AppStorage("model") private var model = ""
    @AppStorage("allowWrites") private var allowWrites = true

    @State private var messages: [Message] = []
    @State private var draft = ""
    @State private var thinking = false

    private var provider: Provider? {
        backend.providers.first { $0.id == providerID } ?? backend.providers.first
    }

    var body: some View {
        VStack(spacing: 0) {
            if messages.isEmpty { intro } else { transcript }
            Divider()
            composer
        }
        .navigationTitle("Chat")
        .navigationSubtitle(provider.map { $0.leavesMachine ? $0.label : "\($0.label) · nothing leaves this Mac" } ?? "")
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Picker("Model provider", selection: $providerID) {
                    ForEach(backend.providers) { p in
                        Text(p.label).tag(p.id)
                    }
                }
                .pickerStyle(.menu)
                .labelsHidden()
            }
        }
    }

    private var intro: some View {
        ContentUnavailableView {
            Label("Ask about your kitchen", systemImage: "bubble.left.and.text.bubble.right")
        } description: {
            Text("It can read your stock, your shopping list and what is about to go off — and record what you tell it.")
        } actions: {
            VStack(alignment: .leading, spacing: 6) {
                ForEach(["What can I make for dinner tonight?",
                         "We're out of milk.",
                         "What am I about to waste?"], id: \.self) { suggestion in
                    Button(suggestion) { draft = suggestion; send() }
                        .buttonStyle(.link)
                }
            }
        }
    }

    private var transcript: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 16) {
                    ForEach($messages) { $message in
                        MessageRow(message: $message).id(message.id)
                    }
                    if thinking {
                        HStack(spacing: 8) {
                            ProgressView().controlSize(.small)
                            Text("Thinking…").font(.callout).foregroundStyle(.secondary)
                        }
                    }
                }
                .padding(20)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .onChange(of: messages.count) {
                if let last = messages.last { proxy.scrollTo(last.id, anchor: .bottom) }
            }
        }
    }

    private var composer: some View {
        HStack(spacing: 10) {
            TextField("Ask about your kitchen…", text: $draft, axis: .vertical)
                .textFieldStyle(.plain)
                .lineLimit(1...5)
                .onSubmit(send)
            Button(action: send) {
                Image(systemName: "arrow.up.circle.fill").font(.title2)
            }
            .buttonStyle(.borderless)
            .disabled(draft.trimmingCharacters(in: .whitespaces).isEmpty || thinking)
            .keyboardShortcut(.return, modifiers: [])
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
        .background(.bar)
    }

    private func send() {
        let text = draft.trimmingCharacters(in: .whitespaces)
        guard !text.isEmpty, !thinking, let provider else { return }
        draft = ""
        messages.append(Message(role: .user, text: text))
        thinking = true

        Task {
            defer { thinking = false }
            do {
                let key = provider.needsKey ? Keychain.key(for: provider.id) : nil
                let reply = try await backend.send(history: messages, provider: provider,
                                                   model: model, key: key,
                                                   allowWrites: allowWrites)
                messages.append(Message(role: .assistant, text: reply.reply,
                                        toolCalls: reply.toolCalls, undo: reply.undo))
                // A turn that wrote to the event log changes what every other
                // screen should show.
                if reply.toolCalls.contains(where: \.wrote) { await backend.refresh() }
            } catch {
                messages.append(Message(role: .failure, text: error.localizedDescription))
            }
        }
    }
}

struct MessageRow: View {
    @Environment(Backend.self) private var backend
    @Binding var message: Message

    var body: some View {
        switch message.role {
        case .user:
            HStack {
                Spacer(minLength: 60)
                Text(message.text)
                    .textSelection(.enabled)
                    .padding(.horizontal, 13).padding(.vertical, 9)
                    .background(Color.accentColor, in: .rect(cornerRadius: 13))
                    .foregroundStyle(.white)
            }
        case .assistant:
            VStack(alignment: .leading, spacing: 7) {
                // Same promise the pantry window makes about every number it
                // prints: you can see what it looked at to get here.
                if !message.toolCalls.isEmpty { ToolCallStrip(calls: message.toolCalls) }
                Text(message.text).textSelection(.enabled)
                // A small model asked a read-only question will still sometimes
                // reach for a destructive tool, so anything a turn wrote can be
                // withdrawn in one click.
                if !message.undo.isEmpty {
                    HStack(spacing: 8) {
                        Image(systemName: message.undone ? "arrow.uturn.backward.circle.fill"
                                                         : "square.and.pencil")
                        Text(message.undone
                             ? "Undone — your kitchen is back as it was."
                             : "This changed your kitchen.")
                        if !message.undone {
                            Button("Undo") {
                                let ids = message.undo
                                message.undone = true
                                Task { await backend.undo(ids) }
                            }
                            .buttonStyle(.link)
                        }
                    }
                    .font(.caption)
                    .foregroundStyle(message.undone ? AnyShapeStyle(.secondary) : AnyShapeStyle(Color.orange))
                }
            }
        case .failure:
            Label(message.text, systemImage: "exclamationmark.triangle.fill")
                .foregroundStyle(.orange)
                .padding(.horizontal, 12).padding(.vertical, 9)
                .background(.quaternary, in: .rect(cornerRadius: 10))
        }
    }
}

struct ToolCallStrip: View {
    let calls: [ToolCall]

    var body: some View {
        HStack(spacing: 6) {
            ForEach(Array(calls.enumerated()), id: \.offset) { _, call in
                Label(call.name, systemImage: call.wrote ? "square.and.pencil" : "magnifyingglass")
                    .font(.caption.monospaced())
                    .padding(.horizontal, 7).padding(.vertical, 3)
                    .background(.quaternary, in: .capsule)
                    .foregroundStyle(call.wrote ? .primary : .secondary)
            }
        }
    }
}
