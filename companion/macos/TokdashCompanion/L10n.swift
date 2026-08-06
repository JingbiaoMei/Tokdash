import Foundation

/// Manual localization table for the companion. The app has no native `.strings`/`.xcstrings`
/// catalog: most display strings are built in code (``CompanionStore`` computed properties),
/// and a runtime language toggle that switches live (without restart) is simplest with a plain
/// per-language dictionary. ``current`` is resolved once at launch from ``CompanionSettings``
/// and again on change; views re-render because the change is driven through the store's
/// `@Published` language property.
///
/// English is the default and stays byte-identical to the pre-localization strings so the
/// contract/snapshot tests (which run under English) keep passing. zh-Hans is the only other
/// language. Mirrored by `L10n.cs` on Windows.
enum AppLanguage: String, Codable, CaseIterable {
    case system, english, zhHans

    /// Label shown in the Settings picker. "中文" is its own label in both languages.
    var displayName: String {
        switch self {
        case .system: return L10n.t("language_system")
        case .english: return "English"
        case .zhHans: return "中文"
        }
    }
}

enum L10n {
    /// Resolved language actually used for lookups. Tests default to `.english`; production
    /// sets it in ``CompanionStore`` init / `setLanguage`.
    static var current: AppLanguage = .english

    /// Resolve the effective language for a setting. `.system` follows the primary preferred
    /// language; Chinese maps to Simplified (the only variant shipped).
    static func resolve(
        _ setting: AppLanguage,
        preferredLanguages: [String] = Locale.preferredLanguages
    ) -> AppLanguage {
        switch setting {
        case .system:
            return preferredLanguages.first?.lowercased().hasPrefix("zh") == true ? .zhHans : .english
        case .english, .zhHans:
            return setting
        }
    }

    /// Translate a key, with optional `String(format:)` arguments. Falls back to English, then
    /// to the key itself, so a missing entry is visible rather than empty.
    static func t(_ key: String, _ args: CVarArg...) -> String {
        let table = current == .zhHans ? zh : en
        let template = table[key] ?? en[key] ?? key
        guard !args.isEmpty else { return template }
        return String(format: template, arguments: args)
    }

    // MARK: English (source of truth; identical to the original hardcoded strings)

    private static let en: [String: String] = [
        "language_system": "System",
        "connecting": "Connecting…",
        "connected": "Connected",
        "busy": "Busy",
        "offline": "Offline",
        "not_tokdash": "Not Tokdash",
        "local": "Local",
        "server_connected": "%@ · Connected",

        "banner_offline_title": "Tokdash is not reachable",
        "banner_offline_body": "Start Tokdash, or check the server address in Settings.",
        "banner_busy_title": "Tokdash is busy - retrying",
        "banner_busy_body": "Last data shown below. Backing off automatically.",
        "banner_wrong_title": "This address is not a Tokdash service",
        "banner_wrong_body": "Check that the server address in Settings points at a Tokdash instance.",
        "retry": "Retry",
        "settings": "Settings",

        "today": "TODAY",
        "no_usage_today": "No usage recorded today",
        "tokdash_running": "Tokdash is running.",
        "today_unavailable": "Today's data unavailable",
        "will_retry_shortly": "Will retry shortly.",
        "retrying": "retrying",
        "today_tokens_messages": "%@ tokens · %d messages%@",
        "today_retrying_suffix": " · retrying",
        "month_tokens_retrying": "%@ tokens · retrying",
        "month_tokens": "%@ tokens",

        "subscription": "SUBSCRIPTION",
        "all_subscriptions": "ALL SUBSCRIPTIONS",
        "low": "Low",
        "all": "All",
        "no_low_windows": "No subscription window is below its alert threshold.",
        "tracking_off": "Subscription tracking is off",
        "open_dashboard": "Open Dashboard",
        "couldnt_refresh": "Couldn't refresh - showing last known",
        "quota_unavailable": "Quota data unavailable - will retry shortly.",
        "retry_now": "Retry now",
        "estimated": "Estimated",
        "percent_left": "%d%% left",
        "window_5h": "5-hour",
        "window_weekly": "Weekly",

        "most_used_today": "Most used today  %@ · %@",

        "updated_just_now": "Updated just now",
        "updated_min_ago": "Updated %d min ago",
        "updated_h_ago": "Updated %d h ago",
        "updated_d_ago": "Updated %d d ago",
        "stale_suffix": " · stale",
        "no_data_yet": "No data yet",

        "quit": "Quit",
        "refresh": "Refresh",

        "comparison_below": "%d%% below yesterday",
        "comparison_above": "%d%% above yesterday",

        "tooltip_today": "Tokdash - Today %@ · %@ tokens",
        "tooltip_connecting": "Tokdash - connecting…",
        "tooltip_no_usage": "Tokdash - No usage yet",
        "tooltip_busy": "Tokdash - Busy",
        "tooltip_offline": "Tokdash - Offline",
        "tooltip_not_tokdash": "Tokdash - Not Tokdash",

        "notif_low_title": "Tokdash - low quota",
        "notif_low_single": "%@ %@ is at %d%% remaining.",
        "notif_low_multi": "%d subscription windows are low. %@ %@ at %d%%.",

        "settings_title": "Settings",
        "section_server": "Server",
        "base_url": "Base URL",
        "test": "Test",
        "server_hint": "Default: http://127.0.0.1:55423. Tailscale HTTPS URLs are supported.",
        "section_startup": "Startup",
        "launch_at_login": "Launch at Login",
        "section_notifications": "Notifications",
        "low_quota_notifications": "Low-quota notifications",
        "low_quota_hint": "Notifies when a subscription window crosses its threshold. Opt-in.",
        "section_thresholds": "Quota Alert Thresholds (% remaining)",
        "threshold_5h": "5-hour: %d%%",
        "threshold_weekly": "Weekly: %d%%",
        "threshold_other": "Default: %d%%",
        "section_language": "Language",
        "language_hint": "Follows the system language by default.",
        "cancel": "Cancel",
        "save": "Save",
        "testing": "Testing…",
        "test_bad_url": "Enter an absolute http:// or https:// URL.",
        "test_not_tokdash": "Reachable, but not a Tokdash server.",
        "test_reachable_error": "Couldn't reach it: %@",
        "test_ok": "Connected to %@ · Tokdash %@",

        "resets_soon": "resets soon",
        "resets_in_minutes": "resets in %d minute%@",
        "resets_in_hours": "resets in %d hour%@",
        "resets_in_days": "resets in %d day%@",
        "plural_s": "s",
    ]

    // MARK: Simplified Chinese (zh-Hans)

    private static let zh: [String: String] = [
        "language_system": "跟随系统",
        "connecting": "连接中…",
        "connected": "已连接",
        "busy": "忙碌",
        "offline": "离线",
        "not_tokdash": "非 Tokdash",
        "local": "本地",
        "server_connected": "%@ · 已连接",

        "banner_offline_title": "无法连接 Tokdash",
        "banner_offline_body": "请启动 Tokdash，或在设置中检查服务器地址。",
        "banner_busy_title": "Tokdash 正忙 - 正在重试",
        "banner_busy_body": "下方显示最近的数据，正在自动退避重试。",
        "banner_wrong_title": "该地址不是 Tokdash 服务",
        "banner_wrong_body": "请在设置中确认服务器地址指向 Tokdash 实例。",
        "retry": "重试",
        "settings": "设置",

        "today": "今日",
        "no_usage_today": "今日暂无用量",
        "tokdash_running": "Tokdash 正在运行。",
        "today_unavailable": "今日数据不可用",
        "will_retry_shortly": "稍后重试。",
        "retrying": "重试中",
        "today_tokens_messages": "%@ tokens · %d 条消息%@",
        "today_retrying_suffix": " · 重试中",
        "month_tokens_retrying": "%@ tokens · 重试中",
        "month_tokens": "%@ tokens",

        "subscription": "订阅",
        "all_subscriptions": "全部订阅",
        "low": "少量",
        "all": "全部",
        "no_low_windows": "没有订阅窗口低于其提醒阈值。",
        "tracking_off": "订阅跟踪已关闭",
        "open_dashboard": "打开面板",
        "couldnt_refresh": "无法刷新 - 显示最近数据",
        "quota_unavailable": "配额数据不可用 - 稍后重试。",
        "retry_now": "立即重试",
        "estimated": "估算",
        "percent_left": "剩余 %d%%",
        "window_5h": "5 小时",
        "window_weekly": "每周",

        "most_used_today": "今日最常用  %@ · %@",

        "updated_just_now": "刚刚更新",
        "updated_min_ago": "%d 分钟前更新",
        "updated_h_ago": "%d 小时前更新",
        "updated_d_ago": "%d 天前更新",
        "stale_suffix": " · 已过期",
        "no_data_yet": "暂无数据",

        "quit": "退出",
        "refresh": "刷新",

        "comparison_below": "低于昨日 %d%%",
        "comparison_above": "高于昨日 %d%%",

        "tooltip_today": "Tokdash - 今日 %@ · %@ tokens",
        "tooltip_connecting": "Tokdash - 连接中…",
        "tooltip_no_usage": "Tokdash - 暂无用量",
        "tooltip_busy": "Tokdash - 忙碌",
        "tooltip_offline": "Tokdash - 离线",
        "tooltip_not_tokdash": "Tokdash - 非 Tokdash",

        "notif_low_title": "Tokdash - 配额不足",
        "notif_low_single": "%@ %@ 剩余 %d%%。",
        "notif_low_multi": "%d 个订阅窗口配额不足。%@ %@ 剩余 %d%%。",

        "settings_title": "设置",
        "section_server": "服务器",
        "base_url": "基础地址",
        "test": "测试",
        "server_hint": "默认：http://127.0.0.1:55423。支持 Tailscale HTTPS 地址。",
        "section_startup": "启动",
        "launch_at_login": "登录时启动",
        "section_notifications": "通知",
        "low_quota_notifications": "低配额通知",
        "low_quota_hint": "当订阅窗口跌破阈值时通知。需手动开启。",
        "section_thresholds": "配额提醒阈值（剩余百分比）",
        "threshold_5h": "5 小时：%d%%",
        "threshold_weekly": "每周：%d%%",
        "threshold_other": "默认：%d%%",
        "section_language": "语言",
        "language_hint": "默认跟随系统语言。",
        "cancel": "取消",
        "save": "保存",
        "testing": "测试中…",
        "test_bad_url": "请输入以 http:// 或 https:// 开头的完整地址。",
        "test_not_tokdash": "可访问，但不是 Tokdash 服务器。",
        "test_reachable_error": "无法访问：%@",
        "test_ok": "已连接到 %@ · Tokdash %@",

        "resets_soon": "即将重置",
        "resets_in_minutes": "%d 分钟后重置%@",
        "resets_in_hours": "%d 小时后重置%@",
        "resets_in_days": "%d 天后重置%@",
        "plural_s": "",
    ]

    /// Plural suffix for the current language ("s" in English, "" in Chinese).
    static var pluralS: String { current == .zhHans ? "" : "s" }

    /// Test-only: sorted keys present for a language, used to assert en/zh parity so a key can't
    /// silently ship without a Chinese translation.
    static func keys(for language: AppLanguage) -> [String] {
        (language == .zhHans ? zh : en).keys.sorted()
    }
}
