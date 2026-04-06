import os
import asyncio
import datetime
import json
from dotenv import load_dotenv
import discord
from discord.ext import commands
import httpx   # Already used for UW, now also for Grok

load_dotenv()

# CONFIG
XAI_API_KEY = os.getenv("XAI_API_KEY")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
ALERT_CHANNEL_ID = 1490357987154460862

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

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
        "name": "get_dark_pool_trades",
        "description": "Get recent dark pool prints.",
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 15}}
        }
    }
]

# ====================== EXECUTE TOOL (Unusual Whales) ======================
async def execute_tool(tool_name: str, tool_input: dict):
    try:
        headers = {"Authorization": f"Bearer {os.getenv('UW_API_KEY')}"}
        base_url = "https://api.unusualwhales.com"

        if tool_name == "get_flow_alerts":
            ticker = tool_input.get("ticker")
            limit = min(tool_input.get("limit", 200), 200)
            since_hours = tool_input.get("since_hours")
            min_premium = tool_input.get("min_premium")

            params = {"limit": limit}

            if since_hours is not None:
                cutoff = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=since_hours)).isoformat()
                params["newer_than"] = cutoff

            if min_premium is not None:
                params["min_premium"] = min_premium

            if ticker:
                url = f"{base_url}/api/stock/{ticker.upper()}/flow-alerts"
            else:
                url = f"{base_url}/api/option-trades/flow-alerts"

            print(f"→ Calling {url} | limit={limit} | since_hours={since_hours or 'None (most recent)'}")

            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(url, headers=headers, params=params)
                print(f"→ Status: {resp.status_code}")

                data = resp.json() if resp.status_code == 200 else {"error": resp.text}

                if isinstance(data, dict) and isinstance(data.get("data"), list):
                    results = data["data"]
                    return {
                        "count": len(results),
                        "samples": results[:150],
                        "ticker": ticker or "broad",
                        "note": f"Most recent {len(results)} trades"
                    }
                return data

        # Keep other tools if you want them
        return {"error": f"Unknown tool: {tool_name}"}
    except Exception as e:
        print(f"Tool error: {str(e)}")
        return {"error": str(e)}

# ====================== CONVERSATIONAL MODE (Direct HTTP for Grok) ======================
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if bot.user.mentioned_in(message) or isinstance(message.channel, discord.DMChannel):
        query = message.clean_content.replace(f"<@{bot.user.id}>", "").strip()
        if not query:
            return

        try:
            async with message.channel.typing():
                pass
        except:
            pass

        try:
            # Direct HTTP call to Grok (xAI API)
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    "https://api.x.ai/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {XAI_API_KEY}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": "grok-beta",
                        "messages": [{"role": "user", "content": query}],
                        "temperature": 0.4,
                        "max_tokens": 1000
                    }
                )

                if resp.status_code != 200:
                    await message.reply(f"API error: {resp.status_code}")
                    return

                data = resp.json()
                final_reply = data["choices"][0]["message"]["content"]
                await send_long_message(message.channel, final_reply or "No strong signals found.")

        except Exception as e:
            print(f"Error: {e}")
            await message.reply("Sorry, I ran into an error while analyzing.")

# ====================== SEND LONG MESSAGES ======================
async def send_long_message(channel, text):
    if len(text) <= 1900:
        await channel.send(text)
        return
    chunks = [text[i:i+1900] for i in range(0, len(text), 1900)]
    for i, chunk in enumerate(chunks, 1):
        prefix = f"**Part {i}/{len(chunks)}**\n" if len(chunks) > 1 else ""
        await channel.send(prefix + chunk)

@bot.event
async def on_ready():
    print(f"✅ Grok Bot is online as {bot.user} — Ready for DM tests and mentions!")

bot.run(DISCORD_TOKEN)