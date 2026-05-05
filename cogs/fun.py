import discord
from discord import app_commands
from discord.ext import commands
import random
import json
import asyncio

with open("data/roasts.json") as f:
    ROASTS = json.load(f)

EIGHT_BALL_RESPONSES = [
    # Positive
    "It is certain.", "Without a doubt.", "Yes, definitely.",
    "You may rely on it.", "Most likely.", "Outlook good.", "Signs point to yes.",
    # Neutral
    "Reply hazy, try again.", "Ask again later.", "Cannot predict now.",
    "Concentrate and ask again.",
    # Negative
    "Don't count on it.", "My reply is no.", "My sources say no.",
    "Outlook not so good.", "Very doubtful."
]


# --- Tic Tac Toe ---

class TicTacToeButton(discord.ui.Button):
    def __init__(self, row, col):
        super().__init__(style=discord.ButtonStyle.secondary, label="\u200b", row=row)
        self.row_pos = row
        self.col_pos = col

    async def callback(self, interaction: discord.Interaction):
        view: TicTacToeView = self.view
        if interaction.user != view.current_player:
            await interaction.response.send_message("It's not your turn.", ephemeral=True)
            return

        self.label = view.current_symbol
        self.style = discord.ButtonStyle.success if view.current_symbol == "X" else discord.ButtonStyle.danger
        self.disabled = True
        view.board[self.row_pos][self.col_pos] = view.current_symbol

        winner = view.check_winner()
        if winner:
            view.disable_all()
            await interaction.response.edit_message(
                content=f"**{interaction.user.mention} wins!** 🎉", view=view
            )
            view.stop()
        elif view.is_full():
            view.disable_all()
            await interaction.response.edit_message(content="**It's a draw!**", view=view)
            view.stop()
        else:
            view.switch_turn()
            await interaction.response.edit_message(
                content=f"**{view.current_player.mention}'s turn** ({view.current_symbol})", view=view
            )


class TicTacToeView(discord.ui.View):
    def __init__(self, player1, player2):
        super().__init__(timeout=120)
        self.player1 = player1
        self.player2 = player2
        self.current_player = player1
        self.current_symbol = "X"
        self.board = [[None]*3 for _ in range(3)]

        for row in range(3):
            for col in range(3):
                self.add_item(TicTacToeButton(row, col))

    def switch_turn(self):
        if self.current_player == self.player1:
            self.current_player = self.player2
            self.current_symbol = "O"
        else:
            self.current_player = self.player1
            self.current_symbol = "X"

    def check_winner(self):
        b = self.board
        lines = (
            [b[r] for r in range(3)],                        # rows
            [[b[r][c] for r in range(3)] for c in range(3)], # cols
            [[b[i][i] for i in range(3)]],                   # diag
            [[b[i][2-i] for i in range(3)]],                 # anti-diag
        )
        for group in lines:
            for line in group:
                if line[0] and all(c == line[0] for c in line):
                    return line[0]
        return None

    def is_full(self):
        return all(self.board[r][c] for r in range(3) for c in range(3))

    def disable_all(self):
        for item in self.children:
            item.disabled = True


class ChallengeView(discord.ui.View):
    def __init__(self, challenger, opponent):
        super().__init__(timeout=60)
        self.challenger = challenger
        self.opponent = opponent
        self.accepted = False

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.opponent:
            await interaction.response.send_message("This challenge isn't for you.", ephemeral=True)
            return
        self.accepted = True
        self.stop()
        view = TicTacToeView(self.challenger, self.opponent)
        await interaction.response.edit_message(
            content=f"**{self.challenger.mention} (X) vs {self.opponent.mention} (O)**\n{self.challenger.mention}'s turn (X)",
            view=view
        )

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.opponent:
            await interaction.response.send_message("This challenge isn't for you.", ephemeral=True)
            return
        self.stop()
        await interaction.response.edit_message(content=f"{self.opponent.mention} declined the challenge.", view=None)


# --- Connect 4 ---

EMPTY = "⬛"
RED = "🔴"
YELLOW = "🟡"
COLS = 7
ROWS = 6
COL_EMOJIS = ["1️⃣","2️⃣","3️⃣","4️⃣","5️⃣","6️⃣","7️⃣"]

class Connect4View(discord.ui.View):
    def __init__(self, player1, player2):
        super().__init__(timeout=180)
        self.players = [player1, player2]
        self.symbols = [RED, YELLOW]
        self.turn = 0
        self.board = [[EMPTY] * COLS for _ in range(ROWS)]
        self.game_over = False

        for col in range(COLS):
            row_num = 0 if col < 4 else 1
            btn = discord.ui.Button(label=COL_EMOJIS[col], style=discord.ButtonStyle.secondary, row=row_num)
            btn.col = col
            btn.callback = self.make_callback(col)
            self.add_item(btn)

    def make_callback(self, col):
        async def callback(interaction: discord.Interaction):
            if self.game_over:
                await interaction.response.defer()
                return
            if interaction.user != self.players[self.turn]:
                await interaction.response.send_message("It's not your turn.", ephemeral=True)
                return

            # Drop piece
            row = self.drop(col)
            if row is None:
                await interaction.response.send_message("That column is full.", ephemeral=True)
                return

            symbol = self.symbols[self.turn]
            self.board[row][col] = symbol

            if self.check_winner(row, col, symbol):
                self.game_over = True
                self.disable_all()
                await interaction.response.edit_message(
                    content=f"{self.render()}\n**{interaction.user.mention} wins!** 🎉", view=self
                )
                self.stop()
            elif all(self.board[0][c] != EMPTY for c in range(COLS)):
                self.game_over = True
                self.disable_all()
                await interaction.response.edit_message(content=f"{self.render()}\n**It's a draw!**", view=self)
                self.stop()
            else:
                self.turn = 1 - self.turn
                await interaction.response.edit_message(
                    content=f"{self.render()}\n{self.players[self.turn].mention}'s turn ({self.symbols[self.turn]})",
                    view=self
                )
        return callback

    def drop(self, col):
        for row in range(ROWS - 1, -1, -1):
            if self.board[row][col] == EMPTY:
                return row
        return None

    def check_winner(self, row, col, symbol):
        def count(dr, dc):
            r, c, n = row + dr, col + dc, 0
            while 0 <= r < ROWS and 0 <= c < COLS and self.board[r][c] == symbol:
                n += 1; r += dr; c += dc
            return n

        for dr, dc in [(0,1),(1,0),(1,1),(1,-1)]:
            if count(dr, dc) + count(-dr, -dc) >= 3:
                return True
        return False

    def render(self):
        rows = ["".join(self.board[r]) for r in range(ROWS)]
        return "".join(COL_EMOJIS) + "\n" + "\n".join(rows)

    def disable_all(self):
        for item in self.children:
            item.disabled = True


class Connect4ChallengeView(discord.ui.View):
    def __init__(self, challenger, opponent):
        super().__init__(timeout=60)
        self.challenger = challenger
        self.opponent = opponent

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.opponent:
            await interaction.response.send_message("This challenge isn't for you.", ephemeral=True)
            return
        self.stop()
        view = Connect4View(self.challenger, self.opponent)
        await interaction.response.edit_message(
            content=f"{view.render()}\n{self.challenger.mention}'s turn ({RED})",
            view=view
        )

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.opponent:
            await interaction.response.send_message("This challenge isn't for you.", ephemeral=True)
            return
        self.stop()
        await interaction.response.edit_message(content=f"{self.opponent.mention} declined.", view=None)


# --- Cog ---

class Fun(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="roast", description="Roast someone. All in good fun.")
    @app_commands.checks.cooldown(1, 10, key=lambda i: (i.guild_id, i.user.id))
    async def roast(self, interaction: discord.Interaction, target: discord.Member):
        if target == interaction.user:
            await interaction.response.send_message("Roasting yourself? Respect.", ephemeral=True)
            return
        if target.bot:
            await interaction.response.send_message("Leave the bots alone.", ephemeral=True)
            return
        roast = random.choice(ROASTS).replace("{target}", target.mention)
        await interaction.response.send_message(roast)

    @app_commands.command(name="8ball", description="Ask the magic 8-ball a question.")
    @app_commands.checks.cooldown(1, 5, key=lambda i: (i.guild_id, i.user.id))
    async def eight_ball(self, interaction: discord.Interaction, question: str):
        answer = random.choice(EIGHT_BALL_RESPONSES)
        embed = discord.Embed(
            description=f"🎱 **{answer}**",
            color=discord.Color.dark_blue()
        )
        embed.set_footer(text=f'"{question}"')
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="tictactoe", description="Challenge someone to tic tac toe.")
    async def tictactoe(self, interaction: discord.Interaction, opponent: discord.Member):
        if opponent == interaction.user:
            await interaction.response.send_message("You can't challenge yourself.", ephemeral=True)
            return
        if opponent.bot:
            await interaction.response.send_message("Bots don't play games. Yet.", ephemeral=True)
            return
        view = ChallengeView(interaction.user, opponent)
        await interaction.response.send_message(
            content=f"{opponent.mention}, {interaction.user.mention} challenges you to Tic Tac Toe!",
            view=view
        )

    @app_commands.command(name="connect4", description="Challenge someone to Connect 4.")
    async def connect4(self, interaction: discord.Interaction, opponent: discord.Member):
        if opponent == interaction.user:
            await interaction.response.send_message("You can't challenge yourself.", ephemeral=True)
            return
        if opponent.bot:
            await interaction.response.send_message("Bots don't play games. Yet.", ephemeral=True)
            return
        view = Connect4ChallengeView(interaction.user, opponent)
        await interaction.response.send_message(
            content=f"{opponent.mention}, {interaction.user.mention} challenges you to Connect 4!",
            view=view
        )

    @roast.error
    @eight_ball.error
    async def cooldown_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CommandOnCooldown):
            try:
                await interaction.response.send_message(
                    f"Slow down. Try again in {error.retry_after:.1f}s.", ephemeral=True
                )
            except discord.errors.NotFound:
                pass


async def setup(bot):
    await bot.add_cog(Fun(bot))
