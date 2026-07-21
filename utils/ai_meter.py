"""Pure metering logic for the AI cog — costs, energy, budgets.

All money is integer micro-dollars (1 microdollar = $0.000001) so accounting
never drifts on floats. Prices are stored as integer microdollars per 1,000
tokens — $/MTok sticker price × 1000 — which stays integral even for cheap
models like DeepSeek ($0.14/MTok = 140 µ$/kTok).

Models not in the builtin table get priced from the AI_PRICES env var
("model=in/out,model2=in/out" in $/MTok, e.g. "deepseek-chat=0.14/0.28"),
falling back to DEFAULT_PRICE so an unlisted model can never bill as free.

Energy is the user-facing unit: 1 energy = 600 microdollars ($0.0006).
100 free energy/day means a maxed-out user costs at most ~$0.06/day.
"""
import os
import math

# model -> (input µ$/kTok, output µ$/kTok) == $/MTok * 1000
PRICES = {
    "claude-sonnet-5": (3000, 15000),
    "claude-haiku-4-5": (1000, 5000),
}

# unknown-model fallback ($1/$3 per MTok) — deliberately non-zero
DEFAULT_PRICE = (1000, 3000)

MICRO_PER_ENERGY = 600
DAILY_FREE_ENERGY = 100

# Flat bucks prices for overflow requests (charged up front, refunded on API
# failure), keyed by tier so they survive model swaps. ~2 bucks per expected
# energy at Claude prices: a full extra day of AI costs about one full day of
# chat earnings (200 bucks/day cap). Cheaper backends just make overflow a
# better deal for the server.
BUCKS_PRICE = {
    "smart": int(os.getenv("AI_BUCKS_SMART", "40")),
    "quick": int(os.getenv("AI_BUCKS_QUICK", "10")),
}


def parse_prices_env(raw: str) -> dict:
    """'model=0.14/0.28,model2=1/3' ($/MTok) -> {model: (µ$/kTok in, out)}.

    Malformed entries are skipped — a typo shouldn't take the whole cog down.
    """
    table = {}
    for entry in (raw or "").split(","):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        model, _, prices = entry.partition("=")
        parts = prices.split("/")
        if len(parts) != 2:
            continue
        try:
            pin, pout = (int(round(float(p) * 1000)) for p in parts)
        except ValueError:
            continue
        table[model.strip()] = (pin, pout)
    return table


_ENV_PRICES = parse_prices_env(os.getenv("AI_PRICES", ""))


def price_for(model: str) -> tuple:
    """(input µ$/kTok, output µ$/kTok) for a model: env > builtin > fallback."""
    return _ENV_PRICES.get(model) or PRICES.get(model) or DEFAULT_PRICE


def cost_micro(model: str, tokens_in: int, tokens_out: int) -> int:
    """Actual request cost in microdollars from the API's usage numbers."""
    price_in, price_out = price_for(model)
    return (math.ceil(tokens_in * price_in / 1000)
            + math.ceil(tokens_out * price_out / 1000))


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
