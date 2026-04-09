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

MAJOR_INDEX_ETFS = {"SPY", "QQQ", "SOXX", "IWM", "DIA", "XLK", "XLF"}

alert_configs = {}
last_alert_time = None
underlying_move_cache = {}
seen_alerts = set()

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

async def get_underlying_move(ticker: str) -> float:
    ticker = ticker.upper()
    now = datetime.datetime.now(datetime.UTC)
    if ticker in underlying_move_cache:
        move, ts = underlying_move_cache[ticker]
        if (now - ts).total_seconds() < 60:
            return move

    try:
        headers = {"Authorization": f"Bearer {UW_API_KEY}"}
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(f"https://api.unusualwhales.com/api/stock/{ticker}", headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                move = data.get("change_percent") or data.get("today_change_percent") or data.get("diff") or 0.0
                underlying_move_cache[ticker] = (float(move), now)
                return float(move)
    except Exception:
        pass
    return 0.0

async def get_custom_alerts():
    global last_alert_time
    if not alert_configs:
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
                return []

            data = resp.json()
            trades = data.get("data", []) if isinstance(data, dict) else []
            if trades:
                newest_time = trades[0].get("created_at") or trades[0].get("tape_time")
                if newest_time:
                    last_alert_time = newest_time
            return trades
    except Exception as e:
        print(f"Custom alerts fetch error: {e}")
        return []

def parse_option_symbol(symbol):
    symbol = str(symbol).strip()
    match = re.match(r'^([A-Z]+)(\d{6})([CP])0*(\d+)', symbol)
    if match:
        ticker = match.group(1)
        date_str = match.group(2)
        opt_type = "CALL" if match.group(3) == "C" else "PUT"
        strike = int(match.group(4)) / 1000
        try:
            expiry_date = datetime.datetime.strptime(date_str, "%y%m%d").date()
            expiry = expiry_date.strftime("%m/%d/%y")
        except:
            expiry = "UNKNOWN"
        return ticker, expiry, strike, opt_type
    return None, None, None, None

def clean_ticker(symbol):
    parsed = parse_option_symbol(symbol)
    return parsed[0] if parsed[0] else "UNKNOWN"

def format_short_alert(trade, conviction="Medium", explanation=""):
    symbol = trade.get("symbol", "")
    ticker, expiry, strike, option_type = parse_option_symbol(symbol)
    if not ticker:
        ticker = clean_ticker(symbol)
    side = "BULLISH" if option_type == "CALL" else "BEARISH"
    
    meta = {k.replace("meta_", ""): v for k, v in trade.items() if k.startswith("meta_")}
    
    premium = meta.get("total_premium", 0)
    vol = meta.get("volume", meta.get("ask_volume", 0) + meta.get("bid_volume", 0))
    avg_fill = meta.get("avg_fill", meta.get("avg_fill_price", "N/A"))
    oi = meta.get("open_interest", 1)
    vol_oi = meta.get("vol_oi_ratio", round(vol / oi, 2) if oi > 0 else 0)
    sweep = "SWEEP" if meta.get("has_sweep") or meta.get("is_sweep") else "BLOCK"
    exec_side = meta.get("execution_side", "N/A")
    exec_pct = f"{meta.get('execution_side_percent', '')}%"

    line1 = f"🚨🚨🚨 {ticker} ${strike} {expiry} {option_type} | {side} | Conviction: {conviction}"
    line2 = f"Prem:${premium:,} | Vol:{vol} | Avg Fill:${avg_fill} | OI:{oi} | Vol/OI:{vol_oi} | {sweep} | {exec_side} {exec_pct}"

    full_alert = f"~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~\n🚨🚨🚨\n\n{line1}\n\n{line2}"
    if explanation and explanation.strip():
        full_alert += f"\n\n→ {explanation.strip()}"
    full_alert += "\n\n~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~\n"

    return full_alert

def is_market_open():
    now = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=4)
    if now.weekday() >= 5:
        return False
    hour = now.hour + now.minute / 60.0
    return 9.5 <= hour <= 16.0

@tasks.loop(seconds=45)
async def auto_alert_scanner():
    if not is_market_open():
        return

    channel = bot.get_channel(ALERT_CHANNEL_ID)
    if not channel:
        return

    print("→ === CUSTOM ALERT SCAN START ===")
    triggered = await get_custom_alerts()

    if not triggered:
        print("  No new trades this cycle")
        print("→ === CUSTOM ALERT SCAN COMPLETED ===\n")
        return

    for trade in triggered:
        meta = {k.replace("meta_", ""): v for k, v in trade.items() if k.startswith("meta_")}
        underlying_ticker = meta.get("underlying_symbol") or clean_ticker(trade.get("symbol", ""))
        if underlying_ticker:
            move = await get_underlying_move(underlying_ticker)
            trade["underlying_move_percent"] = move

    try:
        context = json.dumps(triggered, default=str, indent=2)

        system_prompt = """You are a sharp, conservative options flow analyst. 
Be extremely selective.

STRICT NO-CHASING RULE:
- If underlying up > 3% today, do not chase bullish flow (calls). Larger moves = stricter.
- If underlying down > 3% today, do not chase bearish flow (puts). Larger moves = stricter.
- No chasing rule can be ignored ONLY if the signal is extremely high elsewhere.

VERY STRICT ETF RULES:
- Major Index ETFs (SPY, QQQ, etc.): Extremely high bar. Look for either super sudden high volume spikes or longer dated high conviction/extremely high premium on top of higher strictness with other rules.

Other Rules:
- Ignore deep ITM (more than 5% ITM). Prefer OTM contracts. ITM/ATM contracts must be very high on other signals for alerts.
- Minimum volume: at least 1000 contracts for most alerts (higher for low-OI contracts)
- Larger volume + larger premium + higher vol/OI = higher conviction
- Prefer new opening positions (volume > OI)
- Prefer directional conviction

For each alert you choose, assign Conviction: High / Medium / Exceptional and write a short but informative 1-2 sentence explanation that includes:
- Why it flagged (volume spike, sweep, opening positions, etc.)
- Possible context (hedging, institutional positioning, insider knowledge, etc.)
- Trade implication (quick trade vs longer hold)

Output exactly in this format:

~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
🚨🚨🚨

SYMBOL $STRIKE EXPIRY TYPE | SIDE | Conviction: XXX

Prem:$PREMIUM | Vol:VOL | Avg Fill:$AVG | OI:OI | Vol/OI:RATIO | SWEEP/BLOCK | EXEC_SIDE XX%

→ Short explanation here
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

If nothing qualifies, output nothing."""

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                "https://api.x.ai/v1/chat/completions",
                headers={"Authorization": f"Bearer {XAI_API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": "grok-4-fast-reasoning",
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": f"Here are the latest custom alert trades (with underlying move % added):\n{context}\n\nApply all rules strictly. Output only valid alerts in the exact format, or nothing."}
                    ],
                    "temperature": 0.25,
                    "max_tokens": 2000
                }
            )

            if resp.status_code != 200:
                return

            data = resp.json()
            ai_reply = data["choices"][0]["message"]["content"].strip()

            if ai_reply and len(ai_reply) > 20 and "nothing" not in ai_reply.lower():
                alerts = [block.strip() for block in ai_reply.split("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~") if "🚨" in block]
                for alert in alerts:
                    clean_alert = alert.strip()
                    if clean_alert:
                        alert_key = re.sub(r'\s+', '', clean_alert.lower())[:120]
                        if alert_key in seen_alerts:
                            continue
                        seen_alerts.add(alert_key)
                        await channel.send(clean_alert)
                        await channel.send(" ")  
                        print(f"  ✅ AI ALERT SENT")
                        await asyncio.sleep(1.5)
            else:
                print("  AI decided no high-conviction alerts this cycle")

    except Exception as e:
        print(f"  AI decision error: {e}")

    print("→ === CUSTOM ALERT SCAN COMPLETED ===\n")

# Conversational mode
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

            for trade in custom_alerts:
                meta = {k.replace("meta_", ""): v for k, v in trade.items() if k.startswith("meta_")}
                underlying_ticker = meta.get("underlying_symbol") or clean_ticker(trade.get("symbol", ""))
                if underlying_ticker:
                    move = await get_underlying_move(underlying_ticker)
                    trade["underlying_move_percent"] = move

            context = f"General recent flow:\n{json.dumps(general_flow, default=str, indent=2)}\n\nTriggered custom alerts (with underlying move %):\n{json.dumps(custom_alerts, default=str, indent=2)}"

            system_prompt = """You are a balanced, evidence-based smart-money options flow analyst.

Guardrails:
- Larger volume + higher vol/OI = higher conviction
- Always consider underlying price move and market direction
- For ETFs: extra scrutiny

Be objective. Cite specific numbers."""

            full_query = f"{query}\n\n{context}\n\nProvide a concise, evidence-based analysis. Highlight only high-conviction setups with specific numbers."

            async with httpx.AsyncClient(timeout=50.0) as client:
                resp = await client.post(
                    "https://api.x.ai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {XAI_API_KEY}", "Content-Type": "application/json"},
                    json={
                        "model": "grok-4-fast-reasoning",
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": full_query}
                        ],
                        "temperature": 0.35,
                        "max_tokens": 1400
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