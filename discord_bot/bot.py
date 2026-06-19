import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
import logging
import urllib.parse
import discord
from discord.ext import commands
from discord import app_commands
from typing import Optional, Dict
import httpx
from config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)

API_BASE = f"http://localhost:{settings.api_port}"
API_TIMEOUT = 120.0  # Qwen needs time — do not lower this


# ── Bot Setup ─────────────────────────────────────────────────────────────────

class ScoutBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="/", intents=intents)

    async def setup_hook(self):
        await self.tree.sync()
        logger.info("Slash commands synced")

    async def on_ready(self):
        logger.info(f"SCOUT online as {self.user} (ID: {self.user.id})")
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="startup ecosystems",
            )
        )


bot = ScoutBot()


# ── Helper ────────────────────────────────────────────────────────────────────

async def call_api(
    method: str,
    path: str,
    payload: Optional[dict] = None,
    params: Optional[Dict[str, str]] = None,
) -> dict:
    """Call the local FastAPI backend with proper param handling."""
    async with httpx.AsyncClient(timeout=API_TIMEOUT) as client:
        if method == "GET":
            resp = await client.get(f"{API_BASE}{path}", params=params)
        else:
            resp = await client.post(
                f"{API_BASE}{path}", json=payload or {}, params=params
            )
        resp.raise_for_status()
        return resp.json()


def build_scout_embed(query: str, data: dict) -> discord.Embed:
    """Format scout results as a Discord embed."""
    analysis = data.get("ai_analysis") or ""
    # Limit intro to maximum 2 sentences
    sentences = [s.strip() for s in analysis.split(".") if s.strip()]
    intro = ". ".join(sentences[:2]) + ("." if len(sentences) >= 2 else "")

    embed = discord.Embed(
        title=f"Scout: {query[:100]}",
        description=intro[:500] or "No analysis available.",
        color=discord.Color.blue(),
    )
    embed.set_footer(text=f"Found {data.get('total_found', 0)} startups in database")

    for startup in data.get("startups", [])[:5]:
        name = startup.get("name", "Unknown")
        city = startup.get("city") or ""
        country = startup.get("country") or ""
        place = f"{city}, {country}".strip(", ") or "N/A"
        founded = startup.get("founded_year") or "N/A"

        stage = startup.get("funding_stage") or "N/A"
        amount = startup.get("funding_amount")
        funding = f"{stage} ({amount})" if amount else stage

        contact = startup.get("contact_info") or "N/A"

        raw_desc = str(startup.get("description") or "")
        summary = raw_desc.split(".")[0].strip()
        if summary and not summary.endswith("."):
            summary += "."

        source_url = startup.get("source_url") or startup.get("website") or "N/A"
        published = (
            startup.get("published_date")
            or startup.get("published_at")
            or "N/A"
        )

        value = (
            f"**Place/Origin:** {place}\n"
            f"**Founded:** {founded}\n"
            f"**Funding:** {funding}\n"
            f"**Contact:** {contact}\n"
            f"**Summary:** {summary or 'N/A'}\n"
            f"**Source:** {source_url} | **Date:** {published}"
        )
        embed.add_field(name=f"**{name}**", value=value[:1024], inline=False)

    return embed


def build_match_embed(investor_name: str, data: dict) -> discord.Embed:
    """Format matchmaking results as a Discord embed."""
    embed = discord.Embed(
        title=f"Matches for {investor_name}",
        description=f"Found **{data.get('matches_found', 0)}** matching startups",
        color=discord.Color.green(),
    )

    for match in data.get("matches", [])[:5]:
        startup = match.get("startup", {})
        score = match.get("match_score", 0)
        rationale = match.get("match_rationale") or ""

        name = startup.get("name", "Unknown")
        location = f"{startup.get('city', '')}, {startup.get('country', '')}".strip(", ")
        stage = startup.get("funding_stage") or "N/A"
        desc = str(startup.get("description") or "")[:100]

        field_value = f"{desc}\n*{rationale[:150]}*" if rationale else desc
        embed.add_field(
            name=f"{name} | {location} | {stage} | {score:.0%} match",
            value=field_value or "No description",
            inline=False,
        )

    return embed


# ── Slash Commands ────────────────────────────────────────────────────────────

@bot.tree.command(name="scout", description="Scout startups matching your criteria")
@app_commands.describe(
    query="What you're looking for (e.g. 'AI robotics startups in Germany')",
    country="Optional country filter",
    industry="Optional industry filter",
    stage="Optional funding stage filter (Seed, Series A, etc.)",
)
async def scout_command(
    interaction: discord.Interaction,
    query: str,
    country: Optional[str] = None,
    industry: Optional[str] = None,
    stage: Optional[str] = None,
):
    await interaction.response.defer()

    try:
        data = await call_api("POST", "/scout/search", {
            "query": query,
            "country": country,
            "industry": industry,
            "funding_stage": stage,
            "limit": 10,
        })
        embed = build_scout_embed(query, data)
        await interaction.followup.send(embed=embed)

    except httpx.HTTPStatusError as exc:
        await interaction.followup.send(f"API error: {exc.response.status_code}")
    except Exception as exc:
        logger.error(f"[Bot] /scout failed: {exc}")
        await interaction.followup.send("Something went wrong. Check that the API server is running.")


@bot.tree.command(name="match", description="Find startups matching an investor profile")
@app_commands.describe(
    industries="Comma-separated industries (e.g. 'AI, Climate Tech, Fintech')",
    stages="Comma-separated stages (e.g. 'Seed, Series A')",
    regions="Comma-separated regions (e.g. 'Germany, Europe, DACH')",
    thesis="Optional investment thesis",
)
async def match_command(
    interaction: discord.Interaction,
    industries: str,
    stages: str,
    regions: str,
    thesis: Optional[str] = None,
):
    await interaction.response.defer()

    try:
        data = await call_api("POST", "/matchmaking/find-startups", {
            "name": str(interaction.user),
            "focus_industries": [i.strip() for i in industries.split(",")],
            "focus_stages": [s.strip() for s in stages.split(",")],
            "focus_regions": [r.strip() for r in regions.split(",")],
            "thesis": thesis,
            "limit": 10,
        })
        embed = build_match_embed(str(interaction.user), data)
        await interaction.followup.send(embed=embed)

    except Exception as exc:
        logger.error(f"[Bot] /match failed: {exc}")
        await interaction.followup.send("Matchmaking failed. Ensure the API server is running.")


@bot.tree.command(name="sector", description="Get a sector intelligence report")
@app_commands.describe(sector="Sector to analyze (e.g. 'AI', 'Fintech', 'Climate Tech')")
async def sector_command(interaction: discord.Interaction, sector: str):
    await interaction.response.defer()

    try:
        # Pass sector as a proper query param (httpx handles URL-encoding)
        data = await call_api("POST", "/scout/sector-report", params={"sector": sector})
        embed = discord.Embed(
            title=f"Sector Report: {sector}",
            description=(data.get("report") or "No report generated.")[:2000],
            color=discord.Color.gold(),
        )
        embed.set_footer(text=f"Based on {data.get('startups_analyzed', 0)} startups")
        await interaction.followup.send(embed=embed)

    except Exception as exc:
        logger.error(f"[Bot] /sector failed: {exc}")
        await interaction.followup.send("Report generation failed. Run /ingest first to populate the database.")


@bot.tree.command(name="add", description="Manually add a startup to the database")
@app_commands.describe(
    name="Startup name",
    description="What they do",
    industry="Industry/sector",
    country="Country",
    stage="Funding stage",
)
async def add_command(
    interaction: discord.Interaction,
    name: str,
    description: str,
    industry: Optional[str] = None,
    country: Optional[str] = None,
    stage: Optional[str] = None,
):
    await interaction.response.defer()

    try:
        data = await call_api("POST", "/scout/add-startup", {
            "name": name,
            "description": description,
            "industry": industry,
            "country": country,
            "funding_stage": stage,
            "source": "discord",
        })
        await interaction.followup.send(
            f"Added **{name}** to the database (ID: `{data.get('id', 'unknown')}`). "
            "AI analysis is running in the background."
        )

    except Exception as exc:
        logger.error(f"[Bot] /add failed: {exc}")
        await interaction.followup.send("Failed to add startup. Is the API server running?")


@bot.tree.command(name="ingest", description="Trigger data ingestion from startup sources")
@app_commands.describe(source="What to ingest: rss | accelerators | universities | all")
async def ingest_command(interaction: discord.Interaction, source: str = "rss"):
    await interaction.response.defer()

    endpoint_map = {
        "rss":           ("/ingestion/rss", {}),
        "accelerators":  ("/ingestion/scrape-accelerators", {}),
        "universities":  ("/ingestion/scrape-universities", {}),
        "newsletters":   ("/ingestion/newsletters", {}),
        "all":           ("/ingestion/run-all", {}),
    }

    path, payload = endpoint_map.get(source.lower(), ("/ingestion/rss", {}))

    try:
        data = await call_api("POST", path, payload)
        await interaction.followup.send(f"{data.get('message', 'Ingestion started.')}")
    except Exception as exc:
        logger.error(f"[Bot] /ingest failed: {exc}")
        await interaction.followup.send("Ingestion trigger failed.")


@bot.tree.command(name="status", description="Check system status and database size")
async def status_command(interaction: discord.Interaction):
    # 1. Instantly tell Discord "I am thinking..." to prevent the 3-second timeout crash
    await interaction.response.defer()
    
    try:
        data = await call_api("GET", "/health")
        embed = discord.Embed(
            title="SCOUT System Status",
            color=discord.Color.green() if data.get("status") == "ok" else discord.Color.red(),
        )
        embed.add_field(name="Status", value=data.get("status", "unknown").upper(), inline=True)
        embed.add_field(name="Startups in DB", value=str(data.get("startups_in_db", "N/A")), inline=True)

        # 2. Use followup.send() because we deferred the original response!

        await interaction.followup.send(embed=embed)
    except Exception as exc:
        # 3. Use followup.send() for the error state too
        await interaction.followup.send("API server not reachable. Run `start.ps1` first.")


# ── Entry Point ───────────────────────────────────────────────────────────────

def run():
    if not settings.discord_bot_token:
        logger.error("DISCORD_BOT_TOKEN is not set in .env — bot cannot start")
        return
    bot.run(settings.discord_bot_token, log_handler=None)


if __name__ == "__main__":
    run()
