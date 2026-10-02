"""Shared date-range parsing utilities."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
import os
import sys
from typing import Optional, Tuple

_DETECTED_SYSTEM_TZ: Optional[tzinfo] = None
_DETECTED_SYSTEM_TZ_LOADED: bool = False

_WINDOWS_TZ_MAP = {
    "Eastern Standard Time": "America/New_York",
    "Central Standard Time": "America/Chicago",
    "Mountain Standard Time": "America/Denver",
    "Pacific Standard Time": "America/Los_Angeles",
    "Alaskan Standard Time": "America/Anchorage",
    "Hawaiian Standard Time": "Pacific/Honolulu",
    "US Eastern Standard Time": "America/Indiana/Indianapolis",
    "US Mountain Standard Time": "America/Phoenix",
    "Atlantic Standard Time": "America/Halifax",
    "Newfoundland Standard Time": "America/St_Johns",
    "SA Pacific Standard Time": "America/Bogota",
    "SA Western Standard Time": "America/La_Paz",
    "SA Eastern Standard Time": "America/Cayenne",
    "Argentina Standard Time": "America/Argentina/Buenos_Aires",
    "E. South America Standard Time": "America/Sao_Paulo",
    "Pacific SA Standard Time": "America/Santiago",
    "Montevideo Standard Time": "America/Montevideo",
    "Venezuela Standard Time": "America/Caracas",
    "Central Brazilian Standard Time": "America/Cuiaba",
    "Greenland Standard Time": "America/Godthab",
    "GMT Standard Time": "Europe/London",
    "Greenwich Standard Time": "Atlantic/Reykjavik",
    "W. Europe Standard Time": "Europe/Berlin",
    "Romance Standard Time": "Europe/Paris",
    "Central Europe Standard Time": "Europe/Budapest",
    "Central European Standard Time": "Europe/Warsaw",
    "W. Central Africa Standard Time": "Africa/Lagos",
    "South Africa Standard Time": "Africa/Johannesburg",
    "Egypt Standard Time": "Africa/Cairo",
    "Morocco Standard Time": "Africa/Casablanca",
    "E. Europe Standard Time": "Europe/Chisinau",
    "FLE Standard Time": "Europe/Kyiv",
    "Israel Standard Time": "Asia/Jerusalem",
    "Arabic Standard Time": "Asia/Baghdad",
    "Arab Standard Time": "Asia/Riyadh",
    "Russian Standard Time": "Europe/Moscow",
    "Iran Standard Time": "Asia/Tehran",
    "Arabian Standard Time": "Asia/Dubai",
    "West Asia Standard Time": "Asia/Tashkent",
    "Central Asia Standard Time": "Asia/Almaty",
    "N. Central Asia Standard Time": "Asia/Novosibirsk",
    "North Asia Standard Time": "Asia/Krasnoyarsk",
    "North Asia East Standard Time": "Asia/Irkutsk",
    "Yakutsk Standard Time": "Asia/Yakutsk",
    "Vladivostok Standard Time": "Asia/Vladivostok",
    "Sakhalin Standard Time": "Asia/Sakhalin",
    "Magadan Standard Time": "Asia/Magadan",
    "Kamchatka Standard Time": "Asia/Kamchatka",
    "SE Asia Standard Time": "Asia/Bangkok",
    "India Standard Time": "Asia/Kolkata",
    "China Standard Time": "Asia/Shanghai",
    "Tokyo Standard Time": "Asia/Tokyo",
    "Korea Standard Time": "Asia/Seoul",
    "AUS Eastern Standard Time": "Australia/Sydney",
    "Cen. Australia Standard Time": "Australia/Adelaide",
    "AUS Central Standard Time": "Australia/Darwin",
    "W. Australia Standard Time": "Australia/Perth",
    "Tasmania Standard Time": "Australia/Hobart",
    "Lord Howe Standard Time": "Australia/Lord_Howe",
    "New Zealand Standard Time": "Pacific/Auckland",
    "Singapore Standard Time": "Asia/Singapore",
    "Taipei Standard Time": "Asia/Taipei",
    "Central Pacific Standard Time": "Pacific/Guadalcanal",
    "UTC": "UTC",
}


def get_configured_timezone() -> Optional[tzinfo]:
    """Return timezone from TOKDASH_TZ or TZ environment variables, if set."""
    env_tz = os.environ.get("TOKDASH_TZ") or os.environ.get("TZ")
    if env_tz:
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(env_tz)
        except Exception:
            pass
    return None


def _detect_system_zoneinfo() -> Optional[tzinfo]:
    """Attempt to detect the system IANA timezone via OS-level configurations."""
    try:
        from zoneinfo import ZoneInfo

        if sys.platform != "win32":
            for tz_file in ("/etc/timezone",):
                if os.path.exists(tz_file):
                    try:
                        with open(tz_file, "r", encoding="utf-8") as f:
                            zone_name = f.read().strip()
                        if zone_name:
                            return ZoneInfo(zone_name)
                    except Exception:
                        pass
            if os.path.exists("/etc/localtime"):
                try:
                    target = os.path.realpath("/etc/localtime")
                    parts = target.split("zoneinfo/")
                    if len(parts) > 1:
                        return ZoneInfo(parts[-1])
                except Exception:
                    pass
        else:
            try:
                import winreg

                with winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    r"SYSTEM\CurrentControlSet\Control\TimeZoneInformation",
                ) as key:
                    win_tz_name = winreg.QueryValueEx(key, "TimeZoneKeyName")[0]
                    iana_name = _WINDOWS_TZ_MAP.get(win_tz_name)
                    if iana_name:
                        return ZoneInfo(iana_name)
            except Exception:
                pass
    except Exception:
        pass
    return None


def get_system_timezone() -> Optional[tzinfo]:
    """Return cached detected system timezone."""
    global _DETECTED_SYSTEM_TZ, _DETECTED_SYSTEM_TZ_LOADED
    if not _DETECTED_SYSTEM_TZ_LOADED:
        _DETECTED_SYSTEM_TZ = _detect_system_zoneinfo()
        _DETECTED_SYSTEM_TZ_LOADED = True
    return _DETECTED_SYSTEM_TZ


def reset_cached_timezone() -> None:
    """Reset cached detected system timezone (primarily for tests)."""
    global _DETECTED_SYSTEM_TZ, _DETECTED_SYSTEM_TZ_LOADED
    _DETECTED_SYSTEM_TZ = None
    _DETECTED_SYSTEM_TZ_LOADED = False


def local_midnight(dt: datetime, tz: Optional[tzinfo] = None) -> datetime:
    """Resolve a datetime representing local midnight to a timezone-aware datetime.

    Each date is resolved against its own date-specific local DST offset,
    preventing phase shifts when querying dates across daylight-saving boundaries.
    """
    if dt.tzinfo is not None:
        if tz is not None:
            return dt.astimezone(tz)
        return dt

    if tz is not None:
        return dt.replace(tzinfo=tz)

    configured_tz = get_configured_timezone()
    if configured_tz is not None:
        return dt.replace(tzinfo=configured_tz)

    system_tz = get_system_timezone()
    if system_tz is not None:
        return dt.replace(tzinfo=system_tz)

    try:
        return dt.astimezone()
    except (OSError, OverflowError, ValueError):
        fallback_tz = datetime.now().astimezone().tzinfo or timezone.utc
        return dt.replace(tzinfo=fallback_tz)


def parse_date_range(
    date_from: str,
    date_to: str,
    tz: Optional[tzinfo] = None,
) -> Tuple[datetime, datetime]:
    """Parse YYYY-MM-DD strings into a (since, until) datetime pair.

    ``until`` is set to the *start* of the day after ``date_to`` so that the
    range is inclusive of the full final day.

    Each boundary is resolved to local midnight in its own date-specific
    daylight-saving offset, avoiding phase-shift misattribution.

    Raises ``ValueError`` on malformed input or if date_from is after date_to.
    """
    since_naive = datetime.strptime(date_from, "%Y-%m-%d")
    until_naive = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
    if since_naive >= until_naive:
        raise ValueError("date_from must be on or before date_to")
    return local_midnight(since_naive, tz=tz), local_midnight(until_naive, tz=tz)

