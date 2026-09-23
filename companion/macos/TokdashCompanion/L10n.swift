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
        "no_usage_period": "No usage recorded %@",
        "tokdash_running": "Tokdash is running.",
        "unavailable_today": "Today's data unavailable",
        "unavailable_week": "This week's data unavailable",
        "unavailable_month": "This month's data unavailable",
        "unavailable_year": "This year's data unavailable",
        "will_retry_shortly": "Will retry shortly.",
        "retrying": "retrying",
        "today_tokens_messages": "%@ tokens · %d messages%@",
        "today_retrying_suffix": " · retrying",

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

        "updated_just_now": "Updated just now",
        "updated_min_ago": "Updated %d min ago",
        "updated_h_ago": "Updated %d h ago",
        "updated_d_ago": "Updated %d d ago",
        "stale_suffix": " · stale",
        "no_data_yet": "No data yet",

        "quit": "Quit",
        "refresh": "Refresh",

        "comparison_below": "%1$d%% below %2$@",
        "comparison_above": "%1$d%% above %2$@",

        // Period segment (always visible above the hero) and every period-following string.
        "period_today": "Today",
        "period_week": "Week",
        "period_month": "Month",
        "period_year": "Year",
        "kicker_week": "THIS WEEK",
        "kicker_month": "THIS MONTH",
        "kicker_year": "THIS YEAR",
        "suffix_today": "today",
        "suffix_week": "this week",
        "suffix_month": "this month",
        "suffix_year": "this year",
        "vs_yesterday": "vs yesterday",
        "vs_last_week": "vs last week",
        "vs_last_month": "vs last month",
        "vs_last_year": "vs last year",
        "word_yesterday": "yesterday",
        "word_last_week": "last week",
        "word_last_month": "last month",
        "word_last_year": "last year",

        // Full delta row (E1): `{glyph} {pct}% cost · … {sentence}`.
        "delta_cost": "%1$@ %2$d%% cost",
        "delta_tokens": "%1$@ %2$d%% tokens",
        "delta_msgs": "%1$@ %2$d%% msgs",

        // Active time on the hero sub-line (ladder; ms in, ladder out).
        "active_label": "active %@",
        "dur_lt1m": "<1 m",
        "dur_m": "%d m",
        "dur_hm": "%d h %d m",
        "dur_dh": "%d d %d h",

        // Top ranks (E3).
        "top_tools_kicker": "Top tools · %@",
        "top_models_kicker": "Top models · %@",

        // Per-server rows (E10).
        "per_server_kicker": "Per server · %@",
        "per_server_footnote": "combined in hero · this cycle only",
        "server_unreachable_short": "unreachable",

        // Reset credits (E2).
        "credits_row": "⚡ %@ · %d reset credits · expire %@",
        "credits_row_plain": "⚡ %@ · %d reset credits",
        "credits_in_days": "in %d d",
        "credits_tomorrow": "tomorrow",
        "credits_today": "today",
        "credits_use_or_lose": "use or lose",

        // Activity glance (E9).
        "peak_caption": "Peak %02d:00",
        "glance_kicker_hours": "MOST ACTIVE HOURS",
        "glance_kicker_days": "THIS WEEK · BY DAY",
        "glance_kicker_90": "LAST 90 DAYS",
        "glance_kicker_180": "LAST 180 DAYS",
        "wd_mon": "Mon",
        "wd_tue": "Tue",
        "wd_wed": "Wed",
        "wd_thu": "Thu",
        "wd_fri": "Fri",
        "wd_sat": "Sat",
        "wd_sun": "Sun",

        // Components section (Settings, schema v3).
        "section_components": "Components",
        "comp_full_delta_row": "Full delta row",
        "comp_full_delta_row_desc": "cost + tokens + messages",
        "comp_top_ranks": "Top tools & models",
        "comp_top_ranks_desc": "under the hero; rows set below",
        "rank_rows": "Rows: %d",
        "comp_reset_credits": "Reset-credits row",
        "comp_reset_credits_desc": "under Codex quota, All view",
        "comp_activity_glance": "Activity glance",
        "comp_activity_glance_desc": "grid on month / year",
        "comp_histogram_today_week": "Histogram for today / week",
        "comp_histogram_today_week_desc": "instead of grid",
        "comp_per_server_rows": "Per-server rows",
        "comp_per_server_rows_desc": "only with more than one server",

        // Server runtime + update badge (E7), Settings-only.
        "server_runtime_row": "Tokdash server",
        "server_runtime_version": "Tokdash v%@",
        "server_update_available": "Server update available: v%@",

        "tooltip_usage": "Tokdash - %@ · %@ tokens",
        "tooltip_connecting": "Tokdash - connecting…",
        "tooltip_no_usage": "Tokdash - No usage yet",
        "tooltip_busy": "Tokdash - Busy",
        "tooltip_offline": "Tokdash - Offline",
        "tooltip_not_tokdash": "Tokdash - Not Tokdash",

        "notif_low_title": "Tokdash - low quota",
        "notif_low_single": "%@ %@ is at %d%% remaining.",
        "notif_low_multi": "%d subscription windows are low. %@ %@ at %d%%.",
        "notif_credits_title": "Tokdash - reset credits expiring",
        "notif_credits_body": "%d reset credits expire %@. Use or lose them.",

        "settings_title": "Settings",
        "section_server": "Server",
        "section_servers": "Servers",
        "server_label": "Label",
        "server_name_placeholder": "Server name",
        "server_unnamed": "Unnamed server",
        "enable_server": "Enable %@",
        "keep_one_server_enabled": "At least one server must remain enabled.",
        "remove_server": "Remove %@",
        "remove": "Remove",
        "remove_last_enabled_title": "Remove the active server?",
        "remove_last_enabled_message": "Another server will be enabled so the companion can continue refreshing.",
        "add_server": "Add server",
        "servers_count": "%d servers",
        "servers_unavailable": "Unavailable: %@",
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
        "server_not_tested": "Not tested",
        "test_server": "Test connection to %@",
        "test_reachable_latency": "Reachable · %d ms",
        "test_unreachable": "Unreachable",
        "test_timed_out": "Timed out",
        "test_invalid_response": "Invalid response",
        "test_bad_url": "Enter an absolute http:// or https:// URL.",
        "test_not_tokdash": "Reachable, but not a Tokdash server.",
        "test_reachable_error": "Couldn't reach it: %@",
        "test_ok": "Connected to %@ · Tokdash %@",

        "resets_soon": "resets soon",
        "resets_in_minutes": "resets in %d minute%@",
        "resets_in_hours": "resets in %d hour%@",
        "resets_in_days": "resets in %d day%@",
        "resets_at": "resets %@",
        "plural_s": "s",

        "section_updates": "Updates",
        "settings_update_available": "Settings, update available",
        "update_current_version": "Current version: %@",
        "update_auto_check": "Check for updates automatically",
        "update_auto_check_hint": "Checks the public GitHub releases page at most once a day. Opt-in.",
        "update_check_now": "Check now",
        "update_checking": "Checking…",
        "update_up_to_date": "Tokdash Companion is up to date.",
        "update_available": "Version %@ is available",
        "update_view": "View update",
        "update_skip": "Skip this version",
        "update_skipped": "Skipped - the badge stays hidden until a newer version ships.",
        "update_manual_hint": "Opens the release page in your browser. Nothing is downloaded or installed.",
        "update_never_checked": "Not checked yet",
        "update_last_checked_just_now": "Last checked just now",
        "update_last_checked_min": "Last checked %d min ago",
        "update_last_checked_h": "Last checked %d h ago",
        "update_last_checked_d": "Last checked %d d ago",
        "update_failed_offline": "Couldn't reach GitHub.",
        "update_failed_rate_limited": "GitHub rate limit reached. Try again later.",
        "update_failed_generic": "Update check failed.",
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
        "no_usage_period": "%@暂无用量",
        "tokdash_running": "Tokdash 正在运行。",
        "unavailable_today": "今日数据不可用",
        "unavailable_week": "本周数据不可用",
        "unavailable_month": "本月数据不可用",
        "unavailable_year": "本年数据不可用",
        "will_retry_shortly": "稍后重试。",
        "retrying": "重试中",
        "today_tokens_messages": "%@ tokens · %d 条消息%@",
        "today_retrying_suffix": " · 重试中",

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

        "updated_just_now": "刚刚更新",
        "updated_min_ago": "%d 分钟前更新",
        "updated_h_ago": "%d 小时前更新",
        "updated_d_ago": "%d 天前更新",
        "stale_suffix": " · 已过期",
        "no_data_yet": "暂无数据",

        "quit": "退出",
        "refresh": "刷新",

        "comparison_below": "低于%2$@ %1$d%%",
        "comparison_above": "高于%2$@ %1$d%%",

        "period_today": "今天",
        "period_week": "本周",
        "period_month": "本月",
        "period_year": "本年",
        "kicker_week": "本周",
        "kicker_month": "本月",
        "kicker_year": "本年",
        "suffix_today": "今日",
        "suffix_week": "本周",
        "suffix_month": "本月",
        "suffix_year": "今年",
        "vs_yesterday": "对比昨日",
        "vs_last_week": "对比上周",
        "vs_last_month": "对比上月",
        "vs_last_year": "对比去年",
        "word_yesterday": "昨日",
        "word_last_week": "上周",
        "word_last_month": "上月",
        "word_last_year": "去年",

        "delta_cost": "%1$@%2$d%% 成本",
        "delta_tokens": "%1$@%2$d%% tokens",
        "delta_msgs": "%1$@%2$d%% 消息",

        "active_label": "活跃 %@",
        "dur_lt1m": "不足 1 分",
        "dur_m": "%d 分",
        "dur_hm": "%d 小时 %d 分",
        "dur_dh": "%d 天 %d 小时",

        "top_tools_kicker": "常用工具 · %@",
        "top_models_kicker": "常用模型 · %@",

        "per_server_kicker": "各服务器 · %@",
        "per_server_footnote": "已合并入上方总数 · 仅本轮",
        "server_unreachable_short": "不可达",

        "credits_row": "⚡ %@ · %d 个重置额度 · %@到期",
        "credits_row_plain": "⚡ %@ · %d 个重置额度",
        "credits_in_days": "%d 天内",
        "credits_tomorrow": "明天",
        "credits_today": "今天",
        "credits_use_or_lose": "不用即失效",

        "peak_caption": "峰值 %02d:00",
        "glance_kicker_hours": "最活跃时段",
        "glance_kicker_days": "本周 · 每日",
        "glance_kicker_90": "近 90 天",
        "glance_kicker_180": "近 180 天",
        "wd_mon": "周一",
        "wd_tue": "周二",
        "wd_wed": "周三",
        "wd_thu": "周四",
        "wd_fri": "周五",
        "wd_sat": "周六",
        "wd_sun": "周日",

        "section_components": "组件",
        "comp_full_delta_row": "完整对比行",
        "comp_full_delta_row_desc": "成本 + tokens + 消息",
        "comp_top_ranks": "常用工具与模型",
        "comp_top_ranks_desc": "位于概览下方；下方可设置行数",
        "rank_rows": "行数：%d",
        "comp_reset_credits": "重置额度行",
        "comp_reset_credits_desc": "显示在 Codex 订阅下（全部视图）",
        "comp_activity_glance": "活动概览",
        "comp_activity_glance_desc": "本月 / 本年显示热力格",
        "comp_histogram_today_week": "今日 / 本周柱状图",
        "comp_histogram_today_week_desc": "代替热力格",
        "comp_per_server_rows": "各服务器数据行",
        "comp_per_server_rows_desc": "仅在多台服务器时显示",

        "server_runtime_row": "Tokdash 服务器",
        "server_runtime_version": "Tokdash v%@",
        "server_update_available": "服务器有新版本：v%@",

        "tooltip_usage": "Tokdash - %@ · %@ tokens",
        "tooltip_connecting": "Tokdash - 连接中…",
        "tooltip_no_usage": "Tokdash - 暂无用量",
        "tooltip_busy": "Tokdash - 忙碌",
        "tooltip_offline": "Tokdash - 离线",
        "tooltip_not_tokdash": "Tokdash - 非 Tokdash",

        "notif_low_title": "Tokdash - 配额不足",
        "notif_low_single": "%@ %@ 剩余 %d%%。",
        "notif_low_multi": "%d 个订阅窗口配额不足。%@ %@ 剩余 %d%%。",
        "notif_credits_title": "Tokdash - 重置额度即将到期",
        "notif_credits_body": "%d 个重置额度将于%@到期，不用即失效。",

        "settings_title": "设置",
        "section_server": "服务器",
        "section_servers": "服务器",
        "server_label": "名称",
        "server_name_placeholder": "服务器名称",
        "server_unnamed": "未命名服务器",
        "enable_server": "启用 %@",
        "keep_one_server_enabled": "必须至少启用一台服务器。",
        "remove_server": "移除 %@",
        "remove": "移除",
        "remove_last_enabled_title": "移除当前服务器？",
        "remove_last_enabled_message": "将自动启用另一台服务器，以便伴侣应用继续刷新。",
        "add_server": "添加服务器",
        "servers_count": "%d 台服务器",
        "servers_unavailable": "不可用：%@",
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
        "server_not_tested": "尚未测试",
        "test_server": "测试与 %@ 的连接",
        "test_reachable_latency": "可访问 · %d 毫秒",
        "test_unreachable": "无法访问",
        "test_timed_out": "连接超时",
        "test_invalid_response": "响应无效",
        "test_bad_url": "请输入以 http:// 或 https:// 开头的完整地址。",
        "test_not_tokdash": "可访问，但不是 Tokdash 服务器。",
        "test_reachable_error": "无法访问：%@",
        "test_ok": "已连接到 %@ · Tokdash %@",

        "resets_soon": "即将重置",
        "resets_in_minutes": "%d 分钟后重置%@",
        "resets_in_hours": "%d 小时后重置%@",
        "resets_in_days": "%d 天后重置%@",
        "resets_at": "将于 %@ 重置",
        "plural_s": "",

        "section_updates": "更新",
        "settings_update_available": "设置，有可用更新",
        "update_current_version": "当前版本：%@",
        "update_auto_check": "自动检查更新",
        "update_auto_check_hint": "每天最多访问一次 GitHub 公开发布页。需手动开启。",
        "update_check_now": "立即检查",
        "update_checking": "检查中…",
        "update_up_to_date": "Tokdash Companion 已是最新版本。",
        "update_available": "有新版本 %@ 可用",
        "update_view": "查看更新",
        "update_skip": "跳过此版本",
        "update_skipped": "已跳过 - 在更新版本发布前不再提示。",
        "update_manual_hint": "在浏览器中打开发布页面，不会自动下载或安装。",
        "update_never_checked": "尚未检查",
        "update_last_checked_just_now": "刚刚检查",
        "update_last_checked_min": "%d 分钟前检查",
        "update_last_checked_h": "%d 小时前检查",
        "update_last_checked_d": "%d 天前检查",
        "update_failed_offline": "无法连接 GitHub。",
        "update_failed_rate_limited": "已达到 GitHub 访问频率上限，请稍后再试。",
        "update_failed_generic": "检查更新失败。",
    ]

    /// Plural suffix for the current language ("s" in English, "" in Chinese).
    static var pluralS: String { current == .zhHans ? "" : "s" }

    /// Locale matching ``current``, for date/time formatting so weekday names ("Thu" vs
    /// "周四") follow the app language rather than the system locale. Mirrors L10n.Culture
    /// on Windows.
    static var locale: Locale { current == .zhHans ? Locale(identifier: "zh-Hans") : Locale(identifier: "en") }

    /// Test-only: sorted keys present for a language, used to assert en/zh parity so a key can't
    /// silently ship without a Chinese translation.
    static func keys(for language: AppLanguage) -> [String] {
        (language == .zhHans ? zh : en).keys.sorted()
    }
}
