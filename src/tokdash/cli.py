from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import threading
import webbrowser
from datetime import datetime
from pathlib import Path

import uvicorn

from . import __version__
from .api import app
from .compute import compute_usage

_DB_WATCH_THREAD_STARTED = False
_DB_WATCH_THREAD_LOCK = threading.Lock()
_QUOTA_POLL_THREAD_STARTED = False
_QUOTA_POLL_THREAD_LOCK = threading.Lock()


class TokdashArgumentParser(argparse.ArgumentParser):
    def parse_args(self, args=None, namespace=None):
        parsed = super().parse_args(args, namespace)
        if getattr(parsed, "command", None) == "quota":
            parsed.quota_action = getattr(parsed, "db_action", "show")
        else:
            parsed.quota_action = None
        return parsed


def _port_type(value: str) -> int:
    try:
        port = int(value)
    except Exception:
        raise argparse.ArgumentTypeError(f"Invalid port {value!r}. Must be an integer in 1..65535.")

    if not (1 <= port <= 65535):
        raise argparse.ArgumentTypeError(f"Invalid port {port}. Valid range is 1..65535.")

    return port


def _default_port() -> int:
    raw = os.environ.get("TOKDASH_PORT", "55423")
    try:
        return _port_type(raw)
    except argparse.ArgumentTypeError as e:
        raise SystemExit(f"Invalid TOKDASH_PORT={raw!r}. {e} Use --port <1-65535>.")


def _positive_int_env(name: str, default: int) -> int:
    """Read a positive integer from the environment, falling back on bad/empty values.

    A misconfigured knob must never crash ``serve``; we just use the default.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def build_parser(prog: str) -> argparse.ArgumentParser:
    parser = TokdashArgumentParser(prog=prog, description="Tokdash")
    parser.add_argument(
        "--version",
        action="version",
        version=f"tokdash {__version__}",
        help="Print the Tokdash version and exit",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="serve",
        choices=["serve", "export", "db", "quota", "version", "setup", "doctor", "update", "uninstall"],
        help="Command (default: serve)",
    )
    parser.add_argument(
        "db_action",
        nargs="?",
        default="status",
        choices=["status", "sync", "resync", "verify", "repair", "watch", "poll", "show", "consent"],
        help="Database action for `tokdash db` or quota action for `tokdash quota` (default: status)",
    )

    # Serve options
    parser.add_argument(
        "--bind",
        "--host",
        dest="bind",
        default=os.environ.get("TOKDASH_HOST", "127.0.0.1"),
        help="Bind address (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=_port_type,
        default=None,
        help="Port to listen on (default: 55423)",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("TOKDASH_LOG_LEVEL", "info"),
        help="Uvicorn log level (default: info)",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Don't automatically open the browser",
    )

    # Export options
    parser.add_argument(
        "--period",
        default="today",
        help='Usage period: "today", "week", "month", or an integer number of days (default: today)',
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="(compat) export outputs JSON by default",
    )
    parser.add_argument(
        "--output",
        type=str,
        help="Write output to a file instead of stdout",
    )
    parser.add_argument(
        "--include-quota",
        action="store_true",
        help="For `tokdash export`, include local quota state. Off by default.",
    )
    parser.add_argument(
        "--verify-period",
        default="today",
        help='Period for `tokdash db verify` (default: today)',
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="For `tokdash db repair`, report checks without changing counters. "
        "For setup/update/uninstall, print the plan/command and change nothing.",
    )
    parser.add_argument("--codex-api", choices=["on", "off"], help="For `tokdash quota consent`: enable/disable Codex API quota polling.")
    parser.add_argument("--claude-api", choices=["on", "off"], help="For `tokdash quota consent`: enable/disable Claude API quota polling.")
    parser.add_argument("--antigravity-api", choices=["on", "off"], help="For `tokdash quota consent`: enable/disable Antigravity API quota polling.")
    parser.add_argument("--enabled", choices=["on", "off"], help="For `tokdash quota consent`: master switch for all quota tracking.")
    parser.add_argument(
        "--poll-interval",
        type=int,
        choices=[15, 30, 60, 120],
        help="For `tokdash quota consent`: background poll interval in minutes (15/30/60/120).",
    )

    # Lifecycle options (setup / doctor / update / uninstall). These reuse the global
    # --bind/--port/--json/--dry-run above; their defaults are inert for the
    # serve/export/db verbs so the parser-compatibility contract (§7.1) holds.
    lifecycle = parser.add_argument_group("lifecycle (setup / doctor / update / uninstall)")
    lifecycle.add_argument(
        "--auto",
        action="store_true",
        help="Non-interactive 'easy' route with safe local-only defaults",
    )
    lifecycle.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Assume yes to confirmations (apply without prompting)",
    )
    lifecycle.add_argument(
        "--runtime",
        choices=["auto", "existing", "pipx", "venv", "binary"],
        default="auto",
        help="Service runtime to use (default: auto = the current interpreter)",
    )
    lifecycle.add_argument(
        "--service",
        choices=["auto", "systemd", "launchd", "winsched", "none"],
        default="auto",
        help="Background service type (default: auto)",
    )
    lifecycle.add_argument(
        "--no-service",
        action="store_true",
        help="Do not create a background service",
    )
    lifecycle.add_argument(
        "--purge",
        action="store_true",
        help="uninstall: also delete usage history and config (off by default)",
    )
    lifecycle.add_argument(
        "--keep-runtime",
        action="store_true",
        help="uninstall: remove the service but keep a setup-owned runtime",
    )
    lifecycle.add_argument(
        "--force",
        "--adopt",
        dest="force",
        action="store_true",
        help="setup: replace a pre-existing unmarked tokdash.service; "
        "uninstall: remove a unit that is unmarked or no longer carries setup's marker",
    )

    return parser


def _has_display() -> bool:
    """Best-effort check for a usable GUI session.

    Returns False in headless contexts (CI, SSH sessions, systemd/launchd
    services, Linux without an X11/Wayland display) so we don't try to launch
    a browser where there is none. ``--no-open`` remains the explicit hard
    override on top of this.
    """
    # CI runners are headless regardless of OS. Most providers (GitHub Actions,
    # GitLab, Travis, CircleCI, ...) set CI=true.
    ci = os.environ.get("CI", "").strip().lower()
    if ci and ci not in {"0", "false", "no"}:
        return False
    # A remote shell with no local console: opening a browser is wrong here
    # even on macOS/Windows.
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return False
    # On Linux a GUI needs an X11 or Wayland display. macOS and Windows don't
    # expose these vars but do have a desktop session, so only gate on Linux.
    if sys.platform.startswith("linux"):
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    return True


def _open_browser(url: str) -> None:
    """Open ``url`` in a browser, swallowing any error.

    Opening a browser is a best-effort convenience; a missing/misconfigured
    browser must never take down the server.
    """
    try:
        webbrowser.open(url)
    except Exception:
        pass


def serve(host: str, port: int, log_level: str, open_browser: bool = True) -> None:
    url_host = "localhost" if host in {"0.0.0.0", "::"} else host
    url = f"http://{url_host}:{port}"
    # Tell the app its effective bind/port so the write-protection gate
    # (api._write_guard) can enforce loopback-only mutations and a Host allowlist.
    app.state.bind = host
    app.state.port = port
    print(f"🚀 Starting Tokdash on {url}")
    if os.environ.get("TOKDASH_NO_RETENTION_NOTICE", "").strip().lower() not in {"1", "true", "yes"}:
        print(
            "ℹ️  Note: Claude Code & Gemini CLI auto-delete sessions older than ~30 days, "
            "which can silently shrink Tokdash's history.\n"
            "   Keep full history → https://github.com/JingbiaoMei/tokdash#history-retention\n"
            "   Silence this notice with TOKDASH_NO_RETENTION_NOTICE=1"
        )
    # Open the browser only when explicitly enabled (--no-open is a hard
    # override) and a GUI is actually available. Fire it from a short-delay
    # daemon timer so the server has a moment to start listening first.
    if open_browser and _has_display():
        timer = threading.Timer(1.0, _open_browser, args=(url,))
        timer.daemon = True
        timer.start()
    _start_usage_db_sync_daemon()
    _start_quota_poll_daemon()
    # Backpressure: cap accepted concurrency and keep-alive lifetime so a load burst
    # returns 503 fast instead of queuing forever and wedging the server. The limit
    # sits above the AnyIO worker pool (~40) so cheap cache hits aren't rejected, but
    # is bounded so the connection backlog can't grow without limit.
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=log_level,
        limit_concurrency=_positive_int_env("TOKDASH_LIMIT_CONCURRENCY", 64),
        timeout_keep_alive=_positive_int_env("TOKDASH_KEEPALIVE", 5),
    )


def export(period: str, pretty: bool, output: str | None, include_quota: bool = False) -> None:
    data = compute_usage(period)
    if include_quota:
        from .sources.quota import quota_state

        data["quota"] = quota_state()
    payload = json.dumps(data, indent=2 if pretty else None)

    if output:
        Path(output).write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)


def _emit_json(payload: dict, pretty: bool, output: str | None = None) -> None:
    text = json.dumps(payload, indent=2 if pretty else None, sort_keys=bool(pretty))
    if output:
        Path(output).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)


def _sync_usage_database() -> dict:
    from .compute import _sync_usage_store
    from .sources.coding_tools import CodingToolsUsageTracker
    from .sources.openclaw import get_usage_for_days
    from .sessions import get_sessions_data
    from .usage_store import UsageEntryStore

    tracker = CodingToolsUsageTracker()
    _sync_usage_store(tracker)
    # OpenClaw syncs all discovered files before applying the date window.
    get_usage_for_days(36500)
    # Session records for DB-backed Session tab paths.
    get_sessions_data("codex", "all")
    get_sessions_data("claude", "all")
    return UsageEntryStore().status()


def _resync_usage_database() -> dict:
    from .usage_store import UsageEntryStore, usage_db_path, usage_db_process_lock

    path = usage_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with usage_db_process_lock(path):
        old_status_error = ""
        try:
            old_status = UsageEntryStore(path).status() if path.exists() else {"usage_entries": 0}
        except Exception as exc:
            old_status = {"usage_entries": 0}
            old_status_error = str(exc)
        old_entries = int(old_status.get("usage_entries", 0) or 0)
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        tmp_path = path.with_name(f"{path.name}.tmp.{timestamp}")

        old_env = os.environ.get("TOKDASH_USAGE_DB_PATH")
        try:
            os.environ["TOKDASH_USAGE_DB_PATH"] = str(tmp_path)
            status = _sync_usage_database()
            tmp_store = UsageEntryStore(tmp_path)
            tmp_store.checkpoint()
            new_entries = int(status.get("usage_entries", 0) or 0)
            if old_entries > 0 and new_entries == 0:
                status["ok"] = False
                status["error"] = "refusing to replace populated DB with empty resync result"
                status["old_usage_entries"] = old_entries
                return status
        finally:
            if old_env is None:
                os.environ.pop("TOKDASH_USAGE_DB_PATH", None)
            else:
                os.environ["TOKDASH_USAGE_DB_PATH"] = old_env

        backup_paths: list[str] = []
        for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
            if candidate.exists():
                backup = candidate.with_name(candidate.name + f".bak.{timestamp}")
                candidate.replace(backup)
                backup_paths.append(str(backup))
        tmp_path.replace(path)
        for suffix in ("-wal", "-shm"):
            tmp_sidecar = Path(str(tmp_path) + suffix)
            if tmp_sidecar.exists():
                tmp_sidecar.replace(Path(str(path) + suffix))

        status = UsageEntryStore(path).status()
        status["ok"] = True
        status["backups"] = backup_paths
        status["resync_mode"] = "temp-db-atomic-replace"
        if old_status_error:
            status["old_status_error"] = old_status_error
        return status


def _verify_usage_database(period: str) -> dict:
    from .compute import compute_usage

    old = os.environ.get("TOKDASH_USAGE_DB")
    attempts = []
    try:
        for attempt in range(1, 6):
            os.environ["TOKDASH_USAGE_DB"] = "0"
            live_data = compute_usage(period)
            os.environ["TOKDASH_USAGE_DB"] = "1"
            db_data = compute_usage(period)

            fields = ("total_tokens", "total_messages")
            diffs = {field: int(db_data.get(field, 0) or 0) - int(live_data.get(field, 0) or 0) for field in fields}
            cost_diff = round(
                float(db_data.get("total_cost", 0.0) or 0.0) - float(live_data.get("total_cost", 0.0) or 0.0),
                6,
            )
            ok = all(value == 0 for value in diffs.values()) and abs(cost_diff) < 0.0001
            result = {
                "ok": ok,
                "period": period,
                "attempt": attempt,
                "db": {
                    "total_tokens": db_data.get("total_tokens"),
                    "total_cost": db_data.get("total_cost"),
                    "total_messages": db_data.get("total_messages"),
                },
                "live": {
                    "total_tokens": live_data.get("total_tokens"),
                    "total_cost": live_data.get("total_cost"),
                    "total_messages": live_data.get("total_messages"),
                },
                "diff": {**diffs, "total_cost": cost_diff},
            }
            if ok:
                if attempts:
                    result["attempts"] = attempts
                return result
            attempts.append(result)
            time.sleep(2)
        result["attempts"] = attempts[:-1]
        return result
    finally:
        if old is None:
            os.environ.pop("TOKDASH_USAGE_DB", None)
        else:
            os.environ["TOKDASH_USAGE_DB"] = old


def _repair_usage_database(*, dry_run: bool = False) -> dict:
    from .usage_store import UsageEntryStore

    return UsageEntryStore().repair(apply=not dry_run)


def _usage_db_watch_enabled() -> bool:
    value = os.environ.get("TOKDASH_USAGE_DB_WATCH", "0").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _usage_db_watch_interval() -> int:
    return _positive_int_env("TOKDASH_USAGE_DB_WATCH_INTERVAL", 30)


def _sync_watch_once_quietly() -> None:
    try:
        _sync_usage_database()
    except Exception:
        pass


def _start_usage_db_sync_daemon() -> None:
    global _DB_WATCH_THREAD_STARTED
    if not _usage_db_watch_enabled():
        return
    try:
        from .usage_store import persistent_usage_db_enabled

        if not persistent_usage_db_enabled():
            return
    except Exception:
        return

    interval = _usage_db_watch_interval()

    def loop() -> None:
        while True:
            _sync_watch_once_quietly()
            time.sleep(interval)

    with _DB_WATCH_THREAD_LOCK:
        if _DB_WATCH_THREAD_STARTED:
            return
        thread = threading.Thread(target=loop, name="tokdash-usage-db-watch", daemon=True)
        thread.start()
        _DB_WATCH_THREAD_STARTED = True


def _quota_poll_interval() -> int:
    from .sources.quota.config import effective_poll_interval

    return effective_poll_interval()[0]


def _quota_jittered_interval() -> int:
    base = _quota_poll_interval()
    return max(300, int(base + random.uniform(-base * 0.05, base * 0.05)))


def _quota_poll_once(*, include_network: bool = True) -> dict:
    from .sources.quota import poll_quota

    return poll_quota(include_network=include_network)


def _quota_tracking_enabled() -> bool:
    try:
        from .sources.quota.config import quota_tracking_enabled

        return quota_tracking_enabled()
    except Exception:
        return False


def _quota_network_enabled() -> bool:
    try:
        from .sources.quota.config import enabled_network_sources

        return bool(enabled_network_sources())
    except Exception:
        return False


def _start_quota_poll_daemon() -> None:
    global _QUOTA_POLL_THREAD_STARTED
    try:
        from .usage_store import persistent_usage_db_enabled

        if not persistent_usage_db_enabled():
            return
    except Exception:
        return

    def loop() -> None:
        time.sleep(60)
        while True:
            try:
                # Re-read the master switch and interval every iteration so the tab's
                # enable/disable + interval choice apply without a restart.
                if _quota_tracking_enabled():
                    _quota_poll_once(include_network=_quota_network_enabled())
            except Exception:
                pass
            time.sleep(_quota_jittered_interval())

    with _QUOTA_POLL_THREAD_LOCK:
        if _QUOTA_POLL_THREAD_STARTED:
            return
        thread = threading.Thread(target=loop, name="tokdash-quota-poll", daemon=True)
        thread.start()
        _QUOTA_POLL_THREAD_STARTED = True


def _watch_usage_database(pretty: bool, output: str | None) -> int:
    interval = _usage_db_watch_interval()
    try:
        while True:
            status = _sync_usage_database()
            status["watch_interval_seconds"] = interval
            _emit_json(status, pretty, output)
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0


def db_command(action: str, pretty: bool, output: str | None, verify_period: str, dry_run: bool = False) -> int:
    from .usage_store import UsageEntryStore

    # `--dry-run` only previews `db repair`. For the mutating/looping actions it would
    # otherwise be silently ignored while the DB is rebuilt/replaced — fail loudly instead.
    if dry_run and action in {"sync", "resync", "watch"}:
        raise SystemExit(f"--dry-run is not supported for `tokdash db {action}` (only `tokdash db repair`).")

    if action == "status":
        _emit_json(UsageEntryStore().status(), pretty, output)
        return 0
    if action == "sync":
        _emit_json(_sync_usage_database(), pretty, output)
        return 0
    if action == "resync":
        _emit_json(_resync_usage_database(), pretty, output)
        return 0
    if action == "verify":
        result = _verify_usage_database(verify_period)
        _emit_json(result, pretty, output)
        return 0 if result.get("ok") else 1
    if action == "repair":
        result = _repair_usage_database(dry_run=dry_run)
        _emit_json(result, pretty, output)
        return 0 if result.get("ok") else 1
    if action == "watch":
        return _watch_usage_database(pretty, output)
    raise SystemExit(f"Unknown db action: {action}")


def _parse_onoff(value: str | None) -> bool | None:
    if value is None:
        return None
    return value == "on"


def quota_command(args) -> int:
    action = args.quota_action or "show"
    if action == "poll":
        _emit_json(_quota_poll_once(), args.pretty, args.output)
        return 0
    if action == "show" or action == "status":
        from .sources.quota import quota_state

        _emit_json(quota_state(), args.pretty, args.output)
        return 0
    if action == "consent":
        from .sources.quota import config as quota_config

        updates = {
            key: value
            for key, value in {
                "codex_api": _parse_onoff(args.codex_api),
                "claude_api": _parse_onoff(args.claude_api),
                "antigravity_api": _parse_onoff(args.antigravity_api),
            }.items()
            if value is not None
        }
        consent = quota_config.set_quota_consent(updates) if updates else quota_config.read_quota_config()
        enabled_arg = _parse_onoff(getattr(args, "enabled", None))
        if enabled_arg is not None:
            quota_config.set_quota_enabled(enabled_arg)
        if getattr(args, "poll_interval", None) is not None:
            quota_config.set_poll_interval_minutes(args.poll_interval)
        interval_seconds, interval_source = quota_config.effective_poll_interval()
        _emit_json(
            {
                "consent": consent,
                "enabled": quota_config.quota_tracking_enabled(),
                "poll_interval_minutes": quota_config.read_poll_interval_minutes()
                or quota_config.DEFAULT_POLL_INTERVAL_MINUTES,
                "interval_seconds": interval_seconds,
                "interval_source": interval_source,
            },
            args.pretty,
            args.output,
        )
        return 0
    raise SystemExit(f"Unknown quota action: {action}")


def cli(argv: list[str] | None = None, prog: str = "tokdash") -> int:
    parser = build_parser(prog=prog)
    args = parser.parse_args(argv)

    if args.command == "version":
        print(f"tokdash {__version__}")
        return 0

    if args.command in {"setup", "doctor", "update", "uninstall"}:
        # Only `setup` binds a port, so only it resolves (and validates) TOKDASH_PORT here —
        # symmetric with how --bind defaults to TOKDASH_HOST and serve resolves the port.
        # doctor prefers the manifest-recorded port; update/uninstall don't use the port at
        # all (update reads the manifest; uninstall falls back to DEFAULT_PORT internally), so
        # a malformed TOKDASH_PORT must NOT make those two die with "Invalid TOKDASH_PORT".
        if args.port is None and args.command == "setup":
            args.port = _default_port()
        # Imported lazily so serve/export/db don't pay for the onboarding engine.
        from .onboard.engine import run_lifecycle

        return run_lifecycle(args)

    if args.command == "serve":
        port = args.port if args.port is not None else _default_port()
        serve(args.bind, port, args.log_level, open_browser=not args.no_open)
        return 0

    if args.command == "export":
        export(args.period, args.pretty, args.output, include_quota=args.include_quota)
        return 0

    if args.command == "db":
        return db_command(args.db_action, args.pretty, args.output, args.verify_period, args.dry_run)

    if args.command == "quota":
        return quota_command(args)

    parser.error(f"Unknown command: {args.command}")
    return 2


def main() -> None:
    raise SystemExit(cli())
