import Foundation

// Mirrors /api/state. Every figure here is computed by the Python engine —
// nothing in this app does arithmetic on stock, because two implementations
// would mean the window and an agent could disagree about the same item.
struct KitchenState: Decodable {
    var health: Health
    var order: [Item]
    var expiring: [Item]
    var shelf: [Item]
    var pendingCaptures: Int

    enum CodingKeys: String, CodingKey {
        case health, order, expiring, shelf
        case pendingCaptures = "pending_captures"
    }
}

struct Health: Decodable {
    var items: Int
    var receiptLines: Int
    var lastIngestAt: String?
    var daysSinceIngest: Int?
    var stale: Bool
    var dbPath: String

    enum CodingKeys: String, CodingKey {
        case items, stale
        case receiptLines = "receipt_lines"
        case lastIngestAt = "last_ingest_at"
        case daysSinceIngest = "days_since_ingest"
        case dbPath = "db_path"
    }
}

struct Item: Decodable, Identifiable, Hashable {
    var name: String
    var estimatedState: String?
    var confidence: String?
    var confirmed: Bool?
    var correctedQuantity: Double?
    var daysSinceObservation: Int?
    var daysSinceLastPurchase: Int?
    var medianIntervalDays: Double?
    var cyclePosition: Double?
    var daysOver: Int?
    var shelfLifeDays: Int?
    var storage: String?
    var daysLeft: Int?

    var id: String { name }

    enum CodingKeys: String, CodingKey {
        case name, confidence, confirmed, storage
        case estimatedState = "estimated_state"
        case correctedQuantity = "corrected_quantity"
        case daysSinceObservation = "days_since_observation"
        case daysSinceLastPurchase = "days_since_last_purchase"
        case medianIntervalDays = "median_interval_days"
        case cyclePosition = "cycle_position"
        case daysOver = "days_over"
        case shelfLifeDays = "shelf_life_days"
        case daysLeft = "days_left"
    }

    /// The one-line reasoning shown under every name. HomeStock's rule is that
    /// an estimate you cannot interrogate is not worth showing.
    var reasoning: String {
        if let d = daysLeft, let life = shelfLifeDays {
            let place = storage.map { " in the \($0)" } ?? ""
            return d <= 0 ? "past its use-by — keeps about \(life)d\(place)"
                          : "keeps about \(life)d\(place)"
        }
        if confirmed == true {
            return correctedQuantity == 0 ? "you said you were out" : "you confirmed this"
        }
        if let m = medianIntervalDays, let since = daysSinceObservation {
            return "every \(Int(m.rounded()))d · last \(since)d ago"
        }
        if let d = daysSinceLastPurchase { return "bought once, \(d)d ago" }
        return "no pattern yet"
    }
}

struct Provider: Decodable, Identifiable, Hashable {
    var id: String
    var label: String
    var needsKey: Bool
    var defaultModel: String
    var leavesMachine: Bool
    var note: String
    var baseUrl: String

    enum CodingKeys: String, CodingKey {
        case id, label, note
        case needsKey = "needs_key"
        case defaultModel = "default_model"
        case leavesMachine = "leaves_machine"
        case baseUrl = "base_url"
    }
}

struct ToolCall: Decodable, Hashable {
    var name: String
    var wrote: Bool
}

struct ChatReply: Decodable {
    var reply: String
    var toolCalls: [ToolCall]
    enum CodingKeys: String, CodingKey {
        case reply
        case toolCalls = "tool_calls"
    }
}

struct Message: Identifiable, Hashable {
    enum Role { case user, assistant, failure }
    let id = UUID()
    var role: Role
    var text: String
    var toolCalls: [ToolCall] = []
}
