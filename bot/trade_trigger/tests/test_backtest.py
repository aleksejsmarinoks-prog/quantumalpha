"""Backtest harness tests."""

from __future__ import annotations

import pytest

from bot.trade_trigger.backtest import (
    run_backtest, historical_corpus, BacktestReport,
)


@pytest.mark.asyncio
class TestBacktest:

    async def test_corpus_not_empty(self):
        cases = historical_corpus()
        assert len(cases) >= 5

    async def test_run_backtest_basic(self, db):
        report = await run_backtest(db)
        assert isinstance(report, BacktestReport)
        assert report.total == len(historical_corpus())
        assert len(report.results) == report.total

    async def test_classifier_catches_hormuz_easing(self, db):
        report = await run_backtest(db)
        hormuz_result = next(
            r for r in report.results if r.case_name == "hormuz_easing_may3_2026"
        )
        # The May 3 event we missed must be classified correctly
        assert hormuz_result.actual_event_type == "hormuz_easing"
        assert hormuz_result.classifier_match is True

    async def test_banned_source_rejected(self, db):
        report = await run_backtest(db)
        banned_result = next(
            r for r in report.results if r.case_name == "banned_source_rejection"
        )
        assert banned_result.actual_fire is False
        assert banned_result.actual_event_type is None

    async def test_noise_rejected(self, db):
        report = await run_backtest(db)
        noise_result = next(
            r for r in report.results if r.case_name == "generic_earnings_noise"
        )
        assert noise_result.actual_fire is False

    async def test_high_classifier_accuracy(self, db):
        """Sanity: at least 80% of corpus should be classified correctly."""
        report = await run_backtest(db)
        assert report.classifier_accuracy >= 0.8, (
            f"Classifier accuracy degraded: {report.classifier_accuracy:.2f} "
            f"\n{report.to_text()}"
        )

    async def test_report_text_renders(self, db):
        report = await run_backtest(db)
        text = report.to_text()
        assert "BACKTEST REPORT" in text
        assert "Classifier accuracy" in text
