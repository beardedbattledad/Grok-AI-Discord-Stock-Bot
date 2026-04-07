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

# Your custom alert names (exact names from Unusual Whales)
CUSTOM_ALERT_NAMES = ["AI_ETF", "AI_Mega_Cap", "AI_Mid_Cap", "AI_Small_Cap"]

# MAJOR INDEX ETFs (for relaxed chasing)
MAJOR_ETFS = {"SPY", "QQQ", "SOXX", "TQQQ", "SPXU", "SQQQ", "SOXS", "SPXS", "IWM", "DIA", "XLK", "XLF"}

# Global map: alert name → config_id
alert_configs = {}

# ====================== LOAD CUSTOM ALERT CONFIGS ON STARTUP ======================
async def load_alert_configs():
    global alert_configs
    alert_configs = {}
    try:
        headers = {"Authorization": f"Bearer {UW_API_KEY}"}
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get("https://api.unusualwhales.com/api/alerts/configuration", headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                for alert in data.get("data", []):
                    name = alert.get("name")
                    aid = alert.get("id")
                    if name and aid and name in CUSTOM_ALERT_NAMES:
                        alert_configs[name] = aid
                print(f"✅ Loaded {len(alert_configs)} custom alerts: {list(alert_configs.keys())}")
            else:
                print(f"Failed to load alert configs: {resp.status_code}")
    except Exception as e:
        print(f"Error loading alert configs: {e}")

# ====================== DATA FETCHERS ======================
async def get_flow_alerts(limit=200, ticker=None):
    # General flow (used for conversational mode)
    try:
        headers = {"Authorization": f"Bearer {UW_API_KEY}"}
        base_url = "https://api.unusualwhales.com"
        if ticker:
            url = f"{base_url}/api/stock/{ticker.upper()}/flow-alerts"
        else:
            url = f"{base_url}/api/option-trades/flow-alerts"
        params = {"limit": limit}

        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(url, headers=headers, params=params)
            data = resp.json() if resp.status_code == 200 else {"error": resp.text}
            if isinstance(data, dict) and isinstance(data.get("data"), list):
                return data["data"][:150]
            return []
    except Exception as e:
        print(f"Flow fetch error: {e}")
        return []

async def get_custom_alerts():
    # Triggered alerts from your custom filters
    if not alert_configs:
        return []
    try:
        headers = {"Authorization": f"Bearer {UW_API_KEY}"}
        base_url = "https://api.unusualwhales.com"
        config_ids = list(alert_configs.values())
        params = {"limit": 200}
        for cid in config_ids:
            params.setdefault("config_ids[]", []).append(cid)

        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(f"{base_url}/api/alerts", headers=headers, params=params)
            print(f"→ Custom Alerts API Status: {resp.status_code}")
            data = resp.json() if resp.status_code == 200 else {"error": resp.text}
            if isinstance(data, dict) and isinstance(data.get("data"), list):
                return data["data"]
            return []
    except Exception as e:
        print(f"Custom alerts fetch error: {e}")
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

# ====================== AUTO ALERT SCANNER (Custom Alerts Only) ======================
@tasks.loop(seconds=30)
async def auto_alert_scanner():
    if not is_market_open():
        print("→ Scanner: Market closed")
        return

    channel = bot.get_channel(ALERT_CHANNEL_ID)
    if not channel:
        print("→ Scanner: Channel not found")
        return

    print("→ === CUSTOM ALERT SCAN START ===")
    triggered = await get_custom_alerts()
    print(f"  Received {len(triggered)} triggered custom alerts")

    alert_count = 0
    for trade in triggered[:50]:
        # Apply your exact rules on top of the triggered alerts
        vol = trade.get("volume", 0)
        oi = trade.get("open_interest", 1)
        premium = trade.get("premium", 0)
        is_sweep = trade.get("is_sweep", False)
        side = trade.get("side", "").lower()
        option_type = str(trade.get("option_type", "")).upper()
        today_move = abs(trade.get("underlying_change_percent", 0))
        ticker = trade.get("ticker", "").upper()

        # 1. Aggressive execution (sweeps preferred but not required)
        aggressive = is_sweep or ((option_type == "CALL" and side == "ask") or (option_type == "PUT" and side == "bid"))
        if not aggressive:
            continue

        # 2. New opening positions
        if vol <= oi:
            continue

        # 3. No chasing (relaxed for Major ETFs)
        is_major_etf = ticker in MAJOR_ETFS
        max_move = 5 if is_major_etf else 3
        if today_move > max_move:
            continue

        # 4. Premium tier (already pre-filtered by your custom alert, but double-check)
        # (Your custom alerts already enforce premium, so we skip re-checking here)

        # 5. Directional preference
        is_directional = (option_type == "CALL" and side == "ask") or (option_type == "PUT" and side == "bid")
        if not is_directional:
            continue

        # If we reach here → high-conviction alert
        alert = format_short_alert(trade)
        await channel.send(alert)
        alert_count += 1
        print(f"  ✅ ALERT SENT: {ticker} | Prem:${premium:,} | Vol/OI:{vol}/{oi}")
        await asyncio.sleep(1.5)

    if alert_count == 0:
        print("  No high-conviction alerts from custom filters this cycle")
    else:
        print(f"  Sent {alert_count} alerts this cycle")

    print("→ === CUSTOM ALERT SCAN COMPLETED ===\n")

# ====================== CONVERSATIONAL MODE (pulls both general flow + custom alerts) ======================
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
            candidates = re.findall(r'\b[A-Z]{2,5}\b', query)
            common_words = {"THE", "AND", "FOR", "WITH", "WHAT", "ABOUT", "DEEP", "DIVE", "LIKE", "FLOW", "OPTIONS"}
            for cand in candidates:
                if cand not in common_words:
                    ticker = cand
                    break

            # Pull BOTH general flow and your custom alerts
            print(f"→ Fetching general flow (ticker={ticker})")
            general_flow = await get_flow_alerts(limit=200, ticker=ticker)

            print("→ Fetching custom triggered alerts")
            custom_alerts = await get_custom_alerts()

            # Combine for Grok
            combined = {
                "general_flow": general_flow,
                "custom_alerts": custom_alerts
            }

            context = f"Here is the combined data:\nGeneral recent flow:\n{json.dumps(general_flow, default=str, indent=2)}\n\nTriggered custom alerts:\n{json.dumps(custom_alerts, default=str, indent=2)}"
            full_query = f"{query}\n\n{context}\n\nProvide a concise, evidence-based analysis using both datasets. Highlight only high-conviction setups with specific numbers."

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
    await load_alert_configs()   # Load your custom alerts
    auto_alert_scanner.start()

bot.run(DISCORD_TOKEN)