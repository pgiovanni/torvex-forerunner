"""Pure metering logic for the AI cog — costs, energy, budgets.

All money is integer micro-dollars (1 microdollar = $0.000001) so accounting
never drifts on floats. Anthropic's $/MTok sticker price equals microdollars
per token exactly, so the price table below is just the pricing page numbers.

Energy is the user-facing unit: 1 energy = 600 microdollars ($0.0006).
100 free energy/day means a maxed-out user costs at most ~$0.06/day.
"""
import math

# model -> (input microdollars/token, output microdollars/token)
PRICES = {
    "claude-sonnet-5": (3, 15),
    "claude-haiku-4-5": (1, 5),
}

MICRO_PER_ENERGY = 600
DAILY_FREE_ENERGY = 100

# Flat bucks prices for overflow requests (charged up front, refunded on API
# failure). ~2 bucks per expected energy: a full extra day of AI costs about
# one full day of chat earnings (200 bucks/day cap).
BUCKS_PRICE = {
    "claude-sonnet-5": 40,
    "claude-haiku-4-5": 10,
}


def cost_micro(model: str, tokens_in: int, tokens_out: int) -> int:
    """Actual request cost in microdollars from the API's usage numbers."""
    price_in, price_out = PRICES[model]
    return tokens_in * price_in + tokens_out * price_out


def energy_for(micro: int) -> int:
    """Convert a microdollar cost to energy, always charging at least 1."""
    return max(1, math.ceil(micro / MICRO_PER_ENERGY))


def month_key(day: str) -> str:
    """'2026-07-18' -> '2026-07' (monthly budget bucket)."""
    return day[:7]


def budget_micro(budget_usd: float) -> int:
    return int(budget_usd * 1_000_000)


def remaining_energy(spent_micro_today: int) -> int:
    """Free energy a user has left today given their microdollar spend."""
    used = math.ceil(spent_micro_today / MICRO_PER_ENERGY)
    return DAILY_FREE_ENERGY - used
