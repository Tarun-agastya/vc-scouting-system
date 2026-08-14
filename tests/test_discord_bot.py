"""
Discord bot (discord_bot/bot.py) — pure-logic + error-handling tests, no real
Discord gateway connection or live API server.
"""
import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

from discord_bot import bot


def test_build_scout_embed_limits_intro_to_two_sentences():
    data = {
        "ai_analysis": "First sentence. Second sentence. Third sentence should be dropped.",
        "total_found": 3,
        "startups": [],
    }
    embed = bot.build_scout_embed("AI robotics", data)
    assert embed.description == "First sentence. Second sentence."


def test_build_match_embed_formats_score_as_percent():
    data = {
        "matches_found": 1,
        "matches": [
            {
                "startup": {"name": "Acme", "city": "Berlin", "country": "Germany",
                            "funding_stage": "Seed", "description": "Does things."},
                "match_score": 0.87,
                "match_rationale": "Strong industry fit.",
            }
        ],
    }
    embed = bot.build_match_embed("Investor X", data)
    assert "87%" in embed.fields[0].name


def test_status_command_logs_exception_on_api_failure(monkeypatch, caplog):
    """
    Bug fix: status_command's except-block called interaction.followup.send()
    on failure but never logged the exception, unlike every other slash
    command's handler (/scout, /match, /sector, /add, /ingest all call
    logger.error(...)). A real failure (API server down, bad response, etc.)
    was silently swallowed with nothing in the logs to diagnose it — this
    locks in that /status now logs the same way its siblings do.
    """
    async def _boom(*a, **kw):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(bot, "call_api", _boom)

    interaction = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    with caplog.at_level(logging.ERROR, logger="discord_bot.bot"):
        asyncio.run(bot.status_command.callback(interaction))

    interaction.followup.send.assert_awaited_once()
    assert any("/status failed" in r.message for r in caplog.records)
