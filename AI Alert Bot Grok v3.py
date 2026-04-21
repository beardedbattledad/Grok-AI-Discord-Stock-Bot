import os
import asyncio
import datetime
import json
import re
import logging
import copy
from dotenv import load_dotenv
import discord
from discord.ext import commands, tasks
import httpx

load_dotenv()

# Reduce noisy Discord gateway logs
logging.getLogger('discord.gateway').setLevel(logging.WARNING)
logging.getLogger('discord').setLevel(logging.WARNING)

# ====================== CONFIG ======================
XAI_API_KEY = os.getenv("XAI_API_KEY")
UW_API_KEY = os.getenv("UW_API_KEY")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

# Channel mapping by Unusual Whales custom alert name
ALERT_CHANNELS = {
    "AI ETF": 1490357895710376116,      # ETF Alerts
    "AI Mega Cap": 1495063589873123398, # Large Cap Alerts
    "AI Mid Cap": 1495063667924799660,  # Mid Cap Alerts
    "AI Small Cap": 1495063712212451509 # Small Cap Alerts
}
ALERT_CHANNEL_ID = 1490357987154460862   # Main fallback channel

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

CUSTOM_ALERT_NAMES = ["AI Mid Cap", "AI Small Cap", "AI ETF", "AI Mega Cap"]

MAJOR_INDEX_ETFS = {"SPY", "QQQ", "SOXX", "IWM", "DIA", "XLK", "XLF"}

alert_configs = {}
last_alert_time = None
underlying_move_cache = {}
seen_trade_keys = set()
gex_cache = {}  # Global GEX cache to prevent 429s
system_prompt_stage1 = """You are a sharp, conservative options flow analyst. 
Be extremely selective.

STRICT NO-CHASING RULE:
- If underlying up > 3% today, do not chase bullish flow (calls). Larger moves = stricter.
- If underlying down > 3% today, do not chase bearish flow (puts). Larger moves = stricter.
- No chasing rule can be ignored ONLY if the signal is extremely high elsewhere.

VERY STRICT ETF RULES:
- Major Index ETFs (SPY, QQQ, etc.): Extremely high bar. Look for either super sudden high volume spikes or longer dated high conviction/extremely high premium on top of higher strictness with other rules.

IV CHANGE AS ASCENDING FILL PROXY:
- Positive IV change (especially +3% or more) combined with heavy Ask-side volume, sweeps, or high vol/OI often signals aggressive buyers paying up (ascending fills / smart money lifting offers).
- Negative or flat IV with heavy volume is usually less directional or hedging.

PREMIUM & VOLUME CONVICTION:
- The higher the total premium, the higher the conviction (larger dollar amount spent = stronger signal).
- Larger positive IV change + higher premium + larger volume + higher vol/OI = significantly higher conviction.

Other Rules:    
- Ignore deep ITM (more than 5% ITM). Prefer OTM contracts. ITM/ATM contracts must be very high on other signals for alerts.
- Larger volume + larger premium + higher vol/OI = higher conviction
- Prefer new opening positions (volume > OI)
- Must have multiple signals that confirm good trade likelihood.
- Prefer directional conviction

For each alert you choose, assign Conviction: High / Medium / Exceptional and write a short but informative 1-2 sentence explanation.

Use the pre-computed "clean_total_premium" for the Prem: line.
Use whatever side the trade is on (either Bid or Ask) for the "EXEC_SIDE"

Output exactly in this format (nothing else):

~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
🚨🚨🚨🚨🚨🚨

SYMBOL $STRIKE EXPIRY TYPE | SIDE | Conviction: XXX

Prem:$PREMIUM | Vol:VOL | Avg Fill:$AVG | OI:OI | Vol/OI:RATIO | SWEEP/BLOCK | EXEC_SIDE XX%

→ Short explanation here
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

If nothing qualifies, output nothing."""

system_prompt_stage2 = """You are a sharp, conservative options flow analyst. 
Be extremely selective.

STRICT NO-CHASING RULE:
- If underlying up > 3% today, do not chase bullish flow (calls). Larger moves = stricter.
- If underlying down > 3% today, do not chase bearish flow (puts). Larger moves = stricter.
- No chasing rule can be ignored ONLY if the signal is extremely high elsewhere.

VERY STRICT ETF RULES:
- Major Index ETFs (SPY, QQQ, SOXX, etc.): Extremely high bar. Look for either super sudden high volume spikes or longer dated high conviction/extremely high premium on top of higher strictness with other rules.

IV CHANGE AS ASCENDING FILL PROXY:
- Positive IV change (especially +3% or more) combined with heavy Ask-side volume, sweeps, or high vol/OI often signals aggressive buyers paying up (ascending fills / smart money lifting offers).
- Negative or flat IV with heavy volume is usually less directional or hedging.

PREMIUM & VOLUME CONVICTION:
- The higher the total premium, the higher the conviction (larger dollar amount spent = stronger signal).
- Larger positive IV change + higher premium + larger volume + higher vol/OI = significantly higher conviction.

DARK POOL RULES:
- Prints above current price = resistance / sell pressure
- Prints below current price = support / buy pressure
- Prints near current price = unknown until significant move

GEX RULES:
- Positive GEX near strike = dealers sell into strength, buy into weakness
- Negative GEX near strike = dealers chase the move
- Closer to current price = stronger effect

Other Rules:    
- Ignore deep ITM (more than 5% ITM). Prefer OTM contracts. ITM/ATM contracts must be very high on other signals for alerts.
- The higher the volume the better the trade.
- Larger volume + larger premium + higher vol/OI = higher conviction
- Prefer new opening positions (volume > OI)
- Must have multiple signals that confirm good trade likelihood.
- Prefer directional conviction

For each alert you choose, assign Conviction: High / Medium / Exceptional and write a short but informative 1-2 sentence explanation that includes:
- Why it flagged (volume spike, sweep, opening positions, IV spike, etc.)
- Possible context (hedging, institutional positioning, insider knowledge, etc.)
- Trade implication (quick trade vs longer hold)

Use the pre-computed "clean_total_premium" for the Prem: line.
Use whatever side the trade is on (either Bid or Ask) for the "EXEC_SIDE"
For "SWEEP/BLOCK" indicate if the trade was a sweep or a block trade. Typically a block if it is not a sweep.

Output exactly in this format (nothing else):

~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
🚨🚨🚨🚨🚨🚨

SYMBOL $STRIKE EXPIRY TYPE | SIDE | Conviction: XXX

Prem:$PREMIUM | Vol:VOL | Avg Fill:$AVG | OI:OI | Vol/OI:RATIO | SWEEP/BLOCK | EXEC_SIDE XX%

→ Short explanation here
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

If nothing qualifies after full enrichment, output nothing."""

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

async def get_flow_alerts(limit=200, ticker=None):
    """Fallback for conversational mode"""
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
            if resp.status_code == 200:
                data = resp.json()
                return data.get("data", [])[:limit]
            return []
    except Exception as e:
        print(f"Flow alerts fetch error: {e}")
        return []

async def get_dark_pool_trades(ticker=None, limit=300):
    try:
        headers = {"Authorization": f"Bearer {UW_API_KEY}"}
        base_url = "https://api.unusualwhales.com"
        if ticker:
            url = f"{base_url}/api/darkpool/{ticker.upper()}"
        else:
            url = f"{base_url}/api/darkpool/recent"
        params = {"limit": limit}
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(url, headers=headers, params=params)
            print(f"→ Dark Pool API Status: {resp.status_code}")
            if resp.status_code == 200:
                data = resp.json()
                return data.get("data", [])[:limit]
            return []
    except Exception as e:
        print(f"Dark pool fetch error: {e}")
        return []

async def get_gex_by_strike(ticker: str):
    try:
        headers = {"Authorization": f"Bearer {UW_API_KEY}"}
        url = f"https://api.unusualwhales.com/api/stock/{ticker.upper()}/greek-exposure"
        
        print(f"→ GEX API call for {ticker.upper()} → {url}")
        
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers=headers)
            print(f"→ GEX API Status: {resp.status_code} for {ticker.upper()}")
            
            if resp.status_code == 200:
                data = resp.json()
                # Return the raw data (it usually contains gamma exposure per strike etc.)
                return data.get("data", data) if isinstance(data, dict) else data
            else:
                print(f"  GEX API error response: {resp.text[:300]}")
                return []
                
    except Exception as e:
        print(f"  GEX API exception for {ticker}: {e}")
        return []

def parse_option_symbol(symbol):
    symbol = str(symbol).strip().upper()
    if not symbol:
        return None, None, None, None

    match = re.match(r'^([A-Z]+)(\d{6})([CP])(\d{8})', symbol)
    if match:
        ticker = match.group(1)
        date_str = match.group(2)
        opt_type = "CALL" if match.group(3) == "C" else "PUT"
        strike_str = match.group(4)

        if len(strike_str) == 8:
            digits = [int(d) for d in strike_str]
            strike = (digits[0] * 10000 +
                      digits[1] * 1000 +
                      digits[2] * 100 +
                      digits[3] * 10 +
                      digits[4] * 1 +
                      digits[5] * 0.1 +
                      digits[6] * 0.01 +
                      digits[7] * 0.001)
        else:
            strike = None

        try:
            expiry_date = datetime.datetime.strptime(date_str, "%y%m%d").date()
            expiry = expiry_date.strftime("%m/%d/%y")
        except:
            expiry = "UNKNOWN"

        print(f"  DEBUG PARSE - Symbol: {symbol} → Ticker: {ticker}, Expiry: {expiry}, Strike: {strike}, Type: {opt_type}")

        return ticker, expiry, strike, opt_type

    print(f"  DEBUG PARSE FAILED - Symbol: {symbol}")
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

def get_iv_change(trade):
    meta = {k.replace("meta_", ""): v for k, v in trade.items() if k.startswith("meta_")}
    iv_change = meta.get("iv_change") or meta.get("iv_percent_change") or meta.get("delta_iv") or 0.0
    return float(iv_change)

def calculate_total_premium(trade):
    trade_copy = copy.deepcopy(trade)
    flat = {}
    for k, v in trade_copy.items():
        flat[k.lower()] = v
        if isinstance(v, dict):
            for sub_k, sub_v in v.items():
                flat[sub_k.lower()] = sub_v

    ticker = clean_ticker(trade_copy.get("symbol", ""))

    vol = 0
    for key in flat:
        if any(x in key for x in ["volume", "vol"]):
            try:
                vol = int(flat[key])
                if vol > 0:
                    break
            except:
                pass

    avg_fill = 0.0
    for key in flat:
        if any(x in key for x in ["avg_fill", "avgfill", "average_fill", "fill_price", "price"]):
            try:
                avg_fill = float(flat[key])
                if avg_fill > 0:
                    break
            except:
                pass

    premium = vol * avg_fill * 100

    if premium == 0:
        for key in flat:
            if "premium" in key or "total_premium" in key:
                try:
                    premium = float(flat[key])
                    break
                except:
                    pass

    print(f"  DEBUG PREMIUM - Ticker: {ticker} | vol={vol} | avg_fill={avg_fill:.2f} | Calculated: ${int(round(premium)):,}")

    return int(round(premium))

def format_short_alert(trade, conviction="Medium", explanation=""):
    symbol = trade.get("symbol", "")
    ticker, expiry, strike, option_type = parse_option_symbol(symbol)
    if not ticker:
        ticker = clean_ticker(symbol)
    side = "BULLISH" if option_type == "CALL" else "BEARISH"
    
    premium = trade.get("clean_total_premium", 0)
    meta = {k.replace("meta_", ""): v for k, v in trade.items() if k.startswith("meta_")}
    
    vol = meta.get("volume", meta.get("ask_volume", 0) + meta.get("bid_volume", 0))
    avg_fill = meta.get("avg_fill", meta.get("avg_fill_price", "N/A"))
    oi = meta.get("open_interest", 1)
    vol_oi = round(vol / oi, 2) if oi > 0 else 0
    sweep = "SWEEP" if meta.get("has_sweep") or meta.get("is_sweep") else "BLOCK"
    exec_side = get_execution_side(trade)
    exec_pct = f"{meta.get('execution_side_percent', 0)}%"

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

@tasks.loop(seconds=45)  # Slowed down to help with rate limits
async def auto_alert_scanner():
    print("→ === CUSTOM ALERT SCAN START ===")

    if not is_market_open():
        print("  Market closed - skipping scan")
        print("→ === CUSTOM ALERT SCAN COMPLETED ===\n")
        return

    channel = bot.get_channel(ALERT_CHANNEL_ID)
    if not channel:
        print("  Alert channel not found")
        print("→ === CUSTOM ALERT SCAN COMPLETED ===\n")
        return

    triggered = await get_custom_alerts()
    if not triggered:
        print("  No new trades this cycle")
        print("→ === CUSTOM ALERT SCAN COMPLETED ===\n")
        return

    # Deduplication
    unique_trades = []
    for trade in triggered:
        key = get_trade_key(trade)
        if key and key not in seen_trade_keys:
            seen_trade_keys.add(key)
            unique_trades.append(trade)

    if not unique_trades:
        print("  All trades were duplicates")
        print("→ === CUSTOM ALERT SCAN COMPLETED ===\n")
        return

    print(f"  Stage 1: Quick filter on {len(unique_trades)} basic trades")

    # Pre-compute basic fields
    for trade in unique_trades:
        ticker = clean_ticker(trade.get("symbol", ""))
        premium = calculate_total_premium(trade)
        trade["clean_total_premium"] = premium
        trade["iv_change"] = get_iv_change(trade)

    context_basic = json.dumps(unique_trades, default=str, indent=2)

    # === STAGE 1: Basic filter only ===
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                "https://api.x.ai/v1/chat/completions",
                headers={"Authorization": f"Bearer {XAI_API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": "grok-4-fast-reasoning",
                    "messages": [
                        {"role": "system", "content": system_prompt_stage1},  # your original prompt here
                        {"role": "user", "content": f"Here are the latest basic custom alert trades:\n{context_basic}\n\nApply all rules strictly. Output ONLY the alerts you like in the exact format, or nothing."}
                    ],
                    "temperature": 0.25,
                    "max_tokens": 2000
                }
            )
            data = resp.json()
            ai_reply_stage1 = data["choices"][0]["message"]["content"].strip()
            print(f"  Stage 1 Grok reply length: {len(ai_reply_stage1)}")
    except Exception as e:
        print(f"  Stage 1 error: {e}")
        print("→ === CUSTOM ALERT SCAN COMPLETED ===\n")
        return

    # Extract selected alerts from Stage 1
    selected_alerts = [block.strip() for block in ai_reply_stage1.split("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~") if "🚨" in block]
    print(f"  Stage 1 selected {len(selected_alerts)} high-conviction alerts")

    if not selected_alerts:
        print("  Stage 1: No high-conviction alerts")
        print("→ === CUSTOM ALERT SCAN COMPLETED ===\n")
        return

        # === STAGE 2: Enrich ONLY the selected alerts (with full logging) ===
    print(f"  Stage 2: Enriching {len(selected_alerts)} selected alerts with Dark Pool + GEX")

    for i, alert_text in enumerate(selected_alerts):
        print(f"    Stage 2 Alert #{i+1} text preview: {alert_text[:150]}...")

        # Find the matching trade using ticker (much more reliable than full symbol)
        matched = False
        for trade in unique_trades:
            ticker = clean_ticker(trade.get("symbol", ""))
            if ticker and ticker in alert_text.upper():
                print(f"    ✅ Matched trade for ticker {ticker}")
                matched = True

                # Enrich with Dark Pool + GEX
                dark_pools = await get_dark_pool_trades(ticker, limit=300)
                gex_data = await get_gex_by_strike(ticker)
                trade["dark_pool_trades"] = dark_pools[:20]
                trade["gex_by_strike"] = gex_data

                # Route to correct channel
                alert_name = trade.get("name") or trade.get("alert_name") or ""
                target_channel_id = ALERT_CHANNELS.get(alert_name.strip(), ALERT_CHANNEL_ID)
                target_channel = bot.get_channel(target_channel_id) or channel

                # Full context for Grok
                context_full = json.dumps([trade], default=str, indent=2)
                print(f"    Stage 2: Sending enriched data to Grok for {ticker} (context length: {len(context_full)} chars)")

                # Final Grok call
                try:
                    async with httpx.AsyncClient(timeout=60.0) as client:
                        resp = await client.post(
                            "https://api.x.ai/v1/chat/completions",
                            headers={"Authorization": f"Bearer {XAI_API_KEY}", "Content-Type": "application/json"},
                            json={
                                "model": "grok-4-fast-reasoning",
                                "messages": [
                                    {"role": "system", "content": system_prompt_stage2},
                                    {"role": "user", "content": f"Here is the selected high-conviction alert with full Dark Pool + GEX context:\n{context_full}\n\nRe-evaluate and output in the exact format if it still qualifies."}
                                ],
                                "temperature": 0.25,
                                "max_tokens": 2000
                            }
                        )

                        data = resp.json()
                        ai_reply = data["choices"][0]["message"]["content"].strip()

                        print(f"    Stage 2 Grok reply length for {ticker}: {len(ai_reply)} | Starts with: {ai_reply[:200]}...")

                        alerts = [block.strip() for block in ai_reply.split("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~") if "🚨" in block]
                        print(f"    Stage 2 extracted {len(alerts)} formatted alerts")

                        for clean_alert in alerts:
                            if clean_alert:
                                await target_channel.send(clean_alert)
                                await target_channel.send(" ")
                                print(f"    ✅ FINAL AI ALERT SENT for {ticker}")
                                await asyncio.sleep(1.5)

                        # Fallback: if Grok didn't use our format, still show us what it said
                        if not alerts and ai_reply:
                            await target_channel.send("**Raw Stage 2 reply (debug):**")
                            await target_channel.send(ai_reply[:1900])

                except Exception as e:
                    print(f"    Stage 2 EXCEPTION for {ticker}: {e}")
                break   # We found the matching trade, move to next alert

        if not matched:
            print(f"    ❌ No trade matched for alert text (ticker not found)")

    print("→ === CUSTOM ALERT SCAN COMPLETED ===\n")

# Conversational mode (unchanged except for the enrichment block you already had)
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if bot.user.mentioned_in(message) or isinstance(message.channel, discord.DMChannel):
        print(f"→ Received message from {message.author}: {message.content[:100]}...")
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

            # Enrich conversational mode with Dark Pool + GEX
            if ticker:
                dark_pools = await get_dark_pool_trades(ticker, limit=300)
                gex_data = await get_gex_by_strike(ticker)
                context_extra = f"\nRecent Dark Pool prints: {json.dumps(dark_pools[:15], default=str)}\nGEX by strike: {json.dumps(gex_data[:20], default=str)}"
            else:
                context_extra = ""

            context = f"General recent flow:\n{json.dumps(general_flow, default=str, indent=2)}\n\nTriggered custom alerts (with underlying move %):\n{json.dumps(custom_alerts, default=str, indent=2)}{context_extra}"

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