import re


def normalize_model_name(name: str) -> str:
    """Return canonical model key for cross-source merge (model-only, no provider).

    Normalisation pipeline (applied in order):
    1. Strip leading ``models?[:/]`` prefix.
    2. Drop every provider/app prefix chain by taking the last ``/``-delimited segment.
    3. Strip known vendor prefixes (e.g. ``antigravity-``).
    4. Replace whitespace/underscores with hyphens; collapse repeated hyphens.
    5. Strip trailing release noise: ``-latest``, ``-stable``, ``-preview``,
       ``-experimental``, ``-exp``, ``-YYYY-MM-DD``, ``-YYYYMMDD``.
    6. Strip ``-thinking`` suffix to unify thinking/non-thinking variants.
    7. Apply explicit alias map (e.g. ``gemini-3-pro-high`` → ``gemini-3-pro``,
       ``claude-3-5-sonnet`` → ``claude-3.5-sonnet``, ``k2p5`` / ``k2-5`` → ``k2.5``).
    8. Convert hyphenated version numbers: ``4-5`` → ``4.5``.
    9. Normalise Anthropic opus/sonnet names to ``claude-`` prefix.
    10. Collapse all kimi-k2.5 variants (``kimi/kimi-k2p5``, ``kimi-coding/kimi-k2.5``,
        ``infi/kimi-2.5``, ``kimi-k2-5``, ``kimi2.5``, etc.) → ``kimi-k2.5``.
    """
    n = (name or "").strip().lower()
    if not n:
        return "unknown"

    n = n.replace("\\", "/")
    n = re.sub(r"^(models?|model)[:/]", "", n)
    n = n.split("/")[-1]  # remove provider/app prefix chain
    
    # Strip provider prefixes (antigravity-, etc.)
    n = re.sub(r"^antigravity-", "", n)
    
    n = re.sub(r"[\s_]+", "-", n)
    n = re.sub(r"-+", "-", n).strip("-")

    n = re.sub(r"-(latest|stable)$", "", n)
    n = re.sub(r"-(preview|exp|experimental)(?:-[\w\d]+)?$", "", n)
    n = re.sub(r"-(\d{4}-\d{2}-\d{2}|\d{8})$", "", n)
    
    # Strip -thinking suffix to combine thinking/non-thinking variants
    n = re.sub(r"-thinking$", "", n)

    # Common family-level aliases
    alias_map = {
        "gemini-3-pro-high": "gemini-3-pro",
        "gemini-3-pro-low": "gemini-3-pro",
        "gemini-3-pro-preview": "gemini-3-pro",
        "o3-mini-high": "o3-mini",
        "o3-mini-low": "o3-mini",
        "claude-3-5-sonnet": "claude-3.5-sonnet",
        "claude-3-7-sonnet": "claude-3.7-sonnet",
        "k2p5": "k2.5",
        "k2-5": "k2.5",
    }
    n = alias_map.get(n, n)

    # Normalize version numbers: 4-5 → 4.5, 4-6 → 4.6, etc.
    n = re.sub(r"-(\d)-(\d+)", r"-\1.\2", n)

    # Merge Anthropic naming variants under one model key (for combined view).
    n = re.sub(r"^claude-opus", "opus", n)
    
    # Add claude- prefix to opus/sonnet models that don't have it
    if n.startswith("opus") or n.startswith("sonnet"):
        if not n.startswith("claude-"):
            n = "claude-" + n

    # Kimi variants: k2p5 vs k2.5 vs kimi-k2.5
    n = re.sub(r"k2(?:p|-)5", "k2.5", n)
    if n in {"k2.5", "kimi2.5", "kimi-2.5", "kimi-k2p5", "kimi-k2-5"}:
        n = "kimi-k2.5"
    if n.startswith("kimi") and ("k2.5" in n or "k2p5" in n or "k2-5" in n or "2.5" in n):
        n = "kimi-k2.5"

    return n or "unknown"


NORMALIZATION_EXAMPLES = {
    # Provider-prefix stripping
    "openai/gpt-4o-mini": "gpt-4o-mini",
    "github-copilot/GPT_4O_MINI": "gpt-4o-mini",
    "openrouter/openai/gpt-4o-mini-2024-07-18": "gpt-4o-mini",
    # Alias / version-dot normalisation
    "google/gemini-3-pro-high": "gemini-3-pro",
    "anthropic/claude-3-5-sonnet": "claude-3.5-sonnet",
    # Release-suffix stripping
    "models:claude-3.7-sonnet-latest": "claude-3.7-sonnet",
    # Kimi-k2.5 variants — all collapse to one key regardless of provider or
    # spelling (k2p5 / k2-5 / k2.5 / 2.5, with or without leading kimi-).
    "kimi/kimi-k2p5": "kimi-k2.5",
    "kimi-coding/kimi-k2.5": "kimi-k2.5",
    "infi/kimi-2.5": "kimi-k2.5",
    "kimi-coding/k2p5": "kimi-k2.5",
    "vol-engine/kimi-2.5": "kimi-k2.5",
    "kimi-k2p5": "kimi-k2.5",
    "kimi-k2-5": "kimi-k2.5",
    "kimi2.5": "kimi-k2.5",
}
