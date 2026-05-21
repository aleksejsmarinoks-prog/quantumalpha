# Funding-Arb Edge Check — ETH-SOL Spread

Generated: 2026-05-21T07:51:20.065944+00:00

**Thesis under test:** ETH-SOL funding-rate spread shows exploitable
mean-reverting carry edge at production thresholds
(open=±0.0400%/8h, close=±0.0150%/8h).

**PnL model:** enter at |spread|≥open_thr with direction=sign(spread),
accumulate direction*spread per 8h until |spread|≤close_thr. No fees/slippage.

## Segment: `2024_bull`

- Range: `2024-04-01 00:00:00+00:00` → `2024-11-01 00:00:00+00:00`
- Periods (8h): 643

### Spread distribution (% per 8h)

| stat | value |
|---|---:|
| mean | 0.00059 |
| std  | 0.00751 |
| min  | -0.02490 |
| p01  | -0.01616 |
| p05  | -0.01055 |
| p25  | -0.00287 |
| p50  | 0.00000 |
| p75  | 0.00367 |
| p95  | 0.01443 |
| p99  | 0.02349 |
| max  | 0.03511 |

### Autocorrelation of spread

| lag | rho |
|---|---:|
| lag_1_periods_8h | 0.3092 |
| lag_3_periods_24h | 0.1406 |
| lag_6_periods_48h | 0.0753 |
| lag_9_periods_72h | 0.0710 |
| lag_24_periods_192h | 0.0053 |
| lag_72_periods_576h | 0.0028 |

### Signal counts (|spread| ≥ threshold)

| threshold | hits | % of periods |
|---|---:|---:|
| |spread|>=0.0150% | 45 | 6.998% |
| |spread|>=0.0200% | 14 | 2.177% |
| |spread|>=0.0300% | 2 | 0.311% |
| |spread|>=0.0400% | 0 | 0.0% |
| |spread|>=0.0500% | 0 | 0.0% |

### Naive backtest @ production thresholds

- open_thr = ±0.0400%, close_thr = ±0.0150%
- Trades: 0  (0.0/yr)
- Win rate: n/a
- Mean PnL/trade: n/a
- Median PnL/trade: n/a
- Mean hold (periods): n/a
- Total PnL: 0.0%
- Annualized PnL: 0.0%

**Verdict (2024_bull): `NO_EDGE`**

## Segment: `2025_26_bear`

- Range: `2025-11-01 00:00:00+00:00` → `2026-05-01 00:00:00+00:00`
- Periods (8h): 544

### Spread distribution (% per 8h)

| stat | value |
|---|---:|
| mean | 0.00259 |
| std  | 0.00878 |
| min  | -0.02033 |
| p01  | -0.01331 |
| p05  | -0.00914 |
| p25  | -0.00329 |
| p50  | 0.00115 |
| p75  | 0.00780 |
| p95  | 0.01606 |
| p99  | 0.02900 |
| max  | 0.05965 |

### Autocorrelation of spread

| lag | rho |
|---|---:|
| lag_1_periods_8h | 0.3162 |
| lag_3_periods_24h | 0.1238 |
| lag_6_periods_48h | 0.1596 |
| lag_9_periods_72h | 0.1484 |
| lag_24_periods_192h | 0.1248 |
| lag_72_periods_576h | 0.0040 |

### Signal counts (|spread| ≥ threshold)

| threshold | hits | % of periods |
|---|---:|---:|
| |spread|>=0.0150% | 46 | 8.456% |
| |spread|>=0.0200% | 16 | 2.941% |
| |spread|>=0.0300% | 5 | 0.919% |
| |spread|>=0.0400% | 4 | 0.735% |
| |spread|>=0.0500% | 1 | 0.184% |

### Naive backtest @ production thresholds

- open_thr = ±0.0400%, close_thr = ±0.0150%
- Trades: 3  (6.04/yr)
- Win rate: 100.0%
- Mean PnL/trade: 0.068%
- Median PnL/trade: 0.0539%
- Mean hold (periods): 2.33
- Total PnL: 0.2041%
- Annualized PnL: 0.411%

**Verdict (2025_26_bear): `NO_EDGE`**

## Segment: `combined`

- Range: `2024-04-01 00:00:00+00:00` → `2026-05-01 00:00:00+00:00`
- Periods (8h): 1187

### Spread distribution (% per 8h)

| stat | value |
|---|---:|
| mean | 0.00150 |
| std  | 0.00817 |
| min  | -0.02490 |
| p01  | -0.01549 |
| p05  | -0.01001 |
| p25  | -0.00308 |
| p50  | 0.00000 |
| p75  | 0.00561 |
| p95  | 0.01558 |
| p99  | 0.02475 |
| max  | 0.05965 |

### Autocorrelation of spread

| lag | rho |
|---|---:|
| lag_1_periods_8h | 0.3233 |
| lag_3_periods_24h | 0.1435 |
| lag_6_periods_48h | 0.1319 |
| lag_9_periods_72h | 0.1233 |
| lag_24_periods_192h | 0.0803 |
| lag_72_periods_576h | 0.0223 |

### Signal counts (|spread| ≥ threshold)

| threshold | hits | % of periods |
|---|---:|---:|
| |spread|>=0.0150% | 91 | 7.666% |
| |spread|>=0.0200% | 30 | 2.527% |
| |spread|>=0.0300% | 7 | 0.59% |
| |spread|>=0.0400% | 4 | 0.337% |
| |spread|>=0.0500% | 1 | 0.084% |

### Naive backtest @ production thresholds

- open_thr = ±0.0400%, close_thr = ±0.0150%
- Trades: 3  (2.77/yr)
- Win rate: 100.0%
- Mean PnL/trade: 0.068%
- Median PnL/trade: 0.0539%
- Mean hold (periods): 2.33
- Total PnL: 0.2041%
- Annualized PnL: 0.188%

**Verdict (combined): `NO_EDGE`**

---

## OVERALL VERDICT: `NO_EDGE`

_Driver: combined segment_ `combined`
