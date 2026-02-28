from tokdash.pricing import PricingDatabase


def test_pricing_lookup_is_case_insensitive_and_ignores_provider_prefixes():
    db = PricingDatabase()

    direct = db.get_cost("minimax-m2.5", 1000, 2000, 300, 400)
    variant = db.get_cost("minimax/MiniMax-M2.5", 1000, 2000, 300, 400)

    assert direct > 0.0
    assert abs(direct - variant) < 1e-12


def test_pricing_lookup_strips_release_date_suffixes():
    db = PricingDatabase()

    base = db.get_cost("gpt-4o-mini", 1000, 2000, 0, 0)
    dated = db.get_cost("openai/gpt-4o-mini-2024-07-18", 1000, 2000, 0, 0)

    assert base > 0.0
    assert abs(base - dated) < 1e-12


def test_pricing_lookup_supports_kimi_k2p5_aliases():
    db = PricingDatabase()

    base = db.get_cost("kimi-k2.5", 1000, 2000, 0, 0)
    provider_head = db.get_cost("vol-engine/kimi-2.5", 1000, 2000, 0, 0)
    alias_a = db.get_cost("k2.5", 1000, 2000, 0, 0)
    alias_b = db.get_cost("MoonshotAI/KIMI_K2P5", 1000, 2000, 0, 0)

    assert base > 0.0
    assert abs(base - provider_head) < 1e-12
    assert abs(base - alias_a) < 1e-12
    assert abs(base - alias_b) < 1e-12
