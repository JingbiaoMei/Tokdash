Qoder usage fixtures (rebuilt 2026-08-21 from docs/local evidence captures).

localdb_cn/SharedClientCache/cache/db/local.db
    Rebuilt from windows_acplog_context_usage_all.json (61 ACP context_usage
    events, 1:1 per model call) + windows_localdb_sample_rows.json (12 captured
    chat_message rows, which keep their real ids/roles/timestamps) +
    windows_localdb_schema.sql (chat_message DDL).
    The DB holds 60 rows: the 61 ACP events MINUS index 57 (prompt 49895 /
    completion 1027, 2026-06-16T11:20:19) -- the one event with no token_info
    row in the capture. The 60-row sums match
    windows_localdb_session_summary.json exactly: prompt 2,319,522 /
    completion 10,808 / cached 0. Roles: 52 tool / 6 assistant / 2 user.
    model_info is empty on every row (CN build; the parser maps it to "auto").

localdb_intl/SharedClientCache/cache/db/local.db
    The two chat_message rows from mac_localdb_chat_data.json: a user row with
    empty token_info (filtered out) and the assistant row 17,553 in / 115 out,
    model_info {"model_key": "auto"}.

mac_cli_*.jsonl
    Copied verbatim from the international Qoder CLI v1.1.28 captures on the
    macbook (two -p sessions: interactive end_turn + tool_use).
