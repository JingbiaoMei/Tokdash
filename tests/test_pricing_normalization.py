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


def test_pricing_lookup_strips_quantization_suffixes():
    db = PricingDatabase()

    base = db.get_cost("qwen3.6-27b", 1000, 2000, 0, 0)
    fp8 = db.get_cost("vllm-hpc/qwen3.6-27B-FP8", 1000, 2000, 0, 0)
    fp16 = db.get_cost("qwen3.6-27B-FP16", 1000, 2000, 0, 0)
    int8 = db.get_cost("qwen3.6-27B-INT8", 1000, 2000, 0, 0)
    awq = db.get_cost("qwen3.6-27B-AWQ", 1000, 2000, 0, 0)

    assert base > 0.0
    assert abs(base - fp8) < 1e-12
    assert abs(base - fp16) < 1e-12
    assert abs(base - int8) < 1e-12
    assert abs(base - awq) < 1e-12


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


def test_pricing_lookup_strips_effort_suffixes_without_stripping_single_letters():
    db = PricingDatabase()

    pro = db.get_cost("gemini-3-pro", 1000, 2000, 0, 0)
    pro_high = db.get_cost("gemini-3-pro-high", 1000, 2000, 0, 0)
    pro_low = db.get_cost("gemini-3-pro-low", 1000, 2000, 0, 0)
    command_a = db.get_cost("command-a", 1000, 2000, 0, 0)

    assert pro > 0.0
    assert abs(pro - pro_high) < 1e-12
    assert abs(pro - pro_low) < 1e-12
    assert command_a > 0.0


def test_pricing_lookup_supports_real_antigravity_model_ids():
    db = PricingDatabase()

    for model in [
        "gemini-3-flash-a",
        "gemini-3-pro-high",
        "gemini-3-pro-low",
        "claude-opus-4-6-thinking",
    ]:
        assert db.get_cost(model, 1000, 2000, 300, 400) > 0.0
