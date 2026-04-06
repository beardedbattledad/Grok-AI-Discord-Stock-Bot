import os
import asyncio
import datetime
import json
from dotenv import load_dotenv
import discord
from discord.ext import commands, tasks
from openai import AsyncOpenAI   # Grok uses OpenAI-compatible client

load_dotenv()

# ====================== CONFIG ======================
XAI_API_KEY = os.getenv("XAI_API_KEY")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
ALERT_CHANNEL_ID = 1490357987154460862   # Your Grok alert channel

# Custom filters with intervals
CUSTOM_FILTERS = [
    {"name": "AI ETF",      "interval_seconds": 30},
    {"name": "AI Mega Cap", "interval_seconds": 45},
    {"name": "AI Mid Cap",  "interval_seconds": 120},
    {"name": "AI Small Cap","interval_seconds": 180},
]

TEST_MODE = False

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Grok client
GROK = AsyncOpenAI(
    api_key=XAI_API_KEY,
    base_url="https://api.x.ai/v1"
)

# ====================== YOUR STRICT RULES (Auto-alerts only) ======================
TRADING_RULES = """
Apply strictly for auto-alerts:
- Tier by market cap or ETF type.
- Major Index ETFs: ≥ $1M premium, relaxed chasing (|5%|).
- Leveraged/Inverse ETFs: ≥ $100K, flag as high-vol speculative.
- Hard filters: Aggressive sweep, new positions (vol > OI), no chasing (except ETFs), meets premium threshold.
- Prefer directional flow. Flag likely hedges.
Only alert if ALL hard filters pass with high conviction.
"""

# ====================== TOOLS ======================
TOOLS = [
    {
        "name": "get_flow_alerts",
        "description": "Get the most recent options flow activity. Default = last 200 trades.",
        "parameters": {
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
        "parameters": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 15}}
        }
    },
    {
        "name": "get_congress_trades",
        "description": "Get recent congressional trades.",
        "parameters": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 10}}
        }
    },
    {
        "name": "get_insider_trades",
        "description": "Get recent insider transactions.",
        "parameters": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 10}}
        }
    }
]

# ====================== EXECUTE TOOL ======================
async def execute_tool(tool_name: str, tool_input: dict):
    try:
        import httpx
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

            print(f"→ [GROK] Calling {url} | limit={limit} | since_hours={since_hours or 'None (most recent)'}")

            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(url, headers=headers, params=params)
                print(f"→ [GROK] Status: {resp.status_code}")

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

        # Other tools unchanged
        elif tool_name == "get_dark_pool_trades":
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{base_url}/api/darkpool/recent", headers=headers, params={"limit": tool_input.get("limit", 15)})
                data = resp.json() if resp.status_code == 200 else {"error": resp.text}
                return {"count": len(data) if isinstance(data, list) else 0, "samples": data[:6] if isinstance(data, list) else data}

        elif tool_name == "get_congress_trades":
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{base_url}/api/congress/recent-trades", headers=headers, params={"limit": tool_input.get("limit", 10)})
                return resp.json() if resp.status_code == 200 else {"error": resp.text}

        elif tool_name == "get_insider_trades":
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{base_url}/api/insider/transactions", headers=headers, params={"limit": tool_input.get("limit", 10)})
                return resp.json() if resp.status_code == 200 else {"error": resp.text}

        return {"error": f"Unknown tool: {tool_name}"}
    except Exception as e:
        print(f"Tool error: {str(e)}")
        return {"error": str(e)}

# ====================== SHORT ALERT FORMAT ======================
def format_short_alert(flow_item: dict) -> str:
    ticker = flow_item.get("ticker", "N/A")
    expiry = flow_item.get("expiration", "N/A")
    strike = flow_item.get("strike", "N/A")
    side = flow_item.get("side", "N/A").upper()
    premium = flow_item.get("premium", "N/A")
    vol_oi = flow_item.get("vol_oi_ratio", "N/A")
    execution = flow_item.get("execution_type", "N/A")

    return f"🚨 **{ticker}** {expiry} {strike} {side} | ${premium:,} | Vol/OI {vol_oi}x | {execution}"

# ====================== MARKET HOURS ======================
def is_market_open():
    if TEST_MODE:
        return True
    now = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=4)
    if now.weekday() >= 5:
        return False
    return (now.hour > 9 or (now.hour == 9 and now.minute >= 30)) and now.hour < 16

# ====================== AUTO ALERT SCANNER ======================
@tasks.loop(seconds=30)
async def auto_alert_scanner():
    if not is_market_open():
        return

    for f in CUSTOM_FILTERS:
        filter_name = f["name"]
        interval = f["interval_seconds"]

        last_run_attr = f"last_run_{filter_name.replace(' ', '_')}"
        if not hasattr(auto_alert_scanner, last_run_attr):
            setattr(auto_alert_scanner, last_run_attr, datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=interval + 10))

        last_run = getattr(auto_alert_scanner, last_run_attr)
        if (datetime.datetime.now(datetime.UTC) - last_run).total_seconds() < interval:
            continue

        try:
            tool_result = await execute_tool("get_flow_alerts", {})

            if "error" in tool_result or not tool_result.get("samples"):
                continue

            system_prompt = f"""You are scanning flow for high-conviction alerts ONLY.
Apply the user's strict Trading Rules exactly.
Return ONLY a short alert if something passes ALL hard filters.
If nothing meets criteria, return exactly: NO_ALERT"""

            response = await GROK.chat.completions.create(
                model="grok-beta",
                messages=[{"role": "user", "content": f"Filter: {filter_name}\nData: {json.dumps(tool_result)}"}],
                tools=TOOLS,
                temperature=0.0,
                max_tokens=400
            )

            reply = response.choices[0].message.content.strip()

            if "NO_ALERT" not in reply and reply:
                channel = bot.get_channel(ALERT_CHANNEL_ID)
                if channel:
                    await channel.send(format_short_alert(tool_result["samples"][0]))

        except Exception as e:
            print(f"Auto-alert error for {filter_name}: {e}")

        setattr(auto_alert_scanner, last_run_attr, datetime.datetime.now(datetime.UTC))

# ====================== CONVERSATIONAL MODE (same as your stable version) ======================
async def handle_tool_loop(response, messages):
    if not response.choices[0].message.tool_calls:
        return response.choices[0].message.content

    tool_results = []
    for tool_call in response.choices[0].message.tool_calls:
        tool_name = tool_call.function.name
        tool_input = json.loads(tool_call.function.arguments)

        print(f"Grok called tool: {tool_name} with input: {tool_input}")

        result = await execute_tool(tool_name, tool_input)

        tool_results.append({
            "tool_call_id": tool_call.id,
            "role": "tool",
            "content": json.dumps(result, default=str)[:15000]
        })

    messages.append(response.choices[0].message)
    messages.extend(tool_results)

    response = await GROK.chat.completions.create(
        model="grok-beta",
        messages=messages,
        tools=TOOLS,
        temperature=0.4,
        max_tokens=1000
    )

    return response.choices[0].message.content

async def send_long_message(channel, text):
    if len(text) <= 1900:
        await channel.send(text)
        return
    chunks = [text[i:i+1900] for i in range(0, len(text), 1900)]
    for i, chunk in enumerate(chunks, 1):
        prefix = f"**Part {i}/{len(chunks)}**\n" if len(chunks) > 1 else ""
        await channel.send(prefix + chunk)

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
            messages = [{"role": "user", "content": query}]

            response = await GROK.chat.completions.create(
                model="grok-beta",
                messages=messages,
                tools=TOOLS,
                temperature=0.4,
                max_tokens=1000
            )

            final_reply = await handle_tool_loop(response, messages)
            
            if final_reply:
                await send_long_message(message.channel, final_reply)
            else:
                await message.reply("No strong signals or data available at the moment.")

        except Exception as e:
            print(f"Error processing message: {e}")
            await message.reply("Sorry, I ran into an error while analyzing. Please try again.")

# ====================== COMMANDS ======================
@bot.command()
async def testmode(ctx, state: str = "on"):
    if ctx.author.id != 123456789012345678:  # Replace with your user ID if desired
        return
    global TEST_MODE
    TEST_MODE = state.lower() in ["on", "true", "1", "yes"]
    await ctx.send(f"Test Mode is now {'ON' if TEST_MODE else 'OFF'}")

@bot.command()
async def status(ctx):
    await ctx.send(f"Bot Online • Test Mode: {'ON' if TEST_MODE else 'OFF'} • Market Open: {is_market_open()}")

# ====================== STARTUP ======================
@bot.event
async def on_ready():
    print(f"✅ Grok Bot is online as {bot.user}")
    if not auto_alert_scanner.is_running():
        auto_alert_scanner.start()
        print("Auto-alert scanner started (Grok version)")

bot.run(DISCORD_TOKEN)