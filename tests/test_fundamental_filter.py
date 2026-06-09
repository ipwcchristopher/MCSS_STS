"""Unit tests for scripts/fundamental_filter.py"""
import sys
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from fundamental_filter import apply_l2_filter


def _l2_cfg() -> dict:
    return {
        "l2_fundamental": {
            "min_revenue_growth_yoy_pct": 15,
            "require_revenue_growth_acceleration": True,
            "require_eps_acceleration_consecutive_quarters": 2,
            "require_profit_margin_expanding": True,
            "min_gross_margin_pct": 35,
            "min_institutional_ownership_pct": 40,
            "max_pe": 150,
        },
        "earnings_play_pool": {"earnings_within_days": 5},
    }


def _base_row(**overrides) -> dict:
    base = {
        "ticker": "TST",
        "revenue_growth": 0.25,
        "gross_margins": 0.50,
        "institutional_ownership": 0.55,
        "trailing_pe": 30.0,
        "return_on_equity": 0.15,
        "free_cashflow": 1e8,
        "profit_margins": 0.12,
        "peg_ratio": 1.5,
        "price_to_sales": 8.0,
        "short_percent_of_float": 0.05,
    }
    base.update(overrides)
    return base


def test_l2_passes_with_acceleration_unknown():
    """Missing quarterly data (None) → not hard-excluded, note column present."""
    cfg = _l2_cfg()
    df = pd.DataFrame([_base_row()])
    # Mock _check_revenue_acceleration and _check_eps_acceleration to return None
    with patch("fundamental_filter._check_revenue_acceleration", return_value=None), \
         patch("fundamental_filter._check_eps_acceleration", return_value=None):
        passed, excluded = apply_l2_filter(df, cfg)
    assert len(passed) == 1, f"Should pass when acceleration data is unavailable; excluded: {excluded}"
    assert "rev_accel" in passed.columns or "l2_notes" in passed.columns


def test_l2_excludes_decelerating_revenue():
    """Confirmed revenue deceleration (False) → hard-excluded."""
    cfg = _l2_cfg()
    df = pd.DataFrame([_base_row()])
    with patch("fundamental_filter._check_revenue_acceleration", return_value=False), \
         patch("fundamental_filter._check_eps_acceleration", return_value=None):
        passed, excluded = apply_l2_filter(df, cfg)
    assert len(excluded) >= 1, "Decelerating revenue should be excluded"
    assert any("rev_growth:decelerating" in str(r) for r in excluded["filter_reason"].tolist())


def test_l2_excludes_decelerating_eps():
    """Confirmed EPS deceleration (False) → hard-excluded."""
    cfg = _l2_cfg()
    df = pd.DataFrame([_base_row()])
    with patch("fundamental_filter._check_revenue_acceleration", return_value=None), \
         patch("fundamental_filter._check_eps_acceleration", return_value=False):
        passed, excluded = apply_l2_filter(df, cfg)
    assert len(excluded) >= 1, "Decelerating EPS should be excluded"
    assert any("eps_growth:decelerating" in str(r) for r in excluded["filter_reason"].tolist())


def test_l2_passes_with_confirmed_acceleration():
    """Both acceleration checks True → passes through."""
    cfg = _l2_cfg()
    df = pd.DataFrame([_base_row()])
    with patch("fundamental_filter._check_revenue_acceleration", return_value=True), \
         patch("fundamental_filter._check_eps_acceleration", return_value=True):
        passed, excluded = apply_l2_filter(df, cfg)
    assert len(passed) == 1
    assert len(excluded) == 0
