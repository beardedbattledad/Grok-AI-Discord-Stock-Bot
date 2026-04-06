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

# CUSTOM FILTERS (use exactly the names you created in Unusual Whales)
CUSTOM_FILTERS = [
    {"name": "AI_ETF",        "min_premium": 1000000, "interval_seconds": 30},
    {"name": "AI_Mega_Cap",   "min_premium": 500000,  "interval_seconds": 45},
    {"name": "AI_Mid_Cap",    "min_premium": 100000,  "interval_seconds": 120},
    {"name": "AI_Small_Cap",  "min_premium": 50000,   "interval_seconds": 180},
]

# ====================== DATA FETCHERS ======================
async def get_flow_alerts(limit=200, ticker=None, min_premium=None):
    try:
        headers = {"Authorization": f"Bearer {UW_API_KEY}"}
        base_url = "https://api.unusualwhales.com"
        
        if ticker:
            url = f"{base_url}/api/stock/{ticker.upper()}/flow-alerts"
            print(f"→ Ticker-specific flow for {ticker}")
        else:
            url = f"{base_url}/api/option-trades/flow-alerts"
            print("→ Broad recent flow")

        params = {"limit": limit}
        if min_premium is not None:
            params["min_premium"] = min_premium

        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(url, headers=headers, params=params)
            print(f"→ Flow API Status: {resp.status_code} | min_premium={min_premium}")
            data = resp.json() if resp.status_code == 200 else {"error": resp.text}
            if isinstance(data, dict) and isinstance(data.get("data"), list):
                return data["data"][:150]
            return []
    except Exception as e:
        print(f"Flow fetch error: {e}")
        return []

async def get_dark_pool(limit=10):
    try:
        headers = {"Authorization": f"Bearer {UW_API_KEY}"}
        base_url = "https://api.unusualwhales.com"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{base_url}/api/darkpool/recent", headers=headers, params={"limit": limit})
            data = resp.json() if resp.status_code == 200 else []
            return data if isinstance(data, list) else []
    except Exception as e:
        print(f"Dark pool error: {e}")
        return []

# ====================== SHORT ALERT FORMAT ======================
def format_short_alert(trade):
    ticker = trade.get("ticker", "UNKNOWN")
    expiry = str(trade.get("expiration", ""))[:10]
    strike = trade.get("strike_price", "")
    option_type = "CALL" if str(trade.get("option_type", "")).upper() == "CALL" else "PUT"
    side = "BULLISH" if option_type == "CALL" else "BEARISH"
    premium = trade.get("premium", 0)
    vol = trade.get("volume", 0)
    oi = trade.get("open_interest", 0)
    execution = "SWEEP" if trade.get("is_sweep") else "BLOCK"

    return f"🚨 {ticker} {expiry} ${strike} {option_type} | {side} | Prem:${premium:,} | Vol/OI:{vol}/{oi} | {execution}"

# ====================== MARKET HOURS ======================
def is_market_open():
    now = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=4)  # ET
    if now.weekday() >= 5:
        return False
    return 9.5 <= (now.hour + now.minute / 60) <= 16.0

# ====================== AUTO ALERT SCANNER ======================
@tasks.loop(seconds=30)
async def auto_alert_scanner():
    if not is_market_open():
        return
    channel = bot.get_channel(ALERT_CHANNEL_ID)
    if not channel:
        return

    for f in CUSTOM_FILTERS:
        try:
            trades = await get_flow_alerts(limit=200, min_premium=f["min_premium"])
            for trade in trades:
                vol = trade.get("volume", 0)
                oi = trade.get("open_interest", 1)
                premium = trade.get("premium", 0)
                if vol > oi * 3 and premium >= f["min_premium"]:
                    alert = format_short_alert(trade)
                    await channel.send(alert)
                    await asyncio.sleep(1.5)
        except Exception as e:
            print(f"Scanner error for {f['name']}: {e}")

# ====================== CONVERSATIONAL MODE ======================
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
            # Ticker detection
            ticker = None
            potential = re.findall(r'\b[A-Z]{2,5}\b', query)
            if potential:
                ticker = potential[0]

            # Fetch flow (ticker-specific if mentioned)
            print(f"→ Fetching flow (ticker={ticker})")
            flow_data = await get_flow_alerts(limit=200, ticker=ticker)

            # Dark pool only if asked
            extra_context = ""
            if any(word in query.lower() for word in ["dark pool", "darkpool", "block", "institutional"]):
                print("→ Fetching dark pool")
                dark_data = await get_dark_pool(limit=10)
                extra_context = f"\n\nRecent Dark Pool prints:\n{json.dumps(dark_data[:8], default=str, indent=2)}" if dark_data else "\n\nNo recent dark pool prints found."

            context = f"Here is the most recent options flow data:\n{json.dumps(flow_data, default=str, indent=2)}{extra_context}"
            full_query = f"{query}\n\n{context}\n\nProvide a concise, evidence-based analysis. Highlight only high-conviction setups with specific numbers."

            async with httpx.AsyncClient(timeout=40.0) as client:
                resp = await client.post(
                    "https://api.x.ai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {XAI_API_KEY}", "Content-Type": "application/json"},
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
    auto_alert_scanner.start()

bot.run(DISCORD_TOKEN)