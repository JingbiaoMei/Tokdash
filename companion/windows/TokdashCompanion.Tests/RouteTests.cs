using System.Net;
using System.Net.Http;
using System.Text.Json;
using Microsoft.VisualStudio.TestTools.UnitTesting;

namespace TokdashCompanion.Tests;

[TestClass]
public class RouteTests
{
    private static CompanionServerSettings Host() => new() {
        BaseUrl = "https://lan.test/tokdash", Routes = ["https://tail.test/tokdash", "https://wrong.test"], InstanceId = "daemon-a"
    };
    private static RouteProbe Probe(string address, string? identity, double ms) =>
        new(address, new HealthResponse("ok", "tokdash", "1", identity), ms, null);

    [TestMethod]
    public void FastestAndPinnedRoutesMustProveTheSameIdentity()
    {
        var host = Host();
        var probes = new[] { Probe(host.BaseUrl, "daemon-a", 20), Probe(host.Routes[0], "daemon-a", 5), Probe(host.Routes[1], "other", 1) };
        Assert.AreEqual(host.Routes[0], TokdashClient.SelectRoutes(host, probes)[0].Address);
        host.PreferredRoute = host.BaseUrl;
        Assert.AreEqual(host.BaseUrl, TokdashClient.SelectRoutes(host, probes)[0].Address);
        Assert.AreEqual(host.Routes[0], TokdashClient.SelectRoutes(host, probes.Skip(1))[0].Address);
        Assert.AreEqual(2, TokdashClient.SelectRoutes(host, probes).Count);
    }

    [TestMethod]
    public void LegacySettingsAndIdentityLessServersRemainSingleAddress()
    {
        var host = JsonSerializer.Deserialize<CompanionServerSettings>("""{"id":"old","label":"Work","baseUrl":"http://local.test","enabled":true}""")!;
        Assert.AreEqual(1, host.Addresses.Count);
        host.Routes.Add("https://tail.test");
        var selected = TokdashClient.SelectRoutes(host, [Probe(host.BaseUrl, null, 20), Probe(host.Routes[0], "other", 1)]);
        Assert.AreEqual(1, selected.Count);
        Assert.AreEqual(host.BaseUrl, selected[0].Address);
        host.InstanceId = "daemon-a"; host.PreferredRoute = host.Routes[0];
        var roundTrip = JsonSerializer.Deserialize<CompanionServerSettings>(JsonSerializer.Serialize(host))!;
        Assert.AreEqual(host.PreferredRoute, roundTrip.PreferredRoute);
        Assert.AreEqual(host.InstanceId, roundTrip.InstanceId);
        CollectionAssert.AreEqual(host.Addresses, roundTrip.Addresses);
    }

    [TestMethod]
    public void UnverifiedAlternateCannotEstablishAnOfflineHostsIdentity()
    {
        var host = Host(); host.InstanceId = null;
        Assert.AreEqual(0, TokdashClient.SelectRoutes(host, [Probe(host.Routes[0], "other", 1)]).Count);
    }

    [TestMethod]
    public async Task MultipleConfiguredHostsWithOneIdentityCountUsageOnlyOnce()
    {
        var servers = new[] { CompanionServerSettings.Create("http://one.test"), CompanionServerSettings.Create("http://two.test") };
        using var client = new MultiServerTokdashClient(servers, _ => new FakeClient { Health = new HealthResponse("ok", "tokdash", "1", "same") });
        await client.HealthAsync();
        Assert.AreEqual(1000, (await client.UsageAsync("today")).TotalTokens);
        Assert.AreEqual(1, client.LastPerServerRows.Count);
    }

    [TestMethod]
    public async Task FailedActiveRouteRetriesVerifiedAlternateAndPreservesBasePath()
    {
        using var handler = new RouteHandler();
        var host = Host(); host.PreferredRoute = host.BaseUrl;
        using var client = new TokdashClient(host, handler);
        Assert.AreEqual("daemon-a", (await client.HealthAsync()).InstanceId);
        Assert.AreEqual(42, (await client.UsageAsync("today")).TotalTokens);
        CollectionAssert.AreEqual(new[] { "lan.test/tokdash/api/usage?period=today", "tail.test/tokdash/api/usage?period=today" }, handler.UsageRequests.ToArray());
    }

    private sealed class RouteHandler : HttpMessageHandler
    {
        public List<string> UsageRequests { get; } = [];
        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken ct)
        {
            var uri = request.RequestUri!;
            if (uri.AbsolutePath.EndsWith("/health"))
                return Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK) { Content = new StringContent(
                    "{\"status\":\"ok\",\"service\":\"tokdash\",\"version\":\"1\",\"instance_id\":\"" + (uri.Host == "wrong.test" ? "other" : "daemon-a") + "\"}") });
            UsageRequests.Add(uri.Host + uri.PathAndQuery);
            if (uri.Host == "lan.test") throw new HttpRequestException("Route down");
            return Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK) { Content = new StringContent("{\"total_tokens\":42}") });
        }
    }
}
