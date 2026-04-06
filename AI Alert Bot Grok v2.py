import os
import asyncio
import datetime
import json
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

# CUSTOM FILTERS (match what you set up in Unusual Whales)
CUSTOM_FILTERS = [
    {"name": "AI_ETF", "interval_seconds": 30},
    {"name": "AI_Mega_Cap", "interval_seconds": 45},
    {"name": "AI_Mid_Cap", "interval_seconds": 120},
    {"name": "AI_Small_Cap", "interval_seconds": 180},
]

# YOUR TRADING RULES (applied ONLY to auto-alerts)
TRADING_RULES = """
HARD FILTERS - ONLY alert if ALL pass:
1. Aggressive: Sweep or at/above ask (calls) or at/below bid (puts).
2. New opening: Volume or contracts > Open Interest.
3. No chasing: Today's move < |3%| (relaxed for ETFs).
4. Premium tier: Large ≥$500K, Mid ≥$100K, Small ≥$50K, ETFs higher bar.
Only directional flow (bullish calls or bearish puts). Be extremely short.
"""

# ====================== EXECUTE TOOL ======================
async def get_flow_alerts(limit=200):
    try:
        headers = {"Authorization": f"Bearer {UW_API_KEY}"}
        base_url = "https://api.unusualwhales.com"
        url = f"{base_url}/api/option-trades/flow-alerts"
        params = {"limit": limit}

        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(url, headers=headers, params=params)
            data = resp.json() if resp.status_code == 200 else {"error": resp.text}
            if isinstance(data, dict) and isinstance(data.get("data"), list):
                return data["data"]
            return []
    except Exception as e:
        print(f"Flow fetch error: {e}")
        return []

# ====================== SHORT ALERT FORMAT (Clean) ======================
def format_short_alert(trade):
    ticker = trade.get("ticker", "UNKNOWN")
    expiry = trade.get("expiration", "")[:10]
    strike = trade.get("strike_price", "")
    option_type = "CALL" if trade.get("option_type", "").upper() == "CALL" else "PUT"
    side = "BULLISH" if (option_type == "CALL" and trade.get("side", "") == "ask") or (option_type == "PUT" and trade.get("side", "") == "bid") else "BEARISH"
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

    try:
        trades = await get_flow_alerts(limit=200)
        for trade in trades:
            # Simple rule filter (expand with full rules later)
            vol = trade.get("volume", 0)
            oi = trade.get("open_interest", 1)
            premium = trade.get("premium", 0)

            if vol > oi * 3 and premium > 50000:  # Basic high-conviction filter
                alert = format_short_alert(trade)
                await channel.send(alert)
                await asyncio.sleep(1.5)  # Rate limit
    except Exception as e:
        print(f"Scanner error: {e}")

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
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    "https://api.x.ai/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {XAI_API_KEY}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": "grok-4-fast-reasoning",
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