import os
import asyncio
import datetime
import json
from dotenv import load_dotenv
import discord
from discord.ext import commands, tasks
from xai_sdk import AsyncClient   # Official xAI SDK

load_dotenv()

# ====================== CONVERSATIONAL MODE (Fixed for xAI SDK) ======================
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
            # Correct xAI SDK usage
            response = await xai_client.chat.completions.create(
                model="grok-beta",
                messages=[{"role": "user", "content": query}],
                temperature=0.4,
                max_tokens=1000
            )

            final_reply = response.choices[0].message.content
            await message.channel.send(final_reply or "No strong signals found.")

        except Exception as e:
            print(f"Error: {e}")
            await message.reply("Sorry, I ran into an error while analyzing.")

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

# ====================== EXECUTE TOOL ======================
async def execute_tool(tool_name: str, tool_input: dict):
    try:
        import httpx
        headers = {"Authorization": f"Bearer {os.getenv('UW_API_KEY')}"}
        base_url = "https://api.unusualwhales.com"

        if tool_name == "get_flow_alerts":
            ticker = tool_input.get("ticker")
            limit = min(tool_input.get("limit", 200), 200)

            if ticker:
                url = f"{base_url}/api/stock/{ticker.upper()}/flow-alerts"
            else:
                url = f"{base_url}/api/option-trades/flow-alerts"

            params = {"limit": limit}

            print(f"→ [GROK] Calling {url} | limit={limit}")

            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(url, headers=headers, params=params)
                print(f"→ [GROK] Status: {resp.status_code}")

                data = resp.json() if resp.status_code == 200 else {"error": resp.text}

                if isinstance(data, dict) and isinstance(data.get("data"), list):
                    results = data["data"]
                    return {
                        "count": len(results),
                        "samples": results[:100],
                        "ticker": ticker or "broad"
                    }
                return data
        return {"error": "Unknown tool"}
    except Exception as e:
        print(f"Tool error: {str(e)}")
        return {"error": str(e)}

# ====================== CONVERSATIONAL MODE (Simple for Grok) ======================
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
            response = await xai_client.chat.completions.create(
                model="grok-beta",
                messages=[{"role": "user", "content": query}],
                temperature=0.4,
                max_tokens=1000
            )

            await message.channel.send(response.choices[0].message.content or "No strong signals found.")

        except Exception as e:
            print(f"Error: {e}")
            await message.reply("Sorry, I ran into an error while analyzing.")

@bot.event
async def on_ready():
    print(f"✅ Grok Bot is online as {bot.user}")

bot.run(DISCORD_TOKEN)