using System.Globalization;

namespace TokdashCompanion;

/// <summary>
/// Manual localization table for the companion. The app has no native .resx resources: most
/// display strings are built in code (the store + flyout code-behind), and a runtime language
/// toggle that switches live is simplest with a plain per-language dictionary. ``Current`` is
/// resolved once at launch from <see cref="CompanionSettings.Language"/> and again on change;
/// the store raises a property change so the flyout re-renders.
///
/// English is the default and stays byte-identical to the pre-localization strings so the
/// contract/snapshot tests (which run under English) keep passing. zh-Hans is the only other
/// language. Mirrored by <c>L10n.swift</c> on macOS.
/// </summary>
public enum AppLanguage
{
    System,
    English,
    ZhHans,
}

public static class L10n
{
    /// <summary>Resolved language actually used for lookups. Tests default to English.</summary>
    public static AppLanguage Current = AppLanguage.English;

    /// <summary>Label shown in the Settings combo.</summary>
    public static string DisplayName(this AppLanguage lang) => lang switch
    {
        AppLanguage.System => T("language_system"),
        AppLanguage.English => "English",
        AppLanguage.ZhHans => "中文",
        _ => lang.ToString(),
    };

    /// <summary>Resolve the effective language for a setting. System follows the UI culture:
    /// any zh* locale maps to Simplified (the only variant shipped).</summary>
    public static AppLanguage Resolve(AppLanguage setting)
    {
        if (setting != AppLanguage.System) return setting;
        try
        {
            string name = CultureInfo.CurrentUICulture.Name;
            return name.StartsWith("zh", StringComparison.OrdinalIgnoreCase) ? AppLanguage.ZhHans : AppLanguage.English;
        }
        catch
        {
            return AppLanguage.English;
        }
    }

    /// <summary>Translate a key, with optional <see cref="string.Format(string,object[])"/> args.
    /// Falls back to English, then to the key itself, so a missing entry is visible.</summary>
    public static string T(string key, params object?[] args)
    {
        var table = Current == AppLanguage.ZhHans ? Zh : En;
        string template = table.TryGetValue(key, out var v) ? v : (En.TryGetValue(key, out var e) ? e : key);
        return args.Length == 0 ? template : string.Format(CultureInfo.InvariantCulture, template, args);
    }

    /// <summary>Plural suffix for the current language ("s" in English, "" in Chinese).</summary>
    public static string PluralS => Current == AppLanguage.ZhHans ? "" : "s";

    // English (source of truth; identical to the original hardcoded strings).

    private static readonly Dictionary<string, string> En = new()
    {
        ["language_system"] = "System",
        ["connecting"] = "Connecting…",
        ["connected"] = "Connected",
        ["busy"] = "Busy",
        ["offline"] = "Offline",
        ["not_tokdash"] = "Not Tokdash",
        ["local"] = "Local",
        ["server_connected"] = "{0} · Connected",

        ["banner_offline_title"] = "Tokdash is not reachable",
        ["banner_offline_body"] = "Start Tokdash, or check the server address in Settings.",
        ["banner_busy_title"] = "Tokdash is busy - retrying",
        ["banner_busy_body"] = "Last data shown below. Backing off automatically.",
        ["banner_wrong_title"] = "This address is not a Tokdash service",
        ["banner_wrong_body"] = "Check that the server address in Settings points at a Tokdash instance.",
        ["retry"] = "Retry",
        ["settings"] = "Settings",
        ["today"] = "TODAY",
        ["low"] = "Low",
        ["all"] = "All",

        ["today_unavailable"] = "Today's data unavailable",
        ["no_usage_today"] = "No usage recorded today",
        ["will_retry_shortly"] = "Will retry shortly.",
        ["tokdash_running"] = "Tokdash is running.",
        ["today_tokens_messages"] = "{0} tokens · {1} messages{2}",
        ["today_retrying_suffix"] = " · retrying",
        ["month_tokens_retrying"] = "{0} tokens · retrying",
        ["month_tokens"] = "{0} tokens",
        ["retrying"] = "retrying",

        ["subscription"] = "SUBSCRIPTION",
        ["all_subscriptions"] = "ALL SUBSCRIPTIONS",
        ["quota_unavailable"] = "Quota data unavailable - will retry shortly.",
        ["retry_now"] = "Retry now",
        ["tracking_off"] = "Subscription tracking is off",
        ["open_dashboard"] = "Open Dashboard",
        ["no_low_windows"] = "No subscription window is below its alert threshold.",
        ["couldnt_refresh"] = "Couldn't refresh - showing last known",
        ["estimated"] = "Estimated",
        ["percent_left"] = "{0}% left",
        ["window_5h"] = "5-hour",
        ["window_weekly"] = "Weekly",

        ["most_used_today"] = "Most used today  {0} · {1}",

        ["updated_just_now"] = "Updated just now",
        ["updated_min_ago"] = "Updated {0} min ago",
        ["updated_h_ago"] = "Updated {0} h ago",
        ["updated_d_ago"] = "Updated {0} d ago",
        ["stale_suffix"] = " · stale",
        ["no_data_yet"] = "No data yet",

        ["tray_hint"] = "Right-click tray icon for more",
        ["open_tokdash"] = "Open Tokdash",
        ["refresh"] = "Refresh",
        ["exit"] = "Exit",

        ["tooltip_today"] = "Tokdash - Today {0} · {1} tokens",
        ["tooltip_connecting"] = "Tokdash - connecting…",
        ["tooltip_no_usage"] = "Tokdash - No usage yet",
        ["tooltip_busy"] = "Tokdash - Busy",
        ["tooltip_offline"] = "Tokdash - Offline",
        ["tooltip_not_tokdash"] = "Tokdash - Not Tokdash",
        ["tooltip_default"] = "Tokdash",

        ["notif_low_title"] = "Tokdash - low quota",
        ["notif_low_single"] = "{0} {1} is at {2}% remaining.",
        ["notif_low_multi"] = "{0} subscription windows are low. {1} {2} at {3}%.",

        ["settings_window_title"] = "Tokdash Settings",
        ["section_server"] = "Server",
        ["base_url"] = "Base URL",
        ["test"] = "Test",
        ["server_hint"] = "Default: http://127.0.0.1:55423. Tailscale HTTPS URLs are supported.",
        ["section_startup"] = "Startup",
        ["launch_at_login"] = "Launch at login",
        ["section_notifications"] = "Notifications",
        ["low_quota_notifications"] = "Low-quota notifications",
        ["low_quota_hint"] = "Notifies when a subscription window crosses its threshold. Opt-in.",
        ["section_thresholds"] = "Quota alert thresholds (% remaining)",
        ["threshold_5h"] = "5-hour: {0}%",
        ["threshold_weekly"] = "Weekly: {0}%",
        ["threshold_other"] = "Default: {0}%",
        ["section_language"] = "Language",
        ["language_hint"] = "Follows the system language by default.",
        ["cancel"] = "Cancel",
        ["save"] = "Save",
        ["testing"] = "Testing…",
        ["test_bad_url"] = "Enter an absolute http:// or https:// URL.",
        ["valid_url"] = "Enter a valid http:// or https:// URL.",
        ["test_not_tokdash"] = "Reachable, but not a Tokdash server.",
        ["test_reachable_error"] = "Couldn't reach it: {0}",
        ["test_ok"] = "Connected to {0} · Tokdash {1}",
        ["launch_failed"] = "Windows did not enable launch at login. Check Settings > Apps > Startup.",

        ["comparison_below"] = "{0}% below yesterday",
        ["comparison_above"] = "{0}% above yesterday",

        ["resets_soon"] = "resets soon",
        ["resets_in_minutes"] = "resets in {0} minute{1}",
        ["resets_in_hours"] = "resets in {0} hour{1}",
        ["plural_s"] = "s",
    };

    // Simplified Chinese (zh-Hans).

    private static readonly Dictionary<string, string> Zh = new()
    {
        ["language_system"] = "跟随系统",
        ["connecting"] = "连接中…",
        ["connected"] = "已连接",
        ["busy"] = "忙碌",
        ["offline"] = "离线",
        ["not_tokdash"] = "非 Tokdash",
        ["local"] = "本地",
        ["server_connected"] = "{0} · 已连接",

        ["banner_offline_title"] = "无法连接 Tokdash",
        ["banner_offline_body"] = "请启动 Tokdash，或在设置中检查服务器地址。",
        ["banner_busy_title"] = "Tokdash 正忙 - 正在重试",
        ["banner_busy_body"] = "下方显示最近的数据，正在自动退避重试。",
        ["banner_wrong_title"] = "该地址不是 Tokdash 服务",
        ["banner_wrong_body"] = "请在设置中确认服务器地址指向 Tokdash 实例。",
        ["retry"] = "重试",
        ["settings"] = "设置",
        ["today"] = "今日",
        ["low"] = "少量",
        ["all"] = "全部",

        ["today_unavailable"] = "今日数据不可用",
        ["no_usage_today"] = "今日暂无用量",
        ["will_retry_shortly"] = "稍后重试。",
        ["tokdash_running"] = "Tokdash 正在运行。",
        ["today_tokens_messages"] = "{0} tokens · {1} 条消息{2}",
        ["today_retrying_suffix"] = " · 重试中",
        ["month_tokens_retrying"] = "{0} tokens · 重试中",
        ["month_tokens"] = "{0} tokens",
        ["retrying"] = "重试中",

        ["subscription"] = "订阅",
        ["all_subscriptions"] = "全部订阅",
        ["no_low_windows"] = "没有订阅窗口低于其提醒阈值。",
        ["tracking_off"] = "订阅跟踪已关闭",
        ["open_dashboard"] = "打开面板",
        ["couldnt_refresh"] = "无法刷新 - 显示最近数据",
        ["quota_unavailable"] = "配额数据不可用 - 稍后重试。",
        ["retry_now"] = "立即重试",
        ["estimated"] = "估算",
        ["percent_left"] = "剩余 {0}%",
        ["window_5h"] = "5 小时",
        ["window_weekly"] = "每周",

        ["most_used_today"] = "今日最常用  {0} · {1}",

        ["updated_just_now"] = "刚刚更新",
        ["updated_min_ago"] = "{0} 分钟前更新",
        ["updated_h_ago"] = "{0} 小时前更新",
        ["updated_d_ago"] = "{0} 天前更新",
        ["stale_suffix"] = " · 已过期",
        ["no_data_yet"] = "暂无数据",

        ["tray_hint"] = "右键托盘图标查看更多",
        ["open_tokdash"] = "打开 Tokdash",
        ["refresh"] = "刷新",
        ["exit"] = "退出",

        ["tooltip_today"] = "Tokdash - 今日 {0} · {1} tokens",
        ["tooltip_connecting"] = "Tokdash - 连接中…",
        ["tooltip_no_usage"] = "Tokdash - 暂无用量",
        ["tooltip_busy"] = "Tokdash - 忙碌",
        ["tooltip_offline"] = "Tokdash - 离线",
        ["tooltip_not_tokdash"] = "Tokdash - 非 Tokdash",
        ["tooltip_default"] = "Tokdash",

        ["notif_low_title"] = "Tokdash - 配额不足",
        ["notif_low_single"] = "{0} {1} 剩余 {2}%。",
        ["notif_low_multi"] = "{0} 个订阅窗口配额不足。{1} {2} 剩余 {3}%。",

        ["settings_window_title"] = "Tokdash 设置",
        ["section_server"] = "服务器",
        ["base_url"] = "基础地址",
        ["test"] = "测试",
        ["server_hint"] = "默认：http://127.0.0.1:55423。支持 Tailscale HTTPS 地址。",
        ["section_startup"] = "启动",
        ["launch_at_login"] = "登录时启动",
        ["section_notifications"] = "通知",
        ["low_quota_notifications"] = "低配额通知",
        ["low_quota_hint"] = "当订阅窗口跌破阈值时通知。需手动开启。",
        ["section_thresholds"] = "配额提醒阈值（剩余百分比）",
        ["threshold_5h"] = "5 小时：{0}%",
        ["threshold_weekly"] = "每周：{0}%",
        ["threshold_other"] = "默认：{0}%",
        ["section_language"] = "语言",
        ["language_hint"] = "默认跟随系统语言。",
        ["cancel"] = "取消",
        ["save"] = "保存",
        ["testing"] = "测试中…",
        ["test_bad_url"] = "请输入以 http:// 或 https:// 开头的完整地址。",
        ["valid_url"] = "请输入有效的 http:// 或 https:// 地址。",
        ["test_not_tokdash"] = "可访问，但不是 Tokdash 服务器。",
        ["test_reachable_error"] = "无法访问：{0}",
        ["test_ok"] = "已连接到 {0} · Tokdash {1}",
        ["launch_failed"] = "Windows 未启用登录时启动。请检查“设置 > 应用 > 启动”。",

        ["comparison_below"] = "低于昨日 {0}%",
        ["comparison_above"] = "高于昨日 {0}%",

        ["resets_soon"] = "即将重置",
        ["resets_in_minutes"] = "{0} 分钟后重置{1}",
        ["resets_in_hours"] = "{0} 小时后重置{1}",
        ["plural_s"] = "",
    };

    /// <summary>Test-only: sorted keys present for a language, used to assert en/zh parity.</summary>
    public static List<string> KeysFor(AppLanguage lang) =>
        (lang == AppLanguage.ZhHans ? Zh : En).Keys.OrderBy(k => k).ToList();
}
