import os
import asyncio
import datetime
import json
from dotenv import load_dotenv
import discord
from discord.ext import commands
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

# ====================== TOOL DEFINITIONS ======================
TOOLS = [
    {
        "name": "get_flow_alerts",
        "description": "Get the most recent options flow activity. Default = last 200 trades (no time or premium filter unless asked).",
        "parameters": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Specific ticker like DVN (optional)"},
                "limit": {"type": "integer", "default": 200}
            }
        }
    }
]

# ====================== EXECUTE TOOL ======================
async def execute_tool(tool_name: str, tool_input: dict):
    try:
        headers = {"Authorization": f"Bearer {UW_API_KEY}"}
        base_url = "https://api.unusualwhales.com"

        if tool_name == "get_flow_alerts":
            ticker = tool_input.get("ticker")
            limit = min(tool_input.get("limit", 200), 200)

            if ticker:
                url = f"{base_url}/api/stock/{ticker.upper()}/flow-alerts"
            else:
                url = f"{base_url}/api/option-trades/flow-alerts"

            params = {"limit": limit}

            print(f"→ Calling {url} | limit={limit}")

            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(url, headers=headers, params=params)
                print(f"→ Status: {resp.status_code}")

                data = resp.json() if resp.status_code == 200 else {"error": resp.text}

                if isinstance(data, dict) and isinstance(data.get("data"), list):
                    results = data["data"]
                    return {
                        "count": len(results),
                        "samples": results[:100],
                        "ticker": ticker or "broad",
                        "note": f"Most recent {len(results)} trades"
                    }
                return data

        return {"error": "Unknown tool"}
    except Exception as e:
        print(f"Tool error: {str(e)}")
        return {"error": str(e)}

# ====================== HANDLE TOOL LOOP (for Grok) ======================
async def handle_tool_loop(response, messages):
    # For direct HTTP Grok, we simulate simple tool use by checking if the model wants to call a tool
    # But since Grok doesn't support tool calling in the same way as Claude in this setup, we manually call the tool if "flow" is in the query
    if "flow" in messages[-1]["content"].lower() or "unusual" in messages[-1]["content"].lower() or "options" in messages[-1]["content"].lower():
        print("Detected options flow request - calling tool")
        tool_result = await execute_tool("get_flow_alerts", {"limit": 200})
        tool_context = f"Here is the most recent options flow data:\n{json.dumps(tool_result, default=str, indent=2)}"
        messages.append({"role": "user", "content": tool_context})

        # Re-call Grok with the tool data
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.x.ai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {XAI_API_KEY}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": "grok-4-fast-reasoning",
                    "messages": messages,
                    "temperature": 0.4,
                    "max_tokens": 1000
                }
            )
            data = resp.json()
            return data["choices"][0]["message"]["content"]

    return response  # fallback

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
            messages = [{"role": "user", "content": query}]

            # First call to Grok to see if it needs data
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    "https://api.x.ai/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {XAI_API_KEY}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": "grok-4-fast-reasoning",
                        "messages": messages,
                        "temperature": 0.4,
                        "max_tokens": 1000
                    }
                )

                data = resp.json()
                initial_reply = data["choices"][0]["message"]["content"]

                final_reply = await handle_tool_loop(initial_reply, messages)

                await send_long_message(message.channel, final_reply or "No strong signals found.")

        except Exception as e:
            print(f"Error: {e}")
            await message.reply("Sorry, I ran into an error while analyzing.")

# ====================== SEND LONG MESSAGES (Fixed) ======================
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
    print(f"✅ Grok Bot is online as {bot.user} — Ready for DM tests and mentions!")

bot.run(DISCORD_TOKEN)