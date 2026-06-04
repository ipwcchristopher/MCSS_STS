"""SimFin historical fundamental data loader.

Provides point-in-time quarterly fundamentals for backtest L2 filtering.
Eliminates survivorship bias from using current yfinance fundamental snapshots.

Setup:
    1. Register free at https://www.simfin.com → My Account → API Key
    2. export SIMFIN_API_KEY="your_key"
    3. python scripts/backtest.py --use-simfin
"""

import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

ROOT = Path(__file__).parent.parent
SIMFIN_CACHE = ROOT / "data" / "simfin_cache"


def setup_simfin() -> None:
    """Configure SimFin API key and local data directory."""
    try:
        import simfin as sf
    except ImportError:
        print("SimFin not installed. Run: pip install simfin")
        sys.exit(1)

    api_key = os.environ.get("SIMFIN_API_KEY", "free")
    SIMFIN_CACHE.mkdir(parents=True, exist_ok=True)
    sf.set_api_key(api_key)
    sf.set_data_dir(str(SIMFIN_CACHE))


def load_income(tickers: List[str]) -> Dict[str, pd.DataFrame]:
    """Download quarterly income statements for all tickers.

    Returns a dict: { ticker: DataFrame indexed by report date }
    Columns include: Revenue, Gross Profit, Net Income, EPS Diluted.
    Data is cached locally by SimFin and only re-downloaded when stale.
    """
    try:
        import simfin as sf
    except ImportError:
        return {}

    try:
        income = sf.load_income(variant="quarterly", market="us")
    except Exception as e:
        print(f"SimFin load_income failed: {e}")
        return {}

    result: Dict[str, pd.DataFrame] = {}
    all_tickers_in_data = set(income.index.get_level_values("Ticker"))
    found = 0
    for ticker in tickers:
        if ticker in all_tickers_in_data:
            df = income.xs(ticker, level="Ticker")
            # Use Report Date as index (point-in-time when data was published)
            if "Report Date" in df.columns:
                df = df.set_index("Report Date")
            elif "Publish Date" in df.columns:
                df = df.set_index("Publish Date")
            df.index = pd.to_datetime(df.index)
            df.sort_index(inplace=True)
            result[ticker] = df
            found += 1

    print(f"SimFin: {found}/{len(tickers)} tickers found in income data")
    return result


def get_fundamentals_at(
    simfin_data: Dict[str, pd.DataFrame],
    ticker: str,
    date: pd.Timestamp,
) -> Dict:
    """Return the most recent quarterly fundamentals available before `date`.

    Uses point-in-time logic: only reports published on or before `date`.
    Returns empty dict if no data available (caller should treat as pass).
    """
    if ticker not in simfin_data:
        return {}

    df = simfin_data[ticker]
    prior = df[df.index <= date]
    if prior.empty:
        return {}

    row = prior.iloc[-1]

    def _safe(col: str) -> Optional[float]:
        val = row.get(col)
        if val is None or pd.isna(val):
            return None
        return float(val)

    revenue     = _safe("Revenue")
    gross_profit = _safe("Gross Profit")
    net_income  = _safe("Net Income")
    eps         = _safe("EPS Diluted")

    # Compute gross margin % if possible
    gross_margin_pct: Optional[float] = None
    if revenue and gross_profit and revenue > 0:
        gross_margin_pct = (gross_profit / revenue) * 100

    return {
        "revenue":          revenue,
        "gross_profit":     gross_profit,
        "net_income":       net_income,
        "eps":              eps,
        "gross_margin_pct": gross_margin_pct,
        "report_date":      prior.index[-1].date(),
    }
