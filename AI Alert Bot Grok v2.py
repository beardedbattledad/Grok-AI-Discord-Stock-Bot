import os
import asyncio
import datetime
import json
import re
from dotenv import load_dotenv
import discord
from discord.ext import commands, tasks
import httpx

load_dotenv()

# CONFIG
XAI_API_KEY = os.getenv("XAI_API_KEY")
UW_API_KEY = os.getenv("UW_API_KEY")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
ALERT_CHANNEL_ID = 1490357987154460862

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ====================== DATA FETCHERS ======================
async def get_flow_alerts(limit=200, ticker=None):
    try:
        headers = {"Authorization": f"Bearer {UW_API_KEY}"}
        base_url = "https://api.unusualwhales.com"
        
        if ticker:
            url = f"{base_url}/api/stock/{ticker.upper()}/flow-alerts"
            print(f"→ Ticker-specific flow for {ticker.upper()}")
        else:
            url = f"{base_url}/api/option-trades/flow-alerts"
            print("→ Broad recent flow")

        params = {"limit": limit}

        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(url, headers=headers, params=params)
            print(f"→ Flow API Status: {resp.status_code}")
            data = resp.json() if resp.status_code == 200 else {"error": resp.text}
            if isinstance(data, dict) and isinstance(data.get("data"), list):
                return data["data"][:150]
            return []
    except Exception as e:
        print(f"Flow fetch error: {e}")
        return []

# ====================== CONVERSATIONAL MODE (Improved Ticker Detection) ======================
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
            # IMPROVED TICKER DETECTION - prioritize the most likely ticker
            ticker = None
            # Look for common tickers (2-5 uppercase letters, not common words)
            candidates = re.findall(r'\b[A-Z]{2,5}\b', query)
            if candidates:
                # Take the first one that isn't a common word
                common_words = {"THE", "AND", "FOR", "WITH", "WHAT", "ABOUT", "DEEP", "DIVE", "LIKE", "FLOW", "OPTIONS"}
                for cand in candidates:
                    if cand not in common_words:
                        ticker = cand
                        break

            # ALWAYS pull flow — ticker-specific first if found
            print(f"→ Fetching flow (ticker detected: {ticker})")
            flow_data = await get_flow_alerts(limit=200, ticker=ticker)

            context = f"Here is the most recent options flow data:\n{json.dumps(flow_data, default=str, indent=2)}"
            full_query = f"{query}\n\n{context}\n\nProvide a concise, evidence-based analysis. Highlight only high-conviction setups with specific numbers."

            # Call Grok
            async with httpx.AsyncClient(timeout=40.0) as client:
                resp = await client.post(
                    "https://api.x.ai/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {XAI_API_KEY}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": "grok-4-fast-reasoning",
                        "messages": [{"role": "user", "content": full_query}],
                        "temperature": 0.4,
                        "max_tokens": 1200
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
    if not text:
        await channel.send("No data available.")
        return
    if len(text) <= 1900:
        await channel.send(text)
        return
    chunks = [text[i:i+1900] for i in range(0, len(text), 1900)]
    for i, chunk in enumerate(chunks, 1):
        prefix = f"**Part {i}/{len(chunks)}**\n" if len(chunks) > 1 else ""
        await channel.send(prefix + chunk)

@bot.event
async def on_ready():
    print(f"✅ Grok Bot is online as {bot.user}")

bot.run(DISCORD_TOKEN)