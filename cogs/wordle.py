import discord
from discord import app_commands
from discord.ext import commands
import random
import json
import sys
import os
import asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from utils.wordle_renderer import build_reveal_gif, build_static_image

with open("data/words.json") as f:
    WORDS = json.load(f)

ALL_WORDS = set(WORDS["common"] + WORDS["hard"])

# Active games keyed by thread_id
active_games = {}

GREEN_E  = "🟩"
YELLOW_E = "🟨"
BLACK_E  = "⬛"

DIFFICULTY_CHOICES = [
    app_commands.Choice(name="Easy (8 guesses, common words)", value="easy"),
    app_commands.Choice(name="Normal (6 guesses)", value="normal"),
    app_commands.Choice(name="Hard (6 guesses, obscure words)", value="hard"),
]


def wordle_feedback(guess, answer):
    result = [BLACK_E] * 5
    answer_chars = list(answer)
    guess_chars = list(guess)
    for i in range(5):
        if guess_chars[i] == answer_chars[i]:
            result[i] = GREEN_E
            answer_chars[i] = None
            guess_chars[i] = None
    for i in range(5):
        if guess_chars[i] and guess_chars[i] in answer_chars:
            result[i] = YELLOW_E
            answer_chars[answer_chars.index(guess_chars[i])] = None
    return "".join(result)


def update_letter_states(letter_states, guess, feedback):
    tokens = [c for c in feedback if c in (GREEN_E, YELLOW_E, BLACK_E)]
    for i, letter in enumerate(guess):
        fb = tokens[i] if i < len(tokens) else BLACK_E
        current = letter_states.get(letter)
        if fb == GREEN_E:
            letter_states[letter] = "green"
        elif fb == YELLOW_E and current != "green":
            letter_states[letter] = "yellow"
        elif current not in ("green", "yellow"):
            letter_states[letter] = "gray"
    return letter_states


def parse_feedback(feedback_str):
    """Parse emoji string back to list for indexing."""
    emojis = []
    i = 0
    while i < len(feedback_str):
        for e in [GREEN_E, YELLOW_E, BLACK_E]:
            if feedback_str[i:i+len(e)] == e:
                emojis.append(e)
                i += len(e)
                break
        else:
            i += 1
    return emojis


class Wordle(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="wordle", description="Start a Wordle game in a private thread.")
    @app_commands.choices(difficulty=DIFFICULTY_CHOICES)
    async def wordle(self, interaction: discord.Interaction, difficulty: app_commands.Choice[str] = None):
        diff = difficulty.value if difficulty else "normal"

        if diff == "hard":
            word = random.choice(WORDS["hard"])
            max_guesses = 6
        elif diff == "easy":
            word = random.choice(WORDS["common"])
            max_guesses = 8
        else:
            word = random.choice(WORDS["common"])
            max_guesses = 6

        await interaction.response.defer(ephemeral=True)

        # Create thread
        thread = await interaction.channel.create_thread(
            name=f"wordle-{interaction.user.display_name}",
            type=discord.ChannelType.public_thread,
            auto_archive_duration=60
        )

        # Render empty board
        img_buf = build_static_image([], [], {})
        file = discord.File(img_buf, filename="wordle.png")

        msg = await thread.send(
            content=f"**Wordle** — {interaction.user.mention}\nGuess the 5-letter word! You have **{max_guesses}** guesses.\nJust type your guess here.",
            file=file
        )

        active_games[thread.id] = {
            "owner_id": interaction.user.id,
            "answer": word,
            "guesses": [],
            "feedbacks": [],
            "letter_states": {},
            "max_guesses": max_guesses,
            "board_message_id": msg.id,
        }

        await interaction.followup.send(f"Game started! {thread.mention}", ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot:
            return
        if message.channel.id not in active_games:
            return

        game = active_games[message.channel.id]
        if message.author.id != game["owner_id"]:
            return

        guess = message.content.strip().lower()

        # Validate
        if len(guess) != 5 or not guess.isalpha():
            await message.reply("Must be a 5-letter word.", delete_after=4)
            await message.delete(delay=4)
            return

        if guess not in ALL_WORDS:
            await message.reply("Not in word list.", delete_after=4)
            await message.delete(delay=4)
            return

        # Process guess
        feedback = wordle_feedback(guess, game["answer"])
        game["guesses"].append(guess)
        game["feedbacks"].append(feedback)
        update_letter_states(game["letter_states"], guess, feedback)

        remaining = game["max_guesses"] - len(game["guesses"])
        won = guess == game["answer"]
        lost = remaining == 0 and not won

        # Build animated GIF
        gif_buf = build_reveal_gif(game["guesses"], game["feedbacks"], game["letter_states"])
        file = discord.File(gif_buf, filename="wordle.gif")

        # Edit the board message
        try:
            board_msg = await message.channel.fetch_message(game["board_message_id"])
            await board_msg.delete()
        except Exception:
            pass

        # Estimate GIF duration: 5 tiles * 5 steps * 60ms + 800ms final = ~2.3s
        gif_duration = 5 * 5 * 0.06 + 0.8 + 0.3

        if won:
            answer = game["answer"]
            guesses_count = len(game["guesses"])
            del active_games[message.channel.id]
            content = f"🎉 **{message.author.mention} got it!** The word was `{answer.upper()}` — in {guesses_count} guess(es)!"
            new_msg = await message.channel.send(content=content, file=file)
            await message.delete(delay=0)
            await asyncio.sleep(gif_duration)
            static_buf = build_static_image(game["guesses"], game["feedbacks"], game["letter_states"])
            await new_msg.edit(attachments=[discord.File(static_buf, filename="wordle.png")])
            await message.channel.edit(archived=True, locked=True)
        elif lost:
            answer = game["answer"]
            del active_games[message.channel.id]
            content = f"💀 **Game over.** The word was `{answer.upper()}`."
            new_msg = await message.channel.send(content=content, file=file)
            await message.delete(delay=0)
            await asyncio.sleep(gif_duration)
            static_buf = build_static_image(game["guesses"], game["feedbacks"], game["letter_states"])
            await new_msg.edit(attachments=[discord.File(static_buf, filename="wordle.png")])
            await message.channel.edit(archived=True, locked=True)
        else:
            content = f"{message.author.mention} — **{remaining}** guess(es) remaining. Keep going!"
            new_msg = await message.channel.send(content=content, file=file)
            await message.delete(delay=0)
            await asyncio.sleep(gif_duration)
            static_buf = build_static_image(game["guesses"], game["feedbacks"], game["letter_states"])
            await new_msg.edit(attachments=[discord.File(static_buf, filename="wordle.png")])
            game["board_message_id"] = new_msg.id


async def setup(bot):
    await bot.add_cog(Wordle(bot))
