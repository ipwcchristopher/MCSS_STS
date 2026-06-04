"""MCSS Phase 5 — Full Portfolio Backtesting Engine.

Walk-forward simulation of MCSS L3/L4 screening logic over up to 10 years.

Usage:
    python scripts/backtest.py                   # full run (window_days from config)
    python scripts/backtest.py --dry-run         # download + cache only
    python scripts/backtest.py --refresh-cache   # force re-download OHLCV
    python scripts/backtest.py --no-l2          # use L1 universe (bias-free)
    python scripts/backtest.py --use-simfin     # use SimFin historical fundamentals
"""

import argparse
import os
import sys
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import yaml
import yfinance as yf

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from indicators.vcp import compute_vcp_score
from quant_scoring import (
    _momentum_score,
    _quality_score,
    _rs_score,
    _short_squeeze_bonus,
    _value_score,
    _volatility_score,
    _vcp_score_pts,
)

CACHE_DIR   = ROOT / "data" / "backtest_cache"
REPORT_PATH = ROOT / "data" / "backtest_report.html"
TRADES_PATH = ROOT / "data" / "backtest_trades.csv"

SECTOR_ETFS: Dict[str, str] = {
    "Technology":              "XLK",
    "Healthcare":              "XLV",
    "Financial Services":      "XLF",
    "Consumer Cyclical":       "XLY",
    "Industrials":             "XLI",
    "Energy":                  "XLE",
    "Basic Materials":         "XLB",
    "Real Estate":             "XLRE",
    "Consumer Defensive":      "XLP",
    "Utilities":               "XLU",
    "Communication Services":  "XLC",
}


# ── Data Cache ─────────────────────────────────────────────────────────────────

class DataCache:

    def __init__(self, cache_dir: Path = CACHE_DIR):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def load_all(
        self, tickers: List[str], refresh: bool = False
    ) -> Dict[str, pd.DataFrame]:
        cached, need_dl = {}, []
        for t in tickers:
            f = self.cache_dir / f"{t}.parquet"
            if not refresh and f.exists():
                try:
                    df = pd.read_parquet(f)
                    if len(df) >= 400:
                        cached[t] = df
                        continue
                except Exception:
                    pass
            need_dl.append(t)

        print(f"OHLCV cache: {len(cached)} loaded | {len(need_dl)} to download")
        if need_dl:
            cached.update(self._batch_download(need_dl))
        return cached

    def _batch_download(self, tickers: List[str]) -> Dict[str, pd.DataFrame]:
        result: Dict[str, pd.DataFrame] = {}
        try:
            raw = yf.download(
                tickers,
                period="10y",
                auto_adjust=True,
                progress=True,
                threads=True,
            )
        except Exception as e:
            print(f"Batch download failed: {e}")
            return result

        if raw.empty:
            return result

        def _extract(ticker: str) -> pd.DataFrame:
            if isinstance(raw.columns, pd.MultiIndex):
                lvl0 = set(raw.columns.get_level_values(0))
                ohlcv = {"Open", "High", "Low", "Close", "Volume"}
                if lvl0 & ohlcv:
                    return raw.xs(ticker, axis=1, level=1)[list(ohlcv)].dropna(how="all")
                else:
                    return raw[ticker][list(ohlcv)].dropna(how="all")
            return raw[["Open", "High", "Low", "Close", "Volume"]].dropna(how="all")

        for t in tickers:
            try:
                df = _extract(t)
                if not df.empty:
                    df.to_parquet(self.cache_dir / f"{t}.parquet")
                    result[t] = df
            except Exception:
                pass

        print(f"Downloaded {len(result)}/{len(tickers)} tickers")
        return result


# ── Pre-computed indicators (vectorised, all dates at once) ───────────────────

def _build_indicator_matrix(
    all_ohlcv: Dict[str, pd.DataFrame],
) -> Dict[str, pd.DataFrame]:
    """Pre-compute all technical indicator time-series for every ticker."""
    indicators: Dict[str, pd.DataFrame] = {}
    for ticker, ohlcv in all_ohlcv.items():
        if len(ohlcv) < 210:
            continue
        c = ohlcv["Close"]
        h = ohlcv["High"]
        lo = ohlcv["Low"]
        v = ohlcv["Volume"]

        ind = pd.DataFrame(index=ohlcv.index)
        ind["close"]  = c
        ind["high"]   = h
        ind["low"]    = lo
        ind["volume"] = v
        ind["ema20"]  = c.ewm(span=20,  adjust=False).mean()
        ind["ema50"]  = c.ewm(span=50,  adjust=False).mean()
        ind["ema150"] = c.ewm(span=150, adjust=False).mean()
        ind["ema200"] = c.ewm(span=200, adjust=False).mean()

        delta = c.diff()
        gain = delta.clip(lower=0).ewm(com=13, adjust=False).mean()
        loss = (-delta).clip(lower=0).ewm(com=13, adjust=False).mean()
        ind["rsi14"] = 100 - (100 / (1 + gain / loss.replace(0, np.nan))).fillna(100)

        prev = c.shift(1)
        tr = pd.concat([h - lo, (h - prev).abs(), (lo - prev).abs()], axis=1).max(axis=1)
        ind["atr14"] = tr.ewm(com=13, adjust=False).mean()

        ind["vol_avg20"] = v.rolling(20, min_periods=10).mean()

        ind["high_252"] = h.rolling(252, min_periods=63).max()
        ind["dist_52wh"] = (ind["high_252"] - c) / ind["high_252"] * 100

        ind["perf_5d"]  = c.pct_change(5)
        ind["perf_10d"] = c.pct_change(10)
        ind["perf_20d"] = c.pct_change(20)
        ind["momentum"] = (
            0.5 * ind["perf_5d"].fillna(0)
            + 0.3 * ind["perf_10d"].fillna(0)
            + 0.2 * ind["perf_20d"].fillna(0)
        )

        indicators[ticker] = ind
    return indicators


def _precompute_rs_ratings(
    ind_map: Dict[str, pd.DataFrame],
    trading_days: List[pd.Timestamp],
    step: int = 5,
) -> Dict[pd.Timestamp, Dict[str, int]]:
    """Vectorised RS Rating computation, recomputed every `step` trading days."""
    close_matrix = pd.DataFrame({t: df["close"] for t, df in ind_map.items()})
    weights = [(63, 0.4), (126, 0.2), (189, 0.2), (252, 0.2)]
    cache: Dict[pd.Timestamp, Dict[str, int]] = {}

    print(f"Pre-computing RS Ratings (every {step} days)...", end="", flush=True)
    computed = 0
    for i, date in enumerate(trading_days):
        if i % step != 0:
            continue
        sub = close_matrix[close_matrix.index <= date]
        if len(sub) < 63:
            cache[date] = {}
            continue

        composite = pd.Series(0.0, index=sub.columns)
        current = sub.iloc[-1]
        for days, w in weights:
            if len(sub) >= days:
                past = sub.iloc[-days]
                ret = (current / past - 1.0).fillna(0)
                composite = composite + w * ret

        ranked = (composite.rank(pct=True) * 99).round().astype(int)
        cache[date] = ranked.to_dict()
        computed += 1

    print(f" done ({computed} snapshots)")
    return cache


def _get_rs(
    cache: Dict[pd.Timestamp, Dict[str, int]], date: pd.Timestamp
) -> Dict[str, int]:
    dates = [d for d in cache if d <= date]
    return cache[max(dates)] if dates else {}


# ── L3 + L4 filter for a single ticker at a given row ────────────────────────

def _passes_l3(row: pd.Series, l3_cfg: Dict) -> bool:
    if any(pd.isna(row.get(k)) for k in ["ema20", "ema50", "ema150", "ema200", "rsi14", "atr14"]):
        return False
    c = float(row["close"])
    e20, e50, e150, e200 = (
        float(row["ema20"]), float(row["ema50"]),
        float(row["ema150"]), float(row["ema200"]),
    )
    if not (c > e20 > 0 and c > e50 and c > e150 and c > e200):
        return False
    if not (e50 > e150 > e200):
        return False
    rsi = float(row["rsi14"])
    if rsi > float(l3_cfg.get("max_rsi14", 72)) or rsi < float(l3_cfg.get("min_rsi14", 45)):
        return False
    if float(row.get("dist_52wh", 100)) > float(l3_cfg.get("max_distance_from_52w_high_pct", 25)):
        return False
    return True


def _passes_vcp(ohlcv_slice: pd.DataFrame, l3_cfg: Dict) -> Tuple[bool, int]:
    """Returns (vcp_ok, vcp_score). Respects vcp_optional flag."""
    vcp_detected, vcp_sc = compute_vcp_score(ohlcv_slice)
    if l3_cfg.get("vcp_optional", True):
        return True, int(vcp_sc)
    min_score = int(l3_cfg.get("min_vcp_score", 60))
    return int(vcp_sc) >= min_score, int(vcp_sc)


def _passes_l2_historical(fund: Dict, l2_cfg: Dict) -> bool:
    """Simplified point-in-time L2 check using SimFin historical data."""
    if not fund:
        return True  # fallback: pass when no historical data available
    gross_margin = fund.get("gross_margin_pct")
    if gross_margin is not None:
        min_gm = float(l2_cfg.get("min_gross_margin_pct", 35))
        if float(gross_margin) < min_gm:
            return False
    return True


def _l4_score(
    ind_row: pd.Series,
    fund_row: pd.Series,
    rs_rating: int,
    ohlcv_slice: pd.DataFrame,
    l4_cfg: Dict,
) -> float:
    composite = float(ind_row.get("momentum", 0))
    _, vcp_sc = compute_vcp_score(ohlcv_slice)
    gm_pts, roe_pts, fcf_pts = _quality_score(fund_row)
    total = (
        _rs_score(rs_rating, l4_cfg)
        + gm_pts + roe_pts + fcf_pts
        + _momentum_score(composite, int(l4_cfg.get("momentum_score_max", 25)))
        + _vcp_score_pts(vcp_sc, int(l4_cfg.get("vcp_tightness_score_max", 15)))
        + _volatility_score(float(ind_row.get("atr14", 0)), float(ind_row.get("close", 1)))
        + _value_score(fund_row)
        + _short_squeeze_bonus(fund_row, composite, l4_cfg)
    )
    return float(total)


# ── Position dataclass ────────────────────────────────────────────────────────

@dataclass
class Position:
    ticker: str
    entry_date: pd.Timestamp
    entry_price: float
    shares: float
    stop: float
    target: float
    score: float
    t1_price: float = 0.0        # = entry * (1 + t1_gain_pct/100)
    half_exited: bool = False
    trailing_stop: float = 0.0   # ratchets up after T1 hit
    position_id: int = 0         # unique ID for grouping two-stage exit records
    exit_date: Optional[pd.Timestamp] = None
    exit_price: Optional[float] = None
    outcome: Optional[str] = None


# ── Portfolio Simulator ───────────────────────────────────────────────────────

class PortfolioSimulator:

    def __init__(
        self,
        cfg: Dict,
        ind_map: Dict[str, pd.DataFrame],
        all_ohlcv: Dict[str, pd.DataFrame],
        universe_df: pd.DataFrame,
        rs_cache: Dict[pd.Timestamp, Dict[str, int]],
        simfin_data: Optional[Dict] = None,
        bias_note: str = "",
    ):
        self.cfg = cfg
        self.ind_map = ind_map
        self.all_ohlcv = all_ohlcv
        self.universe_df = universe_df
        self.rs_cache = rs_cache
        self.simfin_data = simfin_data or {}
        self.bias_note = bias_note

        bt  = cfg.get("backtest", {})
        sz  = cfg.get("position_sizing", {})
        gr  = cfg.get("guardrails", {})
        l3  = cfg.get("l3_technical", {})

        self.initial_capital: float = float(bt.get("initial_capital", 100_000))
        self.reward_ratio:    float = float(bt.get("reward_ratio", 2.0))
        self.t1_gain_pct:     float = float(bt.get("t1_gain_pct", 5)) / 100
        self.trail_mult:      float = float(bt.get("trailing_atr_mult", 2.0))
        self.time_stop_days:  int   = int(bt.get("time_stop_days", 15))
        self.risk_pct:        float = float(sz.get("risk_per_trade_pct", 2)) / 100
        self.stop_mult:       float = float(sz.get("stop_atr_multiplier", 1.5))
        self.max_pos_pct:     float = float(sz.get("max_position_pct", 25)) / 100
        self.max_positions:   int   = int(gr.get("max_concurrent_positions", 6))
        self.halt_dd:         float = float(gr.get("halt_new_entries_portfolio_drawdown_pct", 10)) / 100

        self._pos_counter: int = 0
        self.spy_residual:    bool = bool(bt.get("spy_residual_allocation", True))
        self.spy_ohlcv:       Optional[pd.DataFrame] = all_ohlcv.get("SPY")

        # Build sector → ETF map from universe
        self.sector_etf_map: Dict[str, str] = {}
        if "sector" in universe_df.columns and l3.get("require_sector_alignment", False):
            for _, row in universe_df.iterrows():
                etf = SECTOR_ETFS.get(str(row.get("sector", "")))
                if etf:
                    self.sector_etf_map[str(row["ticker"])] = etf

    def _log_trade(
        self, trades: List[Dict], pos: Position, exit_date: pd.Timestamp,
        exit_price: float, outcome: str, shares: Optional[float] = None,
    ) -> None:
        used_shares = shares if shares is not None else pos.shares
        pnl_pct = (exit_price / pos.entry_price - 1) * 100
        print(
            f"  [{exit_date.date()}] CLOSE {pos.ticker:6s} "
            f"@ ${exit_price:.2f} ({outcome.upper():10s}) "
            f"{pnl_pct:+.1f}%  ({used_shares:.2f} sh)"
        )
        trades.append({
            "position_id":  pos.position_id,
            "ticker":       pos.ticker,
            "entry_date":   pos.entry_date.date(),
            "entry_price":  round(pos.entry_price, 2),
            "exit_date":    exit_date.date(),
            "exit_price":   round(exit_price, 2),
            "shares":       round(used_shares, 4),
            "pnl_usd":      round((exit_price - pos.entry_price) * used_shares, 2),
            "pnl_pct":      round(pnl_pct, 2),
            "outcome":      outcome,
            "hold_days":    (exit_date - pos.entry_date).days,
            "entry_score":  round(pos.score, 1),
        })

    def run(
        self, trading_days: List[pd.Timestamp],
    ) -> Tuple[pd.Series, pd.DataFrame]:
        l3_cfg    = self.cfg.get("l3_technical", {})
        l4_cfg    = self.cfg.get("l4_scoring", {})
        l2_cfg    = self.cfg.get("l2_fundamental", {})
        min_score = int(l4_cfg.get("min_total_score", 60))

        cash        = self.initial_capital
        peak        = self.initial_capital          # all-time peak (for reporting)
        recent_vals: deque = deque(maxlen=63)       # rolling 63-day window for guardrail
        positions:   List[Position]    = []
        trades:      List[Dict]        = []
        daily_values: Dict[pd.Timestamp, float] = {}
        spy_shares:  float = 0.0                    # residual SPY allocation

        fund_lookup = self.universe_df.set_index("ticker")

        print(f"\n{'='*62}")
        print(f"Walk-forward: {trading_days[0].date()} → {trading_days[-1].date()}")
        print(f"Days: {len(trading_days)} | Universe: {len(self.universe_df)} stocks")
        print(f"Capital: ${self.initial_capital:,.0f} | Max positions: {self.max_positions}")
        print(f"{'='*62}\n")

        for date in trading_days:

            # ── 1. Close / manage open positions ──────────────────────────────
            still_open: List[Position] = []
            for pos in positions:
                df = self.all_ohlcv.get(pos.ticker)
                if df is None or date not in df.index:
                    still_open.append(pos)
                    continue

                day_high  = float(df.loc[date, "High"])
                day_low   = float(df.loc[date, "Low"])
                day_close = float(df.loc[date, "Close"])
                ind_df    = self.ind_map.get(pos.ticker)

                def _atr_now() -> float:
                    if ind_df is not None and date in ind_df.index:
                        return float(ind_df.loc[date, "atr14"])
                    return 0.0

                if pos.half_exited:
                    # Ratchet up trailing stop
                    atr = _atr_now()
                    if atr > 0:
                        new_ts = day_close - self.trail_mult * atr
                        pos.trailing_stop = max(pos.trailing_stop, new_ts)
                    effective_stop = max(pos.stop, pos.trailing_stop)

                    if day_low <= effective_stop:
                        cash += pos.shares * effective_stop
                        self._log_trade(trades, pos, date, effective_stop, "trail_stop")
                    else:
                        still_open.append(pos)

                else:
                    hold_days = (date - pos.entry_date).days

                    # Hard stop (always checked first)
                    if day_low <= pos.stop:
                        cash += pos.shares * pos.stop
                        self._log_trade(trades, pos, date, pos.stop, "stop")

                    # T1: exit half, move stop to breakeven, start trailing
                    elif day_high >= pos.t1_price:
                        half = pos.shares * 0.5
                        cash += half * pos.t1_price
                        self._log_trade(trades, pos, date, pos.t1_price, "t1_partial", shares=half)
                        atr = _atr_now()
                        pos.shares       *= 0.5
                        pos.half_exited   = True
                        pos.stop          = pos.entry_price   # breakeven
                        # Anchor trailing stop to T1 price, not day_close — prevents
                        # an intraday reversal on T1 day from stopping out immediately.
                        pos.trailing_stop = (pos.t1_price - self.trail_mult * atr) if atr > 0 else pos.entry_price
                        still_open.append(pos)

                    # Time stop: no progress after N days → free up capital
                    elif hold_days >= self.time_stop_days:
                        cash += pos.shares * day_close
                        self._log_trade(trades, pos, date, day_close, "time_stop")

                    else:
                        still_open.append(pos)

            positions = still_open

            # ── 2. Daily portfolio value (includes residual SPY) ──────────
            open_value = sum(
                float(self.all_ohlcv[p.ticker].loc[date, "Close"]) * p.shares
                if p.ticker in self.all_ohlcv and date in self.all_ohlcv[p.ticker].index
                else p.entry_price * p.shares
                for p in positions
            )
            spy_price_today = 0.0
            if self.spy_residual and self.spy_ohlcv is not None and date in self.spy_ohlcv.index:
                spy_price_today = float(self.spy_ohlcv.loc[date, "Close"])
            spy_value = spy_shares * spy_price_today
            portfolio_value = cash + open_value + spy_value
            peak = max(peak, portfolio_value)
            recent_vals.append(portfolio_value)
            daily_values[date] = portfolio_value

            # ── 3. Drawdown guardrail (rolling 63-day peak) ───────────────
            # Use recent high rather than all-time peak so a single drawdown
            # event doesn't permanently block entries for the rest of history.
            rolling_peak = max(recent_vals)
            drawdown = (rolling_peak - portfolio_value) / rolling_peak if rolling_peak > 0 else 0
            if drawdown >= self.halt_dd:
                # During halt: keep SPY residual parked as-is (passive), skip stocks
                continue

            # ── 4. Screen for new entries ─────────────────────────────────
            # Liquidate SPY residual first so the cash is available for stock entries.
            if self.spy_residual and spy_shares > 0 and spy_price_today > 0:
                cash += spy_shares * spy_price_today
                spy_shares = 0.0

            slots = self.max_positions - len(positions)
            if slots <= 0:
                # No free slots: redeploy all cash back to SPY
                if self.spy_residual and spy_price_today > 0 and cash > 0:
                    spy_shares = cash / spy_price_today
                    cash = 0.0
                continue

            open_tickers = {p.ticker for p in positions}
            rs_today = _get_rs(self.rs_cache, date)
            candidates: List[Dict] = []

            for ticker, ind_df in self.ind_map.items():
                # Skip sector ETFs (they are not tradeable candidates)
                if ticker in SECTOR_ETFS.values():
                    continue
                if ticker in open_tickers:
                    continue
                if ticker not in fund_lookup.index:
                    continue
                if date not in ind_df.index:
                    continue

                # EMA200 uptrend check (21-day look-back)
                date_iloc = ind_df.index.get_loc(date)
                if date_iloc < 221:
                    continue
                e200_now = float(ind_df["ema200"].iloc[date_iloc])
                e200_21d = float(ind_df["ema200"].iloc[date_iloc - 21])
                if e200_now <= e200_21d:
                    continue

                ind_row = ind_df.loc[date]

                # RS Rating gate
                rs = rs_today.get(ticker, 0)
                if rs < int(l3_cfg.get("min_rs_rating", 80)):
                    continue

                # L3 filter
                if not _passes_l3(ind_row, l3_cfg):
                    continue

                # Volume ratio
                vol_avg   = float(ind_row.get("vol_avg20", 0))
                vol_today = float(ind_row.get("volume", 0))
                if vol_avg > 0 and vol_today / vol_avg < float(l3_cfg.get("min_volume_ratio_vs_avg20d", 1.5)):
                    continue

                # VCP filter (required or optional per config)
                ohlcv_slice = self.all_ohlcv[ticker][self.all_ohlcv[ticker].index <= date]
                vcp_ok, _vcp_sc = _passes_vcp(ohlcv_slice, l3_cfg)
                if not vcp_ok:
                    continue

                # Sector alignment
                if l3_cfg.get("require_sector_alignment", False):
                    etf = self.sector_etf_map.get(ticker)
                    if etf and etf in self.ind_map:
                        etf_ind = self.ind_map[etf]
                        if date in etf_ind.index:
                            etf_row = etf_ind.loc[date]
                            if float(etf_row["close"]) < float(etf_row["ema50"]):
                                continue  # sector ETF below EMA50

                # SimFin historical L2 check
                if self.simfin_data:
                    try:
                        from simfin_loader import get_fundamentals_at
                        hist_fund = get_fundamentals_at(self.simfin_data, ticker, date)
                        if not _passes_l2_historical(hist_fund, l2_cfg):
                            continue
                    except ImportError:
                        pass

                # L4 score
                fund_row = fund_lookup.loc[ticker]
                score = _l4_score(ind_row, fund_row, rs, ohlcv_slice, l4_cfg)

                if score >= min_score:
                    candidates.append({
                        "ticker": ticker,
                        "price":  float(ind_row["close"]),
                        "atr":    float(ind_row["atr14"]),
                        "score":  score,
                    })

            candidates.sort(key=lambda x: x["score"], reverse=True)

            for cand in candidates[:slots]:
                price     = cand["price"]
                atr       = cand["atr"]
                stop_dist = self.stop_mult * atr
                if stop_dist <= 0:
                    continue
                stop   = price - stop_dist
                target = price + self.reward_ratio * stop_dist
                t1     = price * (1 + self.t1_gain_pct)

                shares = min(
                    (portfolio_value * self.risk_pct) / stop_dist,
                    (portfolio_value * self.max_pos_pct) / price,
                )
                cost = shares * price
                if cost > cash or shares < 0.01:
                    continue

                cash -= cost
                positions.append(Position(
                    ticker=cand["ticker"],
                    entry_date=date,
                    entry_price=price,
                    shares=shares,
                    stop=stop,
                    target=target,
                    score=cand["score"],
                    t1_price=t1,
                    trailing_stop=stop,
                    position_id=self._pos_counter,
                ))
                self._pos_counter += 1
                print(
                    f"  [{date.date()}] OPEN  {cand['ticker']:6s} "
                    f"@ ${price:.2f}  stop ${stop:.2f}  T1 ${t1:.2f} "
                    f"score {cand['score']:.0f}  cash left: ${cash:,.0f}"
                )

            # ── 5. Redeploy remaining cash to SPY ─────────────────────────
            if self.spy_residual and spy_price_today > 0 and cash > 0:
                spy_shares = cash / spy_price_today
                cash = 0.0

        # Close remaining open positions at final close price
        for pos in positions:
            df = self.all_ohlcv.get(pos.ticker)
            last_price = float(df["Close"].iloc[-1]) if df is not None else pos.entry_price
            last_date  = df.index[-1] if df is not None else trading_days[-1]
            cash += pos.shares * last_price
            self._log_trade(trades, pos, last_date, last_price, "end_of_backtest")

        daily_series = pd.Series(daily_values, name="portfolio_value")
        returns = daily_series.pct_change().dropna()
        return returns, pd.DataFrame(trades)


# ── Summary + Report ──────────────────────────────────────────────────────────

def _print_summary(
    returns: pd.Series, trades_df: pd.DataFrame, bias_note: str
) -> None:
    print(f"\n{'='*62}")
    print("BACKTEST SUMMARY")
    print(f"{'='*62}")

    if trades_df.empty:
        print("No trades executed.")
        return

    closed = trades_df[trades_df["outcome"] != "end_of_backtest"].copy()

    # ── Combine two-stage exit rows into one position record ─────────────
    # Each position with a t1_partial exit generates 2 rows (t1_partial +
    # trail_stop). Group by position_id to compute the true combined P&L.
    position_stats = []
    if "position_id" in closed.columns:
        for pid, grp in closed.groupby("position_id"):
            total_pnl_usd = grp["pnl_usd"].sum()
            entry_price   = grp["entry_price"].iloc[0]
            # Reconstruct full share count: t1_partial logs half shares,
            # trail_stop logs the other half — sum gives full position size.
            full_shares = grp["shares"].sum()
            if full_shares <= 0 or entry_price <= 0:
                continue
            combined_pnl_pct = (total_pnl_usd / (entry_price * full_shares)) * 100
            outcomes_set = set(grp["outcome"].tolist())
            # Determine summary outcome label
            if "stop" in outcomes_set:
                outcome_label = "stop"
            elif "time_stop" in outcomes_set:
                outcome_label = "time_stop"
            elif "t1_partial" in outcomes_set and "trail_stop" in outcomes_set:
                outcome_label = "two_stage"
            elif "t1_partial" in outcomes_set:
                outcome_label = "t1_partial_open"
            else:
                outcome_label = grp["outcome"].iloc[-1]
            position_stats.append({
                "pnl_pct":      combined_pnl_pct,
                "outcome":      outcome_label,
                "hold_days":    int(grp["hold_days"].max()),
            })
    else:
        # Fallback for CSVs without position_id
        for _, row in closed.iterrows():
            position_stats.append({
                "pnl_pct":  row["pnl_pct"],
                "outcome":  row["outcome"],
                "hold_days": row["hold_days"],
            })

    stats_df  = pd.DataFrame(position_stats)
    wins      = stats_df[stats_df["pnl_pct"] > 0]
    losses    = stats_df[stats_df["pnl_pct"] <= 0]
    n_pos     = len(stats_df)

    total_return = float((1 + returns).prod() - 1)
    win_rate  = len(wins) / max(n_pos, 1) * 100
    avg_win   = float(wins["pnl_pct"].mean()) if not wins.empty else 0.0
    avg_loss  = float(losses["pnl_pct"].mean()) if not losses.empty else 0.0
    expectancy = (win_rate / 100 * avg_win) + ((1 - win_rate / 100) * avg_loss)
    avg_hold  = float(stats_df["hold_days"].mean()) if not stats_df.empty else 0.0
    outcomes  = stats_df["outcome"].value_counts().to_dict()

    print(f"  Total positions:      {n_pos}")
    print(f"  Win rate:             {win_rate:.1f}%")
    print(f"  Avg win:              {avg_win:+.1f}%")
    print(f"  Avg loss:             {avg_loss:+.1f}%")
    print(f"  Expectancy / trade:   {expectancy:+.2f}%")
    print(f"  Avg hold days:        {avg_hold:.1f}")
    print(f"  Outcomes:             {outcomes}")
    print(f"  Total return:         {total_return * 100:+.1f}%")
    ann = ((1 + total_return) ** (252 / max(len(returns), 1)) - 1) * 100
    print(f"  Annualised return:    {ann:+.1f}%")
    print(f"\n  {bias_note}")
    print(f"{'='*62}\n")


def _generate_report(
    returns: pd.Series, trades_df: pd.DataFrame, bias_note: str
) -> None:
    try:
        import quantstats as qs
        qs.extend_pandas()
        qs.reports.html(
            returns,
            benchmark="SPY",
            output=str(REPORT_PATH),
            title=f"MCSS Backtest — 10 Year Walk-Forward Simulation ({bias_note})",
            download_filename=str(REPORT_PATH),
        )
        print(f"HTML report → {REPORT_PATH}")
    except Exception as e:
        print(f"quantstats report failed ({e}); trades saved to {TRADES_PATH}")

    if not trades_df.empty:
        trades_df.to_csv(TRADES_PATH, index=False)
        print(f"Trades CSV  → {TRADES_PATH}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="MCSS Backtesting Engine")
    parser.add_argument("--dry-run",       action="store_true",
                        help="Download + cache data only, skip simulation")
    parser.add_argument("--refresh-cache", action="store_true",
                        help="Force re-download all OHLCV data")
    parser.add_argument("--universe",      default="data/l1_passed.csv",
                        help="Path to universe CSV (default: L1 570-ticker universe)")
    parser.add_argument("--no-l2",         action="store_true",
                        help="Explicit L1 mode (same as default, kept for compatibility)")
    parser.add_argument("--use-simfin",    action="store_true",
                        help="Apply historical L2 filter via SimFin (requires SIMFIN_API_KEY)")
    args = parser.parse_args()

    config_path = ROOT / "config" / "criteria.yaml"
    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    # Universe selection
    if args.no_l2:
        universe_path = ROOT / "data" / "l1_passed.csv"
        bias_note = "L1 universe (570 tickers) — less survivorship bias"
    else:
        universe_path = ROOT / args.universe
        bias_note = "L1 universe (570 tickers) — less survivorship bias"

    if not universe_path.exists():
        print(f"Universe not found: {universe_path}")
        print("Run the pipeline first:  python scripts/run_pipeline.py")
        sys.exit(1)

    universe_df = pd.read_csv(universe_path)
    tickers = universe_df["ticker"].tolist()

    print(f"\nMCSS Backtesting Engine  ({datetime.now(timezone.utc).strftime('%Y-%m-%d')})")
    print(f"Universe: {len(tickers)} tickers  |  {bias_note}\n")

    # Sector ETF tickers — always download so alignment check works
    l3_cfg = cfg.get("l3_technical", {})
    bt_cfg_pre = cfg.get("backtest", {})
    all_tickers = list(tickers)
    if l3_cfg.get("require_sector_alignment", False):
        etf_tickers = list(set(SECTOR_ETFS.values()))
        extra = [e for e in etf_tickers if e not in all_tickers]
        all_tickers.extend(extra)
    # SPY — always download for residual allocation + quantstats benchmark
    if "SPY" not in all_tickers:
        all_tickers.append("SPY")

    # Phase A — data cache
    cache = DataCache()
    all_ohlcv = cache.load_all(all_tickers, refresh=args.refresh_cache)

    if args.dry_run:
        print(f"\n--dry-run complete. {len(all_ohlcv)} tickers cached → {CACHE_DIR}")
        sys.exit(0)

    if not all_ohlcv:
        print("No OHLCV data. Check internet connection.")
        sys.exit(1)

    # Phase B — SimFin historical fundamentals (optional)
    simfin_data: Dict = {}
    if args.use_simfin:
        try:
            sys.path.insert(0, str(ROOT / "scripts"))
            from simfin_loader import setup_simfin, load_income
            setup_simfin()
            print("Loading SimFin historical fundamentals...")
            simfin_data = load_income(tickers)
            print(f"SimFin data loaded for {len(simfin_data)} tickers")
        except Exception as e:
            print(f"SimFin load failed ({e}); proceeding without historical L2")

    # Phase C — pre-compute indicators + RS
    print("\nBuilding indicator matrix...")
    ind_map = _build_indicator_matrix(all_ohlcv)
    print(f"Indicators built for {len(ind_map)} tickers")

    all_dates = sorted({d for df in all_ohlcv.values() for d in df.index})
    bt_cfg = cfg.get("backtest", {})
    window = int(bt_cfg.get("window_days", 2520))
    warmup = int(bt_cfg.get("warmup_days", 260))

    if len(all_dates) < warmup + 20:
        print(f"Insufficient history ({len(all_dates)} days, need {warmup + 20})")
        sys.exit(1)

    trading_days = all_dates[-(window + 1):]

    rs_step  = int(bt_cfg.get("rs_recompute_interval_days", 5))
    rs_cache = _precompute_rs_ratings(ind_map, all_dates, step=rs_step)

    # Phase D — walk-forward simulation
    sim = PortfolioSimulator(
        cfg, ind_map, all_ohlcv, universe_df, rs_cache,
        simfin_data=simfin_data, bias_note=bias_note,
    )
    returns, trades_df = sim.run(trading_days)

    # Phase E — report
    _print_summary(returns, trades_df, bias_note)
    _generate_report(returns, trades_df, bias_note)


if __name__ == "__main__":
    main()
