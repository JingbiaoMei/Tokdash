"""Consumer contract test for pricing_db.json.

Run this after any pricing DB update (manual or auto-generated) to verify
that representative aliases, manual models, and derived models still resolve
correctly through PricingDatabase.

This test lives in tokdash (the consumer), not the updater repo.
"""

from tokdash.pricing import PricingDatabase


def test_manual_models_resolve():
    """Manual models (not on any source) must resolve."""
    db = PricingDatabase()

    # gpt-5.5: official OpenAI pricing page lists it before OpenRouter has an entry.
    cost = db.get_cost("gpt-5.5", 1000, 2000, 0, 0)
    assert cost > 0.0, "gpt-5.5 should resolve"

    # gpt-5.3-codex: manually maintained, uses gpt-5.2-codex pricing.
    cost = db.get_cost("gpt-5.3-codex", 1000, 2000, 0, 0)
    assert cost > 0.0, "gpt-5.3-codex should resolve"

    # k2p5: alias entry for kimi-k2.5.
    cost = db.get_cost("k2p5", 1000, 2000, 0, 0)
    assert cost > 0.0, "k2p5 should resolve"


def test_alias_entries_resolve():
    """All aliases in pricing_db.json must resolve to a real model."""
    db = PricingDatabase()

    representative_aliases = [
        "kimi-2.5",
        "vol-engine/kimi-2.5",
        "volcengine/kimi-2.5",
        "kimi-coding/k2p5",
        "moonshot-ai/kimi-k2.5",
    ]
    base_cost = db.get_cost("kimi-k2.5", 1000, 2000, 0, 0)
    assert base_cost > 0.0

    for alias in representative_aliases:
        alias_cost = db.get_cost(alias, 1000, 2000, 0, 0)
        assert abs(alias_cost - base_cost) < 1e-12, (
            f"Alias {alias!r} should resolve to kimi-k2.5 pricing"
        )


def test_glm_5_1_alias_entries_resolve():
    """GLM-5.1 aliases must resolve to the canonical GLM-5.1 pricing."""
    db = PricingDatabase()

    representative_aliases = [
        "glm5.1",
        "glm-5-1",
        "z-ai/glm-5.1",
        "zhipu/glm-5.1",
    ]
    base_cost = db.get_cost("glm-5.1", 1000, 2000, 0, 0)
    assert base_cost > 0.0

    for alias in representative_aliases:
        alias_cost = db.get_cost(alias, 1000, 2000, 0, 0)
        assert abs(alias_cost - base_cost) < 1e-12, (
            f"Alias {alias!r} should resolve to glm-5.1 pricing"
        )


def test_opus_4_7_alias_entries_resolve():
    """Opus 4.7 shorthand aliases must resolve to the canonical pricing."""
    db = PricingDatabase()

    representative_aliases = [
        "opus-4.7",
        "claude-opus-4-7",
    ]
    base_cost = db.get_cost("claude-opus-4.7", 1000, 2000, 0, 0)
    assert base_cost > 0.0

    for alias in representative_aliases:
        alias_cost = db.get_cost(alias, 1000, 2000, 0, 0)
        assert abs(alias_cost - base_cost) < 1e-12, (
            f"Alias {alias!r} should resolve to claude-opus-4.7 pricing"
        )


def test_derived_antigravity_models_resolve():
    """Antigravity models must resolve and match their base model pricing."""
    db = PricingDatabase()

    pairs = [
        ("antigravity-claude-opus-4-6-thinking", "claude-opus-4.6"),
        ("antigravity-claude-sonnet-4-6", "claude-sonnet-4.6"),
        ("antigravity-gemini-3-flash", "gemini-3-flash-preview"),
    ]
    for derived, base in pairs:
        d_cost = db.get_cost(derived, 1000, 2000, 0, 0)
        b_cost = db.get_cost(base, 1000, 2000, 0, 0)
        assert d_cost > 0.0, f"{derived} should resolve"
        assert abs(d_cost - b_cost) < 1e-12, (
            f"{derived} should match {base} pricing"
        )


def test_core_provider_models_resolve():
    """At least one model per tracked provider must resolve."""
    db = PricingDatabase()

    representative = {
        "openai": "gpt-5.5",
        "anthropic": "claude-opus-4.6",
        "google": "gemini-3-pro-preview",
        "moonshotai": "kimi-k2.5",
        "minimax": "minimax-m2.5",
        "z-ai": "glm-5.1",
    }
    for provider, model in representative.items():
        cost = db.get_cost(model, 1000, 2000, 0, 0)
        assert cost > 0.0, f"{model} ({provider}) should resolve with cost > 0"
