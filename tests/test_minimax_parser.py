"""MiniMax Code (mcode) parser tests.

Fixtures follow the verbatim row shape captured from an installed
@minimax-ai/code v0.4.12 session at
``~/.minimax/v2/sessions/YYYY/MM/DD/<stamp>-session_<id>/messages.jsonl``
(API key material redacted upstream of these fixtures; the usage object and
envelope are verbatim).
"""

import json
from pathlib import Path

from tokdash.compute import _collect_parser_tail
from tokdash.pricing import PricingDatabase
from tokdash.sources.coding_tools import (
    CodingToolsUsageTracker,
    MiniMaxCodeParser,
)

TS = 1_789_742_004_724  # captured timestamp, epoch ms


def _isolate_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("MINIMAX_DATA_DIR", raising=False)
    monkeypatch.delenv("MAVIS_DATA_DIR", raising=False)
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


def _session_dir(home: Path, rel: str = "2026/09/18/14-33-22-887-session_x"):
    """Session dir at the shipped v0.4.12 shape: YYYY/MM/DD/<stamp>/."""
    d = home / ".minimax" / "v2" / "sessions" / rel
    d.mkdir(parents=True, exist_ok=True)
    return d


def _assistant_line(msg_id, ts=TS, model="MiniMax-M3", provider="minimax",
                    input_t=17213, output_t=81, cache_r=128, cache_w=0,
                    cost_total=0.0, role="assistant"):
    return json.dumps({
        "message_id": msg_id,
        "turn_id": "turn_mu726fn5_dw9423",
        "message": {
            "role": role,
            "content": [{"type": "text", "text": "ok"}],
            "api": "anthropic-messages",
            "provider": provider,
            "model": model,
            "usage": {
                "input": input_t,
                "output": output_t,
                "cacheRead": cache_r,
                "cacheWrite": cache_w,
                "totalTokens": input_t + output_t + cache_r + cache_w,
                "cost": {
                    "input": 0, "output": 0, "cacheRead": 0,
                    "cacheWrite": 0, "total": cost_total,
                },
            },
            "stopReason": "stop",
            "timestamp": ts,
            "responseId": "06fc7e93d4aeaf7bc43d1e09a7063d08",
        },
    })


def _drifting_line(msg_id, ts=TS, excess=5000):
    """An assistant row whose declared totalTokens exceeds its four buckets —
    the shape a future cache-inclusive build would produce."""
    row = json.loads(_assistant_line(msg_id, ts=ts))
    usage = row["message"]["usage"]
    usage["totalTokens"] = (
        usage["input"] + usage["output"] + usage["cacheRead"]
        + usage["cacheWrite"] + excess
    )
    return json.dumps(row)


def _user_line(msg_id="msg-user-v1-abc", ts=TS - 2000):
    return json.dumps({
        "message_id": msg_id,
        "turn_id": "turn_mu726fn5_dw9423",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": "hello"}],
            "timestamp": ts,
        },
    })


def _write(path: Path, *lines: str):
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class TestDiscovery:
    def test_finds_date_sharded_transcripts(self, monkeypatch, tmp_path):
        home = _isolate_home(monkeypatch, tmp_path)
        d1 = _session_dir(home)
        _write(d1 / "messages.jsonl", _assistant_line("msg-a"))
        d2 = _session_dir(home, "2026/09/17/09-00-00-000-session_y")
        _write(d2 / "messages.jsonl", _assistant_line("msg-b", ts=TS - 86_400_000))

        parser = MiniMaxCodeParser(PricingDatabase())
        entries = parser.collect()
        assert [e["entry_id"] for e in entries] == [
            "minimax:msg-b", "minimax:msg-a",
        ]  # sorted by timestamp

    def test_skips_dot_dirs_and_non_transcript_files(self, monkeypatch, tmp_path):
        home = _isolate_home(monkeypatch, tmp_path)
        d = _session_dir(home)
        _write(d / "manifest.json", "{}")  # not a transcript
        _write(d / "messages.jsonl", _assistant_line("msg-a"))
        hidden = home / ".minimax" / "v2" / "sessions" / ".cache"
        hidden.mkdir(parents=True)
        _write(hidden / "messages.jsonl", _assistant_line("msg-hidden"))

        parser = MiniMaxCodeParser(PricingDatabase())
        entries = parser.collect()
        assert [e["entry_id"] for e in entries] == ["minimax:msg-a"]

    def test_env_override_precedence(self, monkeypatch, tmp_path):
        _isolate_home(monkeypatch, tmp_path)
        mmx = tmp_path / "mmdata"
        legacy = tmp_path / "legacy"
        monkeypatch.setenv("MAVIS_DATA_DIR", str(legacy))
        from tokdash import clientpaths
        assert clientpaths.minimax_code_data_dir() == legacy
        monkeypatch.setenv("MINIMAX_DATA_DIR", str(mmx))
        assert clientpaths.minimax_code_data_dir() == mmx
        monkeypatch.setenv("MINIMAX_DATA_DIR", "   ")
        assert clientpaths.minimax_code_data_dir() == legacy
        monkeypatch.setenv("MAVIS_DATA_DIR", "")
        assert clientpaths.minimax_code_data_dir() == Path.home() / ".minimax"


class TestParsing:
    def test_captured_row_maps_to_disjoint_buckets(self, monkeypatch, tmp_path):
        home = _isolate_home(monkeypatch, tmp_path)
        d = _session_dir(home)
        _write(d / "messages.jsonl", _user_line(), _assistant_line("msg-a"))

        parser = MiniMaxCodeParser(PricingDatabase())
        entries = parser.collect()
        assert len(entries) == 1
        e = entries[0]
        assert e["source"] == "minimax"
        assert e["model"] == "MiniMax-M3"
        assert e["provider"] == "minimax"
        # Buckets are disjoint in the source (totalTokens == sum): no split.
        assert (e["input"], e["output"], e["cacheRead"], e["cacheWrite"]) == (
            17213, 81, 128, 0,
        )
        assert e["reasoning"] == 0
        assert e["timestamp"] == TS
        assert e["entry_id"] == "minimax:msg-a"
        # Captured cost is all-zero (subscription); pricing DB decides.
        assert e["cost"] > 0
        assert e["_billing"]["kind"] == "pricing"

    def test_bucket_drift_warns_once_and_buckets_stay_verbatim(
        self, monkeypatch, tmp_path, caplog,
    ):
        """totalTokens == sum is the verbatim-pass-through invariant; a
        future cache-inclusive build must surface as a warning, not as a
        silent cacheRead double-count."""
        home = _isolate_home(monkeypatch, tmp_path)
        d = _session_dir(home)
        _write(
            d / "messages.jsonl",
            _drifting_line("msg-a", ts=TS - 1),
            _drifting_line("msg-b"),
        )

        parser = MiniMaxCodeParser(PricingDatabase())
        entries = parser.collect()
        assert len(entries) == 2
        # Rows still pass through with the four buckets verbatim.
        assert all(e["cacheRead"] == 128 and e["input"] == 17213 for e in entries)
        warns = [r for r in caplog.records if "no longer disjoint" in r.getMessage()]
        assert len(warns) == 1  # once per pass, not per row

    def test_bucket_drift_tripwire_rearms_on_the_next_pass(
        self, monkeypatch, tmp_path, caplog,
    ):
        """One warning per pass, not one per process: _parse_all re-arms the
        guard, so a second pass over still-drifting rows reports again
        instead of going quiet for the life of the parser."""
        home = _isolate_home(monkeypatch, tmp_path)
        d = _session_dir(home)
        _write(d / "messages.jsonl", _drifting_line("msg-a"))

        parser = MiniMaxCodeParser(PricingDatabase())
        parser._parse_all()
        parser._parse_all()

        warns = [r for r in caplog.records if "no longer disjoint" in r.getMessage()]
        assert len(warns) == 2
        # The flag is per instance, never latched onto the class.
        assert MiniMaxCodeParser._drift_warned is False

    def test_skips_non_assistant_zero_usage_and_malformed(self, monkeypatch, tmp_path):
        home = _isolate_home(monkeypatch, tmp_path)
        d = _session_dir(home)
        _write(
            d / "messages.jsonl",
            _user_line(),
            _assistant_line("msg-zero", input_t=0, output_t=0, cache_r=0, cache_w=0),
            _assistant_line("msg-no-ts", ts=0),
            "not json at all",
            json.dumps(["a list is not an envelope"]),
            _assistant_line("msg-ok"),
        )

        parser = MiniMaxCodeParser(PricingDatabase())
        entries = parser.collect()
        assert [e["entry_id"] for e in entries] == ["minimax:msg-ok"]

    def test_missing_message_id_still_counts_without_stable_key(self, monkeypatch, tmp_path):
        home = _isolate_home(monkeypatch, tmp_path)
        d = _session_dir(home)
        row = json.loads(_assistant_line("x"))
        del row["message_id"]
        _write(d / "messages.jsonl", json.dumps(row))

        parser = MiniMaxCodeParser(PricingDatabase())
        entries = parser.collect()
        assert len(entries) == 1
        assert entries[0]["entry_id"] == ""
        assert entries[0]["input"] == 17213

    def test_duplicate_ids_deduped_within_file(self, monkeypatch, tmp_path):
        home = _isolate_home(monkeypatch, tmp_path)
        d = _session_dir(home)
        _write(d / "messages.jsonl", _assistant_line("msg-a"), _assistant_line("msg-a"))

        parser = MiniMaxCodeParser(PricingDatabase())
        assert len(parser.collect()) == 1

    def test_copied_id_owned_by_earliest_timestamp_not_first_seen(self, monkeypatch, tmp_path):
        """A fork/restamp copy must be owned by the EARLIEST occurrence even
        when the later-scanned file holds it (store parity: Cline/Qwen C7)."""
        home = _isolate_home(monkeypatch, tmp_path)
        first_scanned = _session_dir(home, "2026/09/17/09-00-00-000-session_a")
        later_scanned = _session_dir(home, "2026/09/18/09-00-00-000-session_b")
        _write(
            first_scanned / "messages.jsonl",
            _assistant_line("msg-a", ts=TS),               # restamped copy
        )
        _write(
            later_scanned / "messages.jsonl",
            _assistant_line("msg-a", ts=TS - 10_000),      # original, earlier
        )

        parser = MiniMaxCodeParser(PricingDatabase())
        entries = parser.collect()
        assert len(entries) == 1
        assert entries[0]["timestamp"] == TS - 10_000

    def test_per_row_model_prices_the_call_that_made_it(self, monkeypatch, tmp_path):
        home = _isolate_home(monkeypatch, tmp_path)
        d = _session_dir(home)
        _write(
            d / "messages.jsonl",
            _assistant_line("msg-a", model="MiniMax-M2.7", ts=TS - 1),
            _assistant_line("msg-b", model="MiniMax-M3"),
        )

        parser = MiniMaxCodeParser(PricingDatabase())
        entries = parser.collect()
        assert [e["model"] for e in entries] == ["MiniMax-M2.7", "MiniMax-M3"]
        assert all(e["cost"] > 0 for e in entries)

    def test_unknown_model_prices_zero(self, monkeypatch, tmp_path):
        home = _isolate_home(monkeypatch, tmp_path)
        d = _session_dir(home)
        _write(d / "messages.jsonl", _assistant_line("msg-a", model=""))

        parser = MiniMaxCodeParser(PricingDatabase())
        entries = parser.collect()
        assert entries[0]["model"] == "unknown"
        assert entries[0]["cost"] == 0.0


class TestAppendTail:
    def test_tail_ingests_only_appended_rows(self, monkeypatch, tmp_path):
        """append_jsonl=True: the persistent sync parses an appended tail from
        the stored offset alone, which is only sound because every assistant
        row is self-contained (own model, own provider, own buckets)."""
        home = _isolate_home(monkeypatch, tmp_path)
        d = _session_dir(home)
        path = d / "messages.jsonl"
        _write(path, _user_line(), _assistant_line("msg-a"))

        parser = MiniMaxCodeParser(PricingDatabase())
        assert [e["entry_id"] for e in parser._parse_all()] == ["minimax:msg-a"]
        start_offset = path.stat().st_size

        with path.open("a", encoding="utf-8") as handle:
            handle.write(_assistant_line("msg-b", ts=TS + 1_000) + "\n")

        file_sig = (str(path), path.stat().st_mtime_ns, path.stat().st_size)
        tail_entries, safe_offset = _collect_parser_tail(parser, file_sig, start_offset)

        assert [e["entry_id"] for e in tail_entries] == ["minimax:msg-b"]
        assert safe_offset == path.stat().st_size
        # Complete on its own: the tail never needed the rows before it.
        assert tail_entries[0]["model"] == "MiniMax-M3"
        assert tail_entries[0]["provider"] == "minimax"
        assert tail_entries[0]["input"] == 17213
        # A full reparse still yields each row exactly once.
        assert [e["entry_id"] for e in parser._parse_all()] == [
            "minimax:msg-a", "minimax:msg-b",
        ]

    def test_tail_stops_at_the_last_complete_line(self, monkeypatch, tmp_path):
        """A half-written final row is left for the next sync: the offset must
        not advance past it, or the completed row would never be ingested."""
        home = _isolate_home(monkeypatch, tmp_path)
        d = _session_dir(home)
        path = d / "messages.jsonl"
        _write(path, _assistant_line("msg-a"))

        parser = MiniMaxCodeParser(PricingDatabase())
        start_offset = path.stat().st_size
        complete = _assistant_line("msg-b", ts=TS + 1_000)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(complete + "\n" + _assistant_line("msg-c")[:40])

        file_sig = (str(path), path.stat().st_mtime_ns, path.stat().st_size)
        tail_entries, safe_offset = _collect_parser_tail(parser, file_sig, start_offset)

        assert [e["entry_id"] for e in tail_entries] == ["minimax:msg-b"]
        # The offset lands just past the last complete line's terminator, so
        # the partial row is re-read whole next time. Derived from the bytes
        # on disk rather than len(), which text-mode newline translation
        # would make wrong on Windows.
        assert safe_offset == path.read_bytes().rindex(b"\n") + 1
        assert safe_offset < path.stat().st_size


class TestRegistry:
    def test_registered_with_stable_identity(self, monkeypatch, tmp_path):
        _isolate_home(monkeypatch, tmp_path)
        tracker = CodingToolsUsageTracker()
        parser = tracker.parsers["minimax"]
        assert parser.source_name == "minimax"
        sig = parser.persistent_parser_signature()
        assert sig["version"] == MiniMaxCodeParser.persistent_parser_version
        assert sig["version"] >= 1
