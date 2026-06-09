"""Unit tests for scripts/technical_filter.py"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from technical_filter import _apply_l3_filter


def _make_uptrend_ohlcv(n: int = 260) -> pd.DataFrame:
    """Generate synthetic uptrend OHLCV that passes all Trend Template checks."""
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    price = pd.Series(50.0 + np.arange(n) * 0.5, index=idx)
    return pd.DataFrame({
        "Open":   price * 0.999,
        "High":   price * 1.006,
        "Low":    price * 0.994,
        "Close":  price,
        "Volume": pd.Series([3_500_000.0] * n, index=idx),
    })


def _relaxed_cfg(vcp_optional: bool) -> dict:
    """L3 config with relaxed limits so VCP check is the decisive gate.

    RSI max is set to 100 so a linear-uptrend OHLCV (RSI ~100) doesn't block
    early, letting us verify whether VCP enforcement fires correctly.
    """
    return {"l3_technical": {
        "min_rs_rating": 70,
        "min_rsi14": 1, "max_rsi14": 100,  # allow RSI up to 100 so VCP is decisive
        "min_volume_ratio_vs_avg20d": 0.5,
        "max_distance_from_52w_high_pct": 30,
        "vcp_optional": vcp_optional,
        "min_vcp_score": 60,
    }}


def test_vcp_hard_filter_when_not_optional():
    """When vcp_optional=False and VCP not detected, stock must fail L3."""
    ohlcv = _make_uptrend_ohlcv()
    passed, reason, _ = _apply_l3_filter(
        pd.Series({"ticker": "TEST"}), ohlcv, 85, False, 0, _relaxed_cfg(False)
    )
    assert not passed, "Should fail when VCP required but not detected"
    assert "vcp" in reason.lower(), f"Reason should mention vcp, got: {reason}"


def test_vcp_optional_skips_filter():
    """When vcp_optional=True, VCP detection does not block the stock."""
    ohlcv = _make_uptrend_ohlcv()
    passed, reason, _ = _apply_l3_filter(
        pd.Series({"ticker": "TEST"}), ohlcv, 85, False, 0, _relaxed_cfg(True)
    )
    assert passed, f"Should pass when VCP is optional; reason: {reason}"


# ── Indicator helper unit tests ────────────────────────────────────────────────

from technical_filter import _ema, _rsi, _atr, _volume_ratio


def test_ema_convergence():
    """EMA20 of a constant series must equal that constant."""
    prices = pd.Series([100.0] * 50)
    result = _ema(prices, 20)
    assert abs(float(result.iloc[-1]) - 100.0) < 0.01


def test_rsi_overbought():
    """Strongly trending up series → RSI near 100."""
    prices = pd.Series(np.linspace(50, 150, 50))
    rsi = _rsi(prices)
    assert rsi > 80, f"Expected RSI > 80 for uptrend, got {rsi}"


def test_rsi_oversold():
    """Strongly trending down series → RSI near 0."""
    prices = pd.Series(np.linspace(150, 50, 50))
    rsi = _rsi(prices)
    assert rsi < 20, f"Expected RSI < 20 for downtrend, got {rsi}"


def test_atr_positive():
    """ATR is always positive for valid OHLCV."""
    idx = pd.date_range("2024-01-01", periods=30)
    close = pd.Series([100.0] * 30, index=idx)
    high  = close * 1.01
    low   = close * 0.99
    atr = _atr(high, low, close)
    assert atr > 0


def test_volume_ratio_uses_prior_complete_day():
    """_volume_ratio uses iloc[-2] so an abnormal last-day volume doesn't distort the ratio."""
    # 26 values: 25 normal days at 3M, then a 100-share last day
    vols = pd.Series([3_000_000.0] * 25 + [100.0])
    ratio = _volume_ratio(vols, window=20)
    # Should reflect ~3M / ~3M ≈ 1.0, not the 100-share outlier
    assert 0.8 <= ratio <= 1.2, f"Expected ratio ~1.0, got {ratio}"


# ── L3 integration tests ───────────────────────────────────────────────────────

def test_l3_fomo_block():
    """Stock with RSI > 72 must fail L3 regardless of other passing conditions."""
    # Inject a 40% gap in last 14 days to force RSI well above 72
    ohlcv = _make_uptrend_ohlcv(260)
    close = ohlcv["Close"].copy()
    close.iloc[-14:] = float(close.iloc[-15]) * np.linspace(1.0, 1.40, 14)
    ohlcv = ohlcv.copy()
    ohlcv["Close"] = close
    ohlcv["High"]  = close * 1.005
    ohlcv["Low"]   = close * 0.995

    cfg = {"l3_technical": {
        "min_rs_rating": 70, "min_rsi14": 45, "max_rsi14": 72,
        "min_volume_ratio_vs_avg20d": 0.5,
        "max_distance_from_52w_high_pct": 25,
        "vcp_optional": True,
    }}
    passed, reason, _ = _apply_l3_filter(
        pd.Series({"ticker": "TEST"}), ohlcv, 85, False, 0, cfg
    )
    assert not passed
    assert "rsi" in reason.lower(), f"Expected RSI block reason, got: {reason}"


def test_l3_insufficient_history():
    """OHLCV with < 210 rows must be excluded with 'insufficient_history'."""
    ohlcv = _make_uptrend_ohlcv(100)   # only 100 days
    passed, reason, _ = _apply_l3_filter(
        pd.Series({"ticker": "TEST"}), ohlcv, 85, True, 80, _relaxed_cfg(True)
    )
    assert not passed
    assert "insufficient_history" in reason


def test_l3_passes_fully_qualifying_stock():
    """Stock meeting every L3 condition must pass with empty reason."""
    # Use very relaxed limits so the linear-uptrend synthetic data passes all gates
    cfg = {"l3_technical": {
        "min_rs_rating": 70, "min_rsi14": 1, "max_rsi14": 100,
        "min_volume_ratio_vs_avg20d": 0.5,
        "max_distance_from_52w_high_pct": 30,
        "vcp_optional": True,
    }}
    ohlcv = _make_uptrend_ohlcv(260)
    passed, reason, metrics = _apply_l3_filter(
        pd.Series({"ticker": "TREND"}), ohlcv, 85, True, 80, cfg
    )
    assert passed, f"Expected pass; reason: {reason}; rsi={metrics.get('rsi14')}"
    assert reason == ""
