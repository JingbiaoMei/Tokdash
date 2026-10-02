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
        "gemini-3.8-flash-n",
        "antigravity-gemini-3.8-flash-n",
    ]:
        assert db.get_cost(model, 1000, 2000, 300, 400) > 0.0


def test_pricing_lookup_gemini_3_8_flash_n_matches_base():
    db = PricingDatabase()
    base = db.get_cost("gemini-3.8-flash", 1000, 2000, 300, 400)
    variant = db.get_cost("gemini-3.8-flash-n", 1000, 2000, 300, 400)
    prefixed = db.get_cost("antigravity-gemini-3.8-flash-n", 1000, 2000, 300, 400)

    assert base > 0.0
    assert abs(base - variant) < 1e-12
    assert abs(base - prefixed) < 1e-12


def test_pricing_lookup_strips_six_digit_date_suffixes():
    """YYMMDD release snapshots (e.g. glm-5-2-260617 = 2026-06-17) resolve to base.

    All three provider date-suffix spellings collapse to the same priced model.
    """
    db = PricingDatabase()

    base = db.get_cost("glm-5.2", 1000, 2000, 0, 0)
    yymmdd = db.get_cost("volcengine-coding-plan/glm-5-2-260617", 1000, 2000, 0, 0)
    yyyymmdd = db.get_cost("glm-5-2-20260617", 1000, 2000, 0, 0)
    iso = db.get_cost("glm-5-2-2026-06-17", 1000, 2000, 0, 0)

    assert base > 0.0
    assert abs(base - yymmdd) < 1e-12
    assert abs(base - yyyymmdd) < 1e-12
    assert abs(base - iso) < 1e-12


def test_strip_common_suffixes_only_strips_valid_dates():
    """The date stripper must not collapse arbitrary numeric identifiers that
    are not plausible dates (month 01-12, day 01-31 where present)."""
    strip = PricingDatabase._strip_common_suffixes

    # Real date snapshots in every supported spelling are stripped.
    assert strip("glm-5-2-260617") == "glm-5-2"        # YYMMDD
    assert strip("glm-5-2-20260617") == "glm-5-2"      # YYYYMMDD
    assert strip("glm-5-2-2026-06-17") == "glm-5-2"    # YYYY-MM-DD
    assert strip("deepseek-v4-flash-2604") == "deepseek-v4-flash"  # YYMM (2026-04)
    assert strip("gpt-4o-mini-2024-07-18") == "gpt-4o-mini"

    # Non-date numeric identifiers are preserved (no false-positive stripping).
    assert strip("model-123456") == "model-123456"     # 34 is not a month
    assert strip("model-999999") == "model-999999"     # not a date
    assert strip("model-260699") == "model-260699"     # valid month, day 99 invalid
    assert strip("model-2699") == "model-2699"         # YYMM, 99 is not a month
    assert strip("model-2613") == "model-2613"         # YYMM, 13 is not a month

    # A genuinely date-shaped suffix is still stripped.
    assert strip("model-261231") == "model"            # Dec 31 -> stripped


def test_pricing_lookup_strips_four_digit_yymm_snapshot():
    """A YYMM year-month snapshot not in the DB falls back to its base model.

    Concrete case: volcengine-coding-plan/deepseek-v4-flash-2604 (2026-04)
    must price as deepseek-v4-flash, not $0.
    """
    db = PricingDatabase()

    base = db.get_cost("deepseek-v4-flash", 1000, 2000, 0, 0)
    snapshot = db.get_cost("volcengine-coding-plan/deepseek-v4-flash-2604", 1000, 2000, 0, 0)
    bare = db.get_cost("deepseek-v4-flash-2604", 1000, 2000, 0, 0)

    assert base > 0.0
    assert abs(base - snapshot) < 1e-12
    assert abs(base - bare) < 1e-12


def test_pricing_resolver_protects_version_stamped_keys():
    """Canonical YYMM version-stamped keys keep their OWN pricing.

    These look like date snapshots but are distinct priced models in the DB.
    Exact-match-first (and normalized-before-stripped) must protect them, so
    adding YYMM stripping for snapshots does not collapse them onto a base.
    """
    db = PricingDatabase()

    def rates(model):
        p = db._resolve_pricing(model)
        assert p is not None, f"{model} failed to resolve"
        return (p["input"], p["output"])

    # mistral-large-2512 is cheaper than base mistral-large; must not collapse.
    assert rates("mistral-large-2512") == (0.5, 1.5)
    assert rates("mistral-large-2407") == (2.0, 6.0)
    assert rates("mistral-large") == (2.0, 6.0)
    assert rates("Mistral-Large-2512") == (0.5, 1.5)  # case-variant still protected
    assert rates("kimi-k2-0905") == (0.6, 2.5)
    assert rates("qwen3-235b-a22b-2507") == (0.071, 0.1)


def test_total_cost_hint_frontend_markup():
    from pathlib import Path
    import tokdash

    index_html = (Path(tokdash.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="totalCostHint"' in index_html
    assert 'data-i18n-title="totalCostHint"' in index_html
    assert 'id="statTotalCostHint"' in index_html
