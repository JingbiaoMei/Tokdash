from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, Optional


class PricingDatabase:
    """Local pricing database (per 1M tokens).

    Backed by `pricing_db.json` shipped with the package.
    """

    def __init__(self, db_path: Optional[Path] = None, override_path: Optional[Path] = None):
        self.db_path = db_path or (Path(__file__).parent / "pricing_db.json")
        # User overrides live under the data dir (not site-packages) so dashboard pricing
        # edits survive `tokdash update` and don't 500 on a read-only install. Resolved
        # lazily per-load (default None) so TOKDASH_DATA_DIR changes are honored.
        self._override_path_explicit = override_path
        # pricing + aliases + a memo cache are published together as ONE immutable snapshot
        # reference (self._state). load() rebuilds the triple and swaps it in a single atomic
        # assignment, and readers (_resolve_pricing) grab the reference once — so a reload on a
        # request worker thread (sessions._pricing_signature reloads on signature drift) can
        # never expose a half-updated pricing/aliases pair, nor leak a stale entry into a
        # freshly-cleared cache. A single attribute rebind/read is atomic under the GIL, so no lock.
        self._state: tuple = ({}, {}, {})
        self.load()

    @property
    def pricing(self) -> Dict[str, Dict[str, Any]]:
        return self._state[0]

    @property
    def aliases(self) -> Dict[str, str]:
        return self._state[1]

    def override_path(self) -> Path:
        if self._override_path_explicit is not None:
            return self._override_path_explicit
        from .onboard import paths

        return paths.pricing_db_override_path()

    def load(self) -> None:
        # The user override (under the data dir) is AUTHORITATIVE when present and valid —
        # full replacement, not a merge. This preserves the dashboard editor's WYSIWYG
        # contract (a deleted model stays deleted; what you save is the effective DB) and
        # still fixes the packaged-file-write defects (edits live under TOKDASH_DATA_DIR).
        # A missing/corrupt override falls back to the packaged baseline (never wiped).
        loaded = self._load_file(self.override_path())
        if loaded is None:
            loaded = self._load_file(self.db_path)
        pricing, aliases = loaded if loaded is not None else ({}, {})
        self._state = (pricing, aliases, {})  # atomic publish (see __init__)

    def signature(self) -> tuple:
        """Stat baseline + override so caches keyed on this bust when EITHER changes.

        The baseline is packaged/read-only (it changes only on reinstall = a new process), so a
        stat is enough. The override is the one file that can change OUT OF BAND while serving (a
        manual edit, or a sibling/--workers process that handled a PUT), so fold in a content
        hash too — that way an edit preserving the byte size within a single mtime tick still
        busts the cache. The override is small and usually absent, so this stays cheap.
        """
        sig: list = []
        try:
            st = self.db_path.stat()
            sig.append((str(self.db_path), st.st_mtime_ns, st.st_size))
        except OSError:
            sig.append((str(self.db_path), 0, 0))
        ov = self.override_path()
        try:
            raw = ov.read_bytes()
            sig.append((str(ov), len(raw), hashlib.blake2b(raw, digest_size=16).hexdigest()))
        except OSError:
            sig.append((str(ov), 0, ""))
        return tuple(sig)

    def _load_file(self, path: Path):
        """Parse one pricing file. Returns (models, aliases), or None if absent/invalid.

        ``None`` (not empty dicts) signals "no usable file here" so the caller can fall back
        to the baseline rather than wiping pricing on a missing/corrupt override.
        """
        try:
            if not path.exists():
                return None
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f) or {}
        except Exception:
            return None
        if not isinstance(raw, dict) or not isinstance(raw.get("models"), dict):
            return None
        models = {k: v for k, v in raw["models"].items() if isinstance(v, dict)}
        aliases: Dict[str, str] = {}
        aliases_raw = raw.get("aliases") or {}
        if isinstance(aliases_raw, dict):
            for k, v in aliases_raw.items():
                if not isinstance(k, str) or not isinstance(v, str):
                    continue
                nk = self._normalize_alias_key(k)
                nv = self._normalize_key(v)
                if not nk or not nv:
                    continue
                aliases[nk] = nv
                aliases.setdefault(nk.split("/")[-1], nv)
        return models, aliases

    @staticmethod
    def _normalize_key(s: str) -> str:
        s = (s or "").strip().lower()
        if not s:
            return ""
        s = s.replace("\\", "/")
        s = re.sub(r"^(models?|model)[:/]", "", s)
        s = s.split("/")[-1]
        s = re.sub(r"[\s_]+", "-", s)
        s = re.sub(r"-+", "-", s).strip("-")
        return s

    @staticmethod
    def _normalize_alias_key(s: str) -> str:
        """Normalize alias keys while preserving provider/model structure."""
        s = (s or "").strip().lower()
        if not s:
            return ""
        s = s.replace("\\", "/")
        s = re.sub(r"^(models?|model):", "", s)
        s = re.sub(r"[\s_]+", "-", s)
        s = re.sub(r"-+", "-", s).strip("-")
        return s

    @staticmethod
    def _strip_common_suffixes(key: str) -> str:
        k = key
        k = re.sub(r"-(latest|stable)$", "", k)
        k = re.sub(r"-(\d{4}-\d{2}-\d{2}|\d{8})$", "", k)
        k = re.sub(r"-thinking$", "", k)
        k = re.sub(r"-(high|medium|low)$", "", k)
        # Quantization / precision format suffixes (e.g. qwen3.6-27B-FP8 -> qwen3.6-27b).
        k = re.sub(r"-(fp16|fp8|int8|int4|bf16|awq|gptq|gguf)$", "", k)
        return k

    @staticmethod
    def _version_hyphen_to_dot(key: str) -> str:
        # 4-6 -> 4.6 (seen in some provider model IDs)
        return re.sub(r"-(\d)-(\d+)", r"-\1.\2", key)

    @staticmethod
    def _kimi_aliases(key: str) -> list[str]:
        k = key
        # Common Kimi / K2.5 naming variations across providers.
        if k in {"k2.5", "k2-5", "k2p5", "kimi2.5", "kimi-k2p5", "kimi-k2-5"}:
            return ["k2p5", "kimi-k2.5"]
        if k.startswith("kimi") and ("k2.5" in k or "k2p5" in k or "k2-5" in k):
            return ["k2p5", "kimi-k2.5"]
        return []

    def _resolve_pricing(self, model: str) -> Optional[Dict[str, Any]]:
        # Grab the published snapshot ONCE so pricing/aliases/cache are a consistent triple
        # even if load() swaps in a new state mid-resolution on another thread.
        pricing, aliases, cache = self._state
        cached = cache.get(model)
        if cached is not None or model in cache:
            return cached

        raw = model or ""
        base = raw.split("/")[-1] if "/" in raw else raw
        base = base or raw

        seen: set[str] = set()

        def consider(k: str) -> Optional[Dict[str, Any]]:
            if not k:
                return None
            if k in seen:
                return None
            seen.add(k)
            return pricing.get(k)

        # Try direct keys first (for exact DB matches).
        for k in (raw, base):
            p = consider(k)
            if p:
                cache[model] = p
                return p

        # Alias map (generated externally) to unify provider heads / naming variants.
        # Example: "vol-engine/kimi-2.5" -> "kimi-k2.5"
        alias_keys = [
            raw,
            base,
            self._normalize_alias_key(raw),
            self._normalize_alias_key(base),
            self._normalize_key(raw),
            self._normalize_key(base),
        ]
        alias_variants: list[str] = []
        for ak in alias_keys:
            if not ak:
                continue
            alias_variants.append(ak)
            alias_variants.append(self._strip_common_suffixes(ak))
            alias_variants.append(self._version_hyphen_to_dot(ak))
            alias_variants.append(self._version_hyphen_to_dot(self._strip_common_suffixes(ak)))

        for ak in alias_variants:
            if not ak:
                continue
            target = aliases.get(ak)
            if not target:
                continue
            p = consider(target)
            if p:
                cache[model] = p
                return p

        # Normalized + suffix-stripped candidates.
        norm = self._normalize_key(raw)
        base_norm = self._normalize_key(base)
        candidates = [norm, base_norm]

        expanded: list[str] = []
        for k in candidates:
            if not k:
                continue
            expanded.append(k)
            expanded.append(self._strip_common_suffixes(k))
            expanded.append(self._version_hyphen_to_dot(k))
            expanded.append(self._version_hyphen_to_dot(self._strip_common_suffixes(k)))

            if k.startswith("antigravity-"):
                k2 = k.removeprefix("antigravity-")
                expanded.append(k2)
                expanded.append(self._strip_common_suffixes(k2))
                expanded.append(self._version_hyphen_to_dot(k2))
                expanded.append(self._version_hyphen_to_dot(self._strip_common_suffixes(k2)))

            expanded.extend(self._kimi_aliases(k))
            expanded.extend(self._kimi_aliases(self._strip_common_suffixes(k)))

        for k in expanded:
            p = consider(k)
            if p:
                cache[model] = p
                return p

        cache[model] = None
        return None

    def get_cost(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_read: int = 0,
        cache_write: int = 0,
    ) -> float:
        pricing = self._resolve_pricing(model)
        if not pricing:
            return 0.0

        input_rate = float(pricing.get("input", 0.0) or 0.0)
        output_rate = float(pricing.get("output", 0.0) or 0.0)
        cache_read_rate = float(pricing.get("cache_read", input_rate * 0.1) or 0.0)
        cache_write_rate = float(pricing.get("cache_write", input_rate) or 0.0)

        return (
            (int(input_tokens or 0) * input_rate)
            + (int(output_tokens or 0) * output_rate)
            + (int(cache_read or 0) * cache_read_rate)
            + (int(cache_write or 0) * cache_write_rate)
        ) / 1_000_000
