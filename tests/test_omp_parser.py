"""Tests for OmpParser (oh-my-pi), the pi/omp dir-ownership rules, and the
base-parser fixes those tests lock (O2 source-keyed ids, O3 qualified
model_change fallback, O6 pricing-DB cost policy)."""
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from tokdash import clientpaths
from tokdash.pricing import PricingDatabase
from tokdash.sources.coding_tools import (
    BaseParser,
    CodingToolsUsageTracker,
    OmpParser,
    PiAgentParser,
    _sig_cache,
)


def _omp_session_lines(
    session_id="01a02912-e3ca-7000-b186-e024973e94b2",
    model="selfhosted-qwen",
    provider="vllm-hpc",
    usage=None,
    assistant_extras=None,
):
    """One minimal omp session file, shaped like evidence/omp_session_file.jsonl.

    ``assistant_extras=None`` gives the normal case (provider/model on the
    assistant message); pass ``{}`` to exercise the model_change fallback.
    """
    if usage is None:
        usage = {
            "input": 3342,
            "output": 46,
            "cacheRead": 0,
            "cacheWrite": 0,
            "totalTokens": 3388,
            "cost": {"input": 0.0006684, "output": 0.0000644, "cacheRead": 0, "cacheWrite": 0, "total": 0.0007328},
        }
    assistant = {"role": "assistant", "usage": usage}
    if assistant_extras is not None:
        assistant.update(assistant_extras)
    return "\n".join(
        [
            json.dumps({"type": "title", "v": 1, "title": "", "updatedAt": "2026-08-22T10:44:58.954Z"}),
            json.dumps({"type": "session", "version": 3, "id": session_id, "timestamp": "2026-08-22T10:44:58.954Z", "cwd": "/tmp/project"}),
            json.dumps({"type": "model_change", "id": "d5fefefb", "parentId": None, "timestamp": "2026-08-22T10:44:59.385Z", "model": f"{provider}/{model}"}),
            json.dumps({"type": "thinking_level_change", "id": "9374c56f", "timestamp": "2026-08-22T10:44:59.386Z", "thinkingLevel": "off"}),
            json.dumps({"type": "message", "id": "3da47ebd", "timestamp": "2026-08-22T10:44:59.498Z", "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}}),
            json.dumps({"type": "message", "id": "2c4de341", "timestamp": "2026-08-22T10:45:01.007Z", "message": assistant}),
            json.dumps({"type": "custom", "customType": "session_exit", "id": "a8d6fd27", "timestamp": "2026-08-22T10:45:01.015Z"}),
        ]
    ) + "\n"


def _write_omp_tree(home: Path, session_id="01a02912-e3ca-7000-b186-e024973e94b2", **kwargs) -> Path:
    bucket = home / ".omp" / "agent" / "sessions" / "--tmp-project--"
    bucket.mkdir(parents=True)
    path = bucket / "2026-08-22T10-44-58_session.jsonl"
    path.write_text(_omp_session_lines(session_id, **kwargs), encoding="utf-8")
    return path


def _fresh_parser():
    _sig_cache.clear()
    BaseParser._entry_cache.clear()
    return OmpParser(PricingDatabase())


def test_omp_parser_reads_real_session_shape(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    _write_omp_tree(home)

    parser = _fresh_parser()
    assert parser.search_dirs == [home / ".omp" / "agent" / "sessions"]
    entries = parser.collect(None, None)

    assert len(entries) == 1
    e = entries[0]
    assert e["source"] == "omp"
    assert e["model"] == "selfhosted-qwen"
    assert e["provider"] == "vllm-hpc"
    assert (e["input"], e["output"], e["cacheRead"], e["cacheWrite"], e["reasoning"]) == (3342, 46, 0, 0, 0)
    expected_ts = int(datetime(2026, 8, 22, 10, 45, 1, 7000, tzinfo=timezone.utc).timestamp() * 1000)
    assert e["timestamp"] == expected_ts


def test_omp_ignores_recorded_cost_and_prices_from_db(monkeypatch, tmp_path):
    """O6: omp's bundled catalog would bill the same endpoint differently than
    every other source; the recorded cost.total must be dropped together with
    its fixed billing provenance."""
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    _write_omp_tree(home)

    entries = _fresh_parser().collect(None, None)
    assert entries[0]["cost"] == 0.0  # selfhosted-qwen is not in the pricing DB
    billing = entries[0]["_billing"]
    assert billing["kind"] == "pricing"
    assert billing["models"] == ["selfhosted-qwen"]
    assert (billing["input"], billing["output"]) == (3342, 46)


def test_omp_cache_tokens_pass_through(monkeypatch, tmp_path):
    """Warm run: input is the uncached remainder, cacheRead the prefix hit —
    206 + 3136 = 3342 = the cold run's full input. No subtraction."""
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    _write_omp_tree(
        home,
        usage={"input": 206, "output": 33, "cacheRead": 3136, "cacheWrite": 0, "totalTokens": 3371, "cost": {"total": 0.0002128}},
    )

    entries = _fresh_parser().collect(None, None)
    assert (entries[0]["input"], entries[0]["cacheRead"], entries[0]["cacheWrite"]) == (206, 3136, 0)


def test_omp_model_change_fallback_splits_qualified_model(monkeypatch, tmp_path):
    """O3: an assistant message without its own model/provider falls back to
    the model_change row, where omp stores a provider-qualified id."""
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    _write_omp_tree(home, assistant_extras={})

    entries = _fresh_parser().collect(None, None)
    assert entries[0]["model"] == "selfhosted-qwen"
    assert entries[0]["provider"] == "vllm-hpc"


def test_entry_and_sig_keys_are_source_scoped(monkeypatch, tmp_path):
    """O2: omp rows and signature-cache entries are keyed on "omp", never on
    the inherited "pi_agent" — both parsers can run against one tree."""
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    _write_omp_tree(home)

    _sig_cache.clear()
    BaseParser._entry_cache.clear()

    omp = OmpParser(PricingDatabase())
    omp_entries = omp.collect(None, None)
    assert omp_entries[0]["entry_id"] == "omp:2c4de341"
    omp_sig_keys = [k for k in _sig_cache if k.startswith("omp:")]
    assert len(omp_sig_keys) == 1

    # The same tree read as pi_agent must not reuse the omp signature cache.
    _sig_cache.clear()
    BaseParser._entry_cache.clear()
    monkeypatch.setenv("PI_AGENT_DIR", str(home / ".omp" / "agent" / "sessions"))
    pi = PiAgentParser(PricingDatabase())
    pi_entries = pi.collect(None, None)
    assert pi_entries[0]["entry_id"] == "pi_agent:2c4de341"
    pi_sig_keys = [k for k in _sig_cache if k.startswith("pi_agent:")]
    assert len(pi_sig_keys) == 1


def test_omp_search_dirs_candidates(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("PI_CONFIG_DIR", raising=False)
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    home.mkdir()

    # Default profile only.
    assert clientpaths.omp_agent_search_dirs() == [home / ".omp" / "agent" / "sessions"]

    # PI_CODING_AGENT_DIR is honored by omp in default-profile mode, but the
    # parser must never claim it: pi_agent owns that override.
    override = tmp_path / "elsewhere" / "agent"
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(override))
    assert clientpaths.omp_agent_search_dirs() == [home / ".omp" / "agent" / "sessions"]
    monkeypatch.delenv("PI_CODING_AGENT_DIR")

    # XDG migration without the variable exported (init-xdg's default root
    # is ~/.local/share): trusted only once the omp app root exists there.
    (home / ".local" / "share" / "omp").mkdir(parents=True)
    assert clientpaths.omp_agent_search_dirs() == [
        home / ".omp" / "agent" / "sessions",
        home / ".local" / "share" / "omp" / "sessions",
    ]
    (home / ".local" / "share" / "omp").rmdir()
    assert clientpaths.omp_agent_search_dirs() == [home / ".omp" / "agent" / "sessions"]

    # XDG migration with the variable set: only once $XDG_DATA_HOME/omp exists.
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    assert clientpaths.omp_agent_search_dirs() == [home / ".omp" / "agent" / "sessions"]
    (xdg / "omp").mkdir(parents=True)
    assert clientpaths.omp_agent_search_dirs() == [
        home / ".omp" / "agent" / "sessions",
        xdg / "omp" / "sessions",
    ]
    monkeypatch.delenv("XDG_DATA_HOME")

    # Named profile trees are scanned alongside the default.
    profile_sessions = home / ".omp" / "profiles" / "work" / "agent" / "sessions"
    profile_sessions.mkdir(parents=True)
    assert clientpaths.omp_agent_search_dirs() == [
        home / ".omp" / "agent" / "sessions",
        profile_sessions,
    ]

    # PI_CONFIG_DIR moves the config root; profiles follow the root in omp
    # (getProfileConfigRoot builds on the base root), so the glob base moves too.
    custom_profile = home / ".custom" / "profiles" / "work" / "agent" / "sessions"
    custom_profile.mkdir(parents=True)
    monkeypatch.setenv("PI_CONFIG_DIR", ".custom")
    assert clientpaths.omp_agent_search_dirs() == [
        home / ".custom" / "agent" / "sessions",
        custom_profile,
    ]
    # A value equal to the default name de-duplicates to one candidate.
    monkeypatch.setenv("PI_CONFIG_DIR", ".omp")
    assert clientpaths.omp_agent_search_dirs() == [
        home / ".omp" / "agent" / "sessions",
        profile_sessions,
    ]


def test_tracker_ownership_drops_later_claimant():
    """O1: two parsers claiming one dir would double-count every token in it
    (the store dedups on (source, entry_key), never across sources). The
    later-registered source drops the dir."""
    shared = Path("/home/user/shared/sessions")
    alpha = SimpleNamespace(source_name="alpha", search_dirs=[shared])
    beta = SimpleNamespace(source_name="beta", search_dirs=[shared])

    tracker = CodingToolsUsageTracker.__new__(CodingToolsUsageTracker)
    tracker.parsers = {"alpha": alpha, "beta": beta}

    conflicts = tracker._claim_search_dirs()

    assert alpha.search_dirs == [shared]
    assert beta.search_dirs == []
    assert [c["source"] for c in conflicts] == ["beta"]
    assert "alpha" in conflicts[0]["error"]


def test_tracker_reemits_dir_conflicts_after_collect_reset(monkeypatch, tmp_path):
    """A conflict note recorded in __init__ must survive collect()'s
    source_errors reset — otherwise it reaches no consumer."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    # Point pi's override at omp's own tree: pi_agent (registered first) keeps
    # the dir, omp drops it.
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(home / ".omp" / "agent"))
    _sig_cache.clear()
    BaseParser._entry_cache.clear()

    tracker = CodingToolsUsageTracker()
    assert [str(d) for d in tracker.parsers["pi_agent"].search_dirs] == [str(home / ".omp" / "agent" / "sessions")]
    assert tracker.parsers["omp"].search_dirs == []

    tracker.collect(None, None, ["omp"])
    errors = tracker.to_json()["source_errors"]
    assert len(errors) == 1
    assert errors[0]["source"] == "omp"
    assert "pi_agent" in errors[0]["error"]

    # The reset in collect() must not swallow the note on the next pass either.
    tracker.collect(None, None, ["omp"])
    assert [e["source"] for e in tracker.to_json()["source_errors"]] == ["omp"]

    # A caller collecting other sources is not told about omp's dropped dir.
    tracker.collect(None, None, ["pi_agent"])
    assert tracker.to_json()["source_errors"] == []
