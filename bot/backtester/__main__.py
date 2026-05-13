"""
QA Backtester CLI

Usage:
    python -m bot.backtester --strategy mean_reversion_v1 --start 2025-11-01 --end 2026-05-01 --capital 1000
    python -m bot.backtester --walk-forward --strategies mean_reversion_v1,funding_arb_v1 --start ... --end ...
    python -m bot.backtester --refresh-cache --symbols ETHUSDT,SOLUSDT --start ... --end ...
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Type

import pandas as pd

from .data_loader import BybitDataLoader
from .execution_sim import ExecutionSimulator
from .replay_engine import ReplayEngine
from .report import write_backtest_report
from .strategy_adapters.base_adapter import BaseAdapter
from .strategy_adapters.dca_dips_adapter import DcaDipsAdapter
from .strategy_adapters.funding_arb_adapter import FundingArbAdapter
from .strategy_adapters.mean_reversion_adapter import MeanReversionAdapter
from .walk_forward import WalkForwardValidator


log = logging.getLogger("qa.backtester.cli")


ADAPTER_REGISTRY: dict[str, Type[BaseAdapter]] = {
    "mean_reversion_v1": MeanReversionAdapter,
    "funding_arb_v1": FundingArbAdapter,
    "dca_dips_v1": DcaDipsAdapter,
}

DEFAULT_GRIDS: dict[str, dict[str, list[Any]]] = {
    "mean_reversion_v1": {
        "lookback_bars": [10, 15, 20, 25, 30],
        "z_entry": [1.5, 2.0, 2.5, 3.0],
    },
    "funding_arb_v1": {
        "open_threshold_8h": [0.0002, 0.0004, 0.0006],
        "close_threshold_8h": [0.00010, 0.00015, 0.00020],
    },
    "dca_dips_v1": {
        "drop_pct": [0.03, 0.05, 0.07],
        "tp_pct": [0.02, 0.04, 0.06],
    },
}


def _parse_date(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def _load_data(
    loader: BybitDataLoader,
    symbols: list[str],
    timeframe: str,
    start: datetime,
    end: datetime,
) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
    klines: dict[str, pd.DataFrame] = {}
    funding: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        klines[sym] = loader.fetch_klines(sym, timeframe, start, end)
        funding[sym] = loader.fetch_funding_history(sym, start, end)
    return klines, funding


def cmd_run_single(args: argparse.Namespace) -> int:
    setup_logging(args.verbose)
    start = _parse_date(args.start)
    end = _parse_date(args.end)
    symbols = args.symbols.split(",")
    adapter_cls = ADAPTER_REGISTRY.get(args.strategy)
    if adapter_cls is None:
        log.error("unknown strategy %s. Available: %s", args.strategy, list(ADAPTER_REGISTRY))
        return 1

    loader = BybitDataLoader(cache_root=Path(args.cache_dir))
    klines, funding = _load_data(loader, symbols, args.timeframe, start, end)

    adapter = adapter_cls()
    adapter.reset({})
    exec_sim = ExecutionSimulator(seed=args.seed)
    engine = ReplayEngine(adapter=adapter, klines=klines, funding=funding,
                          execution_sim=exec_sim, starting_capital_usd=args.capital)
    primary = symbols[0]
    trades, equity = engine.run(start, end, primary)
    from .metrics import compute_metrics
    from .models import WindowResult
    metrics = compute_metrics(trades, equity)

    log.info("backtest complete: %d closed trades, sharpe=%.2f, mdd=%.2%%, pnl=$%.2f",
             metrics["total_trades"], metrics["sharpe_annualized"],
             metrics["max_drawdown_pct"] * 100, metrics["total_pnl_usd"])

    # Wrap as a single 1-window report for consistency
    fake_window = WindowResult(
        window_idx=0,
        train_start=start, train_end=start,
        test_start=start, test_end=end,
        best_params={}, train_metrics={}, test_metrics=metrics,
        train_trades=0, test_trades=metrics["total_trades"],
    )
    out_dir = Path(args.output)
    write_backtest_report(args.strategy, start, end, args.capital, [fake_window], out_dir)
    return 0


def cmd_walk_forward(args: argparse.Namespace) -> int:
    setup_logging(args.verbose)
    start = _parse_date(args.start)
    end = _parse_date(args.end)
    symbols = args.symbols.split(",")
    loader = BybitDataLoader(cache_root=Path(args.cache_dir))

    strategy_names = args.strategies.split(",")
    exit_code = 0
    for strat_name in strategy_names:
        adapter_cls = ADAPTER_REGISTRY.get(strat_name)
        if adapter_cls is None:
            log.error("unknown strategy %s — skipping", strat_name)
            exit_code = 1
            continue
        klines, funding = _load_data(loader, symbols, args.timeframe, start, end)
        primary = symbols[0]
        adapter = adapter_cls()

        def runner_factory(adapter_ref=adapter, klines_ref=klines, funding_ref=funding):
            def runner(params: dict, run_start: datetime, run_end: datetime):
                adapter_ref.reset(params)
                exec_sim = ExecutionSimulator(seed=args.seed)
                engine = ReplayEngine(adapter=adapter_ref, klines=klines_ref, funding=funding_ref,
                                      execution_sim=exec_sim, starting_capital_usd=args.capital)
                return engine.run(run_start, run_end, primary)
            return runner

        grid = DEFAULT_GRIDS.get(strat_name, {})
        validator = WalkForwardValidator(
            runner=runner_factory(),
            param_grid=grid,
            train_days=args.train_days,
            test_days=args.test_days,
            step_days=args.step_days,
        )
        results = validator.run(start, end)
        out_dir = Path(args.output)
        write_backtest_report(strat_name, start, end, args.capital, results, out_dir)
        log.info("strategy %s: %d windows", strat_name, len(results))
    return exit_code


def cmd_refresh_cache(args: argparse.Namespace) -> int:
    setup_logging(args.verbose)
    start = _parse_date(args.start)
    end = _parse_date(args.end)
    symbols = args.symbols.split(",")
    loader = BybitDataLoader(cache_root=Path(args.cache_dir))
    for sym in symbols:
        for tf in (args.timeframe,):
            loader.fetch_klines(sym, tf, start, end, force_refresh=True)
        loader.fetch_funding_history(sym, start, end, force_refresh=True)
    log.info("cache refresh complete: %s", symbols)
    return 0


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bot.backtester", description="QA Backtester CLI")
    p.add_argument("--start", required=True, help="Start date YYYY-MM-DD (UTC)")
    p.add_argument("--end", required=True, help="End date YYYY-MM-DD (UTC)")
    p.add_argument("--symbols", default="ETHUSDT,SOLUSDT", help="Comma-separated Bybit perp symbols")
    p.add_argument("--timeframe", default="5m", help="Kline timeframe (default 5m)")
    p.add_argument("--capital", type=float, default=1000.0, help="Starting capital USD")
    p.add_argument("--output", default="./backtest_results", help="Output dir for reports")
    p.add_argument("--cache-dir", default="data/backtest_cache", help="Data cache directory")
    p.add_argument("--seed", type=int, default=42, help="RNG seed for reproducibility")
    p.add_argument("--train-days", type=int, default=60)
    p.add_argument("--test-days", type=int, default=30)
    p.add_argument("--step-days", type=int, default=30)
    p.add_argument("--verbose", "-v", action="store_true")

    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--strategy", help="Single-strategy backtest (no walk-forward)")
    mode.add_argument("--walk-forward", action="store_true", help="Run walk-forward on --strategies")
    mode.add_argument("--refresh-cache", action="store_true", help="Refetch and overwrite cache only")
    p.add_argument("--strategies", help="Comma-separated strategy ids for walk-forward")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.walk_forward:
        if not args.strategies:
            parser.error("--walk-forward requires --strategies")
        return cmd_walk_forward(args)
    if args.refresh_cache:
        return cmd_refresh_cache(args)
    if args.strategy:
        return cmd_run_single(args)
    parser.error("no mode selected")
    return 2


if __name__ == "__main__":
    sys.exit(main())
