import discord
from discord import app_commands
from discord.ext import commands
import aiohttp
import os
import logging

log = logging.getLogger("trading")

TORVEX_API_URL = os.getenv("TORVEX_API_URL", "http://localhost:5000")
TORVEX_BOT_KEY = os.getenv("TORVEX_BOT_KEY", "")
HEADERS = {"X-Bot-Key": TORVEX_BOT_KEY, "Content-Type": "application/json"}


async def _api(method: str, path: str, **kwargs):
    url = f"{TORVEX_API_URL}{path}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.request(method, url, headers=HEADERS, **kwargs) as r:
                try:
                    data = await r.json()
                except Exception:
                    data = {}
                if r.status >= 400:
                    log.error(f"{method} {path} -> {r.status} | {data}")
                return r.status, data
    except Exception as e:
        log.error(f"{method} {path} -> connection error: {e}")
        return 0, {}


async def _ensure_linked(user: discord.User | discord.Member) -> bool:
    status, _ = await _api("POST", "/api/bot/auto-link", json={
        "discordUserId": str(user.id),
        "discordUsername": user.display_name
    })
    return status == 200


async def _get_inventory(discord_id: str) -> list[dict]:
    """Returns a list of inventory items with itemDefinitionId, name, quantity."""
    status, data = await _api("GET", f"/api/bot/game/inventory/{discord_id}")
    if status == 200 and isinstance(data, list):
        return data
    return []


def _parse_items(raw: str) -> list[tuple[str, int]]:
    """
    Parse a multi-line item list entered in a modal.
    Each line: "<item name>, <quantity>" or just "<item name>" (qty=1).
    Returns [(name, quantity), ...]
    """
    results = []
    for line in raw.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        if "," in line:
            parts = line.rsplit(",", 1)
            name = parts[0].strip()
            try:
                qty = max(1, int(parts[1].strip()))
            except ValueError:
                qty = 1
        else:
            name = line
            qty = 1
        if name:
            results.append((name, qty))
    return results


def _build_offer_embed(
    initiator: discord.Member,
    recipient: discord.Member,
    init_items: list[str],
    init_coins: int,
    recip_items: list[str],
    recip_coins: int,
) -> discord.Embed:
    embed = discord.Embed(
        title="Trade Offer",
        description=f"{initiator.mention} wants to trade with {recipient.mention}",
        color=0x5599FF
    )
    offer_parts = []
    if init_items:
        offer_parts.append("\n".join(f"• {n}" for n in init_items))
    if init_coins > 0:
        offer_parts.append(f"• {init_coins:,} coins")
    embed.add_field(
        name=f"{initiator.display_name} offers",
        value="\n".join(offer_parts) if offer_parts else "*(nothing)*",
        inline=True
    )
    want_parts = []
    if recip_items:
        want_parts.append("\n".join(f"• {n}" for n in recip_items))
    if recip_coins > 0:
        want_parts.append(f"• {recip_coins:,} coins")
    embed.add_field(
        name=f"{recipient.display_name} gives",
        value="\n".join(want_parts) if want_parts else "*(nothing)*",
        inline=True
    )
    embed.set_footer(text=f"{recipient.display_name}: do you accept?  (expires in 5 minutes)")
    return embed


class TradeOfferModal(discord.ui.Modal, title="Set Trade Offer"):
    """Modal the initiator fills out to specify what they're offering and requesting."""

    offer_items = discord.ui.TextInput(
        label="Items you are offering (name, qty per line)",
        style=discord.TextStyle.paragraph,
        placeholder="Iron Sword, 1\nHealth Potion, 3",
        required=False,
        max_length=400
    )
    offer_coins = discord.ui.TextInput(
        label="Coins you are offering",
        style=discord.TextStyle.short,
        placeholder="0",
        required=False,
        max_length=20
    )
    request_items = discord.ui.TextInput(
        label="Items you want in return (name, qty per line)",
        style=discord.TextStyle.paragraph,
        placeholder="Leave blank to offer a gift",
        required=False,
        max_length=400
    )
    request_coins = discord.ui.TextInput(
        label="Coins you want in return",
        style=discord.TextStyle.short,
        placeholder="0",
        required=False,
        max_length=20
    )

    def __init__(self, initiator: discord.Member, recipient: discord.Member):
        super().__init__()
        self.initiator = initiator
        self.recipient = recipient

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)

        init_coins = 0
        recip_coins = 0
        try:
            init_coins = max(0, int(self.offer_coins.value.strip() or "0"))
        except ValueError:
            pass
        try:
            recip_coins = max(0, int(self.request_coins.value.strip() or "0"))
        except ValueError:
            pass

        init_item_entries = _parse_items(self.offer_items.value or "")
        recip_item_entries = _parse_items(self.request_items.value or "")

        if not init_item_entries and init_coins == 0 and not recip_item_entries and recip_coins == 0:
            await interaction.followup.send(
                "You must offer or request at least something.", ephemeral=True
            )
            return

        # Resolve initiator item names -> itemDefinitionIds from their inventory
        init_inventory = await _get_inventory(str(self.initiator.id))
        inv_map = {item["name"].lower(): item for item in init_inventory}

        initiator_items = []
        for name, qty in init_item_entries:
            match = inv_map.get(name.lower())
            if match is None:
                await interaction.followup.send(
                    f"Item **{name}** not found in your inventory.", ephemeral=True
                )
                return
            if match["quantity"] < qty:
                await interaction.followup.send(
                    f"You only have {match['quantity']}x **{match['name']}** (need {qty}).",
                    ephemeral=True
                )
                return
            initiator_items.append({
                "itemDefinitionId": match["itemDefinitionId"],
                "quantity": qty
            })

        # Resolve recipient item names -> itemDefinitionIds
        recip_inventory = await _get_inventory(str(self.recipient.id))
        recip_inv_map = {item["name"].lower(): item for item in recip_inventory}

        recipient_items = []
        for name, qty in recip_item_entries:
            match = recip_inv_map.get(name.lower())
            if match is None:
                await interaction.followup.send(
                    f"**{self.recipient.display_name}** doesn't have **{name}** in their inventory.",
                    ephemeral=True
                )
                return
            if match["quantity"] < qty:
                await interaction.followup.send(
                    f"**{self.recipient.display_name}** only has {match['quantity']}x **{match['name']}** (need {qty}).",
                    ephemeral=True
                )
                return
            recipient_items.append({
                "itemDefinitionId": match["itemDefinitionId"],
                "quantity": qty
            })

        # Create the trade offer on the API
        status, data = await _api("POST", "/api/bot/game/trade/offer", json={
            "initiatorDiscordId": str(self.initiator.id),
            "recipientDiscordId": str(self.recipient.id),
            "initiatorItems": initiator_items,
            "initiatorCoins": init_coins,
            "recipientItems": recipient_items,
            "recipientCoins": recip_coins
        })

        if status != 200:
            err = data.get("error", "Could not create trade offer.")
            await interaction.followup.send(f"Could not create trade: {err}", ephemeral=True)
            return

        trade_id = str(data["tradeOfferId"])

        # Build human-readable item name lists using API-resolved names (fall back to entered names)
        init_item_names = data.get("initiatorItems") or [
            f"{n} x{q}" for n, q in init_item_entries
        ]
        recip_item_names = data.get("recipientItems") or [
            f"{n} x{q}" for n, q in recip_item_entries
        ]

        embed = _build_offer_embed(
            self.initiator, self.recipient,
            init_item_names, init_coins,
            recip_item_names, recip_coins
        )

        view = TradeResponseView(trade_id, self.initiator, self.recipient)
        await interaction.followup.send(
            content=self.recipient.mention,
            embed=embed,
            view=view
        )


class TradeResponseView(discord.ui.View):
    """Accept / Decline buttons shown to the trade recipient. Matches ChallengeView pattern."""

    def __init__(self, trade_id: str, initiator: discord.Member, recipient: discord.Member):
        super().__init__(timeout=300)  # 5 minutes, matches TradeOffer.ExpiresAt
        self.trade_id = trade_id
        self.initiator = initiator
        self.recipient = recipient

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.recipient.id:
            await interaction.response.send_message(
                "This trade offer is not for you.", ephemeral=True
            )
            return

        await interaction.response.defer()
        self.stop()

        status, data = await _api("POST", "/api/bot/game/trade/accept", json={
            "discordUserId": str(interaction.user.id),
            "offerId": self.trade_id
        })

        for child in self.children:
            child.disabled = True

        embed = interaction.message.embeds[0] if interaction.message.embeds else discord.Embed()

        if status == 200:
            embed.color = 0x00FF88
            embed.set_footer(text="Trade accepted and executed!")
            await interaction.message.edit(embed=embed, view=self)
            await interaction.followup.send(
                f"Trade between {self.initiator.mention} and {self.recipient.mention} completed!"
            )
        else:
            err = data.get("error", "Something went wrong.")
            embed.color = 0xFF4444
            embed.set_footer(text=f"Trade failed: {err}")
            await interaction.message.edit(embed=embed, view=self)
            await interaction.followup.send(f"Trade failed: {err}", ephemeral=True)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
    async def decline_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in (self.recipient.id, self.initiator.id):
            await interaction.response.send_message(
                "This trade offer is not for you.", ephemeral=True
            )
            return

        await interaction.response.defer()
        self.stop()

        await _api("POST", "/api/bot/game/trade/decline", json={
            "discordUserId": str(interaction.user.id),
            "offerId": self.trade_id
        })

        for child in self.children:
            child.disabled = True

        embed = interaction.message.embeds[0] if interaction.message.embeds else discord.Embed()
        embed.color = 0xFF4444
        decliner = interaction.user.display_name
        embed.set_footer(text=f"Declined by {decliner}.")
        await interaction.message.edit(embed=embed, view=self)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


class Trading(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="trade",
        description="Offer an item/coin trade to another player."
    )
    @app_commands.describe(recipient="The player you want to trade with")
    async def trade(self, interaction: discord.Interaction, recipient: discord.Member):
        if recipient == interaction.user:
            await interaction.response.send_message(
                "You can't trade with yourself.", ephemeral=True
            )
            return
        if recipient.bot:
            await interaction.response.send_message(
                "You can't trade with a bot.", ephemeral=True
            )
            return

        # Ensure both players are linked before opening the modal
        if not await _ensure_linked(interaction.user):
            await interaction.response.send_message(
                "Could not connect your account to Torvex. Try `/rpg start` first.",
                ephemeral=True
            )
            return
        if not await _ensure_linked(recipient):
            await interaction.response.send_message(
                f"Could not connect {recipient.display_name}'s account. "
                "They may not have an RPG character yet.",
                ephemeral=True
            )
            return

        modal = TradeOfferModal(interaction.user, recipient)
        await interaction.response.send_modal(modal)


async def setup(bot):
    await bot.add_cog(Trading(bot))
