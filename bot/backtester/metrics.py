"""
QA Backtester — Performance Metrics
====================================

Standard backtest performance metrics. Pure functions over trade lists and
equity curves. Annualization uses 365-day basis (24/7 crypto markets).

Author: QuantForge / QuantumAlpha
Phase: 6.3
"""
from __future__ import annotations

import math
from typing import Iterable

import numpy as np
import pandas as pd

from .models import Trade


# Annualization constants (crypto 24/7)
SECONDS_PER_YEAR = 365.0 * 24 * 3600
DAYS_PER_YEAR = 365.0


# ─────────────────────────────────────────────────────────────────────────────
# Core metrics
# ─────────────────────────────────────────────────────────────────────────────

def total_pnl_usd(trades: Iterable[Trade]) -> float:
    return float(sum(t.realized_pnl_usd for t in trades if t.is_closed))


def win_rate(trades: Iterable[Trade]) -> float:
    closed = [t for t in trades if t.is_closed]
    if not closed:
        return 0.0
    wins = sum(1 for t in closed if t.realized_pnl_usd > 0)
    return wins / len(closed)


def profit_factor(trades: Iterable[Trade]) -> float:
    closed = [t for t in trades if t.is_closed]
    gross_win = sum(t.realized_pnl_usd for t in closed if t.realized_pnl_usd > 0)
    gross_loss = abs(sum(t.realized_pnl_usd for t in closed if t.realized_pnl_usd < 0))
    if gross_loss == 0:
        return float("inf") if gross_win > 0 else 0.0
    return gross_win / gross_loss


def avg_win_usd(trades: Iterable[Trade]) -> float:
    wins = [t.realized_pnl_usd for t in trades if t.is_closed and t.realized_pnl_usd > 0]
    return float(np.mean(wins)) if wins else 0.0


def avg_loss_usd(trades: Iterable[Trade]) -> float:
    losses = [t.realized_pnl_usd for t in trades if t.is_closed and t.realized_pnl_usd < 0]
    return float(np.mean(losses)) if losses else 0.0


def largest_loss_usd(trades: Iterable[Trade]) -> float:
    losses = [t.realized_pnl_usd for t in trades if t.is_closed and t.realized_pnl_usd < 0]
    return float(min(losses)) if losses else 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Equity-curve metrics
# ─────────────────────────────────────────────────────────────────────────────

def max_drawdown_pct(equity: pd.Series) -> float:
    """
    Peak-to-trough drawdown as a fraction of running peak.
    Returns positive value (0.06 = 6% DD). Empty/flat → 0.0.
    """
    if equity.empty:
        return 0.0
    running_max = equity.cummax()
    dd = (running_max - equity) / running_max
    return float(dd.max()) if len(dd) else 0.0


def longest_drawdown_days(equity: pd.Series) -> int:
    """
    Longest stretch (in days) during which equity stayed below a prior peak.
    Counts time from new peak until next peak that surpasses it.
    """
    if equity.empty:
        return 0
    running_max = equity.cummax()
    in_dd = equity < running_max
    if not in_dd.any():
        return 0
    # Find longest contiguous True run in in_dd
    max_len = 0
    current_len = 0
    current_start_ts = None
    longest_seconds = 0
    for ts, flag in in_dd.items():
        if flag:
            if current_start_ts is None:
                current_start_ts = ts
            current_len += 1
        else:
            if current_start_ts is not None:
                seconds = (ts - current_start_ts).total_seconds()
                if seconds > longest_seconds:
                    longest_seconds = seconds
                current_start_ts = None
            current_len = 0
    # Handle tail (still in DD at end)
    if current_start_ts is not None:
        seconds = (equity.index[-1] - current_start_ts).total_seconds()
        if seconds > longest_seconds:
            longest_seconds = seconds
    return int(longest_seconds // 86400)


def sharpe_annualized(equity: pd.Series, periods_per_year: float = DAYS_PER_YEAR) -> float:
    """
    Annualized Sharpe of returns from `equity`. Returns are pct changes.

    periods_per_year: resampling frequency for daily resampling → 365.
    For raw bar-level data, set to (365 * 24 * 60 / minutes_per_bar) etc.
    """
    if equity.empty or len(equity) < 3:
        return 0.0
    returns = equity.pct_change().dropna()
    if returns.empty:
        return 0.0
    std = returns.std()
    if std == 0 or math.isnan(std):
        return 0.0
    mean = returns.mean()
    return float((mean / std) * math.sqrt(periods_per_year))


def calmar_ratio(equity: pd.Series, periods_per_year: float = DAYS_PER_YEAR) -> float:
    """
    Annualized return / max drawdown. Higher = better.
    Returns 0 if MDD = 0 (no losses); +inf is suppressed to a large finite value.
    """
    if equity.empty or len(equity) < 2:
        return 0.0
    total_periods = len(equity)
    cumret = (equity.iloc[-1] / equity.iloc[0]) - 1.0
    if cumret <= -1.0:
        return 0.0                                  # equity wiped — undefined annualisation
    years = total_periods / periods_per_year
    if years <= 0:
        return 0.0
    annualised = (1.0 + cumret) ** (1.0 / years) - 1.0
    mdd = max_drawdown_pct(equity)
    if mdd <= 0:
        return 0.0
    return float(annualised / mdd)


# ─────────────────────────────────────────────────────────────────────────────
# Top-level aggregator
# ─────────────────────────────────────────────────────────────────────────────

def compute_metrics(
    trades: list[Trade],
    equity_curve: pd.Series,
    periods_per_year: float = DAYS_PER_YEAR,
) -> dict:
    """
    Return canonical metrics dict (Phase 6.3 spec).
    """
    closed_trades = [t for t in trades if t.is_closed]
    return {
        "total_trades": len(closed_trades),
        "total_pnl_usd": total_pnl_usd(trades),
        "win_rate": win_rate(trades),
        "profit_factor": profit_factor(trades),
        "sharpe_annualized": sharpe_annualized(equity_curve, periods_per_year),
        "max_drawdown_pct": max_drawdown_pct(equity_curve),
        "calmar_ratio": calmar_ratio(equity_curve, periods_per_year),
        "avg_win_usd": avg_win_usd(trades),
        "avg_loss_usd": avg_loss_usd(trades),
        "largest_loss_usd": largest_loss_usd(trades),
        "longest_drawdown_days": longest_drawdown_days(equity_curve),
    }
