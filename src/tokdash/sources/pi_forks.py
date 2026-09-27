"""Pi fork ancestry and price-independent identities for copied usage events.

Only discovered session files participate. A parentSession path is a reference,
not permission to open another file. Missing parents still connect sibling forks
through the session id in Pi's timestamp_UUID.jsonl filename.
"""
from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path


def session_id_from_path(path: str) -> str:
    stem = Path(path.replace("\\", "/")).stem
    # Pi permits '_' inside custom session ids. Only strip its timestamp prefix.
    return re.sub(r"^\d{4}-\d{2}-\d{2}(?:T[^_]*)?_", "", stem)


@lru_cache(maxsize=8192)
def _header(path: str, mtime_ns: int, size: int) -> dict:
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            obj = json.loads(line)
            # Pi's session header is the first record. Artifacts with another
            # schema do not establish ancestry.
            return obj if isinstance(obj, dict) and obj.get("type") == "session" else {}
    return {}


def fork_contexts(signatures: tuple, previous_context: dict | None = None) -> dict[str, dict]:
    """Map files to ancestry metadata and ownership depth, regardless of order.

    An absent intermediate ancestor not previously indexed ends the provable
    chain. We retain usage across disconnected families until that link is
    available; matching ids and token counts alone is not evidence of ancestry.
    """
    previous_context = previous_context or {}
    headers = {}
    paths = {
        os.path.abspath(path): context["session_id"]
        for path, context in previous_context.items()
        if isinstance(context, dict) and context.get("session_id")
    }
    for path, mt, size in signatures:
        try:
            header = _header(path, mt, size)
        except (OSError, ValueError):
            header = {}
        sid = str(header.get("id") or session_id_from_path(path))
        headers[path] = (sid, header)
        paths[os.path.abspath(path)] = sid
    # Durable file state retains these proven edges even after the source files
    # disappear. Reopening the dashboard must not give a replay a new identity.
    parents = {
        context["session_id"]: context["parent_id"]
        for context in previous_context.values()
        if isinstance(context, dict) and context.get("session_id") and context.get("parent_id")
    }
    for path, (sid, header) in headers.items():
        parents.pop(sid, None)
        parent = header.get("parentSession")
        if isinstance(parent, str) and parent:
            parent_path = Path(parent)
            if not parent_path.is_absolute():
                parent_path = Path(path).parent / parent_path
            parent_id = paths.get(os.path.abspath(parent_path)) or session_id_from_path(parent)
            if parent_id and parent_id != sid:
                parents[sid] = parent_id

    def family(sid: str) -> tuple[str, int]:
        chain = set()
        current = sid
        while current in parents and current not in chain:
            chain.add(current)
            current = parents[current]
        if current in chain:
            # Malformed cycles do not justify merging distinct sessions.
            return sid, 0
        return current, len(chain)

    contexts = {}
    for path, (sid, _) in headers.items():
        root, depth = family(sid)
        contexts[path] = {
            "session_id": sid, "parent_id": parents.get(sid, ""),
            "family": root, "dedup_priority": depth,
        }
    return contexts


def event_identity(obj: dict) -> str:
    """Corroborate short ids with the immutable copied event fields, not cost.

    Pi's createBranchedSession removes label entries and rewrites parentId,
    so the tree edge is deliberately excluded. No prompts or response text
    enter usage keys, and repricing cannot change them. Missing ids stay unkeyed.
    """
    if not obj.get("id"):
        return ""
    message = obj.get("message") or {}
    usage = message.get("usage") or {}
    return json.dumps([
        obj["id"], obj.get("timestamp"),
        message.get("provider"), message.get("model"),
        [usage.get(k) for k in ("input", "output", "cacheRead", "cacheWrite", "totalTokens")],
    ], ensure_ascii=True, separators=(",", ":"))


def event_key(family: str, identity: str) -> str:
    return "pi_agent:" + json.dumps([family, identity], separators=(",", ":")) if identity else ""
