from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Optional


class PricingDatabase:
    """Local pricing database (per 1M tokens).

    Backed by `pricing_db.json` shipped with the package.
    """

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or (Path(__file__).parent / "pricing_db.json")
        self.pricing: Dict[str, Dict[str, Any]] = {}
        self.aliases: Dict[str, str] = {}
        self._resolved_cache: Dict[str, Optional[Dict[str, Any]]] = {}
        self.load()

    def load(self) -> None:
        if not self.db_path.exists():
            return
        try:
            with open(self.db_path, "r", encoding="utf-8") as f:
                raw = json.load(f) or {}
                models = (raw.get("models") or {}) if isinstance(raw, dict) else {}
                # Filter out non-dict sentinel/comment entries.
                self.pricing = {k: v for k, v in models.items() if isinstance(v, dict)}

                aliases_raw = (raw.get("aliases") or {}) if isinstance(raw, dict) else {}
                aliases: Dict[str, str] = {}
                if isinstance(aliases_raw, dict):
                    for k, v in aliases_raw.items():
                        if not isinstance(k, str) or not isinstance(v, str):
                            continue
                        nk = self._normalize_alias_key(k)
                        nv = self._normalize_key(v)
                        if not nk or not nv:
                            continue
                        aliases[nk] = nv
                        # Also allow lookups by base model (drop provider prefix) for convenience.
                        aliases.setdefault(nk.split("/")[-1], nv)
                self.aliases = aliases

                self._resolved_cache = {}
        except Exception:
            self.pricing = {}
            self.aliases = {}
            self._resolved_cache = {}

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
        cached = self._resolved_cache.get(model)
        if cached is not None or model in self._resolved_cache:
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
            return self.pricing.get(k)

        # Try direct keys first (for exact DB matches).
        for k in (raw, base):
            p = consider(k)
            if p:
                self._resolved_cache[model] = p
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
            target = self.aliases.get(ak)
            if not target:
                continue
            p = consider(target)
            if p:
                self._resolved_cache[model] = p
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
                self._resolved_cache[model] = p
                return p

        self._resolved_cache[model] = None
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
