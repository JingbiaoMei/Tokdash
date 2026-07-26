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
        var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false)
        let base = components?.path.hasSuffix("/") ?? false
            ? String(components!.path.dropLast())
            : (components?.path ?? "")
        components?.path = base + path
        guard let url = components?.url else {
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

struct HealthResponse: Decodable, Equatable {
    let status: String
    let service: String
    let version: String
}

struct UsageResponse: Decodable {
    let period: String
    let totalTokens: Int
    let totalCost: Double
    let totalMessages: Int
    let byTool: [String: ToolAgg]?
    let topModels: [ModelAgg]?
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
        case combinedModels = "combined_models"
        case comparison
        case timestamp
        case responseCache = "response_cache"
    }
}

struct ToolAgg: Decodable {
    let tokens: Int
    let cost: Double
}

struct ModelAgg: Decodable {
    let name: String
    let tokens: Int
    let cost: Double
}

struct Comparison: Decodable {
    let tokensPct: Double?
    let costPct: Double?
    let messagesPct: Double?

    enum CodingKeys: String, CodingKey {
        case tokensPct = "tokens_pct"
        case costPct = "cost_pct"
        case messagesPct = "messages_pct"
    }
}

struct CacheInfo: Decodable {
    let ageSeconds: Int?
    enum CodingKeys: String, CodingKey { case ageSeconds = "age_seconds" }
}

struct QuotaResponse: Decodable {
    let enabled: Bool
    let providers: [String: ProviderQuota]?
    let timestamp: Int?
}

struct ProviderQuota: Decodable {
    let estimated: Bool?
    let buckets: [BucketQuota]?
}

struct BucketQuota: Decodable {
    let bucket: String
    let bucketLabel: String?
    let remainingPercent: Double?
    let resetsAt: Int?
    let account: String?

    enum CodingKeys: String, CodingKey {
        case bucket
        case bucketLabel = "bucket_label"
        case remainingPercent = "remaining_percent"
        case resetsAt = "resets_at"
        case account
    }
}
