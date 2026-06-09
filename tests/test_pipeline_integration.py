"""
Integration tests — runs filter functions end-to-end on synthetic CSV data.
No network calls. Verifies output schema and row counts at each pipeline stage.
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from fundamental_filter import apply_l1_filter, apply_l2_filter
from unittest.mock import patch


@pytest.fixture
def criteria_cfg() -> dict:
    with open(ROOT / "config" / "criteria.yaml") as f:
        return yaml.safe_load(f)


def _make_passing_universe(n: int = 10, seed: int = 99) -> pd.DataFrame:
    """Build n stocks that pass L1 hard filters with room to spare."""
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n):
        price = float(rng.uniform(20, 200))
        hi52 = price * float(rng.uniform(1.0, 1.20))   # within 20% of 52w high
        rows.append({
            "ticker":                  f"T{i:03d}",
            "price":                   price,
            "avg_volume_20d":          int(rng.integers(3_000_000, 10_000_000)),
            "market_cap":              int(rng.integers(1_000_000_000, 50_000_000_000)),
            "fifty_two_week_high":     hi52,
            "fifty_two_week_low":      price * float(rng.uniform(0.60, 0.90)),
            "revenue_growth":          float(rng.uniform(0.20, 0.80)),
            "gross_margins":           float(rng.uniform(0.40, 0.80)),
            "profit_margins":          float(rng.uniform(0.05, 0.40)),
            "operating_margins":       float(rng.uniform(0.05, 0.30)),
            "return_on_equity":        float(rng.uniform(0.10, 0.50)),
            "free_cashflow":           float(rng.uniform(1e8, 1e10)),
            "institutional_ownership": float(rng.uniform(0.45, 0.90)),
            "trailing_pe":             float(rng.uniform(15, 80)),
            "forward_pe":              float(rng.uniform(12, 60)),
            "peg_ratio":               float(rng.uniform(0.5, 2.5)),
            "price_to_sales":          float(rng.uniform(2, 12)),
            "short_percent_of_float":  float(rng.uniform(0.01, 0.08)),
            "earnings_date":           "",
            "sector":                  "Technology",
            "industry":                "Software",
            "quote_type":              "EQUITY",
            "long_name":               f"Test Corp {i}",
        })
    return pd.DataFrame(rows)


# ── L1 Filter ─────────────────────────────────────────────────────────────────

class TestL1Filter:
    def test_all_pass_on_clean_data(self, criteria_cfg):
        df = _make_passing_universe(10)
        passed, excluded = apply_l1_filter(df, criteria_cfg)
        assert len(passed) == 10
        assert len(excluded) == 0

    def test_low_price_excluded(self, criteria_cfg):
        df = _make_passing_universe(5)
        df.loc[0, "price"] = 5.0   # below min_price=10
        passed, excluded = apply_l1_filter(df, criteria_cfg)
        assert len(excluded) >= 1
        assert any("price" in r for r in excluded["filter_reason"].tolist())

    def test_low_volume_excluded(self, criteria_cfg):
        df = _make_passing_universe(5)
        df.loc[1, "avg_volume_20d"] = 500_000   # below min_avg_volume_20d=2M
        passed, excluded = apply_l1_filter(df, criteria_cfg)
        assert any("volume" in r for r in excluded["filter_reason"].tolist())

    def test_etf_excluded(self, criteria_cfg):
        df = _make_passing_universe(3)
        df.loc[2, "quote_type"] = "ETF"
        passed, excluded = apply_l1_filter(df, criteria_cfg)
        assert any("ETF" in r for r in excluded["filter_reason"].tolist())

    def test_earnings_play_flagged_not_excluded(self, criteria_cfg):
        df = _make_passing_universe(3)
        future = (datetime.utcnow() + timedelta(days=3)).strftime("%Y-%m-%d")
        df.loc[0, "earnings_date"] = future
        passed, _ = apply_l1_filter(df, criteria_cfg)
        assert int(passed["earnings_play"].sum()) >= 1, (
            "Earnings-play stock should be flagged in passed, not moved to excluded"
        )

    def test_output_schema(self, criteria_cfg):
        df = _make_passing_universe(5)
        # Force one exclusion so excluded is non-empty
        df.loc[0, "price"] = 5.0
        passed, excluded = apply_l1_filter(df, criteria_cfg)
        for col in ("ticker", "l1_pass", "earnings_play"):
            assert col in passed.columns
        assert "filter_reason" in excluded.columns

    def test_row_count_is_conserved(self, criteria_cfg):
        df = _make_passing_universe(8)
        passed, excluded = apply_l1_filter(df, criteria_cfg)
        assert len(passed) + len(excluded) == len(df)


# ── L2 Filter ─────────────────────────────────────────────────────────────────

class TestL2Filter:
    def _l1_pass(self, df, cfg):
        passed, _ = apply_l1_filter(df, cfg)
        return passed

    def test_all_pass_on_clean_data(self, criteria_cfg):
        df = _make_passing_universe(10)
        l1 = self._l1_pass(df, criteria_cfg)
        # Patch acceleration checks to return None (unknown = soft pass)
        with patch("fundamental_filter._check_revenue_acceleration", return_value=None), \
             patch("fundamental_filter._check_eps_acceleration", return_value=None):
            passed, excluded = apply_l2_filter(l1, criteria_cfg)
        assert len(passed) + len(excluded) == len(l1)

    def test_low_gross_margin_excluded(self, criteria_cfg):
        df = _make_passing_universe(5)
        df["gross_margins"] = 0.10   # below min_gross_margin_pct=35%
        l1 = self._l1_pass(df, criteria_cfg)
        with patch("fundamental_filter._check_revenue_acceleration", return_value=None), \
             patch("fundamental_filter._check_eps_acceleration", return_value=None):
            passed, excluded = apply_l2_filter(l1, criteria_cfg)
        assert len(excluded) > 0
        assert all("gross_margin" in r for r in excluded["filter_reason"].tolist() if r)

    def test_high_pe_excluded(self, criteria_cfg):
        df = _make_passing_universe(3)
        df["trailing_pe"] = 200.0   # above max_pe=150
        l1 = self._l1_pass(df, criteria_cfg)
        with patch("fundamental_filter._check_revenue_acceleration", return_value=None), \
             patch("fundamental_filter._check_eps_acceleration", return_value=None):
            _, excluded = apply_l2_filter(l1, criteria_cfg)
        assert len(excluded) > 0
        assert any("trailing_pe" in r for r in excluded["filter_reason"].tolist())

    def test_missing_gross_margin_excluded(self, criteria_cfg):
        df = _make_passing_universe(3)
        df.loc[0, "gross_margins"] = np.nan
        l1 = self._l1_pass(df, criteria_cfg)
        with patch("fundamental_filter._check_revenue_acceleration", return_value=None), \
             patch("fundamental_filter._check_eps_acceleration", return_value=None):
            _, excluded = apply_l2_filter(l1, criteria_cfg)
        assert any("gross_margin" in str(r) for r in excluded["filter_reason"].tolist())

    def test_output_has_required_columns(self, criteria_cfg):
        df = _make_passing_universe(5)
        l1 = self._l1_pass(df, criteria_cfg)
        with patch("fundamental_filter._check_revenue_acceleration", return_value=None), \
             patch("fundamental_filter._check_eps_acceleration", return_value=None):
            passed, excluded = apply_l2_filter(l1, criteria_cfg)
        for frame in [passed, excluded]:
            if not frame.empty:
                assert "l2_pass" in frame.columns
                assert "rev_accel" in frame.columns
                assert "eps_accel" in frame.columns

    def test_decelerating_revenue_excluded_end_to_end(self, criteria_cfg):
        """Full L1→L2 flow: confirmed revenue deceleration must reach excluded."""
        df = _make_passing_universe(3)
        l1 = self._l1_pass(df, criteria_cfg)
        with patch("fundamental_filter._check_revenue_acceleration", return_value=False), \
             patch("fundamental_filter._check_eps_acceleration", return_value=None):
            _, excluded = apply_l2_filter(l1, criteria_cfg)
        assert len(excluded) == len(l1), (
            "All stocks should be excluded when revenue is confirmed decelerating"
        )
        assert all("rev_growth:decelerating" in r for r in excluded["filter_reason"].tolist())
