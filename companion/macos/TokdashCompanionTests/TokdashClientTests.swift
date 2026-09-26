import XCTest
@testable import TokdashCompanion

final class TokdashClientTests: XCTestCase {

    func testRoutesSelectFastestPinAndRejectAnotherDaemon() throws {
        var server = CompanionServerSettings(id: "host", label: "Work", baseURL: "https://lan.test/tokdash", enabled: true,
            routes: ["https://tail.test/tokdash", "https://wrong.test"], instanceId: "daemon-a")
        let probes = [
            RouteProbe(address: server.baseURL, health: HealthResponse(status: "ok", service: "tokdash", version: "1", instanceId: "daemon-a"), milliseconds: 20),
            RouteProbe(address: server.routes[0], health: HealthResponse(status: "ok", service: "tokdash", version: "1", instanceId: "daemon-a"), milliseconds: 5),
            RouteProbe(address: server.routes[1], health: HealthResponse(status: "ok", service: "tokdash", version: "1", instanceId: "other"), milliseconds: 1)
        ]
        XCTAssertEqual(TokdashClient.selectRoutes(server, probes: probes).first?.address, server.routes[0])
        server.preferredRoute = server.baseURL
        XCTAssertEqual(TokdashClient.selectRoutes(server, probes: probes).first?.address, server.baseURL)
        XCTAssertEqual(TokdashClient.selectRoutes(server, probes: Array(probes.dropFirst())).first?.address, server.routes[0])
        XCTAssertEqual(TokdashClient.selectRoutes(server, probes: probes).count, 2)
        let roundTrip = try JSONDecoder().decode(CompanionServerSettings.self, from: JSONEncoder().encode(server))
        XCTAssertEqual(roundTrip, server)
    }

    func testLegacyRoutesKeepIdentityLessPrimary() throws {
        let data = #"{"id":"old","label":"Work","baseUrl":"http://local.test","enabled":true}"#.data(using: .utf8)!
        var server = try JSONDecoder().decode(CompanionServerSettings.self, from: data)
        XCTAssertEqual(server.addresses, ["http://local.test"])
        let unverified = RouteProbe(address: "https://other.test", health: HealthResponse(status: "ok", service: "tokdash", version: "1", instanceId: "other"), milliseconds: 1)
        server.routes = [unverified.address]
        XCTAssertTrue(TokdashClient.selectRoutes(server, probes: [unverified]).isEmpty)
        server.routes = ["https://other.test"]
        let probes = [
            RouteProbe(address: server.baseURL, health: HealthResponse(status: "ok", service: "tokdash", version: "1"), milliseconds: 20),
            RouteProbe(address: server.routes[0], health: HealthResponse(status: "ok", service: "tokdash", version: "1", instanceId: "other"), milliseconds: 1)
        ]
        XCTAssertEqual(TokdashClient.selectRoutes(server, probes: probes).map(\.address), [server.baseURL])
    }

    func testFailedActiveRouteRetriesVerifiedAlternate() async throws {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [RouteURLProtocol.self]
        let session = URLSession(configuration: config)
        defer { session.invalidateAndCancel() }
        let server = CompanionServerSettings(id: "host", label: "Work", baseURL: "https://lan.test/tokdash", enabled: true,
            routes: ["https://tail.test/tokdash", "https://wrong.test"], preferredRoute: "https://lan.test/tokdash", instanceId: "daemon-a")
        let client = TokdashClient(server: server, session: session)
        let health = try await client.health()
        XCTAssertEqual(health.instanceId, "daemon-a")
        let usage = try await client.usage(period: "today")
        XCTAssertEqual(usage.totalTokens, 42)
    }

    func testHealthDecode() throws {
        let json = """
        {"status":"ok","service":"tokdash","version":"1.4.5"}
        """.data(using: .utf8)!
        let health = try JSONDecoder().decode(HealthResponse.self, from: json)
        XCTAssertEqual(health.service, "tokdash")
        XCTAssertEqual(health.version, "1.4.5")
    }

    func testUsageDecodeAdditive() throws {
        // Unknown fields (cache_hit_rate, apps) must be ignored without failing.
        let json = """
        {"period":"today","total_tokens":18700000,"total_cost":3.42,"total_messages":248,
         "cache_hit_rate":0.9274,"by_tool":{"codex":{"tokens":1,"cost":2.0}},
         "comparison":{"tokens_pct":-12.0,"cost_pct":-12.0,"messages_pct":-11.7},
         "timestamp":"2026-07-26T20:20:00+00:00","response_cache":{"age_seconds":120.487},
         "unknown_future_field":"ignored"}
        """.data(using: .utf8)!
        let usage = try JSONDecoder().decode(UsageResponse.self, from: json)
        XCTAssertEqual(usage.totalTokens, 18700000)
        XCTAssertEqual(usage.totalCost, 3.42, accuracy: 0.001)
        XCTAssertEqual(usage.byTool?["codex"]?.cost ?? -1, 2.0, accuracy: 0.001)
        XCTAssertEqual(usage.comparison?.costPct ?? 0, -12.0, accuracy: 0.001)
        // age_seconds is a float in live responses; must decode as Double, not Int.
        XCTAssertEqual(usage.responseCache?.ageSeconds ?? -1, 120.487, accuracy: 0.001)
    }

    func testQuotaDecode() throws {
        let json = """
        {"enabled":true,"providers":{"codex":{"estimated":false,"buckets":[
          {"bucket":"5h","bucket_label":"5-hour window","remaining_percent":14.0,
           "resets_at":1782910800,"account":"default"}]}},
         "timestamp":1785080120}
        """.data(using: .utf8)!
        let quota = try JSONDecoder().decode(QuotaResponse.self, from: json)
        XCTAssertTrue(quota.enabled)
        XCTAssertEqual(quota.providers?["codex"]?.buckets?.first?.remainingPercent ?? -1, 14.0, accuracy: 0.001)
    }

    func testQuotaDecodeDisabled() throws {
        let json = """
        {"enabled":false,"providers":{},"timestamp":1785080120}
        """.data(using: .utf8)!
        let quota = try JSONDecoder().decode(QuotaResponse.self, from: json)
        XCTAssertFalse(quota.enabled)
    }

    // MARK: - Request URL building (would have caught the P0 where ?period=today was
    // folded into the path as /api/usage%3Fperiod=today).

    func testBuildURL_Keeps_Query_Out_Of_Path() throws {
        let base = URL(string: "https://wsl.example/tokdash")!
        let url = try XCTUnwrap(TokdashClient.buildURL(baseURL: base, path: "/api/usage?period=today"))
        XCTAssertEqual(url.absoluteString, "https://wsl.example/tokdash/api/usage?period=today")
        let comps = try XCTUnwrap(URLComponents(url: url, resolvingAgainstBaseURL: false))
        XCTAssertEqual(comps.query, "period=today")
        XCTAssertEqual(comps.path, "/tokdash/api/usage")
    }

    func testBuildURL_No_Query() throws {
        let base = URL(string: "http://127.0.0.1:55423")!
        let url = try XCTUnwrap(TokdashClient.buildURL(baseURL: base, path: "/health"))
        XCTAssertEqual(url.absoluteString, "http://127.0.0.1:55423/health")
    }

    func testBuildURL_Strips_Trailing_Slash() throws {
        let base = URL(string: "https://host/tokdash/")!
        let url = try XCTUnwrap(TokdashClient.buildURL(baseURL: base, path: "/api/quota"))
        XCTAssertEqual(url.absoluteString, "https://host/tokdash/api/quota")
    }
}

private final class RouteURLProtocol: URLProtocol, @unchecked Sendable {
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let url = request.url!
        let json: String
        if url.path.hasSuffix("/health") {
            let identity = url.host == "wrong.test" ? "other" : "daemon-a"
            json = "{\"status\":\"ok\",\"service\":\"tokdash\",\"version\":\"1\",\"instance_id\":\"\(identity)\"}"
        } else if url.host == "lan.test" {
            client?.urlProtocol(self, didFailWithError: URLError(.cannotConnectToHost)); return
        } else if url.host == "tail.test" && url.path == "/tokdash/api/usage" && url.query == "period=today" {
            json = #"{"period":"today","total_tokens":42,"total_cost":1,"total_messages":1}"#
        } else {
            client?.urlProtocol(self, didFailWithError: URLError(.badServerResponse)); return
        }
        client?.urlProtocol(self, didReceive: HTTPURLResponse(url: url, statusCode: 200, httpVersion: nil, headerFields: nil)!, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data(json.utf8))
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}
