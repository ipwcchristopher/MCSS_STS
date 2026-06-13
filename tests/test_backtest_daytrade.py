"""Unit tests for backtest_daytrade trade simulation + metrics (synthetic bars)."""

import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from backtest_daytrade import simulate_breakout_trade, summarize

ET = ZoneInfo("America/New_York")
DAY = date(2026, 3, 16)  # a Monday


def _bars(rows):
    """rows: list of (minute_offset_from_open, open, high, low, close)."""
    open_t = datetime.combine(DAY, datetime.min.time(), tzinfo=ET).replace(hour=9, minute=30)
    idx = [open_t + timedelta(minutes=m) for m, *_ in rows]
    data = [{"open": o, "high": h, "low": l, "close": c} for _, o, h, l, c in rows]
    return pd.DataFrame(data, index=pd.DatetimeIndex(idx))


# ── simulate_breakout_trade ────────────────────────────────────────────────────

def test_no_breakout_returns_none():
    # opening range 0-14min sets OR high=101; price never exceeds it after
    rows = [(m, 100, 101, 99, 100) for m in range(0, 30)]
    assert simulate_breakout_trade(_bars(rows), DAY, 15, 2.0, 0.0) is None


def test_target_hit_returns_target_r():
    rows = [(m, 100, 101, 99, 100) for m in range(0, 15)]   # OR high 101, low 99
    # break out then run to target: entry=101, stop=99, risk=2, target=101+4=105
    rows += [(15, 101, 106, 101, 105)]
    res = simulate_breakout_trade(_bars(rows), DAY, 15, 2.0, 0.0)
    assert res["exit"] == "target" and res["r"] == 2.0


def test_stop_hit_returns_minus_one_r():
    rows = [(m, 100, 101, 99, 100) for m in range(0, 15)]   # OR low 99
    rows += [(15, 101, 102, 101, 101.5)]   # triggers entry at 101
    rows += [(16, 101, 101, 98, 98.5)]     # drops through stop 99
    res = simulate_breakout_trade(_bars(rows), DAY, 15, 2.0, 0.0)
    assert res["exit"] == "stop" and res["r"] == -1.0


def test_eod_exit_partial_r():
    rows = [(m, 100, 101, 99, 100) for m in range(0, 15)]
    rows += [(15, 101, 102, 101, 101.5)]   # entry 101, stop 99, risk 2
    # drift up but never hit target 105; last bar near close
    rows += [(m, 102, 103, 101.5, 102) for m in range(16, 400)]
    res = simulate_breakout_trade(_bars(rows), DAY, 15, 2.0, 0.0)
    assert res["exit"] == "eod"
    assert 0 < res["r"] < 2.0   # (102-101)/2 = 0.5R


def test_atr_narrows_stop():
    # OR low 95 (wide), but ATR=1 → stop = entry-1 = 100, much tighter
    rows = [(m, 100, 101, 95, 100) for m in range(0, 15)]   # OR high 101, low 95
    rows += [(15, 101, 102, 101, 101.5)]   # entry 101
    rows += [(16, 101, 101, 99.5, 100)]    # dips to 99.5: below ATR-stop 100, above OR-low 95
    res = simulate_breakout_trade(_bars(rows), DAY, 15, 2.0, daily_atr=1.0)
    assert res["exit"] == "stop"   # ATR stop (100) triggered


def test_empty_bars_returns_none():
    assert simulate_breakout_trade(pd.DataFrame(), DAY, 15, 2.0, 0.0) is None


# ── summarize ──────────────────────────────────────────────────────────────────

def test_summarize_basic_metrics():
    trades = [{"r": 2.0}, {"r": 2.0}, {"r": -1.0}, {"r": -1.0}, {"r": -1.0}]
    s = summarize(trades, trading_days=5)
    assert s["n_trades"] == 5
    assert s["win_rate_pct"] == 40.0
    # expectancy = (2+2-1-1-1)/5 = 0.2
    assert abs(s["expectancy_r"] - 0.2) < 1e-9
    # profit factor = 4 / 3 ≈ 1.33
    assert abs(s["profit_factor"] - 1.33) < 0.01
    assert s["cum_r"] == 1.0


def test_summarize_max_consecutive_losses():
    trades = [{"r": -1.0}, {"r": -1.0}, {"r": 2.0}, {"r": -1.0}, {"r": -1.0}, {"r": -1.0}]
    assert summarize(trades, 6)["max_consec_losses"] == 3


def test_summarize_empty():
    s = summarize([], 0)
    assert s["n_trades"] == 0 and s["profit_factor"] == 0.0


def test_summarize_all_wins_infinite_pf():
    s = summarize([{"r": 2.0}, {"r": 2.0}], 2)
    assert s["profit_factor"] == float("inf")
    assert s["win_rate_pct"] == 100.0
