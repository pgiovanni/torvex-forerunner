import discord
from discord import app_commands
from discord.ext import commands
from dataclasses import dataclass
from typing import Optional
import random
import sys
import os
import chess
import chess.engine

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from utils.chess_renderer import render_board

STOCKFISH_PATH = "/usr/games/stockfish"

PIECE_SYM = {
    (chess.KING,   chess.WHITE): "♔", (chess.QUEEN,  chess.WHITE): "♕",
    (chess.ROOK,   chess.WHITE): "♖", (chess.BISHOP, chess.WHITE): "♗",
    (chess.KNIGHT, chess.WHITE): "♘", (chess.PAWN,   chess.WHITE): "♙",
    (chess.KING,   chess.BLACK): "♚", (chess.QUEEN,  chess.BLACK): "♛",
    (chess.ROOK,   chess.BLACK): "♜", (chess.BISHOP, chess.BLACK): "♝",
    (chess.KNIGHT, chess.BLACK): "♞", (chess.PAWN,   chess.BLACK): "♟",
}

PIECE_NAME = {
    chess.PAWN: "Pawn", chess.KNIGHT: "Knight", chess.BISHOP: "Bishop",
    chess.ROOK: "Rook", chess.QUEEN: "Queen",   chess.KING: "King",
}

# One game per channel
active_games: dict[int, "ChessGame"] = {}


# ─────────────────────────── Game state ──────────────────────────────────────

@dataclass
class ChessGame:
    board: chess.Board
    white: discord.Member
    black: Optional[discord.Member]   # None = vs bot
    difficulty: str = "medium"
    last_move: Optional[chess.Move] = None
    message: Optional[discord.Message] = None

    @property
    def is_vs_bot(self) -> bool:
        return self.black is None

    @property
    def current_player(self) -> Optional[discord.Member]:
        return self.white if self.board.turn == chess.WHITE else self.black

    @property
    def perspective(self) -> chess.Color:
        return self.board.turn


# ─────────────────────────── Bot AI ──────────────────────────────────────────

async def _get_bot_move(board: chess.Board, difficulty: str) -> chess.Move:
    if difficulty == "easy":
        return random.choice(list(board.legal_moves))
    depth = 5 if difficulty == "medium" else 15
    try:
        _, engine = await chess.engine.popen_uci(STOCKFISH_PATH)
        result = await engine.play(board, chess.engine.Limit(depth=depth))
        await engine.quit()
        return result.move
    except Exception:
        return random.choice(list(board.legal_moves))


# ─────────────────────────── Helpers ─────────────────────────────────────────

def _game_over_text(game: ChessGame) -> Optional[str]:
    b = game.board
    if b.is_checkmate():
        winner_is_white = b.turn == chess.BLACK  # the side that just moved won
        if game.is_vs_bot:
            return "Checkmate! **You win!** 🎉" if winner_is_white else "Checkmate! **Bot wins.** 🤖"
        winner = game.white if winner_is_white else game.black
        return f"Checkmate! **{winner.display_name} wins!** 🎉"
    if b.is_stalemate():
        return "Stalemate — it's a draw."
    if b.is_insufficient_material():
        return "Draw — insufficient material."
    if b.is_seventyfive_moves():
        return "Draw — 75-move rule."
    if b.is_fivefold_repetition():
        return "Draw — fivefold repetition."
    return None


def _turn_line(game: ChessGame) -> str:
    player = game.current_player
    color  = "White" if game.board.turn == chess.WHITE else "Black"
    who    = player.mention if player else "Bot"
    line   = f"**{who}'s turn** ({color})"
    if game.board.is_check():
        line += " — **Check!** ⚠️"
    return line


def _board_file(game: ChessGame, *,
                selected=None, move_targets=None) -> discord.File:
    buf = render_board(
        game.board,
        selected=selected,
        move_targets=move_targets,
        last_move=game.last_move,
        perspective=game.perspective,
    )
    return discord.File(buf, filename="chess.png")


# ─────────────────────────── Resign button ───────────────────────────────────

class ResignButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Resign", style=discord.ButtonStyle.danger, row=1)

    async def callback(self, interaction: discord.Interaction):
        game = active_games.get(interaction.channel_id)
        if game is None:
            await interaction.response.send_message("No active game here.", ephemeral=True)
            return
        if interaction.user not in (game.white, game.black):
            await interaction.response.send_message("You're not in this game.", ephemeral=True)
            return

        del active_games[interaction.channel_id]
        self.view.stop()

        if game.is_vs_bot:
            result = f"{interaction.user.mention} resigned. Bot wins. 🤖"
        else:
            winner = game.black if interaction.user == game.white else game.white
            result = f"{interaction.user.mention} resigned. **{winner.display_name} wins!** 🎉"

        await interaction.response.edit_message(
            content=result,
            attachments=[_board_file(game)],
            view=None,
        )


# ─────────────────────────── Piece selection view ────────────────────────────

class PieceSelectMenu(discord.ui.Select):
    def __init__(self, game: ChessGame):
        self.game = game
        movable = {m.from_square for m in game.board.legal_moves}
        options = []
        for sq in chess.SQUARES:
            if sq not in movable:
                continue
            piece = game.board.piece_at(sq)
            if piece is None or piece.color != game.board.turn:
                continue
            sym  = PIECE_SYM[(piece.piece_type, piece.color)]
            name = PIECE_NAME[piece.piece_type]
            options.append(discord.SelectOption(
                label=f"{sym} {name} – {chess.square_name(sq)}",
                value=chess.square_name(sq),
            ))
        super().__init__(placeholder="Select a piece to move…", options=options[:25], row=0)

    async def callback(self, interaction: discord.Interaction):
        game = active_games.get(interaction.channel_id)
        if game is None:
            await interaction.response.send_message("Game ended.", ephemeral=True)
            return
        if interaction.user != game.current_player:
            await interaction.response.send_message("It's not your turn.", ephemeral=True)
            return

        from_sq  = chess.parse_square(self.values[0])
        targets  = {m.to_square for m in game.board.legal_moves if m.from_square == from_sq}

        self.view.stop()
        new_view = ChessMoveView(game, from_sq, targets, interaction.channel_id)
        await interaction.response.edit_message(
            content=_turn_line(game) + f"\nSelected **{self.values[0]}** — pick a destination.",
            attachments=[_board_file(game, selected=from_sq, move_targets=targets)],
            view=new_view,
        )


class ChessBoardView(discord.ui.View):
    def __init__(self, game: ChessGame, channel_id: int):
        super().__init__(timeout=600)
        self.game       = game
        self.channel_id = channel_id
        self.add_item(PieceSelectMenu(game))
        self.add_item(ResignButton())

    async def on_timeout(self):
        if active_games.get(self.channel_id) is not self.game:
            return
        del active_games[self.channel_id]
        if self.game.message:
            try:
                await self.game.message.edit(content="Game timed out.", view=None)
            except Exception:
                pass


# ─────────────────────────── Destination selection view ──────────────────────

class DestSelectMenu(discord.ui.Select):
    def __init__(self, game: ChessGame, from_sq: chess.Square,
                 targets: set, channel_id: int):
        self.game       = game
        self.from_sq    = from_sq
        self.channel_id = channel_id
        ep_sq           = game.board.ep_square
        is_pawn         = (
            game.board.piece_at(from_sq) is not None
            and game.board.piece_at(from_sq).piece_type == chess.PAWN
        )

        options = []
        for sq in sorted(targets, key=chess.square_name):
            sq_name  = chess.square_name(sq)
            captured = game.board.piece_at(sq)
            is_ep    = is_pawn and ep_sq == sq

            if is_ep:
                opp_pawn = PIECE_SYM[(chess.PAWN, not game.board.turn)]
                label = f"→ {sq_name}  ✕ {opp_pawn} (en passant)"
            elif captured:
                sym   = PIECE_SYM[(captured.piece_type, captured.color)]
                label = f"→ {sq_name}  ✕ {sym}"
            else:
                label = f"→ {sq_name}"

            options.append(discord.SelectOption(label=label, value=sq_name))

        super().__init__(placeholder="Select destination…", options=options[:25], row=0)

    async def callback(self, interaction: discord.Interaction):
        game = active_games.get(self.channel_id)
        if game is None:
            await interaction.response.send_message("Game ended.", ephemeral=True)
            return
        if interaction.user != game.current_player:
            await interaction.response.send_message("It's not your turn.", ephemeral=True)
            return

        to_sq = chess.parse_square(self.values[0])

        # Find the legal move; auto-promote to queen
        move = None
        for m in game.board.legal_moves:
            if m.from_square == self.from_sq and m.to_square == to_sq:
                if m.promotion is None or m.promotion == chess.QUEEN:
                    move = m
                    break
        if move is None:
            await interaction.response.send_message("Invalid move.", ephemeral=True)
            return

        game.board.push(move)
        game.last_move = move
        self.view.stop()

        over = _game_over_text(game)
        if over:
            del active_games[self.channel_id]
            await interaction.response.edit_message(
                content=over, attachments=[_board_file(game)], view=None
            )
            return

        if not game.is_vs_bot:
            new_view = ChessBoardView(game, self.channel_id)
            await interaction.response.edit_message(
                content=_turn_line(game),
                attachments=[_board_file(game)],
                view=new_view,
            )
            return

        # vs bot — defer while Stockfish thinks
        await interaction.response.defer()

        bot_move = await _get_bot_move(game.board, game.difficulty)
        game.board.push(bot_move)
        game.last_move = bot_move

        over = _game_over_text(game)
        if over:
            del active_games[self.channel_id]
            await interaction.edit_original_response(
                content=over, attachments=[_board_file(game)], view=None
            )
        else:
            new_view = ChessBoardView(game, self.channel_id)
            await interaction.edit_original_response(
                content=_turn_line(game),
                attachments=[_board_file(game)],
                view=new_view,
            )
            game.message = await interaction.original_response()


class CancelButton(discord.ui.Button):
    def __init__(self, game: ChessGame, channel_id: int):
        super().__init__(label="Cancel", style=discord.ButtonStyle.secondary, row=1)
        self.game       = game
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction):
        if interaction.user != self.game.current_player:
            await interaction.response.send_message("It's not your turn.", ephemeral=True)
            return
        self.view.stop()
        new_view = ChessBoardView(self.game, self.channel_id)
        await interaction.response.edit_message(
            content=_turn_line(self.game),
            attachments=[_board_file(self.game)],
            view=new_view,
        )


class ChessMoveView(discord.ui.View):
    def __init__(self, game: ChessGame, from_sq: chess.Square,
                 targets: set, channel_id: int):
        super().__init__(timeout=600)
        self.game       = game
        self.channel_id = channel_id
        self.add_item(DestSelectMenu(game, from_sq, targets, channel_id))
        self.add_item(CancelButton(game, channel_id))
        self.add_item(ResignButton())

    async def on_timeout(self):
        if active_games.get(self.channel_id) is not self.game:
            return
        del active_games[self.channel_id]
        if self.game.message:
            try:
                await self.game.message.edit(content="Game timed out.", view=None)
            except Exception:
                pass


# ─────────────────────────── PvP challenge view ───────────────────────────────

class ChessChallengeView(discord.ui.View):
    def __init__(self, challenger: discord.Member, opponent: discord.Member,
                 difficulty: str, channel_id: int):
        super().__init__(timeout=60)
        self.challenger = challenger
        self.opponent   = opponent
        self.difficulty = difficulty
        self.channel_id = channel_id

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.opponent:
            await interaction.response.send_message("This challenge isn't for you.", ephemeral=True)
            return
        self.stop()

        game = ChessGame(
            board=chess.Board(),
            white=self.challenger,
            black=self.opponent,
            difficulty=self.difficulty,
        )
        active_games[self.channel_id] = game

        new_view = ChessBoardView(game, self.channel_id)
        await interaction.response.edit_message(
            content=(
                f"**Chess** — {self.challenger.mention} ⬜ vs {self.opponent.mention} ⬛\n"
                + _turn_line(game)
            ),
            attachments=[_board_file(game)],
            view=new_view,
        )
        game.message = await interaction.original_response()

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.opponent:
            await interaction.response.send_message("This challenge isn't for you.", ephemeral=True)
            return
        self.stop()
        await interaction.response.edit_message(
            content=f"{self.opponent.mention} declined the chess challenge.",
            view=None,
        )


# ─────────────────────────── Cog ─────────────────────────────────────────────

DIFFICULTY_CHOICES = [
    app_commands.Choice(name="Easy",   value="easy"),
    app_commands.Choice(name="Medium", value="medium"),
    app_commands.Choice(name="Hard",   value="hard"),
]


class ChessCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="chess", description="Play chess — vs bot or challenge a player.")
    @app_commands.describe(
        opponent="Player to challenge. Leave blank to play vs bot.",
        difficulty="Bot difficulty (easy/medium/hard). Ignored for PvP.",
    )
    @app_commands.choices(difficulty=DIFFICULTY_CHOICES)
    async def chess(
        self,
        interaction: discord.Interaction,
        opponent: Optional[discord.Member] = None,
        difficulty: Optional[app_commands.Choice[str]] = None,
    ):
        channel_id = interaction.channel_id

        if channel_id in active_games:
            await interaction.response.send_message(
                "A chess game is already running in this channel. Resign it first.",
                ephemeral=True,
            )
            return

        diff = difficulty.value if difficulty else "medium"

        if opponent is not None:
            if opponent == interaction.user:
                await interaction.response.send_message("You can't challenge yourself.", ephemeral=True)
                return
            if opponent.bot:
                await interaction.response.send_message(
                    "Can't challenge a bot — leave opponent blank to play vs the AI.", ephemeral=True
                )
                return
            view = ChessChallengeView(interaction.user, opponent, diff, channel_id)
            await interaction.response.send_message(
                content=f"{opponent.mention}, {interaction.user.mention} challenges you to chess! ♟️ Do you accept?",
                view=view,
            )
            return

        # vs bot
        game = ChessGame(
            board=chess.Board(),
            white=interaction.user,
            black=None,
            difficulty=diff,
        )
        active_games[channel_id] = game

        new_view = ChessBoardView(game, channel_id)
        await interaction.response.send_message(
            content=(
                f"**Chess vs Bot** ({diff.capitalize()}) — {interaction.user.mention} ⬜\n"
                + _turn_line(game)
            ),
            file=_board_file(game),
            view=new_view,
        )
        game.message = await interaction.original_response()


async def setup(bot: commands.Bot):
    await bot.add_cog(ChessCog(bot))
