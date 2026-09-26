import Foundation

/// Tokdash API client. Read-only; never writes, never polls providers.
///
/// All network happens off the main actor; the companion store applies results
/// on the main actor. Decoding is additive: unknown fields are ignored and
/// absent optional fields are tolerated.
actor TokdashClient {
    private let session: URLSession
    private var baseURL: URL
    private var server: CompanionServerSettings?
    private var verifiedRoutes: [URL] = []
    private(set) var routeStatus: [RouteProbe] = []

    init(server: CompanionServerSettings, session: URLSession? = nil) {
        self.server = server
        self.baseURL = URL(string: server.baseURL) ?? URL(fileURLWithPath: "/")
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 15
        config.timeoutIntervalForResource = 120
        config.waitsForConnectivity = false
        self.session = session ?? URLSession(configuration: config)
    }

    func configure(_ server: CompanionServerSettings) {
        self.server = server
        baseURL = URL(string: server.baseURL) ?? URL(fileURLWithPath: "/")
        verifiedRoutes = []
    }

    nonisolated static func probe(_ address: String, session: URLSession? = nil) async -> RouteProbe {
        guard let url = URL(string: address) else { return RouteProbe(address: address, health: nil, milliseconds: 0) }
        let start = Date()
        do {
            let health = try await TokdashClient(baseURL: url, session: session).health()
            return RouteProbe(address: address, health: health.service == "tokdash" ? health : nil,
                              milliseconds: Date().timeIntervalSince(start) * 1000)
        } catch {
            return RouteProbe(address: address, health: nil, milliseconds: 0, failure: error as? TokdashError)
        }
    }

    init(baseURL: URL, session: URLSession? = nil) {
        self.baseURL = baseURL
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 15
        // Cold month/year scans server-side run tens of seconds (warm docs: year ~15 s,
        // month ~25 s + base). The per-call ceilings below need this headroom: 30 s used
        // to cut long windows off mid-parse and the view showed "unavailable".
        config.timeoutIntervalForResource = 120
        config.waitsForConnectivity = false
        self.session = session ?? URLSession(configuration: config)
    }

    func updateBaseURL(_ url: URL) {
        baseURL = url
    }

    // MARK: - Endpoints
    //
    // 90 s on the data endpoints: cold month/year scans server-side run tens of
    // seconds (warm docs: year ~15 s, month ~25 s + base), and a 20 s ceiling cut
    // them off mid-parse - the year view then read "unavailable" every cycle.

    func health() async throws -> HealthResponse {
        guard let server else { return try await get("/health", timeout: 5) }
        guard CompanionStore.isValidBaseURL(server.baseURL) else { throw TokdashError.badBaseURL }
        if server.addresses.count == 1 && server.instanceId == nil { return try await get("/health", timeout: 5) }
        let probes = await withTaskGroup(of: RouteProbe.self) { group in
            for address in server.addresses { group.addTask { await Self.probe(address, session: self.session) } }
            var result: [RouteProbe] = []
            for await probe in group { result.append(probe) }
            return result
        }
        try Task.checkCancellation()
        routeStatus = probes
        let good = Self.selectRoutes(server, probes: probes)
        guard let first = good.first, let health = first.health else {
            throw !probes.isEmpty && probes.allSatisfy { $0.failure == .busy } ? TokdashError.busy : TokdashError.offline
        }
        if self.server?.instanceId == nil { self.server?.instanceId = health.instanceId }
        verifiedRoutes = good.compactMap { URL(string: $0.address) }
        baseURL = verifiedRoutes[0]
        return health
    }

    nonisolated static func selectRoutes(_ server: CompanionServerSettings, probes: [RouteProbe]) -> [RouteProbe] {
        let identity = server.instanceId ?? probes.first { $0.address == server.addresses.first }?.health?.instanceId
        return probes.filter { probe in
            guard let health = probe.health, health.service == "tokdash" else { return false }
            if let identity, !identity.isEmpty { return health.instanceId == identity }
            return probe.address == server.addresses.first
        }.sorted {
            if ($0.address == server.preferredRoute) != ($1.address == server.preferredRoute) {
                return $0.address == server.preferredRoute
            }
            return $0.milliseconds < $1.milliseconds
        }
    }

    func usage(period: String) async throws -> UsageResponse {
        try await get("/api/usage?period=\(period)", timeout: 90)
    }

    /// Calendar-week window: `date_from`/`date_to` (local Monday .. today). `period=week`
    /// is a rolling 7-day window and must never be used for the segment. Contract §Period windows.
    func usageRange(from: String, to: String) async throws -> UsageResponse {
        try await get("/api/usage?date_from=\(from)&date_to=\(to)", timeout: 90)
    }

    func activeTime(period: String) async throws -> ActiveTimeResponse {
        try await get("/api/active-time?period=\(period)", timeout: 90)
    }

    func activeTimeRange(from: String, to: String) async throws -> ActiveTimeResponse {
        try await get("/api/active-time?date_from=\(from)&date_to=\(to)", timeout: 90)
    }

    func insightsHourlyToday() async throws -> InsightsResponse {
        try await get("/api/insights?facets=hourly&period=today", timeout: 90)
    }

    /// Hourly facet over an explicit window - the E12 stepped day (contract §Instance
    /// stepper: the hourly facet folds the window's own rows, so it is correct on a past day).
    func insightsHourlyRange(from: String, to: String) async throws -> InsightsResponse {
        try await get("/api/insights?facets=hourly&date_from=\(from)&date_to=\(to)", timeout: 90)
    }

    func insightsDaily(from: String, to: String) async throws -> InsightsResponse {
        try await get("/api/insights?facets=daily&date_from=\(from)&date_to=\(to)", timeout: 90)
    }

    func stats() async throws -> StatsResponse {
        try await get("/api/stats", timeout: 90)
    }

    func quota() async throws -> QuotaResponse {
        // Raw-data path: the All view pins "provider order as detected", and
        // Foundation's Dictionary decode loses JSON object key order - so the
        // wire order is captured from the same bytes (QuotaResponse.decode).
        let data = try await getData("/api/quota", timeout: 90)
        do {
            return try QuotaResponse.decode(from: data)
        } catch {
            // Same error contract as get(): consumers pattern-match TokdashError.
            throw TokdashError.decode(error)
        }
    }

    /// Settings-only diagnostics (contract: never the flyout, never on a schedule).
    func serverVersion() async throws -> VersionResponse {
        try await get("/api/version", timeout: 10)
    }

    /// Settings-only. Deliberately consent-gated server-side and read-only; the consent
    /// POST stays web-only, the companion never writes.
    func serverUpdateCheck() async throws -> ServerUpdateCheckResponse {
        try await get("/api/update-check", timeout: 20)
    }

    // MARK: - Core

    private func get<T: Decodable>(_ path: String, timeout: TimeInterval) async throws -> T {
        let data = try await getData(path, timeout: timeout)
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        do {
            return try decoder.decode(T.self, from: data)
        } catch {
            throw TokdashError.decode(error)
        }
    }

    private func getData(_ path: String, timeout: TimeInterval) async throws -> Data {
        var candidates = [baseURL]
        candidates.append(contentsOf: verifiedRoutes.filter { $0 != baseURL })
        for (index, candidate) in candidates.enumerated() {
            do {
                let data = try await getDataAt(candidate, path: path, timeout: timeout)
                baseURL = candidate
                return data
            } catch {
                try Task.checkCancellation()
                let retry = (error as? TokdashError) == .offline || (error as? TokdashError) == .timeout
                if !retry || index == candidates.count - 1 { throw error }
            }
        }
        throw TokdashError.offline
    }

    private func getDataAt(_ base: URL, path: String, timeout: TimeInterval) async throws -> Data {
        guard let url = Self.buildURL(baseURL: base, path: path) else {
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
            return data
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
    var instanceId: String? = nil
    private enum CodingKeys: String, CodingKey { case status, service, version; case instanceId = "instance_id" }
}

struct RouteProbe: Sendable {
    let address: String
    let health: HealthResponse?
    let milliseconds: Double
    var failure: TokdashError? = nil
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
    // The *_prev fields let a multi-server companion recompute each percentage from
    // summed current and previous totals (contract §Full delta row). A metric whose
    // *_prev is omitted by any contributing server is omitted from the combined row.
    let costPrev: Double?
    let tokensPrev: Double?
    let messagesPrev: Double?

    enum CodingKeys: String, CodingKey {
        case tokensPct = "tokens_pct"
        case costPct = "cost_pct"
        case messagesPct = "messages_pct"
        case costPrev = "cost_prev"
        case tokensPrev = "tokens_prev"
        case messagesPrev = "messages_prev"
    }

    init(tokensPct: Double? = nil, costPct: Double? = nil, messagesPct: Double? = nil,
         costPrev: Double? = nil, tokensPrev: Double? = nil, messagesPrev: Double? = nil) {
        self.tokensPct = tokensPct
        self.costPct = costPct
        self.messagesPct = messagesPct
        self.costPrev = costPrev
        self.tokensPrev = tokensPrev
        self.messagesPrev = messagesPrev
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        tokensPct = try values.decodeIfPresent(Double.self, forKey: .tokensPct)
        costPct = try values.decodeIfPresent(Double.self, forKey: .costPct)
        messagesPct = try values.decodeIfPresent(Double.self, forKey: .messagesPct)
        costPrev = try values.decodeIfPresent(Double.self, forKey: .costPrev)
        tokensPrev = try values.decodeIfPresent(Double.self, forKey: .tokensPrev)
        messagesPrev = try values.decodeIfPresent(Double.self, forKey: .messagesPrev)
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
    /// Provider keys in WIRE order (contract §All view: "provider order as
    /// detected"). Foundation loses object key order, so this is captured from the
    /// raw bytes by ``decode(from:)``; nil on paths that never saw the bytes.
    var providerWireOrder: [String]? = nil

    private enum CodingKeys: String, CodingKey {
        case enabled, providers, timestamp
    }

    /// Decode the quota payload and capture the provider key sequence from the
    /// same bytes (two passes over the data: JSONDecoder, then the key scan).
    nonisolated static func decode(from data: Data) throws -> QuotaResponse {
        var resp = try JSONDecoder().decode(QuotaResponse.self, from: data)
        resp.providerWireOrder = wireProviderKeys(in: data)
        return resp
    }

    /// The immediate key names of the top-level "providers" JSON object, in
    /// document order - read structurally: a minimal walker that tracks quoted
    /// strings (escapes included) and bracket depth, so braces or `"providers"`
    /// appearing inside string VALUES cannot shift the key scan. nil when the
    /// payload is not an object, has no `providers`, or `providers` is not an object.
    nonisolated static func wireProviderKeys(in data: Data) -> [String]? {
        let bytes = [UInt8](data)
        var i = 0
        func ws() {
            while i < bytes.count {
                let b = bytes[i]
                if b == 0x20 || b == 0x09 || b == 0x0A || b == 0x0D { i += 1 } else { break }
            }
        }
        // bytes[i] must be the opening quote; advances past the closing one.
        // Fully UNESCAPES the text (\" \\ \/ b f n r t \uXXXX incl. surrogate
        // pairs): returned provider keys must match the keys JSONDecoder produced
        // from the same payload, or the merged lookup misses and the provider
        // silently vanishes from the All view.
        func stringLit() -> String? {
            guard i < bytes.count, bytes[i] == 0x22 else { return nil }
            i += 1
            var out: [UInt8] = []
            func hex4() -> UInt32? {
                guard i + 4 <= bytes.count else { return nil }
                var value: UInt32 = 0
                for k in i..<(i + 4) {
                    let c = bytes[k]
                    let d: UInt32
                    switch c {
                    case 0x30...0x39: d = UInt32(c - 0x30)
                    case 0x61...0x66: d = UInt32(c - 0x61 + 10)
                    case 0x41...0x46: d = UInt32(c - 0x41 + 10)
                    default: return nil
                    }
                    value = value * 16 + d
                }
                i += 4
                return value
            }
            func appendScalar(_ scalar: UInt32) {
                guard let s = Unicode.Scalar(scalar) else { return }
                out.append(contentsOf: Array(String(s).utf8))
            }
            while i < bytes.count {
                let b = bytes[i]
                if b == 0x5C {
                    i += 1
                    guard i < bytes.count else { return nil }
                    let e = bytes[i]; i += 1
                    switch e {
                    case 0x22: out.append(0x22)
                    case 0x5C: out.append(0x5C)
                    case 0x2F: out.append(0x2F)
                    case 0x62: out.append(0x08)
                    case 0x66: out.append(0x0C)
                    case 0x6E: out.append(0x0A)
                    case 0x72: out.append(0x0D)
                    case 0x74: out.append(0x09)
                    case 0x75:
                        guard let u = hex4() else { return nil }
                        if u >= 0xD800 && u <= 0xDBFF {
                            // High surrogate: JSONDecoder requires the low half.
                            guard i + 1 < bytes.count, bytes[i] == 0x5C, bytes[i + 1] == 0x75 else { return nil }
                            i += 2
                            guard let lo = hex4(), lo >= 0xDC00 && lo <= 0xDFFF else { return nil }
                            appendScalar(0x1_0000 + (u - 0xD800) * 0x400 + (lo - 0xDC00))
                        } else if u >= 0xDC00 && u <= 0xDFFF {
                            return nil // lone low surrogate: JSONDecoder would reject it too
                        } else {
                            appendScalar(u)
                        }
                    default: return nil
                    }
                    continue
                }
                if b == 0x22 { i += 1; return String(decoding: out, as: UTF8.self) }
                out.append(b); i += 1
            }
            return nil
        }
        func skipValue() -> Bool {
            ws()
            guard i < bytes.count else { return false }
            let b = bytes[i]
            if b == 0x22 { return stringLit() != nil }
            if b == 0x7B || b == 0x5B {
                var depth = 0
                while i < bytes.count {
                    let c = bytes[i]
                    if c == 0x22 { if stringLit() == nil { return false }; continue }
                    if c == 0x7B || c == 0x5B { depth += 1 }
                    else if c == 0x7D || c == 0x5D {
                        depth -= 1
                        if depth == 0 { i += 1; return true }
                    }
                    i += 1
                }
                return false
            }
            while i < bytes.count {
                let c = bytes[i]
                if c == 0x2C || c == 0x7D || c == 0x5D { break }
                i += 1
            }
            return true
        }
        func colon() -> Bool { ws(); guard i < bytes.count, bytes[i] == 0x3A else { return false }; i += 1; return true }

        ws()
        guard i < bytes.count, bytes[i] == 0x7B else { return nil }
        i += 1
        while true {
            ws()
            guard let key = stringLit(), colon() else { return nil }
            if key == "providers" {
                ws()
                guard i < bytes.count, bytes[i] == 0x7B else { return nil }
                i += 1
                var keys: [String] = []
                while true {
                    ws()
                    if i < bytes.count, bytes[i] == 0x7D { i += 1; break }
                    guard let provider = stringLit(), colon() else { return nil }
                    guard skipValue() else { return nil }
                    keys.append(provider)
                    ws()
                    if i < bytes.count, bytes[i] == 0x2C { i += 1; continue }
                    if i < bytes.count, bytes[i] == 0x7D { i += 1; break }
                    return nil
                }
                return keys
            }
            guard skipValue() else { return nil }
            ws()
            if i < bytes.count, bytes[i] == 0x2C { i += 1; continue }
            return nil // top-level members exhausted without finding "providers"
        }
    }
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
    // Codex-only reset credits (contract §Reset credits): the quiet All-view row and
    // its expiry notification. Absent on every other provider, and usually on Codex too.
    let resetCredits: ResetCredits?

    // Explicit memberwise init (with defaults) so test construction with status/
    // statusDetail resolves; Decodable's init(from:) is still synthesized.
    init(estimated: Bool? = nil, buckets: [BucketQuota]? = nil, status: String? = nil,
         statusDetail: String? = nil, statusAt: Int? = nil, accounts: [AccountQuota]? = nil,
         resetCredits: ResetCredits? = nil) {
        self.estimated = estimated
        self.buckets = buckets
        self.status = status
        self.statusDetail = statusDetail
        self.statusAt = statusAt
        self.accounts = accounts
        self.resetCredits = resetCredits
    }

    enum CodingKeys: String, CodingKey {
        case estimated, buckets, status, accounts
        case statusDetail = "status_detail"
        case statusAt = "status_at"
        case resetCredits = "reset_credits"
    }
}

/// `providers.<provider>.reset_credits` (contract §Reset credits). Sent by Codex and -
/// since server v2.6.3 - by Claude Code too.
struct ResetCredits: Decodable, Sendable, Equatable {
    let availableCount: Int?
    let credits: [ResetCredit]?

    enum CodingKeys: String, CodingKey {
        case availableCount = "available_count"
        case credits
    }
}

struct ResetCredit: Decodable, Sendable, Equatable {
    let id: String?
    let expiresAt: String?

    enum CodingKeys: String, CodingKey {
        case id
        case expiresAt = "expires_at"
    }

    /// `expires_at` has shipped in two wire shapes: Codex's credits carry an ISO 8601
    /// *string*, Claude Code's limit resets (server v2.6.3) carry epoch *seconds*.
    /// Both normalize to an ISO string here so `parseTimestamp` stays single-format -
    /// and an unexpected shape degrades to nil instead of taking the whole quota payload
    /// down with it (one credit line must never blank the whole quota section).
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decodeIfPresent(String.self, forKey: .id)
        if let raw = try? container.decodeIfPresent(String.self, forKey: .expiresAt) {
            expiresAt = raw
        } else if let seconds = try? container.decodeIfPresent(Int.self, forKey: .expiresAt) {
            let formatter = ISO8601DateFormatter()
            expiresAt = formatter.string(from: Date(timeIntervalSince1970: TimeInterval(seconds)))
        } else {
            expiresAt = nil
        }
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

// MARK: - v1.1 optional-section payloads (additive; failure/404 hides the section silently)

/// `GET /api/active-time` (selected period). `active_ms` is MILLISECONDS - every duration
/// in this payload is; every epoch in the quota payload is seconds. `by_tool`, `comparison`
/// and the `*_sum` fields are not rendered in v1.1 and decode-ignored.
struct ActiveTimeResponse: Decodable, Sendable {
    let activeMs: Int?
    let timestamp: String?

    enum CodingKeys: String, CodingKey {
        case activeMs = "active_ms"
        case timestamp
    }
}

/// `GET /api/insights?facets=hourly...` / `?facets=daily...`. Exactly one facet per
/// request; only the requested facet is present.
struct InsightsResponse: Decodable, Sendable {
    let hourly: HourlyFacet?
    let daily: [DailyPoint]?
}

struct HourlyFacet: Decodable, Sendable {
    let buckets: [HourBucket]?
    let peakHour: Int?

    enum CodingKeys: String, CodingKey {
        case buckets
        case peakHour = "peak_hour"
    }
}

struct HourBucket: Decodable, Sendable {
    let hour: Int?
    let tokens: Int?
}

/// One day of the `daily` facet. Sparse: a date with no usage has no entry.
struct DailyPoint: Decodable, Sendable {
    let date: String?
    let tokens: Int?
    let intensity: Int?
}

/// `GET /api/stats` - a rolling 365 days of contributions; v1.1 windows it client-side
/// (trailing 90 days for month, 180 for year). `summary.*` / `stats.*` are not rendered.
struct StatsResponse: Decodable, Sendable {
    let contributions: [Contribution]?
}

struct Contribution: Decodable, Sendable {
    let date: String?
    let totals: ContributionTotals?
    // int 0..4, ranked quartiles server-side
    let intensity: Int?
}

struct ContributionTotals: Decodable, Sendable {
    let tokens: Int?
}

/// `GET /api/version` - Settings only.
struct VersionResponse: Decodable, Sendable {
    let runtimeVersion: String?
    let updateCheckEnabled: Bool?

    enum CodingKeys: String, CodingKey {
        case runtimeVersion = "runtime_version"
        case updateCheckEnabled = "update_check_enabled"
    }
}

/// `GET /api/update-check` - Settings only, never a POST. `enabled == false` means the
/// server has no update-check consent: render nothing, never try to change that.
struct ServerUpdateCheckResponse: Decodable, Sendable {
    let enabled: Bool?
    let updateAvailable: Bool?
    let latest: String?

    enum CodingKeys: String, CodingKey {
        case enabled
        case updateAvailable = "update_available"
        case latest
    }
}
