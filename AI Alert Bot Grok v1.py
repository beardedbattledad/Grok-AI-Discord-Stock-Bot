import os
import asyncio
import datetime
import json
from dotenv import load_dotenv
import discord
from discord.ext import commands
from anthropic import AsyncAnthropic

load_dotenv()

# CONFIG
UW_API_KEY = os.getenv("UW_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

ANTHROPIC = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

# ====================== TOOL DEFINITIONS ======================
TOOLS = [
    {
        "name": "get_flow_alerts",
        "description": "Get the most recent options flow activity. Default = last 200 trades.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Specific ticker like DVN (optional)"},
                "since_hours": {"type": "integer", "description": "Only use if user specifically asks for a time window"},
                "min_premium": {"type": "integer", "description": "Minimum premium — only use if user asks"},
                "limit": {"type": "integer", "default": 200}
            }
        }
    },
    {
        "name": "get