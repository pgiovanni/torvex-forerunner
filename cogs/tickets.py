import discord
from discord.ext import commands
import os

TICKET_CATEGORY_ID = int(os.getenv("TICKET_CATEGORY_ID", 0))
STAFF_ROLE_ID = int(os.getenv("STAFF_ROLE_ID", 0))
TICKET_CHANNEL_NAME = "general-tickets"

class Tickets(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot:
            return
        if message.channel.name != TICKET_CHANNEL_NAME:
            return

        guild = message.guild
        category = guild.get_channel(TICKET_CATEGORY_ID)
        staff_role = guild.get_role(STAFF_ROLE_ID)

        # Count existing ticket channels to generate ticket number
        ticket_num = len([c for c in category.channels if c.name.startswith("ticket-")]) + 1
        channel_name = f"ticket-{ticket_num:04d}"

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            message.author: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            staff_role: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        }

        ticket_channel = await guild.create_text_channel(
            channel_name,
            category=category,
            overwrites=overwrites,
            topic=f"Ticket opened by {message.author} | {message.author.id}"
        )

        embed = discord.Embed(
            title=f"Ticket #{ticket_num:04d}",
            description=f"**{message.author.mention}** opened a ticket.\n\n**Request:**\n{message.content}",
            color=discord.Color.blurple()
        )
        embed.set_footer(text="Use !close to close this ticket.")

        await ticket_channel.send(content=staff_role.mention, embed=embed)
        await message.reply(f"Ticket created! {ticket_channel.mention}", delete_after=10)

    @commands.command()
    async def close(self, ctx):
        """Close the current ticket channel."""
        if not ctx.channel.name.startswith("ticket-"):
            await ctx.send("This command can only be used in a ticket channel.")
            return

        staff_role = ctx.guild.get_role(STAFF_ROLE_ID)
        if staff_role not in ctx.author.roles:
            await ctx.send("Only staff can close tickets.")
            return

        await ctx.send("Closing ticket...")
        await ctx.channel.delete()

async def setup(bot):
    await bot.add_cog(Tickets(bot))
