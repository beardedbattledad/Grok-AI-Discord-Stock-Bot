import os
import asyncio
import datetime
import json
from dotenv import load_dotenv
import discord
from discord.ext import commands
from openai import AsyncOpenAI   # Grok uses OpenAI-compatible SDK

load_dotenv()

# ====================== CONFIG ======================
XAI_API_KEY = os.getenv("XAI_API_KEY")          # ← Your Grok/xAI API key
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Grok client
GROK = AsyncOpenAI(
    api_key=XAI_API_KEY,
    base_url="https://api.x.ai/v1"
)

# ====================== TOOL DEFINITIONS (same as your stable version) ======================
TOOLS = [
    {
        "name": "get_flow_alerts",
        "description": "Get the most recent options flow activity. Default = last 200 trades (no premium or time filter unless asked).",
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

# ====================== EXECUTE TOOL (Same improved version you liked) ======================
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
                        "note": f"Most recent {len(results)} trades (no default time or premium filter)"
                    }
                return data

        # Keep your other tools unchanged
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

# ====================== OPENAI-STYLE TOOL LOOP FOR GROK ======================
async def handle_tool_loop(response, messages):
    # Grok uses OpenAI-style tool calling
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

    # Second call with tool results
    response = await GROK.chat.completions.create(
        model="grok-beta",
        messages=messages,
        tools=TOOLS,
        temperature=0.4,
        max_tokens=1000
    )

    return response.choices[0].message.content

# ====================== SEND LONG MESSAGES ======================
async def send_long_message(channel, text):
    if len(text) <= 1900:
        await channel.send(text)
        return
    chunks = [text[i:i+1900] for i in range(0, len(text), 1900)]
    for i, chunk in enumerate(chunks, 1):
        prefix = f"**Part {i}/{len(chunks)}**\n" if len(chunks) > 1 else ""
        await channel.send(prefix + chunk)

# ====================== ON MESSAGE ======================
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

@bot.event
async def on_ready():
    print(f"✅ Grok Bot is online as {bot.user} — Ready for DM tests and mentions!")

bot.run(DISCORD_TOKEN)
