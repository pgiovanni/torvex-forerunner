import asyncio
import discord
from discord import app_commands
from discord.ext import commands
import aiohttp
import os
import random

TORVEX_API_URL = os.getenv("TORVEX_API_URL", "http://localhost:5000")
TORVEX_BOT_KEY = os.getenv("TORVEX_BOT_KEY", "")
HEADERS = {"X-Bot-Key": TORVEX_BOT_KEY, "Content-Type": "application/json"}

# Active challenges keyed by (challenger_id, opponent_id)
pending_challenges = {}
# Active battles keyed by message_id
active_battles = {}


async def _get_player(discord_id: str) -> dict | None:
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{TORVEX_API_URL}/api/bot/player/{discord_id}", headers=HEADERS
        ) as r:
            if r.status == 200:
                return await r.json()
    return None


async def _auto_link(user: discord.Member) -> bool:
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{TORVEX_API_URL}/api/bot/auto-link",
            json={"discordUserId": str(user.id), "discordUsername": user.display_name},
            headers=HEADERS
        ) as r:
            return r.status == 200


def _stats_from_discord_level(level: int) -> dict:
    return {
        "level": level,
        "maxHp": 80 + level * 15,
        "str":   5  + level * 2,
        "def":   3  + level,
        "int":   4  + level * 2,
    }


async def _get_discord_stats(bot: commands.Bot, discord_id: str) -> dict:
    economy = bot.cogs.get("Economy")
    level = 1
    if economy and economy.pool:
        row = await economy.pool.fetchrow(
            "SELECT level FROM discord_users WHERE discord_id = $1", discord_id
        )
        if row:
            level = row["level"]
    return _stats_from_discord_level(level)


async def _award_pvp(winner_id: str, loser_id: str) -> dict:
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{TORVEX_API_URL}/api/bot/pvp/reward",
            json={"winnerDiscordId": winner_id, "loserDiscordId": loser_id},
            headers=HEADERS
        ) as r:
            if r.status == 200:
                return await r.json()
    return {}


def _calc_damage(attacker: dict, defender: dict, action: str) -> tuple[int, int]:
    """Returns (damage_dealt, defender_new_hp)"""
    if action == "attack":
        base = attacker["str"] + random.randint(1, 10)
        dmg = max(1, base - defender["def"] // 2)
    elif action == "magic":
        base = attacker["int"] + random.randint(5, 15)
        dmg = max(1, base - defender["def"] // 4)
    else:
        dmg = 0
    new_hp = max(0, defender["currentHp"] - dmg)
    return dmg, new_hp


class PvPBattleView(discord.ui.View):
    def __init__(self, challenger: discord.Member, opponent: discord.Member,
                 ch_stats: dict, op_stats: dict):
        super().__init__(timeout=120)
        self.challenger = challenger
        self.opponent = opponent
        self.ch_stats = dict(ch_stats)
        self.op_stats = dict(op_stats)
        self.turn = challenger  # challenger goes first
        self.log = []
        self.over = False

    def current_enemy(self) -> dict:
        return self.op_stats if self.turn == self.challenger else self.ch_stats

    def current_attacker(self) -> dict:
        return self.ch_stats if self.turn == self.challenger else self.op_stats

    def next_turn(self):
        self.turn = self.opponent if self.turn == self.challenger else self.challenger

    def board_embed(self, title="⚔️ PvP Battle") -> discord.Embed:
        embed = discord.Embed(title=title, color=0xFF6600)
        embed.add_field(
            name=f"{self.challenger.display_name} (Lv.{self.ch_stats['level']})",
            value=f"❤️ {self.ch_stats['currentHp']}/{self.ch_stats['maxHp']}",
            inline=True
        )
        embed.add_field(
            name=f"{self.opponent.display_name} (Lv.{self.op_stats['level']})",
            value=f"❤️ {self.op_stats['currentHp']}/{self.op_stats['maxHp']}",
            inline=True
        )
        if self.log:
            embed.add_field(name="📜 Last turns", value="\n".join(self.log[-4:]), inline=False)
        if not self.over:
            embed.set_footer(text=f"👉 {self.turn.display_name}'s turn")
        return embed

    async def _handle_action(self, interaction: discord.Interaction, action: str):
        if self.over:
            await interaction.response.defer()
            return
        if interaction.user != self.turn:
            await interaction.response.send_message("It's not your turn!", ephemeral=True)
            return

        attacker = self.current_attacker()
        defender = self.current_enemy()
        attacker_member = self.turn
        defender_member = self.opponent if self.turn == self.challenger else self.challenger

        if action == "flee":
            self.log.append(f"💨 {attacker_member.display_name} fled!")
            self.over = True
            for item in self.children:
                item.disabled = True
            winner = defender_member
            loser = attacker_member
            embed = self.board_embed(f"🏃 {loser.display_name} fled!")
            await interaction.response.edit_message(embed=embed, view=self)
            reward = await _award_pvp(str(winner.id), str(loser.id))
            xp = reward.get("winnerXpGained", 0)
            await interaction.followup.send(
                f"🏆 {winner.mention} wins! +**{xp} XP**\n"
                f"😅 {loser.mention} gets some consolation XP too."
            )
            return

        dmg, new_hp = _calc_damage(attacker, defender, action)
        emoji = {"attack": "⚔️", "magic": "🔮", "defend": "🛡️"}.get(action, "")

        if action == "defend":
            self.log.append(f"{emoji} {attacker_member.display_name} is defending!")
            attacker["defending"] = True
        else:
            defender["currentHp"] = new_hp
            self.log.append(f"{emoji} {attacker_member.display_name} hits for **{dmg}** dmg!")

        # Check win
        if defender["currentHp"] <= 0:
            self.over = True
            for item in self.children:
                item.disabled = True
            winner = attacker_member
            loser = defender_member
            embed = self.board_embed(f"💀 {loser.display_name} was defeated!")
            await interaction.response.edit_message(embed=embed, view=self)
            reward = await _award_pvp(str(winner.id), str(loser.id))
            xp = reward.get("winnerXpGained", 0)
            loser_xp = reward.get("loserXpGained", 0)
            await interaction.followup.send(
                f"🏆 {winner.mention} wins! +**{xp} XP**\n"
                f"💪 {loser.mention} gains **{loser_xp} XP** for fighting."
            )
            return

        self.next_turn()
        await interaction.response.edit_message(embed=self.board_embed(), view=self)

    @discord.ui.button(label="⚔️ Attack", style=discord.ButtonStyle.danger)
    async def attack(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_action(interaction, "attack")

    @discord.ui.button(label="🛡️ Defend", style=discord.ButtonStyle.secondary)
    async def defend(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_action(interaction, "defend")

    @discord.ui.button(label="🔮 Magic", style=discord.ButtonStyle.primary)
    async def magic(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_action(interaction, "magic")

    @discord.ui.button(label="💨 Flee", style=discord.ButtonStyle.secondary)
    async def flee(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_action(interaction, "flee")

    async def on_timeout(self):
        self.over = True
        for item in self.children:
            item.disabled = True


class ChallengeView(discord.ui.View):
    def __init__(self, challenger: discord.Member, opponent: discord.Member, bot: commands.Bot):
        super().__init__(timeout=60)
        self.challenger = challenger
        self.opponent = opponent
        self.bot = bot

    @discord.ui.button(label="✅ Accept", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.opponent:
            await interaction.response.send_message("This challenge isn't for you.", ephemeral=True)
            return

        await interaction.response.defer()
        for item in self.children:
            item.disabled = True
        await interaction.message.edit(view=self)

        ch_stats, op_stats = await asyncio.gather(
            _get_discord_stats(self.bot, str(self.challenger.id)),
            _get_discord_stats(self.bot, str(self.opponent.id)),
        )

        # Set current HP for this battle
        ch_stats["currentHp"] = ch_stats["maxHp"]
        op_stats["currentHp"] = op_stats["maxHp"]

        view = PvPBattleView(self.challenger, self.opponent, ch_stats, op_stats)
        embed = view.board_embed("⚔️ PvP Battle — Fight!")
        await interaction.followup.send(embed=embed, view=view)
        self.stop()

    @discord.ui.button(label="❌ Decline", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user not in (self.opponent, self.challenger):
            await interaction.response.send_message("Not your challenge.", ephemeral=True)
            return
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content=f"❌ {self.opponent.display_name} declined the challenge.", view=self
        )
        self.stop()


class PvP(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="challenge", description="Challenge another player to a PvP battle.")
    @app_commands.describe(opponent="The player you want to fight")
    async def challenge(self, interaction: discord.Interaction, opponent: discord.Member):
        if opponent == interaction.user:
            await interaction.response.send_message("You can't challenge yourself.", ephemeral=True)
            return
        if opponent.bot:
            await interaction.response.send_message("You can't challenge a bot.", ephemeral=True)
            return

        view = ChallengeView(interaction.user, opponent, self.bot)
        embed = discord.Embed(
            title="⚔️ PvP Challenge!",
            description=(
                f"{interaction.user.mention} challenges {opponent.mention} to a battle!\n\n"
                f"**No Peepo Bucks at stake** — winner earns XP based on opponent's level.\n"
                f"{opponent.mention}, do you accept?"
            ),
            color=0xFF6600
        )
        await interaction.response.send_message(embed=embed, view=view)


async def setup(bot):
    await bot.add_cog(PvP(bot))
