import Foundation

/// Companion update checking against GitHub's public releases API.
///
/// The companion ships its own version line (`companion/VERSION`) and shares the Tokdash
/// repository with the Python package, so `/releases/latest` is useless here: it resolves
/// to the newest *Python* release, and companion releases are published with
/// `--latest=false` so they never take that pointer,
/// which `latest` skips outright. The check therefore lists releases and filters by tag.
///
/// No credentials are sent (public endpoint, unauthenticated 60 req/hour/IP), and the
/// check never downloads or installs anything - releases are unsigned, so the only action
/// offered is opening the release page in the browser. Mirrored by `UpdateChecker.cs` on
/// Windows; the pure helpers below are pinned to the same cases in both test suites.
enum UpdateChecker {
    /// Unauthenticated releases list. `per_page=100` covers every release the repo has
    /// published so far in one request, so the newest companion tag can't fall off the page.
    static let releasesEndpoint = "https://api.github.com/repos/JingbiaoMei/Tokdash/releases?per_page=100"

    /// Companion releases are tagged `companion-vX.Y.Z`; Python releases are `vX.Y.Z`.
    /// The prefix is what separates the two lines in a shared repository.
    static let tagPrefix = "companion-v"

    /// Only ever open a release page under the Tokdash repo's releases path.
    static let releasesPathPrefix = "/jingbiaomei/tokdash/releases/"

    /// At most one automatic check per day (spec: "at most once every 24 hours").
    static let autoCheckInterval: TimeInterval = 24 * 60 * 60

    // MARK: - Selection (pure)

    /// Newest published companion release, or nil when the list holds none.
    ///
    /// Drafts are dropped (unpublished, their tag may not exist yet); prereleases are
    /// deliberately KEPT. Companion builds are no longer published as prereleases, but the
    /// flag must not be filtered on: releases up to 0.2.0 were prereleases, and excluding
    /// them would hide an upgrade path for anyone still on an older build. Tags that don't parse -
    /// Python releases (`v1.5.8`), partial versions (`companion-v0.1`), suffixed versions
    /// (`companion-v0.1.4-rc1`) - are skipped rather than guessed at.
    static func newestCompanionRelease(in releases: [GitHubRelease]) -> (version: [Int], release: GitHubRelease)? {
        var best: (version: [Int], release: GitHubRelease)?
        for release in releases {
            guard !release.draft, let version = parseTag(release.tagName) else { continue }
            if best == nil || isNewer(version, than: best!.version) {
                best = (version, release)
            }
        }
        return best
    }

    /// Parse a release tag into numeric components: `"companion-v0.1.10"` -> `[0, 1, 10]`.
    /// Returns nil for any tag that isn't exactly `companion-v` + three numeric parts.
    static func parseTag(_ tag: String) -> [Int]? {
        let trimmed = tag.trimmingCharacters(in: .whitespacesAndNewlines)
        guard trimmed.hasPrefix(tagPrefix) else { return nil }
        return parseVersion(String(trimmed.dropFirst(tagPrefix.count)))
    }

    /// Parse a bare version string: `"0.1.10"` -> `[0, 1, 10]`. Strict - exactly three
    /// components, each a non-empty run of ASCII digits. Anything else (empty parts,
    /// `"0.1"`, `"0.1.4-rc1"`, `"v0.1.4"`, digits that overflow Int) returns nil, so a
    /// malformed value can never be compared as if it were a version.
    static func parseVersion(_ version: String) -> [Int]? {
        let trimmed = version.trimmingCharacters(in: .whitespacesAndNewlines)
        let parts = trimmed.split(separator: ".", omittingEmptySubsequences: false)
        guard parts.count == 3 else { return nil }
        var out: [Int] = []
        for part in parts {
            guard !part.isEmpty,
                  part.allSatisfy({ $0.isASCII && $0.isNumber }),
                  let value = Int(part) else { return nil }
            out.append(value)
        }
        return out
    }

    /// Component-wise NUMERIC comparison. This is the whole reason versions are parsed to
    /// `[Int]` first: a string compare puts "0.1.10" *below* "0.1.9", which would hide
    /// every release after the ninth patch.
    static func isNewer(_ candidate: [Int], than current: [Int]) -> Bool {
        for i in 0..<max(candidate.count, current.count) {
            let a = i < candidate.count ? candidate[i] : 0
            let b = i < current.count ? current[i] : 0
            if a != b { return a > b }
        }
        return false
    }

    /// Render parsed components back to a display string ("0.1.10").
    static func versionString(_ parts: [Int]) -> String {
        parts.map(String.init).joined(separator: ".")
    }

    // MARK: - Link safety (pure)

    /// The link the "View update" button opens. Uses the API's `html_url` only when it
    /// validates; otherwise falls back to a URL built entirely from the PARSED numeric
    /// version, which has no injection surface at all (no server-supplied text reaches it).
    static func releaseURL(for release: GitHubRelease, version: [Int]) -> String {
        if let raw = release.htmlURL, isValidReleaseURL(raw) { return raw }
        return "https://github.com/JingbiaoMei/Tokdash/releases/tag/\(tagPrefix)\(versionString(version))"
    }

    /// True for an HTTPS URL whose host is exactly github.com and whose path sits under the
    /// Tokdash repo's releases. Host is compared after URL parsing, so the usual spoofs
    /// (`https://github.com@evil.test/...`, `https://github.com.evil.test/...`) resolve to a
    /// different host and fail. Owner/repo are compared case-insensitively: GitHub treats
    /// them that way and the API's canonical casing need not match ours.
    static func isValidReleaseURL(_ raw: String) -> Bool {
        guard let url = URL(string: raw.trimmingCharacters(in: .whitespacesAndNewlines)),
              url.scheme?.lowercased() == "https",
              url.host?.lowercased() == "github.com" else { return false }
        return url.path.lowercased().hasPrefix(releasesPathPrefix)
    }

    // MARK: - Scheduling (pure)

    /// True when an automatic check is due: the opt-in is on and the last attempt was at
    /// least 24h ago. A timestamp in the future means the clock moved backwards; treat it
    /// as due rather than blocking checks until real time catches up.
    static func shouldAutoCheck(enabled: Bool, lastCheck: Date?, now: Date) -> Bool {
        guard enabled else { return false }
        guard let last = lastCheck else { return true }
        if last > now { return true }
        return now.timeIntervalSince(last) >= autoCheckInterval
    }

    /// Relative "last checked" caption, using the same tiers as the freshness footer.
    static func lastCheckedText(_ last: Date?, now: Date = Date()) -> String {
        guard let last else { return L10n.t("update_never_checked") }
        let age = max(0, now.timeIntervalSince(last))
        if age < 60 { return L10n.t("update_last_checked_just_now") }
        if age < 3600 { return L10n.t("update_last_checked_min", Int(age / 60)) }
        if age < 86400 { return L10n.t("update_last_checked_h", Int(age / 3600)) }
        return L10n.t("update_last_checked_d", Int(age / 86400))
    }

    /// User-facing reason for a FAILED manual check. Scheduled checks never surface these
    /// (spec: silent), so this is only read from the Settings status line.
    static func failureText(_ error: UpdateCheckError) -> String {
        switch error {
        case .offline, .timeout: return L10n.t("update_failed_offline")
        case .rateLimited: return L10n.t("update_failed_rate_limited")
        default: return L10n.t("update_failed_generic")
        }
    }
}

/// Outcome of the most recent check, used only for the Settings status line. The Settings
/// gear badge is deliberately NOT derived from this: it reads the persisted available
/// version instead, so a later `checking`/`failed` state can't clear a pending update, and
/// the dot survives a relaunch. See `CompanionStore.updateAvailableVersion`.
enum UpdateStatus: Equatable {
    case idle
    case checking
    case upToDate
    case available(version: String, url: String)
    case failed(String)
}

enum UpdateCheckError: Error, Equatable {
    case offline
    case timeout
    /// 403 (unauthenticated hourly limit) or 429. Distinct from a plain HTTP failure
    /// because the remedy is "wait", not "check your network".
    case rateLimited
    case httpStatus(Int)
    case decode
    case other
}

/// One entry from the releases list. Additive decoding: unknown fields are ignored and
/// absent optional fields are tolerated, so a GitHub API addition can't break the check.
struct GitHubRelease: Decodable, Equatable {
    let tagName: String
    let draft: Bool
    let prerelease: Bool
    let htmlURL: String?

    enum CodingKeys: String, CodingKey {
        case tagName = "tag_name"
        case draft
        case prerelease
        case htmlURL = "html_url"
    }

    init(tagName: String, draft: Bool = false, prerelease: Bool = false, htmlURL: String? = nil) {
        self.tagName = tagName
        self.draft = draft
        self.prerelease = prerelease
        self.htmlURL = htmlURL
    }

    /// Absent `draft`/`prerelease` default to false rather than failing the whole list -
    /// one odd entry must not cost the user every other release.
    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        tagName = try values.decodeIfPresent(String.self, forKey: .tagName) ?? ""
        draft = try values.decodeIfPresent(Bool.self, forKey: .draft) ?? false
        prerelease = try values.decodeIfPresent(Bool.self, forKey: .prerelease) ?? false
        htmlURL = try values.decodeIfPresent(String.self, forKey: .htmlURL)
    }
}

/// Fetches the releases list. Separate from ``TokdashClient`` on purpose: this talks to a
/// third party, and a GitHub failure must never touch Tokdash connection state.
actor GitHubReleasesClient {
    private let session: URLSession
    private let endpoint: String

    init(endpoint: String = UpdateChecker.releasesEndpoint) {
        self.endpoint = endpoint
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 15
        config.timeoutIntervalForResource = 30
        config.waitsForConnectivity = false
        session = URLSession(configuration: config)
    }

    func fetchReleases() async throws -> [GitHubRelease] {
        guard let url = URL(string: endpoint) else { throw UpdateCheckError.other }
        var request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 15)
        // GitHub rejects requests with no User-Agent (403). No token is sent: this is a
        // public endpoint and the companion holds no credentials.
        request.setValue("TokdashCompanion/\(CompanionStore.currentVersion)", forHTTPHeaderField: "User-Agent")
        request.setValue("application/vnd.github+json", forHTTPHeaderField: "Accept")
        request.setValue("2022-11-28", forHTTPHeaderField: "X-GitHub-Api-Version")
        do {
            let (data, response) = try await session.data(for: request)
            guard let http = response as? HTTPURLResponse else { throw UpdateCheckError.other }
            // 403 is how the unauthenticated hourly limit is reported; 429 is the newer form.
            if http.statusCode == 403 || http.statusCode == 429 { throw UpdateCheckError.rateLimited }
            guard (200..<300).contains(http.statusCode) else { throw UpdateCheckError.httpStatus(http.statusCode) }
            do {
                return try JSONDecoder().decode([GitHubRelease].self, from: data)
            } catch {
                throw UpdateCheckError.decode
            }
        } catch let error as UpdateCheckError {
            throw error
        } catch let error as URLError where error.code == .timedOut {
            throw UpdateCheckError.timeout
        } catch {
            throw UpdateCheckError.offline
        }
    }
}
