import asyncio
import discord
from discord.ext import commands
from config import TOKEN


intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


async def load():
    await bot.load_extension("cogs.gangs")
    await bot.load_extension("cogs.matchmaking")


@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"{bot.user} online")


async def main():
    await load()
    await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
