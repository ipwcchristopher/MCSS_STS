"""Unit tests for indicators/vcp.py"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from indicators.vcp import compute_vcp_score, compute_vcp_batch


def _build_ohlcv(prices: list, volumes: list) -> pd.DataFrame:
    """Build OHLCV from per-period (price_amplitude, volume) pairs."""
    idx = pd.date_range("2024-01-01", periods=len(prices), freq="B")
    close = pd.Series([p for p in prices], index=idx, dtype=float)
    return pd.DataFrame({
        "Open":   close * 0.999,
        "High":   close * 1.005,
        "Low":    close * 0.995,
        "Close":  close,
        "Volume": pd.Series(volumes, index=idx, dtype=float),
    })


def _make_contracting_ohlcv() -> pd.DataFrame:
    """60 days with strong price-range and volume contraction across 3x20-day windows."""
    idx = pd.date_range("2024-01-01", periods=60, freq="B")
    base = 100.0

    # Price ranges: ±10% / ±5% / ±2%  → r1=0.20, r2=0.10, r3=0.04
    prices = []
    amplitudes = [0.10, 0.05, 0.02]
    for amp in amplitudes:
        prices.extend([base + base * amp * np.sin(j * np.pi / 10) for j in range(20)])

    # Volumes: 3M / 1.5M / 0.9M → v2 < v1, v3 < v2, v3 < v1*0.6
    volumes = [3_000_000.0] * 20 + [1_500_000.0] * 20 + [900_000.0] * 20

    close = pd.Series(prices, index=idx)
    return pd.DataFrame({
        "Open":   close * 0.999,
        "High":   close * (1 + pd.Series(amplitudes).repeat(20).values * 0.5),
        "Low":    close * (1 - pd.Series(amplitudes).repeat(20).values * 0.5),
        "Close":  close,
        "Volume": pd.Series(volumes, index=idx),
    })


def test_vcp_detected_with_strong_contraction():
    """Classic VCP: price range and volume both strictly contracting → detected=True, score >= 60."""
    ohlcv = _make_contracting_ohlcv()
    detected, score = compute_vcp_score(ohlcv)
    assert detected, f"Expected VCP detected with strong contraction; score={score}"
    assert score >= 60


def test_vcp_not_detected_with_expansion():
    """Anti-VCP: price range and volume both expanding → score = 0, detected=False."""
    idx = pd.date_range("2024-01-01", periods=60, freq="B")
    base = 100.0
    # Ranges EXPAND: ±2% / ±5% / ±10%
    prices = []
    amplitudes = [0.02, 0.05, 0.10]
    for amp in amplitudes:
        prices.extend([base + base * amp * np.sin(j * np.pi / 10) for j in range(20)])
    # Volumes INCREASE: 1M / 2M / 4M
    volumes = [1_000_000.0] * 20 + [2_000_000.0] * 20 + [4_000_000.0] * 20

    close = pd.Series(prices, index=idx)
    high = close * (1 + pd.Series(amplitudes).repeat(20).values * 0.5)
    low  = close * (1 - pd.Series(amplitudes).repeat(20).values * 0.5)
    ohlcv = pd.DataFrame({
        "Open": close * 0.999, "High": high, "Low": low,
        "Close": close, "Volume": pd.Series(volumes, index=idx),
    })
    detected, score = compute_vcp_score(ohlcv)
    assert not detected, f"Expanding ranges should not trigger VCP; score={score}"
    assert score == 0


def test_insufficient_history_returns_false_zero():
    """Less than 60 rows → (False, 0)."""
    idx = pd.date_range("2024-01-01", periods=30, freq="B")
    ohlcv = pd.DataFrame({
        "Open": [100.0] * 30, "High": [101.0] * 30,
        "Low":  [99.0] * 30,  "Close": [100.0] * 30,
        "Volume": [1_000_000.0] * 30,
    }, index=idx)
    detected, score = compute_vcp_score(ohlcv)
    assert detected is False
    assert score == 0


def test_score_is_bounded_0_to_100():
    """Score must never exceed 100 or go below 0."""
    ohlcv = _make_contracting_ohlcv()
    _, score = compute_vcp_score(ohlcv)
    assert 0 <= score <= 100


def test_batch_skips_empty_df_gracefully():
    """compute_vcp_batch returns (False, 0) for empty DataFrame without raising."""
    results = compute_vcp_batch({
        "GOOD": _make_contracting_ohlcv(),
        "BAD":  pd.DataFrame(),
    })
    assert "GOOD" in results
    assert "BAD" in results
    assert results["BAD"] == (False, 0)
    assert results["GOOD"][0] is True
