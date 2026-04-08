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

# ====================== CONFIG ======================
XAI_API_KEY = os.getenv("XAI_API_KEY")
UW_API_KEY = os.getenv("UW_API_KEY")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
ALERT_CHANNEL_ID = 1490357987154460862

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

CUSTOM_ALERT_NAMES = ["AI Mid Cap", "AI Small Cap", "AI ETF", "AI Mega Cap"]

alert_configs = {}
last_alert_time = None

async def load_alert_configs():
    global alert_configs
    alert_configs = {}
    try:
        headers = {"Authorization": f"Bearer {UW_API_KEY}"}
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get("https://api.unusualwhales.com/api/alerts/configuration", headers=headers)
            print(f"→ Configs API Status: {resp.status_code}")
            if resp.status_code == 200:
                data = resp.json()
                loaded = 0
                for alert in data.get("data", []):
                    name = alert.get("name")
                    aid = alert.get("id")
                    if name and aid and name in CUSTOM_ALERT_NAMES:
                        alert_configs[name] = aid
                        loaded += 1
                        print(f"  Loaded: '{name}' (ID: {aid})")
                print(f"✅ Loaded {loaded} matching custom alerts")
    except Exception as e:
        print(f"Config load error: {e}")

async def get_custom_alerts():
    global last_alert_time
    if not alert_configs:
        print("→ No custom alert configs loaded")
        return []
    try:
        headers = {"Authorization": f"Bearer {UW_API_KEY}"}
        base_url = "https://api.unusualwhales.com"
        config_ids = list(alert_configs.values())
        params = {"limit": 100}
        for cid in config_ids:
            params.setdefault("config_ids[]", []).append(cid)

        if last_alert_time:
            params["newer_than"] = last_alert_time

        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(f"{base_url}/api/alerts", headers=headers, params=params)
            print(f"→ Custom Alerts API Status: {resp.status_code}")
            if resp.status_code != 200:
                print(f"  Error: {resp.text[:300]}")
                return []

            data = resp.json()
            trades = data.get("data", []) if isinstance(data, dict) else []
            print(f"  Received {len(trades)} NEW trades from custom alerts")

            if trades:
                newest_time = trades[0].get("created_at") or trades[0].get("tape_time")
                if newest_time:
                    last_alert_time = newest_time
                    print(f"  Updated last_alert_time to: {last_alert_time}")

            return trades
    except Exception as e:
        print(f"Custom alerts fetch error: {e}")
        return []

def clean_ticker(symbol):
    if not symbol:
        return "UNKNOWN"
    match = re.match(r'^([A-Z]+)', str(symbol))
    return match.group(1) if match else str(symbol).split()[0]

def format_short_alert(trade):
    meta = trade.get("meta", {}) if isinstance(trade.get("meta"), dict) else {}
    ticker = clean_ticker(trade.get("symbol"))
    expiry = str(meta.get("expiration", trade.get("created_at", "")))[:10]
    strike = meta.get("strike_price", meta.get("strike", "N/A"))
    option_type = str(meta.get("option_type", meta.get("type", ""))).upper()
    if option_type not in ["CALL", "PUT"]:
        symbol_str = str(trade.get("symbol", ""))
        if 'P' in symbol_str[-15:]:
            option_type = "PUT"
        elif 'C' in symbol_str[-15:]:
            option_type = "CALL"
        else:
            option_type = "UNKNOWN"
    side = "BULLISH" if option_type == "CALL" else "BEARISH"
    premium = meta.get("total_premium", 0)
    vol = meta.get("volume", meta.get("ask_volume", 0) + meta.get("bid_volume", 0))
    oi = meta.get("open_interest", 1)
    execution = "SWEEP" if meta.get("has_sweep") or meta.get("is_sweep") else "BLOCK"

    return f"🚨 {ticker} {expiry} ${strike} {option_type} | {side} | Prem:${premium:,} | Vol/OI:{vol}/{oi} | {execution}"

@tasks.loop(seconds=45)  # slightly slower to give AI time
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

    if not triggered:
        print("  No new trades this cycle")
        print("→ === CUSTOM ALERT SCAN COMPLETED ===\n")
        return

    # Feed to Grok AI for intelligent filtering
    try:
        context = json.dumps(triggered, default=str, indent=2)
        system_prompt = """You are a sharp options flow analyst. 
Use these strict rules to decide which trades are worth alerting on:

- Ignore anything deep ITM (OTM% ≤ -5%)
- 0 DTE: extremely strict (high premium, strong volume spike, sweep preferred, strong ask/bid imbalance)
- 1-3 DTE: still strict but slightly more lenient
- Sweeps are preferred but not required
- Prefer new opening positions (volume > OI)
- The larger the volume the higher conviction is
- Prefer directional conviction (high ask% for calls, high bid% for puts)
- Use ask/bid volume percentages to judge conviction
- Only alert high-conviction setups

Output ONLY the trades you believe are good in this exact short format. Do not add extra text:

🚨 TICKER EXPIRY $STRIKE TYPE | SIDE | Prem:$PREMIUM | Vol/OI:VOL/OI | EXECUTION

If nothing is worth alerting, output nothing."""

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                "https://api.x.ai/v1/chat/completions",
                headers={"Authorization": f"Bearer {XAI_API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": "grok-4-fast-reasoning",
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": f"Here are the latest custom alert trades:\n{context}\n\nWhich ones are high-conviction? Output only the good ones in the exact format above."}
                    ],
                    "temperature": 0.3,
                    "max_tokens": 2000
                }
            )

            if resp.status_code != 200:
                print(f"  Grok API error: {resp.status_code}")
                return

            data = resp.json()
            ai_reply = data["choices"][0]["message"]["content"].strip()

            if ai_reply and ai_reply != "nothing" and len(ai_reply) > 10:
                # Split into individual alerts and send
                alerts = [line.strip() for line in ai_reply.split('\n') if line.strip().startswith("🚨")]
                for alert in alerts:
                    await channel.send(alert)
                    print(f"  ✅ AI ALERT SENT: {alert}")
                    await asyncio.sleep(1.0)
            else:
                print("  AI decided no high-conviction alerts this cycle")

    except Exception as e:
        print(f"  AI decision error: {e}")

    print("→ === CUSTOM ALERT SCAN COMPLETED ===\n")

def is_market_open():
    now = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=4)
    if now.weekday() >= 5:
        return False
    hour = now.hour + now.minute / 60.0
    return 9.5 <= hour <= 16.0

# Conversational mode (kept as-is — you said it works splendidly)
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
            ticker = None
            candidates = re.findall(r'\b[A-Z]{2,5}\b', query)
            common_words = {"THE", "AND", "FOR", "WITH", "WHAT", "ABOUT", "DEEP", "DIVE", "LIKE", "FLOW", "OPTIONS"}
            for cand in candidates:
                if cand not in common_words:
                    ticker = cand
                    break

            general_flow = await get_flow_alerts(limit=200, ticker=ticker)
            custom_alerts = await get_custom_alerts()

            context = f"General recent flow:\n{json.dumps(general_flow, default=str, indent=2)}\n\nTriggered custom alerts:\n{json.dumps(custom_alerts, default=str, indent=2)}"
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
            print(f"Error processing message: {e}")
            await message.reply("Sorry, I ran into an error while analyzing.")

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
    await load_alert_configs()
    auto_alert_scanner.start()

bot.run(DISCORD_TOKEN)