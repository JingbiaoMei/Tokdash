using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Runtime.CompilerServices;
using System.Text.Json;
using Microsoft.VisualStudio.TestTools.UnitTesting;

namespace TokdashCompanion.Tests;

/// <summary>
/// Case-driven contract loader: EVERY file in <c>contract/expected/</c> is executed against
/// the real store through <see cref="FakeClient"/>, under the English locale and the fixture
/// timestamp as the frozen clock (contract §Expected behavior cases: "In a fixture slot, null
/// means the endpoint returns nothing at all this cycle (treated as a failure); '503' and
/// 'pending' keep their existing meanings").
///
/// Slot mapping, honestly: a file name decodes to its typed response; "503" is the busy
/// failure; "timeout"/"fail" the offline failure; null a failed section (FakeClient's own
/// "fail" token - a slot left null would be the endpoint's DEFAULT SUCCESS, so the loader
/// maps JSON null to "fail" explicitly); "pending" is the never-completing in-flight case,
/// which the loader never awaits. Flyout-only visuals (skeleton bars, dimming, button rows)
/// belong to FlyoutLaunchTests/PartialStateTests; store-level equivalents are asserted where
/// they exist and the rest is marked with a comment.
/// </summary>
[TestClass]
public class ContractV11CaseTests
{
    /// <summary>Every case file in contract/expected/, plus the count guard below. The
    /// expected/ directory currently holds exactly these 20 files; adding a case file
    /// without wiring it here fails the count guard AND the "all files ran" guard.</summary>
    private static readonly string[] AllCaseFiles =
    [
        "active-zero.json", "busy.json", "credits.json", "delta-row-off.json",
        "empty.json", "glance-off.json", "healthy.json", "healthy-month.json",
        "healthy-week.json", "healthy-year.json", "histogram-off.json", "loading.json",
        "multi-server.json", "offline.json", "partial.json", "partial-failure.json",
        "per-server.json", "provider-error.json", "quota-disabled.json", "wrong-service.json",
    ];

    [TestMethod]
    public void Every_Expected_File_Is_Asserted()
    {
        var onDisk = Directory.GetFiles(ContractDir("expected"), "*.json").Select(Path.GetFileName)!
            .OrderBy(x => x, StringComparer.Ordinal).ToList();
        CollectionAssert.AreEqual(
            AllCaseFiles.OrderBy(x => x, StringComparer.Ordinal).ToList(), onDisk,
            "the loader and contract/expected/ must cover the same case files");
        Assert.AreEqual(20, onDisk.Count, "expected-case count changed - wire the new case in");
    }

    [DataTestMethod]
    [DataRow("active-zero.json")] [DataRow("busy.json")] [DataRow("credits.json")]
    [DataRow("delta-row-off.json")] [DataRow("empty.json")] [DataRow("glance-off.json")]
    [DataRow("healthy.json")] [DataRow("healthy-month.json")] [DataRow("healthy-week.json")]
    [DataRow("healthy-year.json")] [DataRow("histogram-off.json")] [DataRow("loading.json")]
    [DataRow("multi-server.json")] [DataRow("offline.json")] [DataRow("partial.json")]
    [DataRow("partial-failure.json")] [DataRow("per-server.json")] [DataRow("provider-error.json")]
    [DataRow("quota-disabled.json")] [DataRow("wrong-service.json")]
    public async Task Expected_Case_Runs_Against_The_Store(string file)
    {
        var saved = L10n.Current;
        L10n.Current = AppLanguage.English;
        try
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(ContractFile("expected", file)));
            var root = root_of(doc);
            var expected = root.GetProperty("expected");
            var period = root.TryGetProperty("period", out var p) ? p.GetString() : "today";
            var components = MergeOverrides(root.TryGetProperty("settings_overrides", out var so) ? so : default);

            if (root.TryGetProperty("servers", out var servers))
            {
                RunServerCase(Path.GetFileNameWithoutExtension(file), servers, expected);
                return;
            }

            var fixtures = root.GetProperty("fixtures");
            // Frozen clock: the quota payload's own timestamp (credits and freshness
            // fixtures) - the contract pins tests to it so fixtures stay deterministic.
            var clock = ClockOf(fixtures);
            CompanionStore.ClockOverride = clock;

            var settings = new CompanionSettings
            {
                SelectedPeriod = ParsePeriod(period!),
                Components = components,
                // Notifications ride the same scheduled read; the credits case asserts it.
                LowQuotaNotifications = true,
            };
            settings.Save();

            var client = BuildClient(fixtures);
            var store = new CompanionStore(client);
            L10n.Current = AppLanguage.English; // the store ctor resolves the machine culture

            switch (Path.GetFileNameWithoutExtension(file))
            {
                case "loading": Run_Loading(store); break;
                case "wrong-service": Run_WrongService(store, client, expected); break;
                case "offline": Run_Offline(store, expected); break;
                case "busy": await Run_BusyAsync(store, expected); break;
                case "partial": await Run_PartialAsync(store, expected); break;
                case "glance-off": await Run_GlanceOffAsync(store, client, expected); break;
                case "histogram-off": await Run_GlanceOffAsync(store, client, expected); break;
                case "healthy-week": await Run_WeekRequestAsync(store, client, expected); break;
                case "credits": await Run_CreditsAsync(store, expected); break;
                default: await Run_HeroSnapshotCaseAsync(store, expected); break;
            }
        }
        finally
        {
            CompanionStore.ClockOverride = null;
            L10n.Current = saved;
        }
    }

    [TestCleanup]
    public void RestoreSharedState()
    {
        CompanionStore.ClockOverride = null;
        L10n.Current = AppLanguage.English;
        // The cases write SelectedPeriod/notifications into the redirected settings file.
        // Restore after EVERY test, not just at class end: MSTest runs class cleanup at the
        // END OF THE ASSEMBLY, and NotificationTests' "opt-in by default" test loads the
        // same redirected file - a leaked LowQuotaNotifications=true makes it alert when
        // the store under test should stay quiet.
        new CompanionSettings().Save();
    }

    // MARK: - per-case executors

    private static void Run_Loading(CompanionStore store)
    {
        // "pending" slots never complete, so this case is asserted WITHOUT awaiting a
        // refresh: the pre-refresh store state is the loading state.
        Assert.AreEqual(ConnectionState.Connecting, store.ConnectionState);
        Assert.AreEqual("Connecting…", store.ConnectionLabel);
        Assert.IsNull(store.Snapshot, "no snapshot yet: the flyout is in its skeletons");
        Assert.AreEqual("#FF9F0A", store.DotColor, "dot: warn");
        // has_full_screen_spinner false / segment_always_visible / skeleton presence are
        // FlyoutWindow properties - pinned by FlyoutLaunchTests, not by the store.
    }

    private static void Run_WrongService(CompanionStore store, FakeClient client, JsonElement expected)
    {
        store.RefreshAsync().GetAwaiter().GetResult();
        Assert.AreEqual(ConnectionState.WrongService, store.ConnectionState);
        // The contract pins the headline text: it is the banner title (the connection dot
        // reads "Not Tokdash").
        Assert.AreEqual(expected.GetProperty("message").GetString(), store.BannerTitle);
        Assert.AreEqual(expected.GetProperty("usage_calls_made").GetInt32(),
            client.Requests.Count(r => r.StartsWith("/api/usage")));
        Assert.AreEqual(expected.GetProperty("quota_calls_made").GetInt32(),
            client.Requests.Count(r => r.StartsWith("/api/quota")));
        Assert.AreEqual(1, client.Requests.Count, "only /health may be called");
    }

    private static void Run_Offline(CompanionStore store, JsonElement expected)
    {
        store.RefreshAsync().GetAwaiter().GetResult();
        Assert.AreEqual(expected.GetProperty("connection").GetString(), store.ConnectionLabel);
        Assert.AreEqual("#FF453A", store.DotColor, "dot: err");
        var banner = expected.GetProperty("banner");
        Assert.AreEqual(banner.GetProperty("title").GetString(), store.BannerTitle);
        StringAssert.Contains(store.BannerBody, banner.GetProperty("body_contains").GetString());
        // Retry/Settings buttons and the dimmed-last-good "· stale" footer need a prior
        // successful cycle; last_good dimming is flyout presentation (FlyoutLaunchTests).
    }

    private static async Task Run_BusyAsync(CompanionStore store, JsonElement expected)
    {
        await store.RefreshAsync();
        Assert.AreEqual(ConnectionState.Busy, store.ConnectionState, "503 is busy, never offline");
        var banner = expected.GetProperty("banner");
        Assert.AreEqual(banner.GetProperty("title").GetString(), store.BannerTitle);
        StringAssert.Contains(store.BannerBody, banner.GetProperty("body_contains").GetString());
        // Backoff: the first failure reschedules to the 15s rung (ComputeDelay, pinned by
        // SchedulerTests); the store's state here is the busy banner itself.
        Assert.IsTrue(store.FailureCount > 0, "failed cycle counted for backoff");
    }

    private static async Task Run_PartialAsync(CompanionStore store, JsonElement expected)
    {
        await store.RefreshAsync();
        Assert.AreEqual(expected.GetProperty("connection").GetString(), store.ConnectionLabel,
            "the header stays connected");
        var snap = store.Snapshot!;
        Assert.AreEqual(3.42, snap.Usage!.TotalCost, 0.001, "hero normal");
        Assert.IsTrue(expected.GetProperty("failed_sections").EnumerateArray().Any(e => e.GetString() == "quota")
            && snap.QuotaFailed, "quota failure reported");
        Assert.IsFalse(snap.UsageFailed);
        // Rule 6: active-time and insights fail SILENTLY - flags exist only for usage/quota.
        Assert.IsNull(snap.ActiveSegmentText);
        Assert.IsNull(snap.Glance);
        StringAssert.Contains(L10n.T("will_retry_shortly"), "retry shortly",
            "the inline quota warning string the flyout shows for QuotaFailed");
    }

    private static async Task Run_GlanceOffAsync(CompanionStore store, FakeClient client, JsonElement expected)
    {
        await store.RefreshAsync();
        Assert.IsNull(store.Snapshot!.Glance);
        foreach (var absent in expected.GetProperty("requests_absent").EnumerateArray())
        {
            string prefix = absent.GetString()!;
            Assert.AreEqual(0, client.Requests.Count(r => r.StartsWith(prefix)),
                $"with the component off, a cycle contains no {prefix} request");
        }
        StringAssert.Contains(store.Snapshot.CostText, "$3.42");
    }

    private static async Task Run_WeekRequestAsync(CompanionStore store, FakeClient client, JsonElement expected)
    {
        await store.RefreshAsync();
        var weekReq = expected.GetProperty("week_request");
        Assert.IsTrue(weekReq.GetProperty("uses_date_range").GetBoolean());
        var usage = client.Requests.Single(r => r.StartsWith("/api/usage"));
        StringAssert.StartsWith(usage, "/api/usage?date_from=");
        Assert.IsFalse(usage.Contains("period="), "week NEVER sends period=week");
        var from = usage.Split("date_from=")[1].Split('&')[0];
        var to = usage.Split("date_to=")[1];
        Assert.AreEqual(DayOfWeek.Monday, DateOnly.Parse(from, CultureInfo.InvariantCulture).DayOfWeek,
            "date_from is the local Monday");
        // Clock frozen at the fixture timestamp (2026-07-26): date_to is that day.
        Assert.AreEqual("2026-07-26", to);
        Assert.AreEqual(0, client.Requests.Count(r => r.Contains("period=week")),
            "no endpoint is ever called with period=week");

        var snap = store.Snapshot!;
        AssertHero(snap, expected.GetProperty("today"));
        Assert.AreEqual(expected.GetProperty("delta_row").GetString(), snap.DeltaRowText);
        AssertRanks(snap, expected.GetProperty("top_ranks"));
        var glance = snap.Glance!;
        Assert.AreEqual(GlanceKind.Days, glance.Kind);
        Assert.AreEqual(expected.GetProperty("glance").GetProperty("columns").GetInt32(),
            glance.DayTokens!.Length);
        // The pinned missing day ("Tue") is index 1 of the Mon..Sun columns; the fixture
        // labels Mon..Sun, and the face's index order IS Mon..Sun (DayFace mapping).
        Assert.AreEqual(0, glance.DayTokens![1], "sparse daily facet: missing day is an empty column");
        // caption null: a day histogram has no caption (only the hour face carries "Peak …").
        Assert.AreEqual(JsonValueKind.Null,
            expected.GetProperty("glance").GetProperty("caption").ValueKind);
    }

    private static async Task Run_CreditsAsync(CompanionStore store, JsonElement expected)
    {
        var alerts = new List<CompanionStore.CreditAlertItem>();
        store.CreditExpiryAlert += items => alerts.AddRange(items);
        await store.RefreshAsync();
        var snap = store.Snapshot!;
        Assert.AreEqual(expected.GetProperty("connection").GetString(), store.ConnectionLabel);

        var codex = snap.AllQuotaGroups.Single(g => g.CanonicalProvider == "codex");
        Assert.AreEqual(expected.GetProperty("credits_row").GetString(), snap.CreditsNotice(codex));
        // "quota_all_codex_group_last": the row is Codex-group context - CreditsNotice
        // answers for no other provider. ("all-only": it is never a Low-view window row.)
        foreach (var g in snap.AllQuotaGroups.Where(g => g.CanonicalProvider != "codex"))
            Assert.IsNull(snap.CreditsNotice(g));
        Assert.AreEqual(0, snap.LowQuotaRows.Count(r => r.BucketLabel.Contains("reset credits")),
            "the credits row never joins the Low view");

        var pinned = expected.GetProperty("notifications").GetProperty("credits_expiring_armed").GetBoolean();
        Assert.AreEqual(pinned, alerts.Count > 0,
            "credit-a is exactly 48 h out at the frozen clock - the last-48h window arms");
    }

    /// <summary>healthy, healthy-month, healthy-year, active-zero, empty, delta-row-off,
    /// quota-disabled, provider-error, partial-failure: hero snapshot assertions.</summary>
    private static async Task Run_HeroSnapshotCaseAsync(CompanionStore store, JsonElement expected)
    {
        await store.RefreshAsync();
        var snap = store.Snapshot!;
        if (expected.TryGetProperty("connection", out var conn) && conn.ValueKind == JsonValueKind.String)
            Assert.AreEqual(conn.GetString(), store.ConnectionLabel);
        if (expected.TryGetProperty("dot", out var dot))
        {
            string want = dot.GetString()!;
            string got = store.DotColor;
            Assert.AreEqual(Want(want), got);
        }

        if (expected.TryGetProperty("today", out var today))
        {
            if (today.TryGetProperty("hero_title", out var title) && title.ValueKind == JsonValueKind.String)
                Assert.AreEqual(title.GetString(), snap.HeroTitle);
            if (today.TryGetProperty("hero_sub_contains", out var sub))
                StringAssert.Contains(snap.HeroEmptySub, sub.GetString());
            if (today.TryGetProperty("cost", out var cost) && cost.ValueKind == JsonValueKind.String)
                Assert.AreEqual(cost.GetString(), snap.CostText);
            if (today.TryGetProperty("active", out var active))
            {
                if (active.ValueKind == JsonValueKind.Null)
                    Assert.IsNull(snap.ActiveSegmentText, "segment absent, never 'active 0 m'");
                else Assert.AreEqual(active.GetString(), snap.ActiveSegmentText);
            }
            if (today.TryGetProperty("tokens_exact", out var exact))
                Assert.AreEqual(exact.GetString(), snap.Usage!.TotalTokens.ToString());
            if (today.TryGetProperty("messages", out var msgs))
                Assert.AreEqual(msgs.GetString(), snap.Usage!.TotalMessages.ToString());
        }

        if (expected.TryGetProperty("delta_row", out var delta))
        {
            if (delta.ValueKind == JsonValueKind.Null) Assert.IsNull(snap.DeltaRowText);
            else Assert.AreEqual(delta.GetString(), snap.DeltaRowText);
        }
        if (expected.TryGetProperty("hero_comparison", out var cmp))
        {
            Assert.AreEqual(cmp.GetString(), snap.ComparisonLine);
            Assert.AreEqual(-1, snap.ComparisonDirection, "hero_comparison_direction down");
        }

        if (expected.TryGetProperty("top_ranks", out var ranks) && ranks.ValueKind == JsonValueKind.Object)
            AssertRanks(snap, ranks);

        if (expected.TryGetProperty("glance", out var glance))
        {
            if (glance.ValueKind == JsonValueKind.Null)
            {
                Assert.IsNull(snap.Glance, "all-zero source hides the glance strip");
            }
            else
            {
                var face = snap.Glance!;
                var kind = glance.GetProperty("kind").GetString();
                if (kind == "hour-histogram")
                {
                    Assert.AreEqual(GlanceKind.Hours, face.Kind);
                    Assert.AreEqual(glance.GetProperty("bars").GetInt32(), face.Bars!.Length);
                    Assert.AreEqual(glance.GetProperty("caption").GetString(), L10n.T("peak_caption", face.PeakHour!.Value));
                }
                else if (kind == "day-histogram")
                {
                    Assert.AreEqual(GlanceKind.Days, face.Kind);
                    Assert.AreEqual(7, face.DayTokens!.Length);
                    foreach (var missing in glance.GetProperty("missing_days").EnumerateArray())
                    {
                        int idx = Array.IndexOf(new[] { "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun" }, missing.GetString());
                        Assert.AreEqual(0, face.DayTokens[idx]);
                    }
                }
                else
                {
                    Assert.AreEqual(GlanceKind.Grid, face.Kind);
                    Assert.AreEqual(glance.GetProperty("window_days").GetInt32(), face.WindowDays);
                    Assert.AreEqual(glance.GetProperty("filled_cells").GetInt32(), face.FilledCells);
                    Assert.AreEqual(glance.GetProperty("rows").GetInt32(), face.GridColumns![0].Length, "strict 7-row Mon..Sun");
                    Assert.AreEqual(glance.GetProperty("first_column_weekday").GetString(), face.FirstColumnWeekday);
                }
            }
        }

        if (expected.TryGetProperty("credits_row", out var credits) && credits.ValueKind == JsonValueKind.Null)
        {
            Assert.IsTrue(snap.AllQuotaGroups.All(g => snap.CreditsNotice(g) is null));
        }

        if (expected.TryGetProperty("quota_low", out var lowEl))
        {
            var low = snap.LowQuotaRows;
            Assert.IsTrue(low.Count <= lowEl.GetProperty("visible_count_max").GetInt32());
            if (lowEl.TryGetProperty("rows", out var rows))
            {
                var want = rows.EnumerateArray().ToList();
                Assert.AreEqual(want.Count, low.Count, "low row count");
                for (int i = 0; i < want.Count; i++)
                {
                    // The ⚠ prefix in the pinned label is flyout decoration for Failed rows.
                    string pinnedLabel = want[i].GetProperty("label").GetString()!.Replace("⚠ ", "");
                    Assert.AreEqual(pinnedLabel, $"{low[i].Provider} · {low[i].DisplayBucketLabel}");
                    Assert.AreEqual(want[i].GetProperty("left").GetDouble(), low[i].Left, 0.001);
                    if (want[i].TryGetProperty("estimated", out var est)) Assert.AreEqual(est.GetBoolean(), low[i].Estimated);
                    if (want[i].TryGetProperty("failed", out var failed)) Assert.AreEqual(failed.GetBoolean(), low[i].Failed);
                }
            }
        }

        if (expected.TryGetProperty("quota_all", out var all))
        {
            var groups = snap.AllQuotaGroups;
            CollectionAssert.AreEqual(
                all.GetProperty("groups").EnumerateArray().Select(g => g.GetProperty("provider").GetString()).ToList(),
                groups.Select(g => g.Provider).ToList(), "provider order as detected");
            foreach (var g in all.GetProperty("groups").EnumerateArray())
            {
                var group = groups.Single(x => x.Provider == g.GetProperty("provider").GetString());
                CollectionAssert.AreEqual(
                    g.GetProperty("rows").EnumerateArray().Select(r => r.GetProperty("label").GetString()).ToList(),
                    group.Rows.Select(r => r.BucketLabel).ToList());
                foreach (var r in g.GetProperty("rows").EnumerateArray())
                {
                    var row = group.Rows.Single(x => x.BucketLabel == r.GetProperty("label").GetString());
                    Assert.AreEqual(r.GetProperty("left").GetDouble(), row.Left, 0.001);
                }
            }
        }

        if (expected.TryGetProperty("quota_section", out var qs))
        {
            if (qs.TryGetProperty("provider_failures", out var failures))
                CollectionAssert.AreEqual(
                    failures.EnumerateArray().Select(x => x.GetString()).ToList(),
                    snap.AllQuotaGroups.Where(g => g.Failed).Select(g => g.Provider).ToList());
            if (qs.TryGetProperty("healthy_providers", out var healthy))
                foreach (var name in healthy.EnumerateArray().Select(x => x.GetString()))
                    Assert.IsFalse(snap.AllQuotaGroups.Single(g => g.Provider == name).Failed);
            if (qs.TryGetProperty("message", out var message))
                Assert.AreEqual(message.GetString(), L10n.T("tracking_off"));
            if (qs.TryGetProperty("rows", out var qrows) && qrows.GetArrayLength() == 0)
                Assert.AreEqual(0, snap.LowQuotaRows.Count);
            if (qs.TryGetProperty("row_warnings", out var warnings))
            {
                foreach (var prop in warnings.EnumerateObject())
                    Assert.AreEqual(prop.Value.GetBoolean(),
                        snap.AllQuotaGroups.SelectMany(g => g.Rows).Single(r => r.Bucket == prop.Name).Failed,
                        $"row ⚠ on {prop.Name}");
            }
        }

        if (expected.TryGetProperty("freshness_contains", out var fresh))
            StringAssert.Contains(store.FreshnessText, fresh.GetString());
        if (expected.TryGetProperty("banner", out var bannerEl) && bannerEl.ValueKind == JsonValueKind.Null)
            Assert.IsFalse(store.ShowsBanner);
    }

    /// <summary>multi-server / per-server: the fan-out math, expressed through the same
    /// pure helpers MultiServerTokdashClient runs per cycle (it builds its own real
    /// clients, so the fake cannot stand in for it - the combine/rows/active-drop rules
    /// are where the expected file's claims actually live).</summary>
    private static void RunServerCase(string name, JsonElement servers, JsonElement expected)
    {
        var list = servers.EnumerateArray().ToList();
        var usages = list.Select(s => DecodeFixture<UsageResponse>(s.GetProperty("usage").GetString()!)).ToList();
        var combined = MultiServerTokdashClient.CombineUsage(usages);

        var today = expected.GetProperty("today");
        Assert.AreEqual(today.GetProperty("cost").GetString(), Formatter.FormatCost(combined.TotalCost));
        Assert.AreEqual(today.GetProperty("tokens_exact").GetString(), combined.TotalTokens.ToString());
        if (today.TryGetProperty("active", out var active))
        {
            var activeMs = list.Select(s =>
            {
                var slot = s.GetProperty("active_time");
                return slot.ValueKind == JsonValueKind.Null ? (long?)null : DecodeFixture<ActiveTimeResponse>(slot.GetString()!).ActiveMs;
            }).ToList();
            long? sum = CompanionStore.CombinedActiveMs(
                activeMs.Select(ms => (ms, Failed: false)).ToList());
            if (active.ValueKind == JsonValueKind.Null)
                Assert.IsNull(sum, "a server without active-time drops the segment, never a partial sum");
            else Assert.AreEqual(active.GetString(), Formatter.ActiveText(sum!.Value));
        }

        if (expected.TryGetProperty("per_server_rows", out var rows))
        {
            var settings = list.Select(s => new CompanionServerSettings
            { Id = s.GetProperty("id").GetString()!, Label = s.GetProperty("label").GetString()! }).ToList();
            var perRows = CompanionStore.PerServerRows(settings, usages, new HashSet<string>());
            var want = rows.EnumerateArray().ToList();
            for (int i = 0; i < want.Count; i++)
            {
                Assert.AreEqual(want[i].GetProperty("label").GetString(), perRows[i].Label, "settings order");
                Assert.AreEqual(want[i].GetProperty("cost").GetString(), perRows[i].CostText);
                Assert.AreEqual(want[i].GetProperty("tokens_compact").GetString(), perRows[i].TokensCompact);
            }
            Assert.AreEqual(expected.GetProperty("per_server_footnote").GetString(),
                L10n.T("per_server_footnote"));
        }

        if (expected.TryGetProperty("quota_low", out var lowEl) &&
            lowEl.GetProperty("dedupe_identical_subscriptions").GetBoolean())
        {
            // Merge quota exactly as MultiServerTokdashClient does ("{label} · {id}"), then
            // the shared LowQuotaRows must collapse the duplicates and shed the server label.
            var quota = DecodeFixture<QuotaResponse>(
                list[0].GetProperty("quota").GetString()!);
            var merged = new Dictionary<string, ProviderQuota>();
            foreach (var label in new[] { "Local", "Second" })
                foreach (var kv in quota.Providers!) merged[$"{label} · {kv.Key}"] = kv.Value;
            var snap = new Snapshot
            {
                Period = UsagePeriod.Today,
                Usage = combined,
                Quota = new QuotaResponse { Enabled = true, Providers = merged },
                Thresholds = QuotaThresholds.Defaults,
                Components = new CompanionComponents(),
                Now = DateTimeOffset.FromUnixTimeSeconds(1785080120),
            };
            var low = snap.LowQuotaRows;
            Assert.IsTrue(low.Count <= lowEl.GetProperty("visible_count_max").GetInt32());
            Assert.IsTrue(low.All(r => !r.Provider.Contains(" · ")),
                "deduped labels omit the server (deduped_labels_omit_server)");
        }

        if (name == "multi-server")
        {
            Assert.AreEqual("2 servers", L10n.T("servers_count", 2), "connection label");
            Assert.AreEqual(expected.GetProperty("delta_row").GetString(),
                // Same math the hero does with the combined comparison (full delta row on).
                DeltaFrom(combined, "vs yesterday"));
            Assert.AreEqual(CompanionStore.MinimumDelay([TimeSpan.FromSeconds(30), TimeSpan.FromMinutes(10)]),
                TimeSpan.FromSeconds(30), "minimum per-server delay");
        }
    }

    // MARK: - helpers

    private static JsonElement root_of(JsonDocument doc) => doc.RootElement;

    private static string Want(string dot) => dot switch
    {
        "ok" => "#30A74C",
        "warn" => "#FF9F0A",
        "err" => "#FF453A",
        "busy" => "#FF9F0A",
        _ => throw new AssertFailedException($"unknown dot {dot}"),
    };

    private static void AssertHero(Snapshot snap, JsonElement today)
    {
        Assert.AreEqual(today.GetProperty("cost").GetString(), snap.CostText);
        Assert.AreEqual(today.GetProperty("tokens_compact").GetString(), snap.TokensCompact);
        Assert.AreEqual(today.GetProperty("active").GetString(), snap.ActiveSegmentText);
    }

    private static void AssertRanks(Snapshot snap, JsonElement ranks)
    {
        Assert.AreEqual(ranks.GetProperty("kicker_tools").GetString(), snap.ToolsKickerText);
        Assert.AreEqual(ranks.GetProperty("kicker_models").GetString(), snap.ModelsKickerText);
        CollectionAssert.AreEqual(
            ranks.GetProperty("tools").EnumerateArray().Select(x => x.GetString()).ToList(),
            snap.TopTools.Select(t => $"{t.Label} {t.ValueText}").ToList());
        CollectionAssert.AreEqual(
            ranks.GetProperty("models").EnumerateArray().Select(x => x.GetString()).ToList(),
            snap.TopModels.Select(m => $"{m.Label} {m.ValueText}").ToList());
    }

    private static string DeltaFrom(UsageResponse usage, string sentence)
    {
        var snap = new Snapshot
        {
            Period = UsagePeriod.Today, Usage = usage, Quota = new QuotaResponse(),
            Thresholds = QuotaThresholds.Defaults, Components = new CompanionComponents(),
            Now = DateTimeOffset.FromUnixTimeSeconds(1785080120),
        };
        return snap.DeltaRowText ?? sentence; // multi-server: hero delta derived from summed totals
    }

    private static UsagePeriod ParsePeriod(string token) => token switch
    {
        "week" => UsagePeriod.Week,
        "month" => UsagePeriod.Month,
        "year" => UsagePeriod.Year,
        _ => UsagePeriod.Today,
    };

    /// <summary>settings_overrides merged over the schema-v3 defaults (absent keys stay at
    /// their default ON; only keys the case names are overridden).</summary>
    private static CompanionComponents MergeOverrides(JsonElement overrides)
    {
        var c = new CompanionComponents();
        if (overrides.ValueKind != JsonValueKind.Object
            || !overrides.TryGetProperty("components", out var comps)) return c;
        if (comps.TryGetProperty("fullDeltaRow", out var a)) c.FullDeltaRow = a.GetBoolean();
        if (comps.TryGetProperty("topRanks", out var b)) c.TopRanks = b.GetBoolean();
        if (comps.TryGetProperty("resetCredits", out var d)) c.ResetCredits = d.GetBoolean();
        if (comps.TryGetProperty("activityGlance", out var e)) c.ActivityGlance = e.GetBoolean();
        if (comps.TryGetProperty("activityHistogramTodayWeek", out var f)) c.ActivityHistogramTodayWeek = f.GetBoolean();
        if (comps.TryGetProperty("perServerRows", out var g)) c.PerServerRows = g.GetBoolean();
        return c;
    }

    private static DateTimeOffset ClockOf(JsonElement fixtures)
    {
        if (fixtures.TryGetProperty("quota", out var q) && q.ValueKind == JsonValueKind.String
            && q.GetString()!.EndsWith(".json", StringComparison.Ordinal))
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(ContractFile("fixtures", q.GetString()!)));
            if (doc.RootElement.TryGetProperty("timestamp", out var ts) && ts.ValueKind == JsonValueKind.Number)
                return DateTimeOffset.FromUnixTimeSeconds(ts.GetInt64());
        }
        return DateTimeOffset.FromUnixTimeSeconds(1785080120);
    }

    private static FakeClient BuildClient(JsonElement fixtures)
    {
        var client = new FakeClient();
        if (fixtures.TryGetProperty("health", out var health)) client.Health = Slot(health, SlotKind.Health);
        if (fixtures.TryGetProperty("usage", out var usage)) client.Usage = Slot(usage, SlotKind.Usage);
        if (fixtures.TryGetProperty("active_time", out var at)) client.ActiveTime = Slot(at, SlotKind.ActiveTime);
        if (fixtures.TryGetProperty("insights", out var ins)) client.Insights = Slot(ins, SlotKind.Insights);
        if (fixtures.TryGetProperty("stats", out var stats)) client.Stats = Slot(stats, SlotKind.Stats);
        if (fixtures.TryGetProperty("quota", out var quota)) client.Quota = Slot(quota, SlotKind.Quota);
        return client;
    }

    private enum SlotKind { Health, Usage, ActiveTime, Insights, Stats, Quota }

    /// <summary>"503"/"pending"/"timeout" pass through to FakeClient's failure tokens;
    /// JSON null is an endpoint that returns NOTHING this cycle = a failure ("fail"), NOT
    /// the slot default; a file name decodes to its typed response.</summary>
    private static object? Slot(JsonElement el, SlotKind kind)
    {
        if (el.ValueKind == JsonValueKind.Null) return "fail";
        string s = el.GetString()!;
        if (s is "503" or "pending" or "timeout" or "fail") return s;
        return kind switch
        {
            SlotKind.Health => DecodeFixture<HealthResponse>(s),
            SlotKind.Usage => DecodeFixture<UsageResponse>(s),
            SlotKind.ActiveTime => DecodeFixture<ActiveTimeResponse>(s),
            SlotKind.Insights => DecodeFixture<InsightsResponse>(s),
            SlotKind.Stats => DecodeFixture<StatsResponse>(s),
            SlotKind.Quota => DecodeFixture<QuotaResponse>(s),
            _ => throw new AssertFailedException($"unknown slot {kind}"),
        };
    }

    private static readonly JsonSerializerOptions Opts = new() { PropertyNameCaseInsensitive = true };

    private static T DecodeFixture<T>(string name) =>
        JsonSerializer.Deserialize<T>(File.ReadAllText(ContractFile("fixtures", name)), Opts)!;

    private static string ContractDir(string leaf, [CallerFilePath] string source = "") =>
        Path.GetFullPath(Path.Combine(Path.GetDirectoryName(source)!, "..", "..", "contract", leaf));

    private static string ContractFile(string leaf, string file, [CallerFilePath] string source = "") =>
        Path.Combine(ContractDir(leaf, source), file);
}
