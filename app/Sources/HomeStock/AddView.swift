import AppKit
import CoreImage.CIFilterBuiltins
import SwiftUI
import UniformTypeIdentifiers

/// Getting things in. Receipt email only ever sees online and delivery orders,
/// so buying milk in a corner shop has to have a door too — and everything
/// lands in the same inbox for an agent to read, rather than going straight
/// into stock. Capture must be instant; reading needs a model.
struct AddView: View {
    @Environment(Backend.self) private var backend

    @State private var note = ""
    @State private var barcode = ""
    @State private var message: String?
    @State private var failed = false
    @State private var dropTargeted = false
    @State private var showingImporter = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                receiptDrop
                typed
                phone
            }
            .padding(22)
            .frame(maxWidth: 620, alignment: .leading)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .navigationTitle("Add")
        .navigationSubtitle(pendingLabel)
        .fileImporter(isPresented: $showingImporter,
                      allowedContentTypes: [.image],
                      allowsMultipleSelection: false) { result in
            if case .success(let urls) = result, let url = urls.first { send(photo: url) }
        }
        .safeAreaInset(edge: .bottom) {
            if let message {
                Label(message, systemImage: failed ? "exclamationmark.triangle.fill" : "checkmark.circle.fill")
                    .foregroundStyle(failed ? .orange : .green)
                    .font(.callout)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 22).padding(.vertical, 10)
                    .background(.bar)
            }
        }
    }

    private var pendingLabel: String {
        let n = backend.state?.pendingCaptures ?? 0
        return n == 0 ? "Nothing waiting to be read"
                      : "\(n) waiting for your agent to read"
    }

    // MARK: - Receipt photo

    private var receiptDrop: some View {
        GroupBox {
            VStack(spacing: 12) {
                Image(systemName: "doc.viewfinder")
                    .font(.system(size: 30, weight: .light))
                    .foregroundStyle(dropTargeted ? Color.accentColor : .secondary)
                Text("Drop a photo of a receipt here")
                    .font(.callout.weight(.medium))
                Text("The door that covers shopping in an actual shop.")
                    .font(.caption).foregroundStyle(.secondary)
                Button("Choose a file…") { showingImporter = true }
                    .controlSize(.small)
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 20)
            .background(dropTargeted ? Color.accentColor.opacity(0.07) : .clear)
            .overlay(
                RoundedRectangle(cornerRadius: 8)
                    .strokeBorder(dropTargeted ? Color.accentColor : Color.secondary.opacity(0.35),
                                  style: StrokeStyle(lineWidth: 1.5, dash: [6, 4]))
            )
            .onDrop(of: [.fileURL], isTargeted: $dropTargeted) { providers in
                guard let provider = providers.first else { return false }
                _ = provider.loadObject(ofClass: URL.self) { url, _ in
                    if let url { Task { @MainActor in send(photo: url) } }
                }
                return true
            }
        } label: {
            Label("Photograph a receipt", systemImage: "camera")
        }
    }

    // MARK: - Typed and scanned

    private var typed: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 12) {
                LabeledContent("Barcode") {
                    HStack {
                        TextField("scan or type the number", text: $barcode)
                            .onSubmit(sendBarcode)
                        Button("Add", action: sendBarcode)
                            .disabled(barcode.trimmingCharacters(in: .whitespaces).isEmpty)
                    }
                }
                Divider()
                LabeledContent("Just type it") {
                    HStack {
                        TextField("2 milk, bread, 6 eggs", text: $note)
                            .onSubmit(sendNote)
                        Button("Add", action: sendNote)
                            .disabled(note.trimmingCharacters(in: .whitespaces).isEmpty)
                    }
                }
            }
            .padding(.vertical, 4)
        } label: {
            Label("Barcode or a note", systemImage: "keyboard")
        }
    }

    // MARK: - Phone

    private var phone: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 14) {
                Toggle(isOn: Binding(
                    get: { backend.lanMode },
                    set: { on in Task { await backend.setLAN(on) } }
                )) {
                    Text("Let my phone add things")
                    Text("Binds this Mac to your wifi so a phone can reach it. Off by default, and off again every launch.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                .toggleStyle(.switch)

                if backend.lanMode {
                    if let pairing = backend.pairing {
                        HStack(alignment: .top, spacing: 18) {
                            QRCode(text: pairing.pairingURL)
                                .frame(width: 132, height: 132)
                            VStack(alignment: .leading, spacing: 7) {
                                Text("Point your phone's camera at this")
                                    .font(.callout.weight(.medium))
                                Text("Or open \(pairing.url) and enter")
                                    .font(.caption).foregroundStyle(.secondary)
                                Text(spaced(pairing.code))
                                    .font(.title2.monospaced().weight(.semibold))
                                    .textSelection(.enabled)
                                Text("Expires in \(max(pairing.expiresIn / 60, 0)) min. Photos go straight to this Mac.")
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                        }
                    } else {
                        ProgressView().controlSize(.small)
                    }
                }
            }
            .padding(.vertical, 4)
        } label: {
            Label("Capture from your phone", systemImage: "iphone")
        }
    }

    private func spaced(_ code: String) -> String {
        code.count == 6 ? "\(code.prefix(3)) \(code.suffix(3))" : code
    }

    // MARK: - Sending

    private func sendNote() {
        let text = note.trimmingCharacters(in: .whitespaces)
        guard !text.isEmpty else { return }
        note = ""
        Task {
            await backend.addNote(text)
            show("Added to the inbox. Your agent will read it.", failed: false)
        }
    }

    private func sendBarcode() {
        let code = barcode.trimmingCharacters(in: .whitespaces)
        guard !code.isEmpty else { return }
        barcode = ""
        Task {
            do {
                try await backend.addBarcode(code)
                show("Barcode queued for reading.", failed: false)
            } catch { show(error.localizedDescription, failed: true) }
        }
    }

    private func send(photo url: URL) {
        Task {
            do {
                try await backend.addPhoto(url)
                show("Receipt queued for reading.", failed: false)
            } catch { show(error.localizedDescription, failed: true) }
        }
    }

    private func show(_ text: String, failed: Bool) {
        message = text
        self.failed = failed
        Task {
            try? await Task.sleep(nanoseconds: 4_000_000_000)
            if message == text { message = nil }
        }
    }
}

/// Generated on the fly with CoreImage — no dependency, and no image fetched
/// from anywhere, which matters for a product whose claim is that nothing
/// leaves the machine.
struct QRCode: View {
    let text: String

    var body: some View {
        if let image { Image(nsImage: image).interpolation(.none).resizable() }
        else { Color.clear }
    }

    private var image: NSImage? {
        let filter = CIFilter.qrCodeGenerator()
        filter.message = Data(text.utf8)
        filter.correctionLevel = "M"
        guard let output = filter.outputImage else { return nil }
        let scaled = output.transformed(by: CGAffineTransform(scaleX: 10, y: 10))
        let context = CIContext()
        guard let cg = context.createCGImage(scaled, from: scaled.extent) else { return nil }
        return NSImage(cgImage: cg, size: NSSize(width: scaled.extent.width,
                                                 height: scaled.extent.height))
    }
}
