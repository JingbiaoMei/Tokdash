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

    cost = db.get_cost("deepseek/deepseek-v4-pro", 1000, 2000, 0, 0)
    assert cost > 0.0, "deepseek-v4-pro should resolve"

    cost = db.get_cost("deepseek/deepseek-v4-flash", 1000, 2000, 0, 0)
    assert cost > 0.0, "deepseek-v4-flash should resolve"

    cost = db.get_cost("kimi-k2.6", 1000, 2000, 0, 0)
    assert cost > 0.0, "kimi-k2.6 should resolve"


def test_gpt_5_6_family_pricing():
    """GPT-5.6 family entries must match OpenAI standard short-context pricing."""
    db = PricingDatabase()

    expected = {
        "gpt-5.6-sol": (5.0, 30.0, 0.5, 6.25),
        "gpt-5.6-terra": (2.0, 12.0, 0.20, 2.50),
        "gpt-5.6-terra-pro": (2.0, 12.0, 0.20, 2.50),
        "gpt-5.6-luna": (0.20, 1.20, 0.02, 0.25),
        "gpt-5.6-luna-pro": (0.20, 1.20, 0.02, 0.25),
    }
    for model, (input_price, output_price, cache_read_price, cache_write_price) in expected.items():
        cost = db.get_cost(model, 1000, 2000, 3000, 4000)
        expected_cost = (
            1000 * input_price
            + 2000 * output_price
            + 3000 * cache_read_price
            + 4000 * cache_write_price
        ) / 1_000_000
        assert abs(cost - expected_cost) < 1e-12, f"{model!r} pricing should match official table"


def test_deepseek_v4_flash_0731_pricing():
    """DeepSeek V4 Flash 0731 must match the official cache-hit/miss/output rates."""
    db = PricingDatabase()

    expected_cost = (
        1000 * 0.44 + 2000 * 1.32 + 3000 * 0.014 + 4000 * 0.44
    ) / 1_000_000
    for model in ["deepseek-v4-flash-0731", "deepseek/deepseek-v4-flash-0731"]:
        cost = db.get_cost(model, 1000, 2000, 3000, 4000)
        assert abs(cost - expected_cost) < 1e-12, (
            f"{model!r} should resolve to DeepSeek V4 Flash 0731 pricing"
        )


def test_deepseek_v4_pro_0813_pricing():
    """DeepSeek V4 Pro 0813 must match the official cache-hit/miss/output rates."""
    db = PricingDatabase()

    expected_cost = (
        1000 * 1.32 + 2000 * 3.96 + 3000 * 0.044 + 4000 * 1.32
    ) / 1_000_000
    for model in ["deepseek-v4-pro-0813", "deepseek/deepseek-v4-pro-0813"]:
        cost = db.get_cost(model, 1000, 2000, 3000, 4000)
        assert abs(cost - expected_cost) < 1e-12, (
            f"{model!r} should resolve to DeepSeek V4 Pro 0813 pricing"
        )


def test_grok_4_6_pricing():
    """Grok 4.6 must match xAI's standard-tier (sub-200K prompt) rates."""
    db = PricingDatabase()

    expected_cost = (
        1000 * 2.0 + 2000 * 6.0 + 3000 * 0.5 + 4000 * 2.0
    ) / 1_000_000
    for model in ["grok-4.6", "x-ai/grok-4.6", "xai/grok-4.6"]:
        cost = db.get_cost(model, 1000, 2000, 3000, 4000)
        assert abs(cost - expected_cost) < 1e-12, (
            f"{model!r} should resolve to Grok 4.6 standard-tier pricing"
        )


def test_glm_5_3_resolves():
    """GLM-5.3 and its aliases must resolve to a non-zero price.

    Rates are provisional (z.ai had not published per-token pricing as of
    2026-08-14), so this asserts resolution only -- deliberately not exact
    values, which would pin an unofficial number as if it were confirmed.
    """
    db = PricingDatabase()

    base_cost = db.get_cost("glm-5.3", 1000, 2000, 0, 0)
    assert base_cost > 0.0, "glm-5.3 should resolve"

    for alias in ["glm5.3", "glm-5-3", "z-ai/glm-5.3", "zhipu/glm-5.3"]:
        alias_cost = db.get_cost(alias, 1000, 2000, 0, 0)
        assert abs(alias_cost - base_cost) < 1e-12, (
            f"Alias {alias!r} should resolve to glm-5.3 pricing"
        )


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


def test_kimi_2_6_alias_entries_resolve():
    """Kimi K2.6 aliases must resolve to the canonical Kimi K2.6 pricing."""
    db = PricingDatabase()

    representative_aliases = [
        "k2p6",
        "k2-6",
        "kimi-2.6",
        "moonshot-ai/kimi-k2.6",
    ]
    base_cost = db.get_cost("kimi-k2.6", 1000, 2000, 0, 0)
    assert base_cost > 0.0

    for alias in representative_aliases:
        alias_cost = db.get_cost(alias, 1000, 2000, 0, 0)
        assert abs(alias_cost - base_cost) < 1e-12, (
            f"Alias {alias!r} should resolve to kimi-k2.6 pricing"
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


def test_glm_5_2_cloudflare_pricing_resolves():
    """GLM-5.2 Cloudflare pricing and aliases must resolve."""
    db = PricingDatabase()

    expected_cost = (1000 * 1.4 + 2000 * 4.4 + 3000 * 0.26 + 4000 * 1.4) / 1_000_000
    for model in [
        "glm-5.2",
        "glm5.2",
        "glm-5-2",
        "cloudflare/glm-5.2",
        "z-ai/glm-5.2",
        "zhipu/glm-5.2",
    ]:
        cost = db.get_cost(model, 1000, 2000, 3000, 4000)
        assert abs(cost - expected_cost) < 1e-12, (
            f"{model!r} should resolve to GLM-5.2 Cloudflare pricing"
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


def test_opus_4_8_alias_entries_resolve():
    """Opus 4.8 shorthand aliases must resolve to the canonical pricing."""
    db = PricingDatabase()

    representative_aliases = [
        "opus-4.8",
        "claude-opus-4-8",
    ]
    base_cost = db.get_cost("claude-opus-4.8", 1000, 2000, 0, 0)
    assert base_cost > 0.0

    for alias in representative_aliases:
        alias_cost = db.get_cost(alias, 1000, 2000, 0, 0)
        assert abs(alias_cost - base_cost) < 1e-12, (
            f"Alias {alias!r} should resolve to claude-opus-4.8 pricing"
        )


def test_opus_4_8_matches_4_7_pricing():
    """Opus 4.8 must price identically to Opus 4.7."""
    db = PricingDatabase()

    cost_47 = db.get_cost("claude-opus-4.7", 1000, 2000, 500, 500)
    cost_48 = db.get_cost("claude-opus-4.8", 1000, 2000, 500, 500)
    assert cost_48 > 0.0
    assert abs(cost_48 - cost_47) < 1e-12, "Opus 4.8 should match Opus 4.7 pricing"


def test_opus_5_alias_entries_resolve():
    """Opus 5 shorthand aliases must resolve to the canonical pricing."""
    db = PricingDatabase()

    representative_aliases = [
        "opus-5",
        "claude-opus-5",
    ]
    base_cost = db.get_cost("claude-opus-5", 1000, 2000, 0, 0)
    assert base_cost > 0.0

    for alias in representative_aliases:
        alias_cost = db.get_cost(alias, 1000, 2000, 0, 0)
        assert abs(alias_cost - base_cost) < 1e-12, (
            f"Alias {alias!r} should resolve to claude-opus-5 pricing"
        )


def test_opus_5_matches_published_pricing():
    """Opus 5 must resolve at $5/$25 per 1M (cache $0.50/$6.25)."""
    db = PricingDatabase()

    expected_cost = (
        1000 * 5 + 2000 * 25 + 3000 * 0.5 + 4000 * 6.25
    ) / 1_000_000
    cost = db.get_cost("claude-opus-5", 1000, 2000, 3000, 4000)
    assert abs(cost - expected_cost) < 1e-12, "Opus 5 should match published pricing"


def test_fable_5_aliases_and_pricing():
    """Fable 5 aliases must resolve to the published input/output pricing."""
    db = PricingDatabase()

    expected_cost = (1000 * 10 + 2000 * 50) / 1_000_000
    for model in ["claude-fable-5", "fable-5", "fable5", "fable"]:
        cost = db.get_cost(model, 1000, 2000, 0, 0)
        assert abs(cost - expected_cost) < 1e-12, (
            f"{model!r} should resolve to Claude Fable 5 pricing"
        )


def test_sonnet_5_aliases_and_introductory_pricing():
    """Sonnet 5 aliases must resolve to Anthropic's introductory API pricing."""
    db = PricingDatabase()

    expected_cost = (1000 * 2 + 2000 * 10 + 3000 * 0.20 + 4000 * 2.50) / 1_000_000
    for model in ["claude-sonnet-5", "sonnet-5", "sonnet5", "claude-sonnet-5-20260630"]:
        cost = db.get_cost(model, 1000, 2000, 3000, 4000)
        assert abs(cost - expected_cost) < 1e-12, (
            f"{model!r} should resolve to Claude Sonnet 5 introductory pricing"
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


def test_qwen3_8_max_pricing_and_aliases():
    """Qwen3.8-Max must resolve at OpenRouter pricing across spellings."""
    db = PricingDatabase()

    expected_cost = (1000 * 2.0 + 2000 * 6.0 + 3000 * 0.25 + 4000 * 2.5) / 1_000_000
    for model in ["qwen3.8-max", "qwen/qwen3.8-max", "qwen3.8max"]:
        cost = db.get_cost(model, 1000, 2000, 3000, 4000)
        assert abs(cost - expected_cost) < 1e-12, (
            f"{model!r} should resolve to Qwen3.8-Max pricing"
        )


def test_qwen3_7_flash_pricing():
    """Qwen3.7-Flash must resolve at OpenRouter pricing across spellings."""
    db = PricingDatabase()

    expected_cost = (1000 * 0.03 + 2000 * 0.13 + 3000 * 0.006 + 4000 * 0.038) / 1_000_000
    for model in ["qwen3.7-flash", "qwen/qwen3.7-flash"]:
        cost = db.get_cost(model, 1000, 2000, 3000, 4000)
        assert abs(cost - expected_cost) < 1e-12, (
            f"{model!r} should resolve to Qwen3.7-Flash pricing"
        )


def test_longcat_2_0_pricing_and_aliases():
    """Meituan LongCat 2.0 must resolve at OpenRouter pricing across spellings."""
    db = PricingDatabase()

    expected_cost = (1000 * 0.3 + 2000 * 1.2 + 3000 * 0.006 + 4000 * 0.3) / 1_000_000
    for model in ["longcat-2.0", "meituan/longcat-2.0", "longcat2.0", "longcat-2-0"]:
        cost = db.get_cost(model, 1000, 2000, 3000, 4000)
        assert abs(cost - expected_cost) < 1e-12, (
            f"{model!r} should resolve to LongCat 2.0 pricing"
        )


def test_seed_2_1_turbo_pricing_and_aliases():
    """Doubao Seed 2.1 Turbo must resolve at Volcengine-derived pricing.

    Official Volcengine CNY price (3.0/15.0/0.6 in/out/cache_read, tier [0,256])
    converted at 0.137 USD/CNY; cache_write=input (no separate Volcengine fee).
    """
    db = PricingDatabase()

    expected_cost = (1000 * 0.411 + 2000 * 2.055 + 3000 * 0.0822 + 4000 * 0.411) / 1_000_000
    for model in ["seed-2.1-turbo", "doubao-seed-2.1-turbo"]:
        cost = db.get_cost(model, 1000, 2000, 3000, 4000)
        assert abs(cost - expected_cost) < 1e-12, (
            f"{model!r} should resolve to Doubao Seed 2.1 Turbo pricing"
        )


def test_gemini_3_7_flash_pricing():
    """Gemini 3.7 Flash must resolve at list price across spellings.

    OpenRouter currently shows a 50% launch discount (0.375/1.875); the DB tracks
    the undiscounted rate, as the 3.6-flash entry does.
    """
    db = PricingDatabase()

    expected_cost = (1000 * 0.75 + 2000 * 3.75 + 3000 * 0.075 + 4000 * 0.041667) / 1_000_000
    for model in [
        "gemini-3.7-flash",
        "google/gemini-3.7-flash",
        "models/gemini-3.7-flash",
        "gemini-3-7-flash",
        "Gemini 3.7 Flash",
    ]:
        cost = db.get_cost(model, 1000, 2000, 3000, 4000)
        assert abs(cost - expected_cost) < 1e-12, (
            f"{model!r} should resolve to Gemini 3.7 Flash pricing"
        )
