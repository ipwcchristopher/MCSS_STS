"""Unit tests for market_brief.rank_sectors (pure function, synthetic prices)."""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from market_brief import rank_sectors


def _make_closes(days: int = 30) -> pd.DataFrame:
    """SPY flat at 100; STRONG rises 1%/day; WEAK falls 1%/day."""
    idx = pd.date_range("2026-01-01", periods=days, freq="B")
    return pd.DataFrame({
        "SPY": np.full(days, 100.0),
        "STRONG": 100.0 * (1.01 ** np.arange(days)),
        "WEAK": 100.0 * (0.99 ** np.arange(days)),
    }, index=idx)


def test_rank_sectors_orders_strongest_first():
    closes = _make_closes()
    ranked = rank_sectors(closes, ["STRONG", "WEAK"], "SPY", [1, 5, 20])
    assert [r["etf"] for r in ranked] == ["STRONG", "WEAK"]
    assert ranked[0]["composite"] > 0 > ranked[1]["composite"]


def test_rank_sectors_relative_return_math():
    closes = _make_closes()
    ranked = rank_sectors(closes, ["STRONG"], "SPY", [5])
    # STRONG 5d return = 1.01^5 - 1 ≈ 5.10%; SPY = 0% → rel ≈ +5.10 pct points
    assert abs(ranked[0]["rel_5d"] - 5.10) < 0.02
    assert ranked[0]["composite"] == ranked[0]["rel_5d"]


def test_rank_sectors_missing_benchmark_returns_empty():
    closes = _make_closes().drop(columns=["SPY"])
    assert rank_sectors(closes, ["STRONG"], "SPY", [5]) == []


def test_rank_sectors_skips_unknown_and_short_series():
    closes = _make_closes(days=3)  # too short for 5d/20d lookbacks
    ranked = rank_sectors(closes, ["STRONG", "MISSING"], "SPY", [1, 5, 20])
    # STRONG still ranked via the 1d lookback; MISSING skipped entirely
    assert [r["etf"] for r in ranked] == ["STRONG"]
    assert "rel_1d" in ranked[0] and "rel_5d" not in ranked[0]


def test_rank_sectors_sector_name_mapped():
    closes = _make_closes().rename(columns={"STRONG": "XLK"})
    ranked = rank_sectors(closes, ["XLK"], "SPY", [5])
    assert ranked[0]["sector"] == "Technology"
