from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import uvicorn

from .api import app
from .compute import compute_usage


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


def build_parser(prog: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, description="Tokdash")
    parser.add_argument(
        "command",
        nargs="?",
        default="serve",
        choices=["serve", "export"],
        help="Command (default: serve)",
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

    return parser


def serve(host: str, port: int, log_level: str) -> None:
    url_host = "localhost" if host in {"0.0.0.0", "::"} else host
    print(f"🚀 Starting Tokdash on http://{url_host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level=log_level)


def export(period: str, pretty: bool, output: str | None) -> None:
    data = compute_usage(period)
    payload = json.dumps(data, indent=2 if pretty else None)

    if output:
        Path(output).write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)


def cli(argv: list[str] | None = None, prog: str = "tokdash") -> int:
    parser = build_parser(prog=prog)
    args = parser.parse_args(argv)

    if args.command == "serve":
        port = args.port if args.port is not None else _default_port()
        serve(args.bind, port, args.log_level)
        return 0

    if args.command == "export":
        export(args.period, args.pretty, args.output)
        return 0

    parser.error(f"Unknown command: {args.command}")
    return 2


def main() -> None:
    raise SystemExit(cli())
