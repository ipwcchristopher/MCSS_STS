"""Shared pytest fixtures for MCSS test suite."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))


@pytest.fixture
def sample_config() -> dict:
    with open(ROOT / "config" / "criteria.yaml") as f:
        return yaml.safe_load(f)


def _make_ohlcv(
    n: int = 260,
    trend: float = 0.001,
    noise: float = 0.015,
    start_price: float = 100.0,
    base_volume: int = 3_000_000,
    seed: int = 42,
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2024-01-01", periods=n, freq="B")
    returns = rng.normal(trend, noise, n)
    close = pd.Series(start_price * np.cumprod(1 + returns), index=dates)
    high = close * (1 + np.abs(rng.normal(0, 0.005, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.005, n)))
    volume = pd.Series(
        rng.integers(base_volume // 2, base_volume * 2, n).astype(float), index=dates
    )
    return pd.DataFrame({
        "Open": close * 0.999, "High": high, "Low": low,
        "Close": close, "Volume": volume,
    })


@pytest.fixture
def sample_ohlcv() -> pd.DataFrame:
    return _make_ohlcv(n=260)


@pytest.fixture
def uptrend_ohlcv() -> pd.DataFrame:
    n = 260
    dates = pd.date_range("2024-01-01", periods=n, freq="B")
    price = pd.Series(50.0 + np.arange(n) * 0.5, index=dates)
    return pd.DataFrame({
        "Open":   price * 0.999,
        "High":   price * 1.006,
        "Low":    price * 0.994,
        "Close":  price,
        "Volume": pd.Series([3_500_000.0] * n, index=dates),
    })


@pytest.fixture
def make_ohlcv():
    return _make_ohlcv
