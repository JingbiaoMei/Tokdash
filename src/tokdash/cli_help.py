"""Help surfaces for the CLI.

Two views, deliberately different sizes:

- a bare ``tokdash`` prints :func:`brief_help`, a one-screen card of the verbs;
- ``tokdash --help`` keeps the full reference, every verb and every flag.

The parser stays flat on purpose (one namespace, no subparsers), so its own
``format_help`` has to list every flag of every verb at once. That is the price
of the flat parser, and the card is what keeps the everyday view readable.
"""

from __future__ import annotations

from . import __version__

# (verb as typed, one-line summary). Ordered by how often each gets typed, and
# split into the three things people come here to do.
_SECTIONS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (
        "Run",
        (
            ("serve", "start the dashboard server"),
            ("tui", "interactive dashboard in the terminal"),
            ("report", "print the usage report"),
            ("export", "write usage JSON to stdout or --output"),
        ),
    ),
    (
        "Inspect",
        (
            ("db [action]", "usage db: status, sync, resync, verify, repair, watch"),
            ("quota [action]", "quota tracking: show, poll, consent"),
        ),
    ),
    (
        "Maintain",
        (
            ("setup", "install the local-only background service"),
            ("doctor", "check the install and the service"),
            ("update", "upgrade tokdash and restart the service"),
            ("uninstall", "revert what setup did"),
            ("version", "print the version"),
        ),
    ),
    (
        "Help",
        (
            ("--help", "every command and every flag"),
            ("--version", "print the version"),
        ),
    ),
)

# A bare `tokdash` used to fall through to `serve` and open a browser. Say plainly
# that it starts nothing, so the card cannot be misread as a launch.
_FOOTER = "No command given, so nothing was started. Run `tokdash serve`."

# The verbs the CLI accepts, in the order the parser advertises them. Single
# source of truth: `build_parser` builds its `choices` from this, and the card
# below is checked against it (tests/test_cli_help.py), so a new verb cannot be
# added to one and forgotten in the other.
COMMAND_VERBS = (
    "serve",
    "export",
    "db",
    "quota",
    "tui",
    "report",
    "version",
    "setup",
    "doctor",
    "update",
    "uninstall",
)

# Verbs as the card lists them, i.e. without the `--help` rows.
CARD_VERBS = frozenset(
    name.split()[0].strip("[]") for _, rows in _SECTIONS for name, _ in rows if not name.startswith("--")
)


def brief_help(prog: str = "tokdash") -> str:
    """One-screen command card for ``tokdash`` with no verb."""
    width = max(len(name) for _, rows in _SECTIONS for name, _ in rows)
    lines = [
        f"{prog} {__version__} - local token and cost dashboard",
        "",
        f"usage: {prog} <command> [options]",
    ]
    for title, rows in _SECTIONS:
        lines.append("")
        lines.append(f"  {title}")
        for name, summary in rows:
            lines.append(f"    {name:<{width}}  {summary}")
    lines.append("")
    lines.append(_FOOTER)
    return "\n".join(lines) + "\n"
