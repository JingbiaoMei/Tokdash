"""Two help surfaces: a bare `tokdash` prints the verb card, `--help` prints everything.

The split is deliberate. The parser is flat (one namespace, no subparsers), so
its own help has to list every flag of every verb; the card is what keeps the
view people actually see first readable. These tests hold both halves of the
bargain: the card stays small and complete, the reference stays complete.
"""

import pytest

pytest.importorskip("fastapi")

import tokdash.cli as cli
from tokdash.cli_help import CARD_VERBS, COMMAND_VERBS


def test_card_covers_every_verb_and_nothing_else():
    # Drift one way and the card hides a command; drift the other and it
    # advertises a command that no longer exists.
    assert CARD_VERBS == frozenset(COMMAND_VERBS)


def test_parser_advertises_exactly_the_card_verbs():
    parser = cli.build_parser("tokdash")
    command = next(action for action in parser._actions if action.dest == "command")
    assert set(command.choices) == set(COMMAND_VERBS)


def test_bare_command_shows_the_card_and_no_flags(capsys, monkeypatch):
    def _no_serve(*args, **kwargs):
        pytest.fail("bare `tokdash` reached serve() -- it must print the card instead")

    monkeypatch.setattr(cli, "serve", _no_serve)
    assert cli.cli([]) == 0
    out = capsys.readouterr().out

    assert "usage: tokdash" in out
    assert "nothing was started" in out  # the card must not read as a launch
    for verb in COMMAND_VERBS:
        assert f"\n    {verb}" in out, f"{verb} missing from the card"
    # Flags belong to the reference, not the card.
    for flag in ("--credential-scan", "--poll-interval", "--purge", "--dev-fixture"):
        assert flag not in out
    # One screen, or it stopped being a card.
    assert len(out.splitlines()) <= 32


def test_full_help_still_lists_every_flag():
    # The card is allowed to hide flags; `--help` is not. Built from the parser
    # itself, so a flag added later cannot escape the reference.
    parser = cli.build_parser("tokdash")
    text = parser.format_help()
    for action in parser._actions:
        for option in action.option_strings:
            assert option in text, f"{option} missing from `tokdash --help`"


# Each heading claims the verbs that read the flags under it, so a heading is a
# statement about the code and can be wrong. Pinning title and membership
# together means a flag cannot drift into a section whose heading does not read
# it, and a heading cannot advertise a verb that ignores or rejects the flag.
# Verified against the code: `--period` reaches only export/report/tui, tui
# rejects --json/--pretty/--output outright, --dry-run belongs to db repair and
# to the lifecycle verbs, and setup/doctor read --bind/--port.
EXPECTED_SECTIONS = {
    "address and port (serve / setup / doctor)": {"--bind", "--port"},
    "server (serve)": {"--log-level", "--no-open"},
    "development fixture (serve)": {"--dev-fixture", "--dev-seed"},
    "usage window (export / report / tui)": {"--period"},
    "output (export / report / db / quota)": {"--pretty", "--json", "--output", "--include-quota"},
    "usage database (db)": {"--verify-period"},
    "dry run (db repair / setup / update / uninstall)": {"--dry-run"},
    "quota consent (quota consent)": {
        "--codex-api",
        "--claude-api",
        "--antigravity-api",
        "--minimax-api",
        "--kimi-api",
        "--grok-api",
        "--zai-api",
        "--opencode-go-api",
        "--commandcode-api",
        "--credential-scan",
        "--enabled",
        "--poll-interval",
    },
    "lifecycle (setup / doctor / update / uninstall)": {
        "--auto",
        "-y",
        "--runtime",
        "--to",
        "--service",
        "--no-service",
        "--purge",
        "--keep-runtime",
        "--force",
    },
}


def _sections(parser):
    """{heading: flags}, minus argparse's own positional/optional buckets.

    Matched by identity rather than by title: argparse names those two buckets
    through gettext, so the labels are the one part of this that a locale could change.
    """
    builtin = (parser._positionals, parser._optionals)
    found = {}
    for group in parser._action_groups:
        options = {action.option_strings[0] for action in group._group_actions if action.option_strings}
        if group not in builtin and options:
            found[group.title] = options
    return found


def test_full_help_sections_match_the_verbs_that_read_them():
    parser = cli.build_parser("tokdash")
    sections = _sections(parser)
    assert set(sections) == set(EXPECTED_SECTIONS), f"section titles changed: {sorted(sections)}"
    for title, expected in EXPECTED_SECTIONS.items():
        assert sections[title] == expected, f"{title}: {sorted(sections[title] ^ expected)}"


def test_tui_rejects_the_flags_its_heading_does_not_claim(capsys):
    # `output` claims export/report/db/quota and not tui, because tui turns these away.
    for argv in (["tui", "--json"], ["tui", "--pretty"], ["tui", "--output", "x"]):
        with pytest.raises(SystemExit) as excinfo:
            cli.cli(argv)
        assert excinfo.value.code == 2
        assert "interactive only" in capsys.readouterr().err, argv


def test_every_flag_lives_in_a_section():
    # A flag added straight onto the parser lands in argparse's default "options"
    # group, where it would sit unlabelled beside -h -- exactly the wall this split
    # exists to prevent.
    parser = cli.build_parser("tokdash")
    default = {
        action.option_strings[0] for action in parser._optionals._group_actions if action.option_strings
    }
    extra = sorted(default - {"-h", "--version"})
    assert not extra, f"unlabelled flags next to -h: {extra}"


def test_usage_line_names_the_shape_not_the_flags():
    parser = cli.build_parser("tokdash")
    assert parser.format_usage().strip() == "usage: tokdash <command> [options]"


def test_bad_verb_error_keeps_the_short_usage(capsys):
    # Errors print the usage line too; it used to dump forty flags at anyone who
    # mistyped a verb.
    with pytest.raises(SystemExit) as excinfo:
        cli.cli(["nope"])
    assert excinfo.value.code == 2
    assert "usage: tokdash <command> [options]" in capsys.readouterr().err
