import discord
from discord import app_commands
from discord.ext import commands
import random
import math

# =====================
# TIC TAC TOE BOT AI
# =====================

def ttt_check_winner(board):
    lines = (
        [board[r] for r in range(3)],
        [[board[r][c] for r in range(3)] for c in range(3)],
        [[board[i][i] for i in range(3)]],
        [[board[i][2-i] for i in range(3)]],
    )
    for group in lines:
        for line in group:
            if line[0] and all(c == line[0] for c in line):
                return line[0]
    return None

def ttt_is_full(board):
    return all(board[r][c] for r in range(3) for c in range(3))

def ttt_minimax(board, is_bot_turn):
    winner = ttt_check_winner(board)
    if winner == "O": return 1
    if winner == "X": return -1
    if ttt_is_full(board): return 0
    scores = []
    for r in range(3):
        for c in range(3):
            if not board[r][c]:
                board[r][c] = "O" if is_bot_turn else "X"
                scores.append(ttt_minimax(board, not is_bot_turn))
                board[r][c] = None
    return max(scores) if is_bot_turn else min(scores)

def ttt_bot_move(board, difficulty):
    empty = [(r, c) for r in range(3) for c in range(3) if not board[r][c]]
    if not empty:
        return None
    if difficulty == "easy":
        return random.choice(empty)
    if difficulty == "medium":
        if random.random() < 0.4:
            return random.choice(empty)
    # hard / medium fallback — minimax
    best_score, best_move = -math.inf, None
    for r, c in empty:
        board[r][c] = "O"
        score = ttt_minimax(board, False)
        board[r][c] = None
        if score > best_score:
            best_score, best_move = score, (r, c)
    return best_move


# =====================
# TIC TAC TOE VIEW (BOT)
# =====================

class TicTacToeBotButton(discord.ui.Button):
    def __init__(self, row, col):
        super().__init__(style=discord.ButtonStyle.secondary, label="\u200b", row=row)
        self.row_pos = row
        self.col_pos = col

    async def callback(self, interaction: discord.Interaction):
        view: TicTacToeBotView = self.view
        if interaction.user != view.player:
            await interaction.response.send_message("This isn't your game.", ephemeral=True)
            return
        if view.board[self.row_pos][self.col_pos]:
            await interaction.response.send_message("That cell is taken.", ephemeral=True)
            return

        # Player move
        view.board[self.row_pos][self.col_pos] = "X"
        self.label = "X"
        self.style = discord.ButtonStyle.success
        self.disabled = True

        winner = ttt_check_winner(view.board)
        if winner:
            view.disable_all()
            await interaction.response.edit_message(content=f"**You win!** 🎉", view=view)
            view.stop()
            return
        if ttt_is_full(view.board):
            view.disable_all()
            await interaction.response.edit_message(content="**Draw!**", view=view)
            view.stop()
            return

        # Bot move
        move = ttt_bot_move(view.board, view.difficulty)
        if move:
            br, bc = move
            view.board[br][bc] = "O"
            for item in view.children:
                if isinstance(item, TicTacToeBotButton) and item.row_pos == br and item.col_pos == bc:
                    item.label = "O"
                    item.style = discord.ButtonStyle.danger
                    item.disabled = True

        winner = ttt_check_winner(view.board)
        if winner:
            view.disable_all()
            await interaction.response.edit_message(content="**Bot wins!** 🤖", view=view)
            view.stop()
        elif ttt_is_full(view.board):
            view.disable_all()
            await interaction.response.edit_message(content="**Draw!**", view=view)
            view.stop()
        else:
            await interaction.response.edit_message(content="Your turn (X)", view=view)


class TicTacToeBotView(discord.ui.View):
    def __init__(self, player, difficulty):
        super().__init__(timeout=120)
        self.player = player
        self.difficulty = difficulty
        self.board = [[None]*3 for _ in range(3)]
        for row in range(3):
            for col in range(3):
                self.add_item(TicTacToeBotButton(row, col))

    def disable_all(self):
        for item in self.children:
            item.disabled = True


# =====================
# CONNECT 4 BOT AI
# =====================

C4_ROWS, C4_COLS = 6, 7

def c4_drop(board, col, symbol):
    for row in range(C4_ROWS - 1, -1, -1):
        if board[row][col] is None:
            board[row][col] = symbol
            return row
    return None

def c4_check_winner(board, row, col, symbol):
    def count(dr, dc):
        r, c, n = row + dr, col + dc, 0
        while 0 <= r < C4_ROWS and 0 <= c < C4_COLS and board[r][c] == symbol:
            n += 1; r += dr; c += dc
        return n
    for dr, dc in [(0,1),(1,0),(1,1),(1,-1)]:
        if count(dr, dc) + count(-dr, -dc) >= 3:
            return True
    return False

def c4_score_window(window, symbol, opp):
    score = 0
    if window.count(symbol) == 4: score += 100
    elif window.count(symbol) == 3 and window.count(None) == 1: score += 5
    elif window.count(symbol) == 2 and window.count(None) == 2: score += 2
    if window.count(opp) == 3 and window.count(None) == 1: score -= 4
    return score

def c4_heuristic(board, symbol):
    opp = "X" if symbol == "O" else "O"
    score = 0
    center = [board[r][C4_COLS//2] for r in range(C4_ROWS)]
    score += center.count(symbol) * 3
    for r in range(C4_ROWS):
        for c in range(C4_COLS - 3):
            score += c4_score_window([board[r][c+i] for i in range(4)], symbol, opp)
    for c in range(C4_COLS):
        for r in range(C4_ROWS - 3):
            score += c4_score_window([board[r+i][c] for i in range(4)], symbol, opp)
    for r in range(C4_ROWS - 3):
        for c in range(C4_COLS - 3):
            score += c4_score_window([board[r+i][c+i] for i in range(4)], symbol, opp)
            score += c4_score_window([board[r+3-i][c+i] for i in range(4)], symbol, opp)
    return score

def c4_valid_cols(board):
    return [c for c in range(C4_COLS) if board[0][c] is None]

def c4_minimax(board, depth, alpha, beta, maximizing):
    valid = c4_valid_cols(board)
    if depth == 0 or not valid:
        return c4_heuristic(board, "O")
    if maximizing:
        value = -math.inf
        for col in valid:
            b = [row[:] for row in board]
            row = c4_drop(b, col, "O")
            if row is not None and c4_check_winner(b, row, col, "O"):
                return 100000
            value = max(value, c4_minimax(b, depth-1, alpha, beta, False))
            alpha = max(alpha, value)
            if alpha >= beta: break
        return value
    else:
        value = math.inf
        for col in valid:
            b = [row[:] for row in board]
            row = c4_drop(b, col, "X")
            if row is not None and c4_check_winner(b, row, col, "X"):
                return -100000
            value = min(value, c4_minimax(b, depth-1, alpha, beta, True))
            beta = min(beta, value)
            if alpha >= beta: break
        return value

def c4_bot_move(board, difficulty):
    valid = c4_valid_cols(board)
    if not valid:
        return None
    if difficulty == "easy":
        return random.choice(valid)
    depth = 3 if difficulty == "medium" else 6
    best_score, best_col = -math.inf, random.choice(valid)
    for col in valid:
        b = [row[:] for row in board]
        row = c4_drop(b, col, "O")
        score = c4_minimax(b, depth-1, -math.inf, math.inf, False)
        if score > best_score:
            best_score, best_col = score, col
    return best_col


# =====================
# CONNECT 4 BOT VIEW
# =====================

EMPTY = "⬛"
RED = "🔴"
YELLOW = "🟡"
COL_EMOJIS = ["1️⃣","2️⃣","3️⃣","4️⃣","5️⃣","6️⃣","7️⃣"]

class Connect4BotView(discord.ui.View):
    def __init__(self, player, difficulty):
        super().__init__(timeout=180)
        self.player = player
        self.difficulty = difficulty
        self.board = [[None]*C4_COLS for _ in range(C4_ROWS)]
        self.game_over = False
        for col in range(C4_COLS):
            row_num = 0 if col < 4 else 1
            btn = discord.ui.Button(label=COL_EMOJIS[col], style=discord.ButtonStyle.secondary, row=row_num)
            btn.callback = self.make_callback(col)
            self.add_item(btn)

    def render(self):
        grid = [[EMPTY]*C4_COLS for _ in range(C4_ROWS)]
        for r in range(C4_ROWS):
            for c in range(C4_COLS):
                if self.board[r][c] == "X": grid[r][c] = RED
                elif self.board[r][c] == "O": grid[r][c] = YELLOW
        return "".join(COL_EMOJIS) + "\n" + "\n".join("".join(row) for row in grid)

    def disable_all(self):
        for item in self.children:
            item.disabled = True

    def make_callback(self, col):
        async def callback(interaction: discord.Interaction):
            if self.game_over:
                await interaction.response.defer()
                return
            if interaction.user != self.player:
                await interaction.response.send_message("This isn't your game.", ephemeral=True)
                return
            if self.board[0][col] is not None:
                await interaction.response.send_message("That column is full.", ephemeral=True)
                return

            # Player move
            row = c4_drop(self.board, col, "X")
            if c4_check_winner(self.board, row, col, "X"):
                self.game_over = True
                self.disable_all()
                await interaction.response.edit_message(content=f"{self.render()}\n**You win!** 🎉", view=self)
                self.stop()
                return

            if not c4_valid_cols(self.board):
                self.game_over = True
                self.disable_all()
                await interaction.response.edit_message(content=f"{self.render()}\n**Draw!**", view=self)
                self.stop()
                return

            # Bot move
            bot_col = c4_bot_move(self.board, self.difficulty)
            bot_row = c4_drop(self.board, bot_col, "O")

            if c4_check_winner(self.board, bot_row, bot_col, "O"):
                self.game_over = True
                self.disable_all()
                await interaction.response.edit_message(content=f"{self.render()}\n**Bot wins!** 🤖", view=self)
                self.stop()
            elif not c4_valid_cols(self.board):
                self.game_over = True
                self.disable_all()
                await interaction.response.edit_message(content=f"{self.render()}\n**Draw!**", view=self)
                self.stop()
            else:
                await interaction.response.edit_message(content=f"{self.render()}\nYour turn {RED}", view=self)
        return callback


# =====================
# GAMES COG
# =====================

DIFFICULTY_CHOICES = [
    app_commands.Choice(name="Easy", value="easy"),
    app_commands.Choice(name="Medium", value="medium"),
    app_commands.Choice(name="Hard", value="hard"),
]

class Games(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="tictactoe_bot", description="Play Tic Tac Toe against the bot.")
    @app_commands.describe(difficulty="How hard should the bot play?")
    @app_commands.choices(difficulty=DIFFICULTY_CHOICES)
    async def tictactoe_bot(self, interaction: discord.Interaction, difficulty: app_commands.Choice[str]):
        view = TicTacToeBotView(interaction.user, difficulty.value)
        await interaction.response.send_message(
            content=f"**Tic Tac Toe vs Bot** ({difficulty.name})\nYou are X — your turn!",
            view=view
        )

    @app_commands.command(name="connect4_bot", description="Play Connect 4 against the bot.")
    @app_commands.describe(difficulty="How hard should the bot play?")
    @app_commands.choices(difficulty=DIFFICULTY_CHOICES)
    async def connect4_bot(self, interaction: discord.Interaction, difficulty: app_commands.Choice[str]):
        view = Connect4BotView(interaction.user, difficulty.value)
        board_render = "".join(COL_EMOJIS) + "\n" + "\n".join("".join([EMPTY]*C4_COLS) for _ in range(C4_ROWS))
        await interaction.response.send_message(
            content=f"**Connect 4 vs Bot** ({difficulty.name})\n{board_render}\nYou are {RED} — pick a column!",
            view=view
        )

async def setup(bot):
    await bot.add_cog(Games(bot))
