import SwiftUI

struct SettingsView: View {
    @Environment(Backend.self) private var backend
    @AppStorage("provider") private var providerID = "ollama"
    @AppStorage("model") private var model = ""
    @State private var apiKey = ""

    private var provider: Provider? { backend.providers.first { $0.id == providerID } }

    var body: some View {
        Form {
            Section("Model") {
                Picker("Provider", selection: $providerID) {
                    ForEach(backend.providers) { Text($0.label).tag($0.id) }
                }
                TextField("Model", text: $model,
                          prompt: Text(provider?.defaultModel ?? ""))

                if provider?.needsKey == true {
                    SecureField("API key", text: $apiKey)
                        .onSubmit { save() }
                    HStack {
                        Spacer()
                        Button("Save key", action: save)
                            .disabled(apiKey.isEmpty)
                    }
                }
            }

            // Stated per provider, in the settings screen, rather than buried
            // in a policy page nobody opens.
            if let provider {
                Section("Privacy") {
                    Label {
                        Text(provider.note)
                    } icon: {
                        Image(systemName: provider.leavesMachine ? "globe" : "lock.laptopcomputer")
                            .foregroundStyle(provider.leavesMachine ? .orange : .green)
                    }
                    if provider.leavesMachine {
                        Text("Your inventory stays on this Mac. Only the messages you send, and the kitchen data the model asks for, are transmitted.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
            }

            Section("Data") {
                LabeledContent("Database", value: backend.state?.health.dbPath ?? "—")
                    .textSelection(.enabled)
                    .lineLimit(1)
                    .truncationMode(.middle)
                Button("Reveal in Finder") {
                    NSWorkspace.shared.activateFileViewerSelecting([Backend.databasePath])
                }
            }
        }
        .formStyle(.grouped)
        .frame(width: 480)
        .fixedSize(horizontal: false, vertical: true)
        .onAppear { apiKey = provider.flatMap { Keychain.key(for: $0.id) } ?? "" }
        .onChange(of: providerID) {
            apiKey = provider.flatMap { Keychain.key(for: $0.id) } ?? ""
            model = ""
        }
    }

    private func save() {
        guard let provider else { return }
        Keychain.set(apiKey, for: provider.id)
    }
}
