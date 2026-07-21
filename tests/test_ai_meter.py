"""Tests for utils/ai_meter.py — the AI cog's metering math."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from utils.ai_meter import (
    PRICES, BUCKS_PRICE, DAILY_FREE_ENERGY, MICRO_PER_ENERGY,
    cost_micro, energy_for, month_key, budget_micro, remaining_energy,
)


def test_prices_match_anthropic_sticker():
    # $3/$15 per MTok Sonnet, $1/$5 Haiku — microdollars/token == $/MTok
    assert PRICES["claude-sonnet-5"] == (3, 15)
    assert PRICES["claude-haiku-4-5"] == (1, 5)


def test_cost_micro_typical_chat():
    # 2k in / 400 out on Sonnet = 2000*3 + 400*15 = 12,000 µ$ = $0.012
    assert cost_micro("claude-sonnet-5", 2000, 400) == 12_000


def test_cost_micro_haiku_quick():
    # 2k in / 300 out on Haiku = 2000*1 + 300*5 = 3,500 µ$
    assert cost_micro("claude-haiku-4-5", 2000, 300) == 3_500


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


def test_bucks_prices_cover_expected_cost():
    # flat bucks price should be >= ~2 bucks per expected energy so overflow
    # can't undercut the free tier's real cost
    sonnet_chat_energy = energy_for(cost_micro("claude-sonnet-5", 2000, 400))
    assert BUCKS_PRICE["claude-sonnet-5"] >= sonnet_chat_energy * 2
    haiku_energy = energy_for(cost_micro("claude-haiku-4-5", 2000, 300))
    assert BUCKS_PRICE["claude-haiku-4-5"] >= haiku_energy
