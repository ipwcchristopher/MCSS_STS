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
