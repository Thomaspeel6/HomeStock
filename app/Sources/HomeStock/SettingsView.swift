import SwiftUI

struct SettingsView: View {
    @Environment(Backend.self) private var backend
    @AppStorage("provider") private var providerID = "ollama"
    @AppStorage("model") private var model = ""
    @State private var apiKey = ""
    @State private var models: [String] = []
    @State private var loadingModels = false

    private var provider: Provider? { backend.providers.first { $0.id == providerID } }

    var body: some View {
        Form {
            Section("Model") {
                Picker("Provider", selection: $providerID) {
                    ForEach(backend.providers) { Text($0.label).tag($0.id) }
                }
                // A free-text box meant typing a model name blind and finding
                // out it was wrong only when a message failed.
                if models.isEmpty {
                    LabeledContent("Model") {
                        HStack(spacing: 8) {
                            if loadingModels {
                                ProgressView().controlSize(.small)
                            } else {
                                Text(needsKeyFirst ? "Add a key, then load models"
                                                   : "No models found")
                                    .foregroundStyle(.secondary)
                            }
                            Button("Reload", action: loadModels)
                                .controlSize(.small)
                                .disabled(loadingModels || needsKeyFirst)
                        }
                    }
                } else {
                    Picker("Model", selection: $model) {
                        Text("Choose…").tag("")
                        ForEach(models, id: \.self) { Text($0).tag($0) }
                    }
                }
                if let hint = provider?.hint, models.isEmpty {
                    Text(hint).font(.caption).foregroundStyle(.secondary)
                }

                if provider?.needsKey == true {
                    SecureField("API key", text: $apiKey)
                        .onSubmit { save() }
                    HStack {
                        Spacer()
                        Button("Save key and load models") {
                            save()
                            loadModels()
                        }
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
        .onAppear {
            apiKey = provider.flatMap { Keychain.key(for: $0.id) } ?? ""
            loadModels()
        }
        .onChange(of: providerID) {
            apiKey = provider.flatMap { Keychain.key(for: $0.id) } ?? ""
            model = ""
            models = []
            loadModels()
        }
        .onChange(of: backend.providers.count) { loadModels() }
    }

    private var needsKeyFirst: Bool {
        provider?.needsKey == true && apiKey.isEmpty
    }

    private func save() {
        guard let provider else { return }
        Keychain.set(apiKey, for: provider.id)
    }

    private func loadModels() {
        guard let provider, !needsKeyFirst else { models = []; return }
        loadingModels = true
        Task {
            let found = await backend.models(for: provider, key: apiKey)
            models = found
            loadingModels = false
            // Nothing chosen yet, or a choice this provider cannot serve.
            if !found.contains(model) { model = found.first ?? "" }
        }
    }
}
