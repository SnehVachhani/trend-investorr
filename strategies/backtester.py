"""
Trend-Investorr: Backtesting Engine

Replays historical daily candles through the existing trading algorithms
(RaynerDipBuyer, ConnorsRSIPullback) to simulate past performance.

Execution model:
    - Decisions are made on daily close candles
    - Entries/exits execute at NEXT day's open (faithful to original strategies)
    - Single-stock mode: one stock at a time

Data source:
    - candles_1d table from PostgreSQL (already populated by the data pipeline)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np
from loguru import logger


# ── Result Data Models ────────────────────────────────────────


@dataclass
class BacktestTrade:
    """A single completed trade from the backtest."""
    symbol: str
    entry_date: date
    exit_date: date | None = None
    entry_price: float = 0.0
    exit_price: float = 0.0
    quantity: int = 0
    pnl: float = 0.0
    pnl_pct: float = 0.0
    exit_reason: str = ""
    hold_days: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class BacktestResult:
    """Aggregate results from a backtest run."""
    algorithm_name: str
    symbol: str
    start_date: date
    end_date: date
    initial_capital: float

    # Aggregate stats
    final_capital: float = 0.0
    total_return_pct: float = 0.0
    cagr_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe_ratio: float = 0.0
    win_rate_pct: float = 0.0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    profit_factor: float = 0.0
    avg_hold_days: float = 0.0
    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0

    # Detailed data
    trades: list[BacktestTrade] = field(default_factory=list)
    equity_curve: list[dict] = field(default_factory=list)  # [{date, value}]
    error: str = ""


# ── Indicator Computation ─────────────────────────────────────


def compute_sma(closes: np.ndarray, period: int) -> np.ndarray:
    """Simple Moving Average."""
    if len(closes) < period:
        return np.full(len(closes), np.nan)
    sma = np.full(len(closes), np.nan)
    for i in range(period - 1, len(closes)):
        sma[i] = np.mean(closes[i - period + 1 : i + 1])
    return sma


def compute_ema(closes: np.ndarray, period: int) -> np.ndarray:
    """Exponential Moving Average."""
    ema = np.full(len(closes), np.nan)
    if len(closes) < period:
        return ema
    ema[period - 1] = np.mean(closes[:period])
    multiplier = 2.0 / (period + 1)
    for i in range(period, len(closes)):
        ema[i] = (closes[i] - ema[i - 1]) * multiplier + ema[i - 1]
    return ema


def compute_rsi(closes: np.ndarray, period: int = 14) -> np.ndarray:
    """Relative Strength Index."""
    rsi = np.full(len(closes), np.nan)
    if len(closes) < period + 1:
        return rsi
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    if avg_loss == 0:
        rsi[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        rsi[period] = 100.0 - (100.0 / (1.0 + rs))
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            rsi[i + 1] = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi[i + 1] = 100.0 - (100.0 / (1.0 + rs))
    return rsi


def compute_roc(closes: np.ndarray, period: int) -> np.ndarray:
    """Rate of Change (%)."""
    roc = np.full(len(closes), np.nan)
    for i in range(period, len(closes)):
        if closes[i - period] > 0:
            roc[i] = (closes[i] - closes[i - period]) / closes[i - period] * 100
    return roc


# ── Backtester ────────────────────────────────────────────────


class Backtester:
    """
    Replays daily candles through a strategy and produces BacktestResult.

    Usage:
        bt = Backtester(candles, algorithm="rayner_dip_buyer", capital=1_000_000)
        result = bt.run()
    """

    def __init__(
        self,
        candles: list[dict],
        algorithm: str,
        symbol: str = "",
        initial_capital: float = 1_000_000,
        position_size_pct: float = 10.0,
    ):
        """
        Args:
            candles: list of dicts with keys: timestamp, open, high, low, close, volume
                     Must be sorted chronologically (oldest first).
            algorithm: 'rayner_dip_buyer' or 'connors_rsi_pullback'
            symbol: stock symbol for display
            initial_capital: starting capital in INR
            position_size_pct: % of capital per trade
        """
        self.candles = candles
        self.algorithm = algorithm
        self.symbol = symbol
        self.initial_capital = initial_capital
        self.position_size_pct = position_size_pct

        # Pre-compute numpy arrays
        self.closes = np.array([c["close"] for c in candles], dtype=float)
        self.opens = np.array([c["open"] for c in candles], dtype=float)
        self.highs = np.array([c["high"] for c in candles], dtype=float)
        self.lows = np.array([c["low"] for c in candles], dtype=float)
        self.volumes = np.array([c["volume"] for c in candles], dtype=float)
        self.dates = [
            c["timestamp"].date() if isinstance(c["timestamp"], datetime) else c["timestamp"]
            for c in candles
        ]

    def run(self) -> BacktestResult:
        """Execute the backtest and return results."""
        if len(self.candles) < 201:
            return BacktestResult(
                algorithm_name=self.algorithm,
                symbol=self.symbol,
                start_date=self.dates[0] if self.dates else date.today(),
                end_date=self.dates[-1] if self.dates else date.today(),
                initial_capital=self.initial_capital,
                error=f"Insufficient data: {len(self.candles)} candles (need 201+)",
            )

        if self.algorithm == "rayner_dip_buyer":
            return self._run_rayner()
        elif self.algorithm == "connors_rsi_pullback":
            return self._run_connors()
        else:
            return BacktestResult(
                algorithm_name=self.algorithm,
                symbol=self.symbol,
                start_date=self.dates[0],
                end_date=self.dates[-1],
                initial_capital=self.initial_capital,
                error=f"Unknown algorithm: {self.algorithm}",
            )

    # ── Rayner Dip Buyer Backtest ─────────────────────────

    def _run_rayner(self) -> BacktestResult:
        """
        Rayner strategy backtest:
        Entry: price > SMA200, 10-day low hit, buy at 2% below close (next day)
        Exit: close > SMA5, or 5% stop loss, or 20-day time stop
        """
        sma_200 = compute_sma(self.closes, 200)
        sma_5 = compute_sma(self.closes, 5)
        roc_100 = compute_roc(self.closes, 100)

        capital = self.initial_capital
        trades: list[BacktestTrade] = []
        equity_curve: list[dict] = []
        in_trade = False
        entry_price = 0.0
        entry_date = None
        entry_qty = 0
        candles_held = 0
        pending_limit = None  # (limit_price, trigger_date_idx)
        pending_exit = False

        # Start from day 200 (need SMA200 warmup)
        for i in range(200, len(self.candles)):
            current_close = self.closes[i]
            current_open = self.opens[i]
            current_low = self.lows[i]
            current_date = self.dates[i]

            # ── Check pending limit fill at today's open/low ──
            if pending_limit and not in_trade:
                limit_price, trigger_idx = pending_limit
                if current_low <= limit_price:
                    # Filled! Enter at limit price (or open if it gapped below)
                    fill_price = min(current_open, limit_price) if current_open <= limit_price else limit_price
                    pos_capital = capital * (self.position_size_pct / 100)
                    entry_qty = int(pos_capital / fill_price)
                    if entry_qty > 0:
                        entry_price = fill_price
                        entry_date = current_date
                        in_trade = True
                        candles_held = 0
                        capital -= entry_qty * entry_price
                pending_limit = None  # Expired either way

            # ── Check exits for active trade ──
            if in_trade:
                candles_held += 1

                # Stop loss: 5% below entry
                stop_price = entry_price * 0.95
                if current_low <= stop_price:
                    exit_price = stop_price
                    pnl = (exit_price - entry_price) * entry_qty
                    capital += entry_qty * exit_price
                    trades.append(BacktestTrade(
                        symbol=self.symbol, entry_date=entry_date,
                        exit_date=current_date, entry_price=entry_price,
                        exit_price=exit_price, quantity=entry_qty,
                        pnl=pnl, pnl_pct=(exit_price / entry_price - 1) * 100,
                        exit_reason="stop_loss", hold_days=candles_held,
                    ))
                    in_trade = False
                    pending_exit = False
                # Pending exit from yesterday's SMA5 crossover
                elif pending_exit:
                    exit_price = current_open
                    pnl = (exit_price - entry_price) * entry_qty
                    capital += entry_qty * exit_price
                    trades.append(BacktestTrade(
                        symbol=self.symbol, entry_date=entry_date,
                        exit_date=current_date, entry_price=entry_price,
                        exit_price=exit_price, quantity=entry_qty,
                        pnl=pnl, pnl_pct=(exit_price / entry_price - 1) * 100,
                        exit_reason="sma5_exit", hold_days=candles_held,
                    ))
                    in_trade = False
                    pending_exit = False
                # Time stop: 20 candles
                elif candles_held >= 20:
                    exit_price = current_close
                    pnl = (exit_price - entry_price) * entry_qty
                    capital += entry_qty * exit_price
                    trades.append(BacktestTrade(
                        symbol=self.symbol, entry_date=entry_date,
                        exit_date=current_date, entry_price=entry_price,
                        exit_price=exit_price, quantity=entry_qty,
                        pnl=pnl, pnl_pct=(exit_price / entry_price - 1) * 100,
                        exit_reason="time_stop", hold_days=candles_held,
                    ))
                    in_trade = False
                    pending_exit = False
                # Check SMA5 exit condition at close
                elif not np.isnan(sma_5[i]) and current_close > sma_5[i]:
                    pending_exit = True  # Exit at tomorrow's open

            # ── Scan for new setup (only if not in trade) ──
            if not in_trade and not pending_limit:
                if np.isnan(sma_200[i]):
                    pass
                elif current_close > sma_200[i]:
                    # Check 10-day low
                    if i >= 10:
                        ten_day_low = np.min(self.lows[i - 9 : i + 1])
                        if self.lows[i] <= ten_day_low:
                            # Setup! Place limit for tomorrow
                            limit_price = current_close * 0.98
                            pending_limit = (limit_price, i)

            # ── Equity curve ──
            portfolio_val = capital
            if in_trade:
                portfolio_val += entry_qty * current_close
            equity_curve.append({"date": str(current_date), "value": round(portfolio_val, 2)})

        # Close any open trade at the end
        if in_trade:
            exit_price = self.closes[-1]
            pnl = (exit_price - entry_price) * entry_qty
            capital += entry_qty * exit_price
            trades.append(BacktestTrade(
                symbol=self.symbol, entry_date=entry_date,
                exit_date=self.dates[-1], entry_price=entry_price,
                exit_price=exit_price, quantity=entry_qty,
                pnl=pnl, pnl_pct=(exit_price / entry_price - 1) * 100,
                exit_reason="backtest_end", hold_days=candles_held,
            ))

        return self._compute_stats(trades, equity_curve)

    # ── Connors RSI Pullback Backtest ─────────────────────

    def _run_connors(self) -> BacktestResult:
        """
        ConnorsRSI strategy backtest (using shared configuration):
        Entry: price > SMA, RSI < entry_threshold (oversold pullback in uptrend)
        Exit: RSI > exit_threshold, or time stop
        """
        from strategies.config import CONNORS_CONFIG as cfg
        
        sma = compute_sma(self.closes, cfg.sma_period)
        rsi = compute_rsi(self.closes, cfg.rsi_period)

        capital = self.initial_capital
        trades: list[BacktestTrade] = []
        equity_curve: list[dict] = []
        in_trade = False
        entry_price = 0.0
        entry_date = None
        entry_qty = 0
        candles_held = 0
        pending_entry = False  # Signal detected, enter at next open
        cooldown = 0  # Avoid whipsaws

        for i in range(cfg.sma_period, len(self.candles)):
            current_close = self.closes[i]
            current_open = self.opens[i]
            current_low = self.lows[i]
            current_date = self.dates[i]

            if cooldown > 0:
                cooldown -= 1

            # ── Execute pending entry at today's open ──
            if pending_entry and not in_trade:
                pos_capital = capital * (self.position_size_pct / 100)
                entry_qty = int(pos_capital / current_open)
                if entry_qty > 0:
                    entry_price = current_open
                    entry_date = current_date
                    in_trade = True
                    candles_held = 0
                    capital -= entry_qty * entry_price
                pending_entry = False

            # ── Check exits ──
            if in_trade:
                candles_held += 1

                # Stop loss
                if cfg.stop_loss_pct > 0:
                    stop_price = entry_price * (1 - cfg.stop_loss_pct / 100)
                    if current_low <= stop_price:
                        exit_price = stop_price
                        pnl = (exit_price - entry_price) * entry_qty
                        capital += entry_qty * exit_price
                        trades.append(BacktestTrade(
                            symbol=self.symbol, entry_date=entry_date,
                            exit_date=current_date, entry_price=entry_price,
                            exit_price=exit_price, quantity=entry_qty,
                            pnl=pnl, pnl_pct=(exit_price / entry_price - 1) * 100,
                            exit_reason="stop_loss", hold_days=candles_held,
                        ))
                        in_trade = False
                        cooldown = 5
                        continue

                # Time stop
                if candles_held >= cfg.max_hold_candles:
                    exit_price = current_close
                    pnl = (exit_price - entry_price) * entry_qty
                    capital += entry_qty * exit_price
                    trades.append(BacktestTrade(
                        symbol=self.symbol, entry_date=entry_date,
                        exit_date=current_date, entry_price=entry_price,
                        exit_price=exit_price, quantity=entry_qty,
                        pnl=pnl, pnl_pct=(exit_price / entry_price - 1) * 100,
                        exit_reason="time_stop", hold_days=candles_held,
                    ))
                    in_trade = False
                    cooldown = 5
                    continue

                # RSI exit
                if not np.isnan(rsi[i]) and rsi[i] >= cfg.rsi_exit_threshold:
                    exit_price = current_close
                    pnl = (exit_price - entry_price) * entry_qty
                    capital += entry_qty * exit_price
                    trades.append(BacktestTrade(
                        symbol=self.symbol, entry_date=entry_date,
                        exit_date=current_date, entry_price=entry_price,
                        exit_price=exit_price, quantity=entry_qty,
                        pnl=pnl, pnl_pct=(exit_price / entry_price - 1) * 100,
                        exit_reason="rsi_recovery", hold_days=candles_held,
                    ))
                    in_trade = False
                    cooldown = 5
                    continue

            # ── Scan for entry setup ──
            if not in_trade and not pending_entry and cooldown == 0:
                if (
                    not np.isnan(sma[i])
                    and not np.isnan(rsi[i])
                    and current_close > sma[i]
                    and rsi[i] < cfg.rsi_entry_threshold
                ):
                    pending_entry = True  # Enter at tomorrow's open

            # ── Equity curve ──
            portfolio_val = capital
            if in_trade:
                portfolio_val += entry_qty * current_close
            equity_curve.append({"date": str(current_date), "value": round(portfolio_val, 2)})

        # Close any open trade at end
        if in_trade:
            exit_price = self.closes[-1]
            pnl = (exit_price - entry_price) * entry_qty
            capital += entry_qty * exit_price
            trades.append(BacktestTrade(
                symbol=self.symbol, entry_date=entry_date,
                exit_date=self.dates[-1], entry_price=entry_price,
                exit_price=exit_price, quantity=entry_qty,
                pnl=pnl, pnl_pct=(exit_price / entry_price - 1) * 100,
                exit_reason="backtest_end", hold_days=candles_held,
            ))

        return self._compute_stats(trades, equity_curve)

    # ── Statistics ────────────────────────────────────────

    def _compute_stats(
        self,
        trades: list[BacktestTrade],
        equity_curve: list[dict],
    ) -> BacktestResult:
        """Compute aggregate statistics from trade list."""
        result = BacktestResult(
            algorithm_name=self.algorithm,
            symbol=self.symbol,
            start_date=self.dates[200] if len(self.dates) > 200 else self.dates[0],
            end_date=self.dates[-1],
            initial_capital=self.initial_capital,
            trades=trades,
            equity_curve=equity_curve,
        )

        if not trades:
            result.final_capital = self.initial_capital
            return result

        # Final capital from equity curve
        result.final_capital = equity_curve[-1]["value"] if equity_curve else self.initial_capital
        result.total_trades = len(trades)
        result.total_return_pct = round(
            (result.final_capital - self.initial_capital) / self.initial_capital * 100, 2
        )

        # Win/loss
        winners = [t for t in trades if t.pnl > 0]
        losers = [t for t in trades if t.pnl <= 0]
        result.winning_trades = len(winners)
        result.losing_trades = len(losers)
        result.win_rate_pct = round(len(winners) / len(trades) * 100, 1) if trades else 0

        # Avg win/loss
        if winners:
            result.avg_win_pct = round(np.mean([t.pnl_pct for t in winners]), 2)
        if losers:
            result.avg_loss_pct = round(np.mean([t.pnl_pct for t in losers]), 2)

        # Profit factor
        gross_profit = sum(t.pnl for t in winners) if winners else 0
        gross_loss = abs(sum(t.pnl for t in losers)) if losers else 0
        result.profit_factor = round(gross_profit / gross_loss, 3) if gross_loss > 0 else 999.0

        # Avg hold days
        result.avg_hold_days = round(np.mean([t.hold_days for t in trades]), 1)

        # CAGR
        if equity_curve and len(equity_curve) > 1:
            years = len(equity_curve) / 252  # Trading days per year
            if years > 0 and result.final_capital > 0:
                result.cagr_pct = round(
                    (pow(result.final_capital / self.initial_capital, 1 / years) - 1) * 100, 2
                )

        # Max drawdown
        if equity_curve:
            values = [e["value"] for e in equity_curve]
            peak = values[0]
            max_dd = 0.0
            for v in values:
                if v > peak:
                    peak = v
                dd = (peak - v) / peak * 100
                if dd > max_dd:
                    max_dd = dd
            result.max_drawdown_pct = round(max_dd, 2)

        # Sharpe ratio (annualized, using daily returns)
        if equity_curve and len(equity_curve) > 2:
            values = np.array([e["value"] for e in equity_curve])
            daily_returns = np.diff(values) / values[:-1]
            if np.std(daily_returns) > 0:
                result.sharpe_ratio = round(
                    (np.mean(daily_returns) / np.std(daily_returns)) * math.sqrt(252), 3
                )

        # Consecutive wins/losses
        max_cw, max_cl, cw, cl = 0, 0, 0, 0
        for t in trades:
            if t.pnl > 0:
                cw += 1
                cl = 0
            else:
                cl += 1
                cw = 0
            max_cw = max(max_cw, cw)
            max_cl = max(max_cl, cl)
        result.max_consecutive_wins = max_cw
        result.max_consecutive_losses = max_cl

        # Sanitize: convert numpy types to native Python (psycopg2 can't serialize np.float64)
        result.total_return_pct = float(result.total_return_pct)
        result.cagr_pct = float(result.cagr_pct)
        result.max_drawdown_pct = float(result.max_drawdown_pct)
        result.sharpe_ratio = float(result.sharpe_ratio)
        result.win_rate_pct = float(result.win_rate_pct)
        result.avg_win_pct = float(result.avg_win_pct)
        result.avg_loss_pct = float(result.avg_loss_pct)
        result.profit_factor = float(result.profit_factor)
        result.avg_hold_days = float(result.avg_hold_days)
        result.final_capital = float(result.final_capital)
        for t in result.trades:
            t.entry_price = float(t.entry_price)
            t.exit_price = float(t.exit_price)
            t.pnl = float(t.pnl)
            t.pnl_pct = float(t.pnl_pct)

        return result

