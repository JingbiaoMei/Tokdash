import Foundation

/// Tokdash API client. Read-only; never writes, never polls providers.
///
/// All network happens off the main actor; the companion store applies results
/// on the main actor. Decoding is additive: unknown fields are ignored and
/// absent optional fields are tolerated.
actor TokdashClient {
    private let session: URLSession
    private var baseURL: URL

    init(baseURL: URL) {
        self.baseURL = baseURL
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 15
        config.timeoutIntervalForResource = 30
        config.waitsForConnectivity = false
        self.session = URLSession(configuration: config)
    }

    func updateBaseURL(_ url: URL) {
        baseURL = url
    }

    // MARK: - Endpoints

    func health() async throws -> HealthResponse {
        try await get("/health", timeout: 5)
    }

    func usage(period: String) async throws -> UsageResponse {
        try await get("/api/usage?period=\(period)", timeout: 20)
    }

    func quota() async throws -> QuotaResponse {
        try await get("/api/quota", timeout: 20)
    }

    // MARK: - Core

    private func get<T: Decodable>(_ path: String, timeout: TimeInterval) async throws -> T {
        guard let url = Self.buildURL(baseURL: baseURL, path: path) else {
            throw TokdashError.badBaseURL
        }
        var request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: timeout)
        // Native client: never send a browser Origin header.
        request.setValue(nil, forHTTPHeaderField: "Origin")
        do {
            let (data, response) = try await session.data(for: request)
            guard let http = response as? HTTPURLResponse else {
                throw TokdashError.badResponse
            }
            if http.statusCode == 503 {
                throw TokdashError.busy
            }
            guard (200..<300).contains(http.statusCode) else {
                throw TokdashError.httpStatus(http.statusCode)
            }
            let decoder = JSONDecoder()
            decoder.dateDecodingStrategy = .iso8601
            do {
                return try decoder.decode(T.self, from: data)
            } catch {
                throw TokdashError.decode(error)
            }
        } catch let error as TokdashError {
            throw error
        } catch let error as URLError where error.code == .timedOut {
            throw TokdashError.timeout
        } catch let error as URLError where error.code == .cannotConnectToHost || error.code == .cannotFindHost || error.code == .networkConnectionLost {
            throw TokdashError.offline
        } catch {
            throw TokdashError.other(error)
        }
    }

    /// Build a request URL by joining `path` (which may include a `?query`) onto the
    /// base URL, keeping the query out of the path. Pure/testable.
    nonisolated static func buildURL(baseURL: URL, path: String) -> URL? {
        var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false)
        let basePath = components?.path ?? ""
        let trimmed = basePath.hasSuffix("/") ? String(basePath.dropLast()) : basePath
        let parts = path.split(separator: "?", maxSplits: 1, omittingEmptySubsequences: false)
        components?.path = trimmed + String(parts[0])
        if parts.count > 1 { components?.query = String(parts[1]) }
        else { components?.query = nil }
        return components?.url
    }
}

enum TokdashError: Error, Equatable {
    case badBaseURL
    case badResponse
    case timeout
    case offline
    case busy
    case httpStatus(Int)
    case decode(Error)
    case other(Error)

    static func == (lhs: TokdashError, rhs: TokdashError) -> Bool {
        switch (lhs, rhs) {
        case (.badBaseURL, .badBaseURL), (.badResponse, .badResponse),
             (.timeout, .timeout), (.offline, .offline), (.busy, .busy):
            return true
        case (.httpStatus(let a), .httpStatus(let b)):
            return a == b
        default:
            return false
        }
    }
}

// MARK: - DTOs (additive decoding; unknown fields ignored)

struct HealthResponse: Decodable, Equatable, Sendable {
    let status: String
    let service: String
    let version: String
}

struct UsageResponse: Decodable, Sendable {
    let period: String
    let totalTokens: Int
    let totalCost: Double
    let totalMessages: Int
    let byTool: [String: ToolAgg]?
    let topModels: [ModelAgg]?
    let topModelsByCost: [ModelAgg]?
    let combinedModels: [ModelAgg]?
    let comparison: Comparison?
    let timestamp: String?
    let responseCache: CacheInfo?

    enum CodingKeys: String, CodingKey {
        case period
        case totalTokens = "total_tokens"
        case totalCost = "total_cost"
        case totalMessages = "total_messages"
        case byTool = "by_tool"
        case topModels = "top_models"
        case topModelsByCost = "top_models_by_cost"
        case combinedModels = "combined_models"
        case comparison
        case timestamp
        case responseCache = "response_cache"
    }

    init(period: String = "", totalTokens: Int = 0, totalCost: Double = 0,
         totalMessages: Int = 0, byTool: [String: ToolAgg]? = nil,
         topModels: [ModelAgg]? = nil, topModelsByCost: [ModelAgg]? = nil,
         combinedModels: [ModelAgg]? = nil,
         comparison: Comparison? = nil, timestamp: String? = nil,
         responseCache: CacheInfo? = nil) {
        self.period = period; self.totalTokens = totalTokens; self.totalCost = totalCost
        self.totalMessages = totalMessages; self.byTool = byTool; self.topModels = topModels
        self.topModelsByCost = topModelsByCost
        self.combinedModels = combinedModels; self.comparison = comparison
        self.timestamp = timestamp; self.responseCache = responseCache
    }
}

struct ToolAgg: Decodable, Sendable {
    let tokens: Int
    let cost: Double
}

struct ModelAgg: Decodable, Sendable {
    let name: String
    let tokens: Int
    let cost: Double
}

struct Comparison: Decodable, Sendable {
    let tokensPct: Double?
    let costPct: Double?
    let messagesPct: Double?
    let costPrev: Double?

    enum CodingKeys: String, CodingKey {
        case tokensPct = "tokens_pct"
        case costPct = "cost_pct"
        case messagesPct = "messages_pct"
        case costPrev = "cost_prev"
    }

    init(tokensPct: Double? = nil, costPct: Double? = nil, messagesPct: Double? = nil, costPrev: Double? = nil) {
        self.tokensPct = tokensPct
        self.costPct = costPct
        self.messagesPct = messagesPct
        self.costPrev = costPrev
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        tokensPct = try values.decodeIfPresent(Double.self, forKey: .tokensPct)
        costPct = try values.decodeIfPresent(Double.self, forKey: .costPct)
        messagesPct = try values.decodeIfPresent(Double.self, forKey: .messagesPct)
        costPrev = try values.decodeIfPresent(Double.self, forKey: .costPrev)
    }
}

struct CacheInfo: Decodable, Sendable {
    // The server emits age_seconds as a float (e.g. 454.18947); decoding as Int
    // throws and fails the whole usage response on cached requests.
    let ageSeconds: Double?
    enum CodingKeys: String, CodingKey { case ageSeconds = "age_seconds" }
}

struct QuotaResponse: Decodable, Sendable {
    let enabled: Bool
    let providers: [String: ProviderQuota]?
    let timestamp: Int?
}

struct ProviderQuota: Decodable, Sendable {
    let estimated: Bool?
    let buckets: [BucketQuota]?
    // "ok" (or absent) is healthy; anything else means the provider's quota couldn't
    // be refreshed and its buckets are last-known. Spec §7.
    let status: String?
    let statusDetail: String?
    // Epoch seconds the failure status was observed. This is the newest error of ANY
    // account behind the card, so it drives the GROUP warning; rows compare against their
    // own account's statusAt in `accounts` where that is present. Spec §7.
    let statusAt: Int?
    // One entry per credential, present only on a card measuring more than one (a
    // ~/.claude install beside a ~/.claude-<profile> sibling, MiniMax global + CN).
    // Absent for single-credential providers and for every pre-`accounts` server. Spec §7.
    let accounts: [AccountQuota]?

    enum CodingKeys: String, CodingKey {
        case estimated, buckets, status, accounts
        case statusDetail = "status_detail"
        case statusAt = "status_at"
    }

    // Explicit memberwise init (with defaults) so test construction with status/
    // statusDetail resolves; Decodable's init(from:) is still synthesized.
    init(estimated: Bool? = nil, buckets: [BucketQuota]? = nil, status: String? = nil,
         statusDetail: String? = nil, statusAt: Int? = nil, accounts: [AccountQuota]? = nil) {
        self.estimated = estimated
        self.buckets = buckets
        self.status = status
        self.statusDetail = statusDetail
        self.statusAt = statusAt
        self.accounts = accounts
    }
}

/// One credential behind a provider card, with the failure that belongs to it alone.
///
/// `status` is NOT a verdict on its own: the server takes it from the last stored row it
/// iterated and rows arrive ordered by bucket id, so an install with windows reports
/// `status: "ok"` beside a live `statusDetail`. Failure is read the same way as for a
/// group — status present and not "ok", OR a non-empty statusDetail. Spec §7.
struct AccountQuota: Decodable, Sendable {
    let account: String?
    let plan: String?
    let status: String?
    let statusDetail: String?
    let statusAt: Int?

    enum CodingKeys: String, CodingKey {
        case account, plan, status
        case statusDetail = "status_detail"
        case statusAt = "status_at"
    }

    init(account: String? = nil, plan: String? = nil, status: String? = nil,
         statusDetail: String? = nil, statusAt: Int? = nil) {
        self.account = account
        self.plan = plan
        self.status = status
        self.statusDetail = statusDetail
        self.statusAt = statusAt
    }
}

struct BucketQuota: Decodable, Sendable {
    let bucket: String
    let bucketLabel: String?
    let remainingPercent: Double?
    let resetsAt: Int?
    let account: String?
    // Epoch seconds this window was observed. Older than the provider's statusAt means
    // the failure is newer than the data, i.e. this row is last-known. Spec §7.
    let capturedAt: Int?

    enum CodingKeys: String, CodingKey {
        case bucket
        case bucketLabel = "bucket_label"
        case remainingPercent = "remaining_percent"
        case resetsAt = "resets_at"
        case capturedAt = "captured_at"
        case account
    }

    // Explicit memberwise init (with defaults) so test construction without `capturedAt`
    // resolves; Decodable's init(from:) is still synthesized.
    init(bucket: String, bucketLabel: String? = nil, remainingPercent: Double? = nil,
         resetsAt: Int? = nil, account: String? = nil, capturedAt: Int? = nil) {
        self.bucket = bucket
        self.bucketLabel = bucketLabel
        self.remainingPercent = remainingPercent
        self.resetsAt = resetsAt
        self.account = account
        self.capturedAt = capturedAt
    }
}

extension UsageResponse {
    /// Sentinel for a section that has never fetched successfully.
    static let empty = UsageResponse(
        period: "", totalTokens: 0, totalCost: 0, totalMessages: 0,
        byTool: nil, topModels: nil, topModelsByCost: nil, combinedModels: nil,
        comparison: nil, timestamp: nil, responseCache: nil
    )
}

extension QuotaResponse {
    static let empty = QuotaResponse(enabled: false, providers: nil, timestamp: nil)
}
