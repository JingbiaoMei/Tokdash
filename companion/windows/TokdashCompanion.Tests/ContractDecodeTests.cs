using System.Collections.Generic;
using System.Text.Json;
using System.Text.Json.Serialization;
using Microsoft.VisualStudio.TestTools.UnitTesting;

namespace TokdashCompanion.Tests;

/// <summary>
/// Real JSON decoding against the companion API contract (snake_case fields). These
/// would have caught the P0 where only `response_cache` was annotated and every other
/// snake_case field (total_tokens, cost_pct, remaining_percent, ...) decoded to zero/null.
/// Mirrors the shared contract fixtures.
/// </summary>
[TestClass]
public class ContractDecodeTests
{
    private static readonly JsonSerializerOptions Opts = new()
    {
        PropertyNameCaseInsensitive = true,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
    };

    private static Snapshot MakeSnap(QuotaResponse q) => new()
    {
        Period = UsagePeriod.Today,
        Usage = new UsageResponse { TotalTokens = 1 },
        Quota = q,
        Thresholds = QuotaThresholds.Defaults,
        Components = new CompanionComponents(),
        Now = new System.DateTimeOffset(2026, 7, 26, 15, 35, 20, System.TimeSpan.Zero),
    };

    [TestMethod]
    public void Reset_Credits_Decode_And_Row()
    {
        // expires_at is an ISO STRING (unlike every other epoch in the quota payload).
        var json = """
        {"enabled":true,"providers":{"codex":{"buckets":[
            {"account":"a","bucket":"5h","remaining_percent":50.0,"resets_at":1}
          ],"reset_credits":{"available_count":2,"credits":[
            {"id":"credit-a","expires_at":"2026-07-28T15:35:20Z"},
            {"id":"credit-b","expires_at":"2026-09-05T15:35:20Z"}]}}}}
        """;
        var q = JsonSerializer.Deserialize<QuotaResponse>(json, Opts)!;
        var credits = q.Providers!["codex"].ResetCredits!;
        Assert.AreEqual(2, credits.AvailableCount);
        Assert.AreEqual("credit-a", credits.Credits![0].Id);
        Assert.AreEqual("2026-07-28T15:35:20Z", credits.Credits[0].ExpiresAt);

        var snap = MakeSnap(q);
        var codex = snap.AllQuotaGroups.Single(g => g.CanonicalProvider == "codex");
        Assert.AreEqual("⚡ Codex · 2 reset credits · expire in 2 d", snap.CreditsNotice(codex));
    }

    [TestMethod]
    public void Credits_Notice_Suppressed_When_Group_Failed()
    {
        var json = """
        {"enabled":true,"providers":{"codex":{"status":"error","status_detail":"boom","status_at":9,"buckets":[
            {"account":"a","bucket":"5h","remaining_percent":50.0,"resets_at":1,"captured_at":1}
          ],"reset_credits":{"available_count":1,"credits":[{"id":"c","expires_at":"2099-01-01T00:00:00Z"}]}}}}
        """;
        var q = JsonSerializer.Deserialize<QuotaResponse>(json, Opts)!;
        var snap = MakeSnap(q);
        var codex = snap.AllQuotaGroups.Single(g => g.CanonicalProvider == "codex");
        Assert.IsTrue(codex.Failed);
        Assert.IsNull(snap.CreditsNotice(codex), "last-known credit data never backs an expire-in row");
    }

    [TestMethod]
    public void ActiveTime_Insights_Stats_Version_UpdateCheck_Decode()
    {
        var at = JsonSerializer.Deserialize<ActiveTimeResponse>("""{"active_ms":11520000,"timestamp":"2026-07-26"}""", Opts)!;
        Assert.AreEqual(11520000L, at.ActiveMs);

        var ins = JsonSerializer.Deserialize<InsightsResponse>("""
        {"hourly":{"buckets":[{"hour":0,"tokens":10},{"hour":14,"tokens":99}],"peak_hour":14}}
        """, Opts)!;
        Assert.AreEqual(2, ins.Hourly!.Buckets!.Count);
        Assert.AreEqual(14, ins.Hourly.PeakHour);

        var daily = JsonSerializer.Deserialize<InsightsResponse>("""
        {"daily":[{"date":"2026-07-20","tokens":5,"intensity":1}]}
        """, Opts)!;
        Assert.AreEqual("2026-07-20", daily.Daily![0].Date);

        var stats = JsonSerializer.Deserialize<StatsResponse>("""
        {"contributions":[{"date":"2026-07-20","totals":{"tokens":123},"intensity":2}]}
        """, Opts)!;
        Assert.AreEqual(123L, stats.Contributions![0].Totals!.Tokens);
        Assert.AreEqual(2, stats.Contributions[0].Intensity);

        var ver = JsonSerializer.Deserialize<VersionResponse>("""
        {"service":"tokdash","runtime_version":"2.5.7","update_check_enabled":true}
        """, Opts)!;
        Assert.AreEqual("2.5.7", ver.RuntimeVersion);
        Assert.AreEqual(true, ver.UpdateCheckEnabled);

        var check = JsonSerializer.Deserialize<ServerUpdateCheckResponse>("""
        {"enabled":true,"update_available":true,"latest":"2.6.0"}
        """, Opts)!;
        Assert.AreEqual("2.6.0", check.Latest);
        Assert.AreEqual("2.6.0", CompanionStore.ServerBadgeVersion(check));
        // Consent gate: enabled false renders nothing, whatever else the payload says.
        var off = JsonSerializer.Deserialize<ServerUpdateCheckResponse>("""
        {"enabled":false,"update_available":true,"latest":"2.6.0"}
        """, Opts)!;
        Assert.IsNull(CompanionStore.ServerBadgeVersion(off));
    }

    [TestMethod]
    public void Usage_Comparison_Prev_Fields_Decode()
    {
        var u = JsonSerializer.Deserialize<UsageResponse>("""
        {"total_tokens":1,"comparison":{"cost_pct":-12.0,"tokens_pct":-12.0,"messages_pct":-11.7,
          "cost_prev":3.89,"tokens_prev":21250000,"messages_prev":281}}
        """, Opts)!;
        Assert.AreEqual(281.0, u.Comparison!.MessagesPrev!.Value, 0.001);
        Assert.AreEqual(21250000.0, u.Comparison.TokensPrev!.Value, 0.5);
    }

    [TestMethod]
    public void Health_Decodes()
    {
        var json = """{"status":"ok","service":"tokdash","version":"1.4.5"}""";
        var h = JsonSerializer.Deserialize<HealthResponse>(json, Opts)!;
        Assert.AreEqual("tokdash", h.Service);
        Assert.AreEqual("1.4.5", h.Version);
    }

    [TestMethod]
    public void Usage_Decodes_Snake_Case_Fields()
    {
        // Trimmed from contract/fixtures/usage-today.json; unknown fields (cache_hit_rate,
        // apps) must be ignored additively.
        var json = """
        {"period":"today","total_tokens":18700000,"total_cost":3.42,"total_messages":248,
         "cache_hit_rate":0.9274,"by_tool":{"codex":{"tokens":12982308,"cost":2.10,"tokens_in":764857}},
         "apps":{"codex":{"tokens":1,"cost":2.0}},
         "top_models":[{"name":"openai/gpt-5.6-sol","tokens":12112500,"cost":2.10}],
         "combined_models":[{"name":"openai/gpt-5.6-sol","tokens":12112500,"cost":2.10,"source":"codex"}],
         "comparison":{"tokens_prev":21250000,"cost_prev":3.89,"tokens_pct":-12.0,"cost_pct":-12.0,"messages_pct":-11.7},
         "timestamp":"2026-07-26T20:20:00+00:00","response_cache":{"age_seconds":120.487},
         "unknown_future_field":"ignored"}
        """;
        var u = JsonSerializer.Deserialize<UsageResponse>(json, Opts)!;

        Assert.AreEqual(18700000L, u.TotalTokens, "total_tokens must map to TotalTokens");
        Assert.AreEqual(3.42, u.TotalCost, 0.001);
        Assert.AreEqual(248L, u.TotalMessages);
        Assert.AreEqual(2.10, u.ByTool!["codex"].Cost, 0.001);
        Assert.AreEqual("openai/gpt-5.6-sol", u.TopModels![0].Name);
        Assert.AreEqual(2.10, u.CombinedModels![0].Cost, 0.001);
        Assert.AreEqual(-12.0, u.Comparison!.CostPct!.Value, 0.001);
        Assert.AreEqual(-11.7, u.Comparison.MessagesPct!.Value, 0.001);
        // age_seconds is a float in live responses (e.g. 454.18947); must decode as double.
        Assert.AreEqual(120.487, u.ResponseCache!.AgeSeconds!.Value, 0.001);
    }

    [TestMethod]
    public void Usage_Empty_Decodes_With_Null_Comparison()
    {
        var json = """
        {"period":"today","total_tokens":0,"total_cost":0.0,"total_messages":0,
         "by_tool":{},"top_models":[],"combined_models":[],
         "comparison":{"tokens_pct":null,"cost_pct":null,"messages_pct":null},
         "timestamp":"2026-07-26T20:20:00+00:00","response_cache":{"age_seconds":5}}
        """;
        var u = JsonSerializer.Deserialize<UsageResponse>(json, Opts)!;
        Assert.AreEqual(0L, u.TotalTokens);
        Assert.IsNull(u.Comparison!.CostPct);
        Assert.AreEqual(0, u.TopModels!.Count);
    }

    [TestMethod]
    public void Quota_Decodes_Snake_Case_Fields()
    {
        // Trimmed from contract/fixtures/quota.json.
        var json = """
        {"enabled":true,"providers":{
          "codex":{"estimated":false,"buckets":[
            {"account":"637ec3ac","bucket":"5h","bucket_label":"5-hour window","used_percent":86.0,
             "resets_at":1782910800,"remaining_percent":14.0,"status":"ok"}]},
          "claude":{"estimated":true,"buckets":[
            {"account":"default","bucket":"5h","bucket_label":"5-hour window",
             "resets_at":1782919500,"remaining_percent":71.0}]}
         },"timestamp":1785080120}
        """;
        var q = JsonSerializer.Deserialize<QuotaResponse>(json, Opts)!;

        Assert.IsTrue(q.Enabled);
        Assert.AreEqual(false, q.Providers!["codex"].Estimated);
        Assert.AreEqual(true, q.Providers["claude"].Estimated);
        var codex5h = q.Providers["codex"].Buckets![0];
        Assert.AreEqual("5h", codex5h.Bucket);
        Assert.AreEqual("5-hour window", codex5h.BucketLabel);
        Assert.AreEqual(14.0, codex5h.RemainingPercent!.Value, 0.001);
        Assert.AreEqual(1782910800, codex5h.ResetsAt);
        Assert.AreEqual("637ec3ac", codex5h.Account);
    }

    [TestMethod]
    public void Quota_Disabled_Decodes()
    {
        var json = """{"enabled":false,"providers":{},"timestamp":1785080120}""";
        var q = JsonSerializer.Deserialize<QuotaResponse>(json, Opts)!;
        Assert.IsFalse(q.Enabled);
    }

    [TestMethod]
    public void Snapshot_Builds_LowQuotaRows_From_Decoded_Quota()
    {
        // End-to-end: decoded quota -> Snapshot -> LowQuotaRows preserves provider + estimated.
        var json = """
        {"enabled":true,"providers":{
          "codex":{"estimated":false,"buckets":[
            {"account":"a","bucket":"5h","bucket_label":"5-hour","resets_at":1782910800,"remaining_percent":14.0}]}
         }}
        """;
        var q = JsonSerializer.Deserialize<QuotaResponse>(json, Opts)!;
        var snap = MakeSnap(q);
        var low = snap.LowQuotaRows;
        Assert.AreEqual(1, low.Count);
        Assert.AreEqual("Codex", low[0].Provider);
        Assert.AreEqual(14.0, low[0].Left, 0.001);
        Assert.AreEqual("a", low[0].Account);
    }

    [TestMethod]
    public void Quota_Provider_Status_Decodes_And_Flags_Failure()
    {
        // Group failure (provider status != "ok" OR a non-empty status_detail) drives the
        // header warning; row failure compares each bucket's captured_at against the
        // provider's status_at (spec §7). Every bucket reports status "ok" - that field
        // cannot discriminate, which is exactly why freshness does. codex is fully failed
        // (its bucket predates status_at); antigravity is partially failed (one bucket
        // captured in the failing cycle, one older); claude is healthy.
        var json = """
        {"enabled":true,"providers":{
          "codex":{"estimated":false,"status":"error","status_detail":"codex_api unreachable","status_at":1785030000,"buckets":[
            {"account":"a","bucket":"5h","bucket_label":"5-hour","resets_at":1782910800,"remaining_percent":14.0,"captured_at":1785000000,"status":"ok"}]},
          "antigravity":{"estimated":false,"status":"ok","status_detail":"stale_token","status_at":1785030000,"buckets":[
            {"account":"c","bucket":"5h","bucket_label":"5-hour","resets_at":1782919500,"remaining_percent":80.0,"captured_at":1785030000,"status":"ok"},
            {"account":"d","bucket":"weekly","bucket_label":"Weekly","resets_at":1782919500,"remaining_percent":5.0,"captured_at":1785000000,"status":"ok"}]},
          "claude":{"estimated":true,"status_at":null,"buckets":[
            {"account":"b","bucket":"5h","bucket_label":"5-hour","resets_at":1782919500,"remaining_percent":71.0,"captured_at":1785030000,"status":"ok"}]}
         },"timestamp":1785080120}
        """;
        var q = JsonSerializer.Deserialize<QuotaResponse>(json, Opts)!;
        Assert.AreEqual("error", q.Providers!["codex"].Status);
        Assert.AreEqual("codex_api unreachable", q.Providers["codex"].StatusDetail);
        Assert.AreEqual(1785030000, q.Providers["codex"].StatusAt, "status_at must decode");
        Assert.IsNull(q.Providers["claude"].Status);
        Assert.IsNull(q.Providers["claude"].StatusAt);
        Assert.AreEqual(1785000000, q.Providers["antigravity"].Buckets![1].CapturedAt, "captured_at must decode");

        var snap = MakeSnap(q);
        var codex = snap.AllQuotaGroups.First(g => g.Provider == "Codex");
        var claude = snap.AllQuotaGroups.First(g => g.Provider == "Claude");
        var antigravity = snap.AllQuotaGroups.First(g => g.Provider == "Antigravity");
        Assert.IsTrue(codex.Failed, "error-status provider must be flagged failed");
        Assert.IsTrue(antigravity.Failed, "status=ok with non-empty status_detail must be flagged failed");
        Assert.IsFalse(claude.Failed, "absent-status provider with no detail must not be failed");
        Assert.IsTrue(codex.Rows.All(r => r.Failed), "captured_at < status_at -> last-known");
        Assert.IsFalse(antigravity.Rows.Single(r => r.Bucket == "5h").Failed, "captured_at == status_at is fresh, not failed");
        Assert.IsTrue(antigravity.Rows.Single(r => r.Bucket == "weekly").Failed, "the older sibling window is last-known");
        Assert.IsFalse(claude.Rows.Any(r => r.Failed), "a healthy provider never flags rows");

        // Low view: rows carry their own flag, so the ⚠ lands only on the last-known ones.
        var low = snap.LowQuotaRows;
        Assert.AreEqual(2, low.Count);
        Assert.AreEqual("Antigravity", low[0].Provider);
        Assert.IsTrue(low[0].Failed, "the last-known window keeps the inline warning");
        Assert.AreEqual("Codex", low[1].Provider);
        Assert.IsTrue(low[1].Failed);
    }
}
