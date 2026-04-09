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
seen_trade_keys = set()

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
                move = data.get("change_percent") or data.get("today_change_percent") or 0.0
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

def get_trade_key(trade):
    symbol = str(trade.get("symbol", "")).strip()
    ticker, expiry, strike, option_type = parse_option_symbol(symbol)
    if ticker and strike is not None and option_type:
        return f"{ticker}_{strike}_{expiry}_{option_type}"
    return None

def get_execution_side(trade):
    meta = {k.replace("meta_", ""): v for k, v in trade.items() if k.startswith("meta_")}
    bid_vol = int(meta.get("bid_volume", 0))
    ask_vol = int(meta.get("ask_volume", 0))
    total_vol = bid_vol + ask_vol
    if total_vol > 0:
        ask_pct = (ask_vol / total_vol) * 100
        if ask_pct >= 70:
            return "Ask"
        elif ask_pct <= 30:
            return "Bid"
        else:
            return "Mixed"
    return "N/A"

def format_short_alert(trade, conviction="Medium", explanation=""):
    symbol = trade.get("symbol", "")
    ticker, expiry, strike, option_type = parse_option_symbol(symbol)
    if not ticker:
        ticker = clean_ticker(symbol)
    side = "BULLISH" if option_type == "CALL" else "BEARISH"
    
    meta = {k.replace("meta_", ""): v for k, v in trade.items() if k.startswith("meta_")}
    
    # FORCE CORRECT TOTAL PREMIUM - try multiple possible keys
    premium = 0
    if "clean_total_premium" in trade:
        premium = trade["clean_total_premium"]
    else:
        premium = meta.get("total_premium", 0)
        if premium == 0:
            premium = meta.get("premium", 0)
            if premium == 0:
                # Last resort: calculate from volume * avg fill (sometimes the only way)
                vol = meta.get("volume", 0)
                avg_fill = meta.get("avg_fill", meta.get("avg_fill_price", 0))
                premium = vol * avg_fill

    vol = meta.get("volume", meta.get("ask_volume", 0) + meta.get("bid_volume", 0))
    avg_fill = meta.get("avg_fill", meta.get("avg_fill_price", "N/A"))
    oi = meta.get("open_interest", 1)
    vol_oi = round(vol / oi, 2) if oi > 0 else 0
    sweep = "SWEEP" if meta.get("has_sweep") or meta.get("is_sweep") else "BLOCK"
    exec_side = get_execution_side(trade)
    exec_pct = f"{meta.get('execution_side_percent', 0)}%"

    line1 = f"🚨🚨🚨 {ticker} ${strike} {expiry} {option_type} | {side} | Conviction: {conviction}"
    line2 = f"Prem:${int(premium):,} | Vol:{vol} | Avg Fill:${avg_fill} | OI:{oi} | Vol/OI:{vol_oi} | {sweep} | {exec_side} {exec_pct}"

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

    # Pre-compute clean premium
    for trade in triggered:
        meta = {k.replace("meta_", ""): v for k, v in trade.items() if k.startswith("meta_")}
        total_premium = meta.get("total_premium", 0)
        if total_premium == 0:
            total_premium = meta.get("premium", 0)
        if total_premium == 0:
            vol = meta.get("volume", 0)
            avg_fill = meta.get("avg_fill", meta.get("avg_fill_price", 0))
            total_premium = vol * avg_fill
        trade["clean_total_premium"] = int(total_premium)

        underlying_ticker = meta.get("underlying_symbol") or clean_ticker(trade.get("symbol", ""))
        if underlying_ticker:
            move = await get_underlying_move(underlying_ticker)
            trade["underlying_move_percent"] = move

    try:
        context = json.dumps(triggered, default=str, indent=2)

        system_prompt = """You are a sharp, conservative options flow analyst. 
Be extremely selective.

STRICT RULES:
- Ignore deep ITM (more than 5% ITM)
- Minimum volume 1000 contracts
- Larger volume + higher vol/OI = higher conviction

Use the pre-computed "clean_total_premium" for Prem: (this is the real total dollar amount).

Output exactly in this format (nothing else):

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
                        {"role": "user", "content": f"Here are the latest custom alert trades (with clean_total_premium added):\n{context}\n\nApply all rules strictly. Output only valid alerts in the exact format, or nothing."}
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
                        trade_key = None
                        for t in triggered:
                            if str(t.get("symbol", "")).strip() in clean_alert:
                                trade_key = get_trade_key(t)
                                break
                        if trade_key and trade_key in seen_trade_keys:
                            continue
                        if trade_key:
                            seen_trade_keys.add(trade_key)

                        await channel.send(clean_alert)
                        await channel.send(" ")  
                        print(f"  ✅ AI ALERT SENT")
                        await asyncio.sleep(1.5)
            else:
                print("  AI decided no high-conviction alerts this cycle")

    except Exception as e:
        print(f"  AI decision error: {e}")

    print("→ === CUSTOM ALERT SCAN COMPLETED ===\n")

@bot.event
async def on_ready():
    print(f"✅ Grok Bot is online as {bot.user}")
    await load_alert_configs()
    auto_alert_scanner.start()

bot.run(DISCORD_TOKEN)