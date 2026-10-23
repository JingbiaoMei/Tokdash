"""The colour ceiling, decided from the environment before Textual is loaded.

A dashboard made of tints is unrecognisable when the terminal refuses them, and
both of the ways that happens are environment facts rather than code bugs:
NO_COLOR (which greys everything out through Textual's Monochrome filter) and an
understated palette (which rounds the lavender onto a grey step). These tests pin
the ladder, and they pin it on the way ROUND: nothing here may write
TEXTUAL_COLOR_SYSTEM for a test run, a pipe, or a capture.
"""
from __future__ import annotations

import pytest

from tokdash.tui import terminal


def ceiling(env, **kwargs):
    """Configure a COPY of ``env``, and report what the copy ended up as."""
    working = dict(env)
    got = terminal.configure_color_system(working, **kwargs)
    return got, working, terminal.palette()


def test_a_plain_256_terminal_gets_256_and_no_invention():
    got, written_env, report = ceiling({"TERM": "xterm-256color"}, is_tty=True)
    assert (got, written_env.get("TEXTUAL_COLOR_SYSTEM")) == ("256", "256")
    assert report["ceiling"] == "256"
    assert "24-bit" in report["reason"]


def test_tmux_without_the_truecolour_feature_is_capped_and_says_so(monkeypatch):
    monkeypatch.setattr(terminal, "shutil", type("S", (), {"which": staticmethod(lambda _: "/usr/bin/tmux")}))
    monkeypatch.setattr(
        terminal.subprocess, "run",
        lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "clipboard,cstyle\n"})(),
    )
    got, written_env, report = ceiling({"TERM": "tmux-256color", "TMUX": "/tmp/x,1"}, is_tty=True)
    assert (got, written_env.get("TEXTUAL_COLOR_SYSTEM")) == ("256", "256")
    assert "~/.tmux.conf" in report["reason"], report["reason"]


def test_tmux_whose_client_does_24bit_is_raised_all_the_way(monkeypatch):
    monkeypatch.setattr(terminal, "shutil", type("S", (), {"which": staticmethod(lambda _: "/usr/bin/tmux")}))
    monkeypatch.setattr(
        terminal.subprocess, "run",
        lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "truecolour,clipboard\n"})(),
    )
    got, written_env, _ = ceiling({"TERM": "tmux-256color", "TMUX": "/tmp/x,1"}, is_tty=True)
    assert (got, written_env.get("TEXTUAL_COLOR_SYSTEM")) == ("truecolor", "truecolor")


def test_a_broken_tmux_query_leaves_the_ceiling_conservative(monkeypatch):
    def boom(*a, **k):
        raise OSError("no server")
    monkeypatch.setattr(terminal, "shutil", type("S", (), {"which": staticmethod(lambda _: "/usr/bin/tmux")}))
    monkeypatch.setattr(terminal.subprocess, "run", boom)
    got, written_env, _ = ceiling({"TERM": "screen-256color", "TMUX": "/tmp/x,1"}, is_tty=True)
    assert (got, written_env.get("TEXTUAL_COLOR_SYSTEM")) == ("256", "256")


@pytest.mark.parametrize("signal", ["VTE_VERSION", "WEZTERM_PANE", "WT_SESSION"])
def test_a_terminal_that_introduces_itself_is_believed(signal):
    got, written_env, _ = ceiling({"TERM": "xterm-256color", signal: "1"}, is_tty=True)
    assert (got, written_env.get("TEXTUAL_COLOR_SYSTEM")) == ("truecolor", "truecolor")


def test_no_color_is_obeyed_rather_than_argued_with():
    got, written_env, report = ceiling({"NO_COLOR": "1", "TERM": "xterm-256color"}, is_tty=True)
    assert got is None
    assert written_env.get("TEXTUAL_COLOR_SYSTEM") is None
    assert report["mono"] is True and report["ceiling"] == "mono"


def test_tokdash_tui_color_always_overrides_no_color_and_says_it_did():
    env = {"NO_COLOR": "1", "TOKDASH_TUI_COLOR": "always", "TERM": "xterm-256color"}
    got, written_env, report = ceiling(env, is_tty=True)
    assert (got, written_env.get("TEXTUAL_COLOR_SYSTEM")) == ("256", "256")
    assert "NO_COLOR" not in written_env, (
        "Textual would still install its Monochrome filter"
    )
    assert report["mono"] is False and report["no_color"] is True


def test_a_ceiling_anyone_stated_by_hand_is_left_alone():
    for env in (
        {"TERM": "xterm-256color", "TEXTUAL_COLOR_SYSTEM": "standard"},
        {"TERM": "xterm-256color", "COLORTERM": "truecolor"},
    ):
        got, written_env, _ = ceiling(env, is_tty=True)
        # Whatever the reader set is what they get: the ladder raises ceilings
        # and never lowers or replaces one.
        assert written_env.get("TEXTUAL_COLOR_SYSTEM") == env.get("TEXTUAL_COLOR_SYSTEM")
        assert got in (None, env.get("TEXTUAL_COLOR_SYSTEM")), env


@pytest.mark.parametrize("env", [
    {"TERM": "xterm-256color"},   # not a terminal: a test, a pipe, a capture
    {"TERM": "dumb"},
    {"TERM": ""},
])
def test_nothing_is_written_for_a_terminal_that_is_not_there(env):
    got, written_env, _ = ceiling(env, is_tty=False)
    assert got is None
    assert written_env.get("TEXTUAL_COLOR_SYSTEM") is None


def test_the_report_is_a_copy_so_a_painter_cannot_edit_the_law():
    ceiling({"TERM": "xterm-256color"}, is_tty=True)
    first = terminal.palette()
    first["ceiling"] = "nonsense"
    assert terminal.palette()["ceiling"] == "256"


def _fake_tmux(monkeypatch, answers):
    """Stand in for the tmux binary: each argv answers (returncode, stdout)."""
    def run(cmd, *a, **k):
        argv = tuple(str(part) for part in cmd)
        code, out = answers.get(argv[1:], (1, ""))
        return type("R", (), {"returncode": code, "stdout": out})()

    monkeypatch.setattr(
        terminal, "shutil",
        type("S", (), {"which": staticmethod(lambda _: "/usr/bin/tmux")}),
    )
    monkeypatch.setattr(terminal.subprocess, "run", run)


def test_a_detached_tmux_pane_falls_back_to_the_servers_version(monkeypatch):
    """``tmux new-session -d`` has no client to ask, and "no" is the wrong answer.

    The pane the reader is about to attach to is a truecolour pane; capping it
    because nobody was attached when the TUI started would grey the dashboard out
    for the whole session, which is the opposite of what the ceiling is for.
    """
    _fake_tmux(monkeypatch, {
        ("display-message", "-p", "#{client_termfeatures}"): (1, ""),
        ("-V",): (0, "tmux 3.4\n"),
    })
    got, written_env, report = ceiling(
        {"TERM": "tmux-256color", "TMUX": "/tmp/x,1"}, is_tty=True
    )
    assert (got, written_env.get("TEXTUAL_COLOR_SYSTEM")) == ("truecolor", "truecolor")
    assert "tmux 3.4" in report["reason"], report["reason"]


def test_an_old_tmux_stays_on_the_cube_even_with_no_client(monkeypatch):
    """Before 3.2 tmux had no truecolour pass-through, so the cap is real."""
    _fake_tmux(monkeypatch, {
        ("display-message", "-p", "#{client_termfeatures}"): (1, ""),
        ("-V",): (0, "tmux 3.1a\n"),
    })
    got, written_env, _ = ceiling({"TERM": "tmux-256color", "TMUX": "/tmp/x,1"}, is_tty=True)
    assert (got, written_env.get("TEXTUAL_COLOR_SYSTEM")) == ("256", "256")


def test_a_tmux_that_answers_nothing_at_all_stays_conservative(monkeypatch):
    """No features, no version: the ceiling does not get invented upward."""
    _fake_tmux(monkeypatch, {})
    got, written_env, _ = ceiling({"TERM": "tmux-256color", "TMUX": "/tmp/x,1"}, is_tty=True)
    assert (got, written_env.get("TEXTUAL_COLOR_SYSTEM")) == ("256", "256")
