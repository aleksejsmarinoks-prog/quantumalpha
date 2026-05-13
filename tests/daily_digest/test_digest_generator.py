"""DigestGenerator tests — mocked Anthropic client, no network."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.daily_digest.digest_generator import DailyDigestGenerator, DigestConfig


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

class TestConstruction:

    def test_requires_api_key(self, tmp_path):
        with pytest.raises(ValueError):
            DailyDigestGenerator(
                anthropic_api_key="",
                logs_path=tmp_path / "qa.log",
                equity_db_path=tmp_path / "equity.db",
                funding_db_path=tmp_path / "funding.db",
            )

    def test_accepts_paths_as_strings(self, tmp_path, mock_anthropic_client):
        gen = DailyDigestGenerator(
            anthropic_api_key="sk-fake",
            logs_path=str(tmp_path / "qa.log"),
            equity_db_path=str(tmp_path / "equity.db"),
            funding_db_path=str(tmp_path / "funding.db"),
            anthropic_client=mock_anthropic_client,
        )
        assert isinstance(gen.logs_path, Path)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestHappyPath:

    async def test_generates_digest_with_mock_claude(
        self, temp_log_file, temp_equity_db, temp_funding_db,
        calendar_provider_sample, state_provider_sample,
        mock_anthropic_client, utc_now,
    ):
        gen = DailyDigestGenerator(
            anthropic_api_key="sk-fake",
            logs_path=temp_log_file,
            equity_db_path=temp_equity_db,
            funding_db_path=temp_funding_db,
            bot_state_provider=state_provider_sample,
            calendar_provider=calendar_provider_sample,
            anthropic_client=mock_anthropic_client,
            include_trade_trigger=False,  # disable systemctl probe in tests
        )
        digest = await gen.generate_digest()

        assert "🌅" in digest
        assert "QuantumAlpha Daily Digest" in digest
        assert "📊" in digest
        # Mock returns canned response — we check it came through
        assert "Trades opened" in digest
        # Anthropic was called exactly once
        assert mock_anthropic_client.messages.create.call_count == 1

    async def test_aggregates_all_data_sources(
        self, temp_log_file, temp_equity_db, temp_funding_db,
        calendar_provider_sample, state_provider_sample,
        mock_anthropic_client,
    ):
        gen = DailyDigestGenerator(
            anthropic_api_key="sk-fake",
            logs_path=temp_log_file,
            equity_db_path=temp_equity_db,
            funding_db_path=temp_funding_db,
            bot_state_provider=state_provider_sample,
            calendar_provider=calendar_provider_sample,
            anthropic_client=mock_anthropic_client,
            include_trade_trigger=False,
        )
        data = gen._aggregate_all()
        assert "log_events" in data
        assert "equity_changes" in data
        assert "funding_rates" in data
        assert "calendar_today" in data
        assert "bot_health" in data
        # Aggregation happened — exact values depend on real clock vs fixture
        # so we check structural correctness only
        assert data["equity_changes"]["snapshot_count"] >= 0
        assert isinstance(data["funding_rates"], dict)
        assert isinstance(data["bot_health"]["strategies"], dict)

    async def test_prompt_contains_aggregated_data(
        self, temp_log_file, temp_equity_db, temp_funding_db,
        mock_anthropic_client,
    ):
        gen = DailyDigestGenerator(
            anthropic_api_key="sk-fake",
            logs_path=temp_log_file,
            equity_db_path=temp_equity_db,
            funding_db_path=temp_funding_db,
            anthropic_client=mock_anthropic_client,
            include_trade_trigger=False,
        )
        await gen.generate_digest()
        call_args = mock_anthropic_client.messages.create.call_args
        prompt = call_args.kwargs["messages"][0]["content"]
        # The aggregated data should be embedded in the prompt
        assert "log_events" in prompt
        assert "equity_changes" in prompt
        assert "ETHUSDT" in prompt  # funding rates symbol

    async def test_uses_configured_model(
        self, temp_log_file, temp_equity_db, temp_funding_db,
        mock_anthropic_client,
    ):
        config = DigestConfig(model="claude-opus-4-7")
        gen = DailyDigestGenerator(
            anthropic_api_key="sk-fake",
            logs_path=temp_log_file,
            equity_db_path=temp_equity_db,
            funding_db_path=temp_funding_db,
            config=config,
            anthropic_client=mock_anthropic_client,
            include_trade_trigger=False,
        )
        await gen.generate_digest()
        call_args = mock_anthropic_client.messages.create.call_args
        assert call_args.kwargs["model"] == "claude-opus-4-7"


# ---------------------------------------------------------------------------
# Retry & failure handling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestRetryAndFailure:

    async def test_retries_on_transient_failure(
        self, temp_log_file, temp_equity_db, temp_funding_db,
    ):
        # Mock that fails twice then succeeds
        ok_response = MagicMock()
        ok_response.content = [MagicMock(text=(
            "🌅 *QuantumAlpha Daily Digest* — 11 May 2026\n\nOK"
        ))]
        ok_response.usage = MagicMock(input_tokens=100, output_tokens=50)

        mock_client = MagicMock()
        mock_client.messages = MagicMock()
        mock_client.messages.create = AsyncMock(side_effect=[
            ConnectionError("transient 1"),
            ConnectionError("transient 2"),
            ok_response,
        ])

        gen = DailyDigestGenerator(
            anthropic_api_key="sk-fake",
            logs_path=temp_log_file,
            equity_db_path=temp_equity_db,
            funding_db_path=temp_funding_db,
            anthropic_client=mock_client,
            include_trade_trigger=False,
            # Speed up backoff for tests
            config=DigestConfig(max_retries=3),
        )

        # Patch asyncio.sleep so backoff doesn't slow tests
        with patch("bot.daily_digest.digest_generator.asyncio.sleep", new=AsyncMock()):
            digest = await gen.generate_digest()

        assert "QuantumAlpha Daily Digest" in digest
        assert mock_client.messages.create.call_count == 3

    async def test_falls_back_after_max_retries(
        self, temp_log_file, temp_equity_db, temp_funding_db,
        calendar_provider_sample, state_provider_sample,
    ):
        mock_client = MagicMock()
        mock_client.messages = MagicMock()
        mock_client.messages.create = AsyncMock(
            side_effect=ConnectionError("persistent network failure")
        )

        gen = DailyDigestGenerator(
            anthropic_api_key="sk-fake",
            logs_path=temp_log_file,
            equity_db_path=temp_equity_db,
            funding_db_path=temp_funding_db,
            bot_state_provider=state_provider_sample,
            calendar_provider=calendar_provider_sample,
            anthropic_client=mock_client,
            include_trade_trigger=False,
            config=DigestConfig(max_retries=2),
        )

        with patch("bot.daily_digest.digest_generator.asyncio.sleep", new=AsyncMock()):
            digest = await gen.generate_digest()

        # Must still produce a digest — fallback
        assert "QuantumAlpha Daily Digest" in digest
        assert "fallback" in digest.lower()
        assert mock_client.messages.create.call_count == 2

    async def test_auth_error_does_not_retry(
        self, temp_log_file, temp_equity_db, temp_funding_db,
    ):
        mock_client = MagicMock()
        mock_client.messages = MagicMock()
        mock_client.messages.create = AsyncMock(
            side_effect=Exception("HTTP 401 invalid_api_key")
        )

        gen = DailyDigestGenerator(
            anthropic_api_key="sk-bad",
            logs_path=temp_log_file,
            equity_db_path=temp_equity_db,
            funding_db_path=temp_funding_db,
            anthropic_client=mock_client,
            include_trade_trigger=False,
        )

        with patch("bot.daily_digest.digest_generator.asyncio.sleep", new=AsyncMock()):
            digest = await gen.generate_digest()

        # Should produce fallback after a single failed call (no retry on auth error)
        assert "fallback" in digest.lower()
        assert mock_client.messages.create.call_count == 1

    async def test_malformed_response_uses_fallback(
        self, temp_log_file, temp_equity_db, temp_funding_db,
    ):
        # Response missing expected header
        bad_response = MagicMock()
        bad_response.content = [MagicMock(text="Garbage response with no header")]
        bad_response.usage = MagicMock(input_tokens=100, output_tokens=50)

        mock_client = MagicMock()
        mock_client.messages = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=bad_response)

        gen = DailyDigestGenerator(
            anthropic_api_key="sk-fake",
            logs_path=temp_log_file,
            equity_db_path=temp_equity_db,
            funding_db_path=temp_funding_db,
            anthropic_client=mock_client,
            include_trade_trigger=False,
        )

        digest = await gen.generate_digest()
        assert "QuantumAlpha Daily Digest" in digest
        assert "fallback" in digest.lower()


# ---------------------------------------------------------------------------
# Fallback digest content
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestFallbackDigest:

    async def test_fallback_contains_all_sections(
        self, temp_log_file, temp_equity_db, temp_funding_db,
        calendar_provider_sample, state_provider_sample,
    ):
        mock_client = MagicMock()
        mock_client.messages = MagicMock()
        mock_client.messages.create = AsyncMock(side_effect=ConnectionError("down"))

        gen = DailyDigestGenerator(
            anthropic_api_key="sk-fake",
            logs_path=temp_log_file,
            equity_db_path=temp_equity_db,
            funding_db_path=temp_funding_db,
            bot_state_provider=state_provider_sample,
            calendar_provider=calendar_provider_sample,
            anthropic_client=mock_client,
            include_trade_trigger=False,
            config=DigestConfig(max_retries=1),
        )

        with patch("bot.daily_digest.digest_generator.asyncio.sleep", new=AsyncMock()):
            digest = await gen.generate_digest()

        # All required sections
        assert "📊" in digest                # Last 24h
        assert "📈" in digest                # Current state
        assert "🔮" in digest                # Catalysts
        assert "⚠️" in digest                # Issues
        assert "🎯" in digest                # Recommended action
        # Fallback marker
        assert "fallback" in digest.lower()

    async def test_fallback_handles_zero_equity_data(
        self, temp_log_file, tmp_path, calendar_provider_sample,
    ):
        mock_client = MagicMock()
        mock_client.messages = MagicMock()
        mock_client.messages.create = AsyncMock(side_effect=ConnectionError("down"))

        gen = DailyDigestGenerator(
            anthropic_api_key="sk-fake",
            logs_path=temp_log_file,
            equity_db_path=tmp_path / "missing_equity.db",
            funding_db_path=tmp_path / "missing_funding.db",
            calendar_provider=calendar_provider_sample,
            anthropic_client=mock_client,
            include_trade_trigger=False,
            config=DigestConfig(max_retries=1),
        )

        with patch("bot.daily_digest.digest_generator.asyncio.sleep", new=AsyncMock()):
            digest = await gen.generate_digest()

        # Should not crash, should not contain raw "None"
        assert "QuantumAlpha Daily Digest" in digest
        assert "None" not in digest, f"Raw 'None' leaked: {digest}"


# ---------------------------------------------------------------------------
# Cost tracking
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestCostTracking:

    async def test_cost_warning_logged_above_threshold(
        self, temp_log_file, temp_equity_db, temp_funding_db, caplog,
    ):
        # Force high token count → cost > $0.10
        big_response = MagicMock()
        big_response.content = [MagicMock(text=(
            "🌅 *QuantumAlpha Daily Digest* — Test\n\nBig digest"
        ))]
        # 50K input + 5K output ~ $0.15 + $0.075 = $0.225
        big_response.usage = MagicMock(input_tokens=50_000, output_tokens=5_000)

        mock_client = MagicMock()
        mock_client.messages = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=big_response)

        gen = DailyDigestGenerator(
            anthropic_api_key="sk-fake",
            logs_path=temp_log_file,
            equity_db_path=temp_equity_db,
            funding_db_path=temp_funding_db,
            anthropic_client=mock_client,
            include_trade_trigger=False,
        )

        import logging
        with caplog.at_level(logging.WARNING, logger="qa.daily_digest"):
            digest = await gen.generate_digest()

        assert any("cost" in rec.message.lower() and "exceeds" in rec.message.lower()
                   for rec in caplog.records)
        # Digest still produced (we don't fail on cost)
        assert "QuantumAlpha Daily Digest" in digest

    async def test_normal_cost_no_warning(
        self, temp_log_file, temp_equity_db, temp_funding_db,
        mock_anthropic_client, caplog,
    ):
        gen = DailyDigestGenerator(
            anthropic_api_key="sk-fake",
            logs_path=temp_log_file,
            equity_db_path=temp_equity_db,
            funding_db_path=temp_funding_db,
            anthropic_client=mock_anthropic_client,
            include_trade_trigger=False,
        )

        import logging
        with caplog.at_level(logging.WARNING, logger="qa.daily_digest"):
            await gen.generate_digest()

        # No "exceeds threshold" warning
        assert not any("exceeds" in rec.message.lower() for rec in caplog.records)
