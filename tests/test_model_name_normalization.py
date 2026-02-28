from tokdash.model_normalization import normalize_model_name


def test_provider_prefix_and_case_punctuation_normalization():
    assert normalize_model_name("openai/gpt-4o-mini") == "gpt-4o-mini"
    assert normalize_model_name("github-copilot/GPT_4O_MINI") == "gpt-4o-mini"
    assert normalize_model_name("openrouter/openai/gpt-4o-mini") == "gpt-4o-mini"


def test_snapshot_and_release_suffix_normalization():
    assert normalize_model_name("openai/gpt-4o-mini-2024-07-18") == "gpt-4o-mini"
    assert normalize_model_name("models:claude-3.7-sonnet-latest") == "claude-3.7-sonnet"
    assert normalize_model_name("google/gemini-3-pro-preview") == "gemini-3-pro"


def test_alias_variants_normalization():
    assert normalize_model_name("google/gemini-3-pro-high") == "gemini-3-pro"
    assert normalize_model_name("anthropic/claude-3-5-sonnet") == "claude-3.5-sonnet"
    assert normalize_model_name("kimi-coding/k2p5") == "kimi-k2.5"
    assert normalize_model_name("vol-engine/kimi-2.5") == "kimi-k2.5"


def test_kimi_k25_variants():
    """All provider/name combinations for kimi-k2.5 must collapse to one key."""
    expected = "kimi-k2.5"
    cases = [
        # provider-prefixed variants (the "independent provider" cases)
        "kimi/kimi-k2p5",
        "kimi-coding/kimi-k2.5",
        "infi/kimi-2.5",
        "kimi/k2.5",
        "kimi/k2p5",
        "kimi/kimi-k2.5",
        "anything/kimi-k2.5",
        "anything/kimi2.5",
        "anything/kimi-2.5",
        # bare name variants (no provider prefix)
        "kimi-k2p5",
        "kimi-k2-5",
        "kimi-k2.5",
        "kimi2.5",
        "kimi-2.5",
        "kimi-2-5",
        # capitalisation
        "Kimi/KIMI-K2P5",
        # with suffix noise
        "kimi-k2.5-2025-01-01",
        "kimi-k2.5-thinking",
        "kimi--k2.5",
    ]
    for inp in cases:
        assert normalize_model_name(inp) == expected, (
            f"normalize_model_name({inp!r}) → {normalize_model_name(inp)!r}, expected {expected!r}"
        )
