"""What the terminal will actually accept, worked out before Textual is loaded.

Two environment facts decide whether this dashboard has any colour at all, and
both of them are usually wrong in the same direction:

* ``NO_COLOR``. Textual reads it when the App is built and installs a Monochrome
  line filter, which greys out a design that is made of colour. It is the right
  answer for a log line and the wrong one for a full-screen tool, so it is
  honoured by default and overridable (see ``TOKDASH_TUI_COLOR``).
* How many colours the terminal thinks it has. Textual asks Rich, Rich asks the
  environment, and inside tmux that answer is usually understated: ``COLORTERM``
  is not passed through and ``TERM`` is often a plain ``*-256color``, which makes
  Rich report 256 and round the lavender and mint of this palette onto grey
  steps of the xterm cube. Eight colours lands the navy on black.

The ceiling is therefore raised here, once, before Textual imports
(``textual.constants`` reads ``TEXTUAL_COLOR_SYSTEM`` at import time), and only
on evidence the terminal itself gave: its own environment variables, a ``TERM``
that says truecolour, or a tmux client that advertises the ``truecolour``
feature. Anything the user already stated wins, and the ceiling is never
lowered.

This lives in its own module and is called from the TUI entry point rather than
from the package ``__init__``: reading the environment is right for a person at a
terminal and wrong for a test run, a pipe or a capture.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

#: Set this to ``always`` (or ``1``/``true``) to keep colour in a terminal that
#: the ``NO_COLOR`` convention was never meant for. ``auto`` is the default.
COLOR_ENV = "TOKDASH_TUI_COLOR"

#: What the rest of the app is told the palette ended up as.
CEILING_TRUECOLOR = "truecolor"
CEILING_256 = "256"
CEILING_MONO = "mono"

# Environment variables that are a terminal introducing itself, whatever TERM
# happens to say. ``tmux`` and ``screen`` are the families whose TERM entries are
# the historical reason this module exists.
#: tmux has passed 24-bit through since 3.2, where the client's own capability
#: became something the server works out for itself. Older servers need the
#: terminal-overrides incantation, and claiming truecolour to them only makes
#: the palette quantise twice.
_TMUX_TRUECOLOUR_SINCE = (3, 2)

_TRUECOLOR_SIGNALS = (
    "ITERM_PROFILE", "ITERM_SESSION_ID", "WEZTERM_PANE", "KITTY_WINDOW_ID",
    "GHOSTTY_RESOURCES_DIR", "VTE_VERSION", "WT_SESSION", "MSYSCON",
)
_MODERN_FAMILIES = (
    "xterm", "screen", "tmux", "rxvt", "st", "alacritty", "foot", "kitty",
    "wezterm", "ghostty", "vt", "cygwin", "msys",
)

# What the ceiling ended up being, for the help screen and the captures. Written
# by configure_color_system(); nothing here reads it back to change behaviour.
_REPORT: dict[str, str | bool] = {
    "ceiling": CEILING_TRUECOLOR,
    "reason": "not configured (a capture or a test keeps the palette it was given)",
    "mono": False,
    "no_color": False,
}


def palette() -> dict[str, str | bool]:
    """The last decision, for the keys screen. A copy, never the live dict."""
    return dict(_REPORT)


def _tmux_run(tmux: str, *args: str) -> str:
    """One short tmux query, or "" when there is no answer to be had."""
    try:
        done = subprocess.run(  # noqa: S603 -- a fixed argv, no shell
            [tmux, *args], capture_output=True, text=True, timeout=1.0, check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""
    if done.returncode != 0:
        return ""
    return (done.stdout or "").strip()


def _tmux_version(text: str) -> tuple[int, ...] | None:
    """(3, 4) out of "tmux 3.4"; None when the answer is not a version."""
    for part in text.replace(",", " ").split():
        if part[:1].isdigit():
            bits = tuple(int(bit) for bit in part.split(".") if bit.isdigit())
            return bits or None
    return None


def _ask_tmux(env: dict[str, str]) -> str:
    """Why this tmux session passes 24-bit through, or "" when it does not.

    tmux 3.2+ publishes its client's capabilities as ``client_termfeatures``.
    When that list says ``truecolour`` tmux passes 24-bit sequences through
    untouched; when it does not, tmux rounds them onto the 256-colour cube
    itself, so claiming truecolour would change nothing but the bytes on the
    wire. Worth one short subprocess: inside tmux ``COLORTERM`` is gone, and the
    answer is not recoverable any other way.

    A pane started with nobody attached -- a ``tmux new-session -d`` a person
    attaches to later -- has NO client to ask, and answering "no" there caps a
    dashboard that will be read on a truecolour terminal for the rest of its
    life. tmux has been able to pass 24-bit through since 3.2, and a server that
    cannot simply quantises what it is sent, so the version is the fallback.
    """
    if not (env.get("TMUX") or env.get("TMUX_PANE")):
        return ""
    tmux = shutil.which("tmux")
    if not tmux:
        return ""
    features = _tmux_run(tmux, "display-message", "-p", "#{client_termfeatures}")
    if features:
        lowered = features.lower()
        return (
            "tmux's client advertises truecolour"
            if "truecolour" in lowered or "rgb" in lowered else ""
        )
    answer = _tmux_run(tmux, "-V")
    version = _tmux_version(answer)
    if version and version >= _TMUX_TRUECOLOUR_SINCE:
        return (f"tmux {answer or 'unknown version'} passes 24-bit through "
                "(no client attached to ask)")
    return ""


def configure_color_system(
    environ: dict[str, str] | None = None, *, is_tty: bool | None = None
) -> str | None:
    """Choose the colour ceiling, and write it where Textual will read it.

    Returns the ceiling it settled on (``None`` when it decided to leave the
    environment alone). Deliberately narrow, because guessing upward writes
    escape codes a terminal cannot read:

    * A real interactive terminal only -- tests, pipes and captures keep the
      answer they already have.
    * ``NO_COLOR`` is obeyed unless ``TOKDASH_TUI_COLOR`` says otherwise, and a
        ``NO_COLOR`` that was overridden is recorded so the app can say so.
    * Anything the user or the terminal already stated (``TEXTUAL_COLOR_SYSTEM``,
      ``COLORTERM=truecolor``) is respected. This raises the ceiling; it never
      lowers one.
    """
    env = os.environ if environ is None else environ
    forced = env.get(COLOR_ENV, "").strip().lower() in ("1", "true", "yes", "always")
    no_color = bool(env.get("NO_COLOR")) and not forced
    if no_color:
        _REPORT.update(ceiling=CEILING_MONO, reason="NO_COLOR is set in the environment",
                       mono=True, no_color=True)
        return None
    overrode = bool(forced and env.get("NO_COLOR"))
    if overrode:
        # The monochrome filter is installed from NO_COLOR when the App is
        # built, so an override has to remove it rather than argue with it.
        env.pop("NO_COLOR", None)
    _REPORT.update(mono=False, no_color=overrode)

    stated = env.get("TEXTUAL_COLOR_SYSTEM", "auto")
    if stated not in ("", "auto"):
        _REPORT.update(ceiling=stated, reason=f"{COLOR_ENV} ceiling was already set")
        return stated
    if env.get("COLORTERM", "").strip().lower() in ("truecolor", "24bit"):
        _REPORT.update(ceiling=CEILING_TRUECOLOR, reason="COLORTERM says truecolour")
        return None  # Rich reads this one correctly on its own.
    if is_tty is None:
        try:
            is_tty = sys.stdout.isatty() and sys.stdin.isatty()
        except (AttributeError, ValueError):
            is_tty = False
    if not is_tty:
        _REPORT.update(ceiling=CEILING_TRUECOLOR, reason="not a terminal (capture or test)")
        return None
    term = str(env.get("TERM", "")).strip().lower()
    if not term or term == "dumb":
        _REPORT.update(ceiling=CEILING_256, reason="TERM does not name a colour terminal")
        return None
    if "24bit" in term or "truecolor" in term:
        want, why = CEILING_TRUECOLOR, f"TERM={term} says truecolour"
    elif any(name in env for name in _TRUECOLOR_SIGNALS):
        want, why = CEILING_TRUECOLOR, "the terminal introduced itself"
    elif (tmux_why := _ask_tmux(env)):
        want, why = CEILING_TRUECOLOR, tmux_why
    elif any(term.startswith(family) for family in _MODERN_FAMILIES):
        want = CEILING_256
        why = f"TERM={term} and nothing claimed 24-bit"
        if env.get("TMUX"):
            why += ("; tmux is capping the palette -- add "
                    "set -as terminal-features ',xterm*:truecolour' to ~/.tmux.conf")
    else:
        _REPORT.update(ceiling=CEILING_256, reason=f"TERM={term} is not recognised")
        return None
    env["TEXTUAL_COLOR_SYSTEM"] = want
    _REPORT.update(ceiling=want, reason=why)
    return want
