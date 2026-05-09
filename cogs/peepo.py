import discord
from discord import app_commands
from discord.ext import commands
import aiohttp
import math
import os
import logging

log = logging.getLogger("peepo")

TORVEX_API_URL = os.getenv("TORVEX_API_URL", "http://localhost:5000")
TORVEX_BOT_KEY = os.getenv("TORVEX_BOT_KEY", "")
HEADERS = {"X-Bot-Key": TORVEX_BOT_KEY, "Content-Type": "application/json"}

RARITY_COLORS = {
    "Common":    0xAAAAAA,
    "Uncommon":  0x55FF55,
    "Rare":      0x5599FF,
    "Epic":      0xAA44FF,
    "Legendary": 0xFFAA00,
    "Limited":   0xFF6600,
}

RARITY_STARS = {
    "Common": "★", "Uncommon": "★★", "Rare": "★★★",
    "Epic": "★★★★", "Legendary": "★★★★★",
}

RARITY_ORDER = ["Common", "Uncommon", "Rare", "Epic", "Legendary"]

ITEMS_PER_PAGE = 8


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
                    log.error(f"{method} {path} → {r.status} | {data}")
                return r.status, data
    except Exception as e:
        log.error(f"{method} {path} → connection error: {e}")
        return 0, {}


async def _ensure_linked(user: discord.User | discord.Member) -> bool:
    status, _ = await _api("POST", "/api/bot/auto-link", json={
        "discordUserId": str(user.id),
        "discordUsername": user.display_name
    })
    return status == 200


class PeepoPageView(discord.ui.View):
    """Prev/next paginator for peepo embeds."""

    def __init__(self, pages: list[discord.Embed]):
        super().__init__(timeout=120)
        self.pages = pages
        self.current = 0
        self._sync()

    def _sync(self):
        self.prev_btn.disabled = self.current == 0
        self.next_btn.disabled = self.current >= len(self.pages) - 1

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current = max(0, self.current - 1)
        self._sync()
        await interaction.response.edit_message(embed=self.pages[self.current], view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current = min(len(self.pages) - 1, self.current + 1)
        self._sync()
        await interaction.response.edit_message(embed=self.pages[self.current], view=self)


class TradeOfferView(discord.ui.View):
    """Accept / Decline buttons sent to the trade recipient."""

    def __init__(self, trade_id: str, initiator: discord.Member, recipient: discord.Member):
        super().__init__(timeout=300)
        self.trade_id  = trade_id
        self.initiator = initiator
        self.recipient = recipient

    @discord.ui.button(label="✅ Accept", style=discord.ButtonStyle.success)
    async def accept_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.recipient.id:
            await interaction.response.send_message("This trade isn't for you.", ephemeral=True)
            return
        self.stop()
        status, data = await _api("POST", f"/api/bot/peepos/trade/{self.trade_id}/accept",
                                  json={"discordUserId": str(interaction.user.id)})
        if status == 200:
            for child in self.children:
                child.disabled = True
            embed = interaction.message.embeds[0] if interaction.message.embeds else discord.Embed()
            embed.color = 0x00FF88
            embed.set_footer(text="✅ Trade accepted!")
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            err = data.get("error", "Something went wrong.")
            await interaction.response.send_message(f"❌ {err}", ephemeral=True)

    @discord.ui.button(label="❌ Decline", style=discord.ButtonStyle.danger)
    async def decline_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in (self.recipient.id, self.initiator.id):
            await interaction.response.send_message("This trade isn't for you.", ephemeral=True)
            return
        self.stop()
        await _api("POST", f"/api/bot/peepos/trade/{self.trade_id}/decline",
                   json={"discordUserId": str(interaction.user.id)})
        for child in self.children:
            child.disabled = True
        embed = interaction.message.embeds[0] if interaction.message.embeds else discord.Embed()
        embed.color = 0xFF4444
        embed.set_footer(text="❌ Trade declined.")
        await interaction.response.edit_message(embed=embed, view=self)


def _shop_pages(peepos: list) -> list[discord.Embed]:
    """Build shop pages grouped by rarity, showing coin and orb prices."""
    # Group by rarity
    by_rarity: dict[str, list] = {}
    for p in peepos:
        by_rarity.setdefault(p.get("rarity", "Common"), []).append(p)

    # Build one embed per rarity that has purchasable items
    pages = []
    for rarity in RARITY_ORDER:
        items = by_rarity.get(rarity, [])
        if not items:
            continue
        color  = RARITY_COLORS.get(rarity, 0xFFAA00)
        stars  = RARITY_STARS.get(rarity, "")

        # Chunk within each rarity so we don't exceed 25 fields
        for i in range(0, len(items), ITEMS_PER_PAGE):
            chunk       = items[i:i + ITEMS_PER_PAGE]
            chunk_num   = i // ITEMS_PER_PAGE + 1
            chunk_total = math.ceil(len(items) / ITEMS_PER_PAGE)
            title_extra = f"  (page {chunk_num}/{chunk_total})" if chunk_total > 1 else ""
            embed = discord.Embed(
                title=f"🛒 Peepo Shop — {stars} {rarity}{title_extra}",
                description="Buy peepo collectibles with your RPG Coins!",
                color=color
            )
            for p in chunk:
                coin_price = p.get("coinPrice", 0)
                orb_price  = p.get("orbPrice", 0)
                sell_price = p.get("sellPrice", 0)
                if coin_price > 0 and orb_price > 0:
                    price_str = f"🪙 **{coin_price:,}** coins  ·  🔮 **{orb_price:,}** orbs"
                elif coin_price > 0:
                    price_str = f"🪙 **{coin_price:,}** coins"
                else:
                    price_str = f"🔮 **{orb_price:,}** orbs only"
                embed.add_field(
                    name=f"**{p['name']}**  {stars}",
                    value=f"{price_str}\n↩ Sell: {sell_price:,} coins",
                    inline=True
                )
            embed.set_footer(text="Use /peepo buy <name> to purchase with coins")
            pages.append(embed)

    if not pages:
        pages.append(discord.Embed(
            title="🛒 Peepo Shop",
            description="No peepos available yet. Ask an admin to run `/peepo sync`!",
            color=0xFFAA00
        ))
    return pages


def _market_pages(listings: list) -> list[discord.Embed]:
    pages = []
    total = len(listings)
    if total == 0:
        embed = discord.Embed(title="🏪 Peepo Marketplace", description="No listings right now.", color=0x5599FF)
        return [embed]
    for i in range(0, total, ITEMS_PER_PAGE):
        chunk = listings[i:i + ITEMS_PER_PAGE]
        page_num    = i // ITEMS_PER_PAGE + 1
        total_pages = math.ceil(total / ITEMS_PER_PAGE)
        embed = discord.Embed(
            title=f"🏪 Peepo Marketplace  (page {page_num}/{total_pages})",
            color=0x5599FF
        )
        for l in chunk:
            rarity = l.get("rarity", "Common")
            stars  = RARITY_STARS.get(rarity, "")
            embed.add_field(
                name=f"`{str(l['id'])[:8]}…`  **{l['itemName']}**  {stars}",
                value=f"🪙 **{l['pricePerUnit']:,}** coins  ·  Seller: {l['sellerName']}",
                inline=False
            )
        embed.set_footer(text="Use /peepo market buy <id> to purchase (first 8 chars of ID)")
        pages.append(embed)
    return pages


class PeepoMarketGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="market", description="Peepo marketplace commands")

    @app_commands.command(name="browse", description="Browse peepos for sale by other players.")
    async def browse(self, interaction: discord.Interaction):
        await interaction.response.defer()
        status, data = await _api("GET", "/api/bot/peepos/market")
        if status != 200:
            await interaction.followup.send("❌ Could not load marketplace.", ephemeral=True)
            return
        pages = _market_pages(data)
        view  = PeepoPageView(pages)
        await interaction.followup.send(embed=pages[0], view=view)

    @app_commands.command(name="list", description="List one of your peepos for sale.")
    @app_commands.describe(peepo_name="Name of the peepo to list", price="Price in coins")
    async def list_cmd(self, interaction: discord.Interaction, peepo_name: str, price: int):
        await interaction.response.defer(ephemeral=True)
        if not await _ensure_linked(interaction.user):
            await interaction.followup.send("❌ Could not connect to Torvex.", ephemeral=True)
            return
        status, data = await _api("POST", "/api/bot/peepos/market/list", json={
            "discordUserId": str(interaction.user.id),
            "peepoName": peepo_name,
            "price": price
        })
        if status == 200:
            await interaction.followup.send(f"✅ **{peepo_name}** listed for **{price:,}** coins!", ephemeral=True)
        else:
            await interaction.followup.send(f"❌ {data.get('error', 'Failed to list.')}", ephemeral=True)

    @list_cmd.autocomplete("peepo_name")
    async def list_autocomplete(self, interaction: discord.Interaction, current: str):
        status, data = await _api("GET", f"/api/bot/peepos/inventory/{interaction.user.id}")
        if status != 200 or not isinstance(data, list):
            return []
        matches = [p for p in data if current.lower() in p["name"].lower()][:25]
        return [app_commands.Choice(name=f"{p['name']} [{p['rarity']}]", value=p["name"])
                for p in matches]

    @app_commands.command(name="buy", description="Buy a listing from the marketplace.")
    @app_commands.describe(listing_id="Listing ID (from /peepo market browse)")
    async def buy(self, interaction: discord.Interaction, listing_id: str):
        await interaction.response.defer(ephemeral=True)
        if not await _ensure_linked(interaction.user):
            await interaction.followup.send("❌ Could not connect to Torvex.", ephemeral=True)
            return
        # Accept full UUID or first 8 chars prefix
        status, market_data = await _api("GET", "/api/bot/peepos/market")
        if status != 200:
            await interaction.followup.send("❌ Could not load marketplace.", ephemeral=True)
            return
        match = next((l for l in market_data if str(l["id"]).startswith(listing_id.lower())), None)
        if match is None:
            await interaction.followup.send("❌ Listing not found.", ephemeral=True)
            return
        status, data = await _api("POST", "/api/bot/peepos/market/buy", json={
            "discordUserId": str(interaction.user.id),
            "listingId": match["id"]
        })
        if status == 200:
            await interaction.followup.send(
                f"✅ Bought **{match['itemName']}** for **{match['pricePerUnit']:,}** coins!\n"
                f"New balance: 🪙 **{data.get('newCoinBalance', 0):,}**",
                ephemeral=True
            )
        else:
            await interaction.followup.send(f"❌ {data.get('error', 'Purchase failed.')}", ephemeral=True)

    @app_commands.command(name="cancel", description="Cancel one of your active listings.")
    @app_commands.describe(listing_id="Listing ID to cancel (from /peepo market browse)")
    async def cancel(self, interaction: discord.Interaction, listing_id: str):
        await interaction.response.defer(ephemeral=True)
        # Resolve prefix to full ID
        status, market_data = await _api("GET", "/api/bot/peepos/market")
        match = None
        if status == 200:
            match = next((l for l in market_data if str(l["id"]).startswith(listing_id.lower())), None)
        if match is None:
            await interaction.followup.send("❌ Listing not found.", ephemeral=True)
            return
        status, data = await _api(
            "DELETE",
            f"/api/bot/peepos/market/{match['id']}?discordUserId={interaction.user.id}"
        )
        if status == 200:
            await interaction.followup.send(f"✅ Listing cancelled — **{match['itemName']}** returned to inventory.", ephemeral=True)
        else:
            await interaction.followup.send(f"❌ {data.get('error', 'Cancel failed.')}", ephemeral=True)


class Peepo(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    peepo = app_commands.Group(name="peepo", description="Peepo collectible commands")
    peepo.add_command(PeepoMarketGroup())

    # ── /peepo shop ──────────────────────────────────────────────────────────
    @peepo.command(name="shop", description="Browse the peepo shop — buy with RPG Coins, grouped by rarity.")
    async def shop(self, interaction: discord.Interaction):
        await interaction.response.defer()
        status, data = await _api("GET", "/api/bot/peepos/shop")
        if status != 200 or not data:
            await interaction.followup.send("❌ Could not load shop.", ephemeral=True)
            return
        # Only show coin-purchasable peepos in the main shop view
        buyable = [p for p in data if p.get("buyWithCoins", False)]
        if not buyable:
            await interaction.followup.send(
                "No peepos are available for coins right now. Try `/peepo crate`!",
                ephemeral=True
            )
            return
        pages = _shop_pages(buyable)
        view  = PeepoPageView(pages)
        await interaction.followup.send(embed=pages[0], view=view)

    # ── /peepo buy <name> ────────────────────────────────────────────────────
    @peepo.command(name="buy", description="Buy a peepo from the fixed-price shop with coins.")
    @app_commands.describe(name="Peepo name (start typing for suggestions)")
    async def buy(self, interaction: discord.Interaction, name: str):
        await interaction.response.defer(ephemeral=True)
        if not await _ensure_linked(interaction.user):
            await interaction.followup.send("❌ Could not connect to Torvex.", ephemeral=True)
            return

        # Resolve name → itemDefinitionId from shop catalog
        status, catalog = await _api("GET", "/api/bot/peepos/shop")
        if status != 200 or not isinstance(catalog, list):
            await interaction.followup.send("❌ Could not load shop catalog.", ephemeral=True)
            return
        match = next((p for p in catalog if p["name"].lower() == name.lower()), None)
        if match is None:
            await interaction.followup.send(f"❌ **{name}** not found in the shop.", ephemeral=True)
            return
        if not match.get("buyWithCoins", False):
            orb_price = match.get("orbPrice", 0)
            await interaction.followup.send(
                f"❌ **{name}** is Legendary and can only be purchased with orbs "
                f"(🔮 {orb_price:,}). Try `/peepo crate premium`!",
                ephemeral=True
            )
            return

        status, data = await _api("POST", "/api/bot/peepos/buy-coins", json={
            "discordId": str(interaction.user.id),
            "itemDefinitionId": str(match["id"])
        })
        if status == 200:
            peepo_data  = data.get("peepo", {})
            rarity      = peepo_data.get("rarity", "Common")
            stars       = RARITY_STARS.get(rarity, "")
            color       = RARITY_COLORS.get(rarity, 0xFFAA00)
            coin_price  = match.get("coinPrice", 0)
            new_balance = data.get("newCoinBalance", 0)
            embed = discord.Embed(
                title="✅ Peepo Purchased!",
                description=f"You bought **{peepo_data.get('name', name)}** {stars}",
                color=color
            )
            embed.add_field(name="Rarity",    value=f"{stars} {rarity}",         inline=True)
            embed.add_field(name="Paid",       value=f"🪙 {coin_price:,} coins",  inline=True)
            embed.add_field(name="Balance",    value=f"🪙 {new_balance:,} coins", inline=True)
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.followup.send(f"❌ {data.get('error', 'Purchase failed.')}", ephemeral=True)

    @buy.autocomplete("name")
    async def buy_autocomplete(self, interaction: discord.Interaction, current: str):
        status, data = await _api("GET", "/api/bot/peepos/shop")
        if status != 200 or not isinstance(data, list):
            return []
        buyable = [p for p in data if p.get("buyWithCoins", False)]
        matches = [p for p in buyable if current.lower() in p["name"].lower()][:25]
        return [app_commands.Choice(
            name=f"{p['name']} [{p['rarity']}] — {p['coinPrice']:,} coins",
            value=p["name"]
        ) for p in matches]

    # ── /peepo collection [@user] ────────────────────────────────────────────
    @peepo.command(name="collection", description="View your peepo collection (or another player's).")
    @app_commands.describe(user="The user to check (leave blank for yourself)")
    async def collection(self, interaction: discord.Interaction, user: discord.Member = None):
        await interaction.response.defer()
        target = user or interaction.user
        if not await _ensure_linked(target):
            await interaction.followup.send("❌ That user isn't linked to Torvex.", ephemeral=True)
            return
        status, data = await _api("GET", f"/api/bot/peepos/inventory/{target.id}")
        if status == 404:
            await interaction.followup.send(f"{target.display_name} has no character yet.", ephemeral=True)
            return
        if status != 200:
            await interaction.followup.send("❌ Could not load collection.", ephemeral=True)
            return
        if not data:
            await interaction.followup.send(f"{target.display_name} has no peepos yet. Use `/peepo shop`!", ephemeral=True)
            return

        # Group by rarity for display
        by_rarity: dict[str, list] = {}
        for p in data:
            by_rarity.setdefault(p["rarity"], []).append(p)

        embed = discord.Embed(
            title=f"🎴 {target.display_name}'s Peepo Collection",
            color=0xFFAA00
        )
        rarity_order = ["Legendary", "Epic", "Rare", "Uncommon", "Common"]
        for r in rarity_order:
            items = by_rarity.get(r, [])
            if not items:
                continue
            stars = RARITY_STARS.get(r, "")
            lines = [f"**{p['name']}** ×{p['quantity']}" for p in items]
            embed.add_field(name=f"{stars} {r}", value="\n".join(lines), inline=False)
        embed.set_footer(text=f"{len(data)} unique peepo(s) collected")
        await interaction.followup.send(embed=embed)

    # ── /peepo trade @user ───────────────────────────────────────────────────
    @peepo.command(name="trade", description="Offer a peepo trade to another player.")
    @app_commands.describe(
        recipient="The player to trade with",
        peepo_name="Peepo you're offering (leave blank for coins-only)",
        coins="Coins you're offering (default 0)"
    )
    async def trade(self, interaction: discord.Interaction,
                    recipient: discord.Member,
                    peepo_name: str = "",
                    coins: int = 0):
        if recipient == interaction.user:
            await interaction.response.send_message("You can't trade with yourself.", ephemeral=True)
            return
        if recipient.bot:
            await interaction.response.send_message("You can't trade with a bot.", ephemeral=True)
            return
        if not peepo_name and coins <= 0:
            await interaction.response.send_message("Offer at least a peepo or some coins.", ephemeral=True)
            return

        await interaction.response.defer()
        if not await _ensure_linked(interaction.user) or not await _ensure_linked(recipient):
            await interaction.followup.send("❌ Both players must be linked to Torvex.", ephemeral=True)
            return

        status, data = await _api("POST", "/api/bot/peepos/trade/offer", json={
            "initiatorDiscordId": str(interaction.user.id),
            "recipientDiscordId": str(recipient.id),
            "initiatorPeepoName": peepo_name,
            "initiatorCoins": coins
        })
        if status != 200:
            await interaction.followup.send(f"❌ {data.get('error', 'Could not create trade.')}", ephemeral=True)
            return

        trade_id = str(data["tradeOfferId"])
        offer_parts = []
        if peepo_name:
            offer_parts.append(f"peepo **{peepo_name}**")
        if coins > 0:
            offer_parts.append(f"🪙 **{coins:,}** coins")
        offer_str = " + ".join(offer_parts) or "nothing"

        embed = discord.Embed(
            title="🤝 Peepo Trade Offer",
            description=(
                f"{interaction.user.mention} is offering {offer_str} to {recipient.mention}.\n\n"
                f"{recipient.mention}, do you accept?"
            ),
            color=0x5599FF
        )
        embed.set_footer(text="Offer expires in 5 minutes")

        view = TradeOfferView(trade_id, interaction.user, recipient)
        await interaction.followup.send(embed=embed, view=view)

    @trade.autocomplete("peepo_name")
    async def trade_peepo_autocomplete(self, interaction: discord.Interaction, current: str):
        status, data = await _api("GET", f"/api/bot/peepos/inventory/{interaction.user.id}")
        if status != 200 or not isinstance(data, list):
            return []
        matches = [p for p in data if current.lower() in p["name"].lower()][:25]
        return [app_commands.Choice(name=f"{p['name']} [{p['rarity']}]", value=p["name"])
                for p in matches]

    # ── /peepo crate ─────────────────────────────────────────────────────────
    @peepo.command(name="crate", description="Open a Peepo Crate for 5,000 coins — chance at any rarity including Legendary!")
    async def crate(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if not await _ensure_linked(interaction.user):
            await interaction.followup.send("❌ Could not connect to Torvex.", ephemeral=True)
            return

        status, data = await _api("POST", "/api/bot/peepos/crate", json={
            "discordId": str(interaction.user.id)
        })
        if status != 200:
            await interaction.followup.send(f"❌ {data.get('error', 'Failed to open crate.')}", ephemeral=True)
            return

        peepo_data    = data.get("peepo", {})
        rarity        = peepo_data.get("rarity", "Common")
        peepo_name    = peepo_data.get("name", "Unknown Peepo")
        already_owned = data.get("alreadyOwned", False)
        refund        = data.get("refundAmount", 0)
        new_coins     = data.get("newCoinBalance", 0)
        stars         = RARITY_STARS.get(rarity, "")
        color         = RARITY_COLORS.get(rarity, 0xFFAA00)

        if already_owned:
            desc = (
                f"You pulled: **{peepo_name}** {stars}\n"
                f"*You already own this peepo!*\n"
                f"💰 Refunded **{refund:,}** coins (sell value)."
            )
        else:
            desc = f"You pulled: **{peepo_name}** {stars}\n✨ *New addition to your collection!*"

        embed = discord.Embed(title="📦 Peepo Crate Opened!", description=desc, color=color)
        embed.add_field(name="Rarity",  value=f"{stars} {rarity}",  inline=True)
        embed.add_field(name="Cost",    value="🪙 5,000 coins",      inline=True)
        embed.add_field(name="Balance", value=f"🪙 {new_coins:,}",   inline=True)
        embed.set_footer(text="Odds: Common 62% · Uncommon 25% · Rare 9% · Epic 3.5% · Legendary 0.5%")
        await interaction.followup.send(embed=embed)

    # ── /peepo add (admin-only) ───────────────────────────────────────────────
    @peepo.command(name="add", description="[Admin] Add a peepo by name and image URL.")
    @app_commands.describe(name="Peepo name (no spaces)", url="Direct image URL", rarity="Rarity tier (default: Common)")
    @app_commands.choices(rarity=[
        app_commands.Choice(name="Common",    value="Common"),
        app_commands.Choice(name="Uncommon",  value="Uncommon"),
        app_commands.Choice(name="Rare",      value="Rare"),
        app_commands.Choice(name="Epic",      value="Epic"),
        app_commands.Choice(name="Legendary", value="Legendary"),
    ])
    @app_commands.checks.has_permissions(administrator=True)
    async def add_peepo(self, interaction: discord.Interaction, name: str, url: str, rarity: str = "Common"):
        await interaction.response.defer(ephemeral=True)
        status, data = await _api("POST", "/api/bot/peepos/add", json={"name": name, "url": url, "rarity": rarity})
        if status == 200:
            created      = data.get("created", False)
            final_rarity = data.get("rarity", rarity)
            stars        = RARITY_STARS.get(final_rarity, "")
            action       = "added" if created else "updated"
            await interaction.followup.send(f"✅ **{name}** {action} — {stars} {final_rarity}", ephemeral=True)
        else:
            await interaction.followup.send(f"❌ {data.get('error', 'Failed.')}", ephemeral=True)

    @add_peepo.error
    async def add_peepo_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("❌ Admin only.", ephemeral=True)

    # ── /peepo sync (admin-only) ─────────────────────────────────────────────
    @peepo.command(name="sync", description="[Admin] Sync server emojis to the peepo catalog.")
    @app_commands.checks.has_permissions(administrator=True)
    async def sync_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        emoji_payload = [{"name": e.name, "url": str(e.url)} for e in guild.emojis] if guild else []
        status, data = await _api("POST", "/api/bot/peepos/sync", json=emoji_payload)
        if status == 200:
            await interaction.followup.send(
                f"✅ Sync complete — created **{data.get('created', 0)}**, "
                f"updated **{data.get('updated', 0)}**, "
                f"total **{data.get('total', 0)}** peepos.",
                ephemeral=True
            )
        else:
            await interaction.followup.send("❌ Sync failed.", ephemeral=True)

    @sync_cmd.error
    async def sync_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("❌ Admin only.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Peepo(bot))
