"""Tests for utils/ai_meter.py — the AI cog's metering math."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from utils.ai_meter import (
    PRICES, DEFAULT_PRICE, BUCKS_PRICE, DAILY_FREE_ENERGY, MICRO_PER_ENERGY,
    parse_prices_env, price_for, cost_micro, energy_for, month_key,
    budget_micro, remaining_energy,
)


def test_prices_match_anthropic_sticker():
    # $3/$15 per MTok Sonnet, $1/$5 Haiku — µ$/kTok == $/MTok * 1000
    assert PRICES["claude-sonnet-5"] == (3000, 15000)
    assert PRICES["claude-haiku-4-5"] == (1000, 5000)


def test_cost_micro_typical_chat():
    # 2k in / 400 out on Sonnet = 2000*3 + 400*15 $/MTok = 12,000 µ$ = $0.012
    assert cost_micro("claude-sonnet-5", 2000, 400) == 12_000


def test_cost_micro_haiku_quick():
    # 2k in / 300 out on Haiku = 2000*1 + 300*5 $/MTok = 3,500 µ$
    assert cost_micro("claude-haiku-4-5", 2000, 300) == 3_500


def test_cost_micro_sub_dollar_models_stay_integral():
    # DeepSeek-class pricing $0.14/$0.28 per MTok — the reason prices are µ$/kTok
    assert parse_prices_env("cheap=0.14/0.28") == {"cheap": (140, 280)}
    # 2000 in / 400 out: ceil(2000*140/1000) + ceil(400*280/1000) = 280 + 112
    import utils.ai_meter as m
    m._ENV_PRICES["cheap"] = (140, 280)
    try:
        assert cost_micro("cheap", 2000, 400) == 392
        # rounding always ceils — 1 token in costs 1 µ$, never 0
        assert cost_micro("cheap", 1, 0) == 1
    finally:
        del m._ENV_PRICES["cheap"]


def test_unknown_model_never_free():
    assert price_for("mystery-model-9000") == DEFAULT_PRICE
    assert cost_micro("mystery-model-9000", 1000, 100) > 0


def test_parse_prices_env_skips_garbage():
    table = parse_prices_env("good=1/3, bad, worse=1, nan=x/y ,also-good=0.5/1.5")
    assert table == {"good": (1000, 3000), "also-good": (500, 1500)}
    assert parse_prices_env("") == {}


def test_energy_for_rounds_up_and_charges_minimum():
    assert energy_for(1) == 1              # tiny request still costs 1 energy
    assert energy_for(MICRO_PER_ENERGY) == 1
    assert energy_for(MICRO_PER_ENERGY + 1) == 2
    assert energy_for(12_000) == 20        # typical Sonnet chat = 20 energy


def test_daily_allowance_costs_at_most_six_cents():
    # the whole free allowance fully consumed = 100 * 600 µ$ = $0.06
    assert DAILY_FREE_ENERGY * MICRO_PER_ENERGY == 60_000


def test_remaining_energy():
    assert remaining_energy(0) == DAILY_FREE_ENERGY
    assert remaining_energy(12_000) == DAILY_FREE_ENERGY - 20
    # over-consumption goes negative (blocks the next request) rather than clamping
    assert remaining_energy(DAILY_FREE_ENERGY * MICRO_PER_ENERGY + 1) < 0


def test_month_key():
    assert month_key("2026-07-18") == "2026-07"


def test_budget_micro():
    assert budget_micro(20) == 20_000_000
    assert budget_micro(0.5) == 500_000


def test_bucks_prices_are_tiered():
    # bucks prices are keyed by tier (survive model swaps) and priced so a full
    # extra day of AI costs about one day of chat earnings (200 bucks/day cap)
    assert BUCKS_PRICE["smart"] >= 2 * BUCKS_PRICE["quick"]
    smart_chat_energy = energy_for(cost_micro("claude-sonnet-5", 2000, 400))
    assert BUCKS_PRICE["smart"] >= smart_chat_energy * 2
