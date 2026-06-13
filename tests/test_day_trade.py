"""Unit tests for day_trade_screen ORB + Gap filters (synthetic data)."""

import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from day_trade_screen import (
    compute_orb_metrics,
    resolve_prev_session,
    screen_gap,
    screen_orb,
)

ORB_CFG = {
    "min_price": 5,
    "min_avg_volume_20d": 1_000_000,
    "min_rvol": 1.5,
    "min_atr_pct": 2.5,
    "min_change_pct": 3,
    "strong_rvol_override": 2.5,
    "close_range_top_pct": 30,
    "top_n": 5,
}


def _make_ohlcv(
    days: int = 40,
    base_price: float = 50.0,
    base_volume: float = 2_000_000,
    last_change_pct: float = 5.0,
    last_volume_mult: float = 3.0,
    close_at_range_pos: float = 0.9,   # 0..1, 1 = closed at high
    daily_range_pct: float = 4.0,
) -> pd.DataFrame:
    """Flat history then one configurable 'event' day at the end."""
    closes = np.full(days, base_price)
    closes[-1] = base_price * (1 + last_change_pct / 100)
    rng = closes * daily_range_pct / 100
    lows = closes - rng * close_at_range_pos
    highs = lows + rng
    volumes = np.full(days, base_volume)
    volumes[-1] = base_volume * last_volume_mult
    return pd.DataFrame({
        "Open": closes, "High": highs, "Low": lows, "Close": closes,
        "Volume": volumes,
    })


# ── compute_orb_metrics ────────────────────────────────────────────────────────

def test_metrics_change_and_rvol():
    m = compute_orb_metrics(_make_ohlcv(last_change_pct=5.0, last_volume_mult=3.0))
    assert abs(m["change_pct"] - 5.0) < 0.01
    assert abs(m["rvol"] - 3.0) < 0.01


def test_metrics_close_range_position():
    m = compute_orb_metrics(_make_ohlcv(close_at_range_pos=0.9))
    assert abs(m["close_range_pos"] - 90.0) < 0.5


def test_metrics_insufficient_history_returns_none():
    assert compute_orb_metrics(_make_ohlcv(days=10)) is None


def test_metrics_empty_frame_returns_none():
    assert compute_orb_metrics(pd.DataFrame()) is None


# ── screen_orb ─────────────────────────────────────────────────────────────────

def _meta(tickers):
    return pd.DataFrame({
        "ticker": tickers,
        "long_name": [f"{t} Inc" for t in tickers],
        "sector": ["Technology"] * len(tickers),
    }).set_index("ticker")


def test_screen_orb_keeps_strong_mover():
    ohlcv = {"GOOD": _make_ohlcv(last_change_pct=5, last_volume_mult=3)}
    out = screen_orb(ohlcv, _meta(["GOOD"]), ORB_CFG)
    assert list(out["ticker"]) == ["GOOD"]
    assert out.iloc[0]["long_name"] == "GOOD Inc"


def test_screen_orb_rejects_low_rvol():
    ohlcv = {"DULL": _make_ohlcv(last_change_pct=5, last_volume_mult=1.2)}
    assert screen_orb(ohlcv, _meta(["DULL"]), ORB_CFG).empty


def test_screen_orb_rejects_weak_close():
    ohlcv = {"FADE": _make_ohlcv(last_change_pct=5, last_volume_mult=3,
                                 close_at_range_pos=0.3)}
    assert screen_orb(ohlcv, _meta(["FADE"]), ORB_CFG).empty


def test_screen_orb_volume_explosion_overrides_small_change():
    # only +1% move but RVOL 4x → strong_rvol_override keeps it
    ohlcv = {"VOL": _make_ohlcv(last_change_pct=1, last_volume_mult=4)}
    out = screen_orb(ohlcv, _meta(["VOL"]), ORB_CFG)
    assert list(out["ticker"]) == ["VOL"]


def test_screen_orb_rejects_small_change_and_modest_volume():
    ohlcv = {"MEH": _make_ohlcv(last_change_pct=1, last_volume_mult=2)}
    assert screen_orb(ohlcv, _meta(["MEH"]), ORB_CFG).empty


def test_screen_orb_rejects_penny_and_illiquid():
    cheap = {"PNY": _make_ohlcv(base_price=2, last_change_pct=5, last_volume_mult=3)}
    thin = {"THN": _make_ohlcv(base_volume=200_000, last_change_pct=5, last_volume_mult=3)}
    assert screen_orb(cheap, _meta(["PNY"]), ORB_CFG).empty
    assert screen_orb(thin, _meta(["THN"]), ORB_CFG).empty


def test_screen_orb_sorted_by_score_desc():
    ohlcv = {
        "MID": _make_ohlcv(last_change_pct=4, last_volume_mult=2.0),
        "TOP": _make_ohlcv(last_change_pct=8, last_volume_mult=4.0),
    }
    out = screen_orb(ohlcv, _meta(["MID", "TOP"]), ORB_CFG)
    assert list(out["ticker"]) == ["TOP", "MID"]


# ── resolve_prev_session ───────────────────────────────────────────────────────

TODAY = date(2026, 6, 11)


def test_resolve_prev_session_daily_bar_is_yesterday():
    # pre-market before today's bar exists: daily_bar IS the previous session
    prev = resolve_prev_session(100.0, 105.0, 95.0, date(2026, 6, 10),
                                90.0, 92.0, 88.0, TODAY)
    assert prev == {"prev_close": 100.0, "prev_high": 105.0, "prev_low": 95.0}


def test_resolve_prev_session_daily_bar_is_today():
    # daily_bar already rolled to today's partial → use previous_daily_bar
    prev = resolve_prev_session(101.0, 102.0, 100.0, TODAY,
                                90.0, 92.0, 88.0, TODAY)
    assert prev == {"prev_close": 90.0, "prev_high": 92.0, "prev_low": 88.0}


def test_resolve_prev_session_no_usable_bars():
    assert resolve_prev_session(0.0, 0.0, 0.0, None, 0.0, 0.0, 0.0, TODAY) is None


# ── screen_gap ─────────────────────────────────────────────────────────────────

GAP_CFG = {"min_gap_pct": 3, "max_gap_pct": 12, "min_price": 5, "top_n": 5}


def _gap_row(ticker, pm_price, prev_close):
    return {"ticker": ticker, "long_name": f"{ticker} Inc", "sector": "Tech",
            "pm_price": pm_price, "prev_close": prev_close,
            "prev_high": prev_close * 1.02, "prev_low": prev_close * 0.97}


def test_screen_gap_keeps_in_band_and_sorts_desc():
    rows = [
        _gap_row("GAP5", 105.0, 100.0),   # +5%
        _gap_row("GAP8", 108.0, 100.0),   # +8%
    ]
    out = screen_gap(rows, GAP_CFG)
    assert list(out["ticker"]) == ["GAP8", "GAP5"]
    assert abs(out.iloc[0]["gap_pct"] - 8.0) < 0.01


def test_screen_gap_rejects_small_extreme_and_down_gaps():
    rows = [
        _gap_row("TINY", 101.0, 100.0),   # +1% — below min
        _gap_row("WILD", 120.0, 100.0),   # +20% — FOMO guardrail
        _gap_row("DOWN", 95.0, 100.0),    # -5% — long-only band
    ]
    assert screen_gap(rows, GAP_CFG).empty


def test_screen_gap_rejects_penny_price():
    assert screen_gap([_gap_row("PNY", 4.2, 4.0)], GAP_CFG).empty


def test_screen_gap_carries_prev_day_levels():
    out = screen_gap([_gap_row("GAP5", 105.0, 100.0)], GAP_CFG)
    assert abs(out.iloc[0]["day_high"] - 102.0) < 0.01
    assert abs(out.iloc[0]["day_low"] - 97.0) < 0.01
