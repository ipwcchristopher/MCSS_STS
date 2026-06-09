"""Unit tests for indicators/rs_rating.py"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from indicators.rs_rating import compute_rs_ratings


def test_strong_outperformer_gets_high_rating():
    """Stock that doubled while peers were flat → RS near 99."""
    dates = pd.date_range("2023-01-01", periods=260, freq="B")
    close = pd.DataFrame({
        "BEST": pd.Series(np.linspace(50, 100, 260), index=dates),
        "AVG1": pd.Series([100.0] * 260, index=dates),
        "AVG2": pd.Series([100.0] * 260, index=dates),
        "AVG3": pd.Series([100.0] * 260, index=dates),
        "AVG4": pd.Series([100.0] * 260, index=dates),
    })
    ratings = compute_rs_ratings(["BEST", "AVG1", "AVG2", "AVG3", "AVG4"], close)
    assert ratings["BEST"] >= 95, f"Expected RS >= 95, got {ratings['BEST']}"
    # 4 flat stocks tie at the median rank in a 5-stock universe → ~50, not < 30
    for t in ["AVG1", "AVG2", "AVG3", "AVG4"]:
        assert ratings[t] < ratings["BEST"], f"Flat stock should rate below BEST, got {ratings[t]}"


def test_worst_performer_gets_low_rating():
    """Stock that halved while peers were flat → RS near 0."""
    dates = pd.date_range("2023-01-01", periods=260, freq="B")
    close = pd.DataFrame({
        "WORST": pd.Series(np.linspace(100, 50, 260), index=dates),
        "F1": pd.Series([100.0] * 260, index=dates),
        "F2": pd.Series([100.0] * 260, index=dates),
        "F3": pd.Series([100.0] * 260, index=dates),
    })
    ratings = compute_rs_ratings(["WORST", "F1", "F2", "F3"], close)
    # In a 4-stock universe, bottom-ranked stock gets pct rank ~0.25 → RS ~25
    assert ratings["WORST"] <= 30, f"Expected RS <= 30 for halving stock, got {ratings['WORST']}"
    for t in ["F1", "F2", "F3"]:
        assert ratings[t] > ratings["WORST"], f"Flat stock should rate above WORST"


def test_insufficient_history_returns_zero():
    """Ticker with < 63 days of data must return RS = 0."""
    dates = pd.date_range("2024-01-01", periods=50, freq="B")
    close = pd.DataFrame({
        "SHORT": pd.Series([100.0] * 50, index=dates),
        "LONG":  pd.Series([100.0] * 50, index=dates),
    })
    ratings = compute_rs_ratings(["SHORT", "LONG"], close)
    assert ratings["SHORT"] == 0


def test_empty_ticker_list():
    """Empty input returns empty dict."""
    ratings = compute_rs_ratings([], pd.DataFrame())
    assert ratings == {}


def test_all_same_performance_gets_middle_rating():
    """All stocks with identical performance → all rated ~50."""
    dates = pd.date_range("2023-01-01", periods=260, freq="B")
    tickers = [f"T{i}" for i in range(10)]
    close = pd.DataFrame({t: pd.Series([100.0] * 260, index=dates) for t in tickers})
    ratings = compute_rs_ratings(tickers, close)
    for t in tickers:
        assert 30 <= ratings[t] <= 70, f"Expected ~50 for flat universe, got {ratings[t]}"


def test_rs_composite_formula_weights_recent_momentum():
    """Recent 3m performance should outscore older momentum (0.4 weight vs 0.2)."""
    dates = pd.date_range("2023-01-01", periods=260, freq="B")
    # Stock A: gains +20% only in the most recent 63 days (3m window)
    a_prices = [100.0] * (260 - 63) + list(np.linspace(100, 120, 63))
    # Stock B: gained +20% in the oldest 63-day block (12m ago), flat since
    b_prices = list(np.linspace(100, 120, 63)) + [120.0] * (260 - 63)
    close = pd.DataFrame({
        "A": pd.Series(a_prices, index=dates),
        "B": pd.Series(b_prices, index=dates),
    })
    ratings = compute_rs_ratings(["A", "B"], close)
    # A gains more from the 0.4-weighted 3m window → should rate higher than B
    assert ratings["A"] > ratings["B"], (
        f"Recent-momentum stock should outscore old-momentum. "
        f"Got A={ratings['A']}, B={ratings['B']}"
    )
