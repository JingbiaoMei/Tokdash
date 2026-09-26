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

    private static readonly CultureInfo EnCulture = CultureInfo.GetCultureInfo("en");
    private static readonly CultureInfo ZhCulture = CultureInfo.GetCultureInfo("zh-Hans");

    /// <summary>Culture matching <see cref="Current"/>, for date/time formatting so weekday
    /// names ("Thu" vs "周四") follow the app language rather than the OS UI culture.</summary>
    public static CultureInfo Culture => Current == AppLanguage.ZhHans ? ZhCulture : EnCulture;

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

        // Period-aware hero titles (contract §Period windows): the failure / empty hero
        // follows the SELECTED segment, so "today" is never hard-coded again.
        ["no_usage_period"] = "No usage recorded {0}",
        ["unavailable_today"] = "Today's data unavailable",
        ["unavailable_week"] = "This week's data unavailable",
        ["unavailable_month"] = "This month's data unavailable",
        ["unavailable_year"] = "This year's data unavailable",
        ["will_retry_shortly"] = "Will retry shortly.",
        ["tokdash_running"] = "Tokdash is running.",
        ["today_tokens_messages"] = "{0} tokens · {1} messages{2}",
        ["today_retrying_suffix"] = " · retrying",
        ["retrying"] = "retrying",

        // v1.1 period segment, ranks, glance, credits (mirrors L10n.swift).
        ["period_today"] = "Today",
        ["period_week"] = "Week",
        ["period_month"] = "Month",
        ["period_year"] = "Year",
        ["kicker_week"] = "THIS WEEK",
        ["kicker_month"] = "THIS MONTH",
        ["kicker_year"] = "THIS YEAR",
        ["suffix_today"] = "today",
        ["suffix_week"] = "this week",
        ["suffix_month"] = "this month",
        ["suffix_year"] = "this year",
        ["vs_yesterday"] = "vs yesterday",
        ["vs_last_week"] = "vs last week",
        ["vs_last_month"] = "vs last month",
        ["vs_last_year"] = "vs last year",
        ["word_yesterday"] = "yesterday",
        ["word_last_week"] = "last week",
        ["word_last_month"] = "last month",
        ["word_last_year"] = "last year",
        // E12 instance stepper button names (contract §Instance stepper).
        ["step_earlier"] = "Previous period",
        ["step_later"] = "Next period",
        ["delta_cost"] = "{0} {1}% cost",
        ["delta_tokens"] = "{0} {1}% tokens",
        ["delta_msgs"] = "{0} {1}% msgs",
        ["active_label"] = "active {0}",
        ["dur_lt1m"] = "<1 m",
        ["dur_m"] = "{0} m",
        ["dur_hm"] = "{0} h {1} m",
        ["dur_dh"] = "{0} d {1} h",
        ["top_tools_kicker"] = "Top tools · {0}",
        ["top_models_kicker"] = "Top models · {0}",
        ["per_server_kicker"] = "Per server · {0}",
        ["per_server_footnote"] = "combined in hero · this cycle only",
        ["server_unreachable_short"] = "unreachable",
        ["credits_row"] = "⚡ {0} · {1} reset credits · expire {2}",
        ["credits_row_plain"] = "⚡ {0} · {1} reset credits",
        ["credits_in_days"] = "in {0} d",
        ["credits_tomorrow"] = "tomorrow",
        ["credits_today"] = "today",
        // Static decoration on the credits row (right-aligned): never part of the pinned
        // row string, never localized per-credit (contract §Reset credits).
        ["credits_use_or_lose"] = "use or lose",
        ["peak_caption"] = "Peak {0:00}:00",
        ["glance_kicker_hours"] = "MOST ACTIVE HOURS",
        ["glance_kicker_days"] = "THIS WEEK · BY DAY",
        ["glance_kicker_90"] = "LAST 90 DAYS",
        ["glance_kicker_180"] = "LAST 180 DAYS",
        ["wd_mon"] = "Mon",
        ["wd_tue"] = "Tue",
        ["wd_wed"] = "Wed",
        ["wd_thu"] = "Thu",
        ["wd_fri"] = "Fri",
        ["wd_sat"] = "Sat",
        ["wd_sun"] = "Sun",
        ["section_components"] = "Components",
        ["comp_full_delta_row"] = "Full delta row",
        ["comp_full_delta_row_desc"] = "cost + tokens + messages",
        ["comp_top_ranks"] = "Top tools & models",
        ["comp_top_ranks_desc"] = "under the hero; rows set below",
        ["rank_rows"] = "Rows: {0}",
        ["comp_reset_credits"] = "Reset-credits row",
        ["comp_reset_credits_desc"] = "under Codex quota, All view",
        ["comp_activity_glance"] = "Activity glance",
        ["comp_activity_glance_desc"] = "grid on month / year",
        ["comp_histogram_today_week"] = "Histogram for today / week",
        ["comp_histogram_today_week_desc"] = "instead of grid",
        ["comp_per_server_rows"] = "Per-server rows",
        ["comp_per_server_rows_desc"] = "only with more than one server",
        ["server_runtime_row"] = "Tokdash server",
        ["server_runtime_version"] = "Tokdash v{0}",
        ["server_update_available"] = "Server update available: v{0}",
        ["tooltip_usage"] = "Tokdash - {0} · {1} tokens",
        ["notif_credits_title"] = "Tokdash - reset credits expiring",
        ["notif_credits_body"] = "{0} reset credits expire {1}. Use or lose them.",

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
        ["section_servers"] = "Servers",
        ["server_label"] = "Label",
        ["add_server"] = "Add server",
        ["servers_count"] = "{0} servers",
        ["servers_unavailable"] = "Unavailable: {0}",
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

        // The worded comparison now follows the selected segment (word_* args).
        ["comparison_below"] = "{0}% below {1}",
        ["comparison_above"] = "{0}% above {1}",

        ["resets_soon"] = "resets soon",
        ["resets_in_minutes"] = "resets in {0} minute{1}",
        ["resets_in_hours"] = "resets in {0} hour{1}",
        ["resets_in_days"] = "resets in {0} day{1}",
        ["resets_at"] = "resets {0}",
        ["plural_s"] = "s",

        ["section_updates"] = "Updates",
        // Store (MSIX) builds only: the Updates section is hidden there, so the version
        // still needs a home and the user needs to know where updates come from.
        ["section_version"] = "Version",
        ["update_managed_by_store"] = "Updates are delivered through the Microsoft Store.",
        ["settings_update_available"] = "Settings, update available",
        ["update_current_version"] = "Current version: {0}",
        ["update_auto_check"] = "Check for updates automatically",
        ["update_auto_check_hint"] = "Checks the public GitHub releases page at most once a day. Opt-in.",
        ["update_check_now"] = "Check now",
        ["update_checking"] = "Checking…",
        ["update_up_to_date"] = "Tokdash Companion is up to date.",
        ["update_available"] = "Version {0} is available",
        ["update_view"] = "View update",
        ["update_skip"] = "Skip this version",
        ["update_skipped"] = "Skipped - the badge stays hidden until a newer version ships.",
        ["update_manual_hint"] = "Opens the release page in your browser. Nothing is downloaded or installed.",
        ["update_never_checked"] = "Not checked yet",
        ["update_last_checked_just_now"] = "Last checked just now",
        ["update_last_checked_min"] = "Last checked {0} min ago",
        ["update_last_checked_h"] = "Last checked {0} h ago",
        ["update_last_checked_d"] = "Last checked {0} d ago",
        ["update_failed_offline"] = "Couldn't reach GitHub.",
        ["update_failed_rate_limited"] = "GitHub rate limit reached. Try again later.",
        ["update_failed_generic"] = "Update check failed.",
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

        ["no_usage_period"] = "{0}暂无用量",
        ["unavailable_today"] = "今日数据不可用",
        ["unavailable_week"] = "本周数据不可用",
        ["unavailable_month"] = "本月数据不可用",
        ["unavailable_year"] = "本年数据不可用",
        ["will_retry_shortly"] = "稍后重试。",
        ["tokdash_running"] = "Tokdash 正在运行。",
        ["today_tokens_messages"] = "{0} tokens · {1} 条消息{2}",
        ["today_retrying_suffix"] = " · 重试中",
        ["retrying"] = "重试中",

        ["period_today"] = "今天",
        ["period_week"] = "本周",
        ["period_month"] = "本月",
        ["period_year"] = "本年",
        ["kicker_week"] = "本周",
        ["kicker_month"] = "本月",
        ["kicker_year"] = "本年",
        ["suffix_today"] = "今日",
        ["suffix_week"] = "本周",
        ["suffix_month"] = "本月",
        ["suffix_year"] = "今年",
        ["vs_yesterday"] = "对比昨日",
        ["vs_last_week"] = "对比上周",
        ["vs_last_month"] = "对比上月",
        ["vs_last_year"] = "对比去年",
        ["word_yesterday"] = "昨日",
        ["word_last_week"] = "上周",
        ["word_last_month"] = "上月",
        ["word_last_year"] = "去年",
        ["step_earlier"] = "上一期",
        ["step_later"] = "下一期",
        ["delta_cost"] = "{0}{1}% 成本",
        ["delta_tokens"] = "{0}{1}% tokens",
        ["delta_msgs"] = "{0}{1}% 消息",
        ["active_label"] = "活跃 {0}",
        ["dur_lt1m"] = "不足 1 分",
        ["dur_m"] = "{0} 分",
        ["dur_hm"] = "{0} 小时 {1} 分",
        ["dur_dh"] = "{0} 天 {1} 小时",
        ["top_tools_kicker"] = "常用工具 · {0}",
        ["top_models_kicker"] = "常用模型 · {0}",
        ["per_server_kicker"] = "各服务器 · {0}",
        ["per_server_footnote"] = "已合并入上方总数 · 仅本轮",
        ["server_unreachable_short"] = "不可达",
        ["credits_row"] = "⚡ {0} · {1} 个重置额度 · {2}到期",
        ["credits_row_plain"] = "⚡ {0} · {1} 个重置额度",
        ["credits_in_days"] = "{0} 天内",
        ["credits_tomorrow"] = "明天",
        ["credits_today"] = "今天",
        ["credits_use_or_lose"] = "不用即失效",
        ["peak_caption"] = "峰值 {0:00}:00",
        ["glance_kicker_hours"] = "最活跃时段",
        ["glance_kicker_days"] = "本周 · 每日",
        ["glance_kicker_90"] = "近 90 天",
        ["glance_kicker_180"] = "近 180 天",
        ["wd_mon"] = "周一",
        ["wd_tue"] = "周二",
        ["wd_wed"] = "周三",
        ["wd_thu"] = "周四",
        ["wd_fri"] = "周五",
        ["wd_sat"] = "周六",
        ["wd_sun"] = "周日",
        ["section_components"] = "组件",
        ["comp_full_delta_row"] = "完整对比行",
        ["comp_full_delta_row_desc"] = "成本 + tokens + 消息",
        ["comp_top_ranks"] = "常用工具与模型",
        ["comp_top_ranks_desc"] = "位于概览下方；下方可设置行数",
        ["rank_rows"] = "行数：{0}",
        ["comp_reset_credits"] = "重置额度行",
        ["comp_reset_credits_desc"] = "显示在 Codex 订阅下（全部视图）",
        ["comp_activity_glance"] = "活动概览",
        ["comp_activity_glance_desc"] = "本月 / 本年显示热力格",
        ["comp_histogram_today_week"] = "今日 / 本周柱状图",
        ["comp_histogram_today_week_desc"] = "代替热力格",
        ["comp_per_server_rows"] = "各服务器数据行",
        ["comp_per_server_rows_desc"] = "仅在多台服务器时显示",
        ["server_runtime_row"] = "Tokdash 服务器",
        ["server_runtime_version"] = "Tokdash v{0}",
        ["server_update_available"] = "服务器有新版本：v{0}",
        ["tooltip_usage"] = "Tokdash - {0} · {1} tokens",
        ["notif_credits_title"] = "Tokdash - 重置额度即将到期",
        ["notif_credits_body"] = "{0} 个重置额度将于{1}到期，不用即失效。",

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
        ["section_servers"] = "服务器",
        ["server_label"] = "名称",
        ["add_server"] = "添加服务器",
        ["servers_count"] = "{0} 台服务器",
        ["servers_unavailable"] = "不可用：{0}",
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

        ["comparison_below"] = "低于{1} {0}%",
        ["comparison_above"] = "高于{1} {0}%",

        ["resets_soon"] = "即将重置",
        ["resets_in_minutes"] = "{0} 分钟后重置{1}",
        ["resets_in_hours"] = "{0} 小时后重置{1}",
        ["resets_in_days"] = "{0} 天后重置{1}",
        ["resets_at"] = "将于 {0} 重置",
        ["plural_s"] = "",

        ["section_updates"] = "更新",
        ["section_version"] = "版本",
        ["update_managed_by_store"] = "更新通过 Microsoft Store 发布。",
        ["settings_update_available"] = "设置，有可用更新",
        ["update_current_version"] = "当前版本：{0}",
        ["update_auto_check"] = "自动检查更新",
        ["update_auto_check_hint"] = "每天最多访问一次 GitHub 公开发布页。需手动开启。",
        ["update_check_now"] = "立即检查",
        ["update_checking"] = "检查中…",
        ["update_up_to_date"] = "Tokdash Companion 已是最新版本。",
        ["update_available"] = "有新版本 {0} 可用",
        ["update_view"] = "查看更新",
        ["update_skip"] = "跳过此版本",
        ["update_skipped"] = "已跳过 - 在更新版本发布前不再提示。",
        ["update_manual_hint"] = "在浏览器中打开发布页面，不会自动下载或安装。",
        ["update_never_checked"] = "尚未检查",
        ["update_last_checked_just_now"] = "刚刚检查",
        ["update_last_checked_min"] = "{0} 分钟前检查",
        ["update_last_checked_h"] = "{0} 小时前检查",
        ["update_last_checked_d"] = "{0} 天前检查",
        ["update_failed_offline"] = "无法连接 GitHub。",
        ["update_failed_rate_limited"] = "已达到 GitHub 访问频率上限，请稍后再试。",
        ["update_failed_generic"] = "检查更新失败。",
    };

    /// <summary>Test-only: sorted keys present for a language, used to assert en/zh parity.</summary>
    public static List<string> KeysFor(AppLanguage lang) =>
        (lang == AppLanguage.ZhHans ? Zh : En).Keys.OrderBy(k => k).ToList();
}
