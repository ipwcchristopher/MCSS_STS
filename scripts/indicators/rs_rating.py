"""RS Rating calculation — IBD/Minervini style relative strength."""

from typing import Dict, List, Optional
import pandas as pd
import yfinance as yf


def compute_rs_ratings(
    tickers: List[str],
    close_prices: Optional[pd.DataFrame] = None,
) -> Dict[str, int]:
    """
    Compute RS Rating (0–99) for each ticker, ranked within the universe.

    Formula: rs_raw = 0.4 * perf_3m + 0.2 * perf_6m + 0.2 * perf_9m + 0.2 * perf_12m
    Then percentile-rank within the universe → integer 0–99.

    Args:
        tickers: List of ticker symbols.
        close_prices: Optional pre-downloaded close prices DataFrame (columns = tickers).
                      If None, downloads via yfinance.

    Returns:
        Dict mapping ticker → RS Rating (0–99). Missing tickers get 0.
    """
    if close_prices is None:
        close_prices = _download_close_prices(tickers)

    if close_prices.empty:
        return {t: 0 for t in tickers}

    weights = [("3m", 63, 0.4), ("6m", 126, 0.2), ("9m", 189, 0.2), ("12m", 252, 0.2)]
    rs_raw: Dict[str, float] = {}

    for ticker in tickers:
        if ticker not in close_prices.columns:
            continue
        prices = close_prices[ticker].dropna()
        if len(prices) < 63:
            continue

        current = float(prices.iloc[-1])
        composite = 0.0
        for _, days, weight in weights:
            if len(prices) >= days:
                past = float(prices.iloc[-days])
                composite += weight * ((current / past) - 1.0) if past > 0 else 0.0

        rs_raw[ticker] = composite

    if not rs_raw:
        return {t: 0 for t in tickers}

    series = pd.Series(rs_raw)
    ranked = (series.rank(pct=True) * 99).round().astype(int)
    result = {t: 0 for t in tickers}
    result.update(ranked.to_dict())
    return result


def _download_close_prices(tickers: List[str]) -> pd.DataFrame:
    """Batch download 1-year daily close prices for all tickers."""
    if not tickers:
        return pd.DataFrame()

    try:
        raw = yf.download(
            tickers,
            period="1y",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
    except Exception as exc:
        print(f"[rs_rating] download failed: {exc}")
        return pd.DataFrame()

    if raw.empty:
        return pd.DataFrame()

    # yfinance returns MultiIndex columns for multiple tickers
    if isinstance(raw.columns, pd.MultiIndex):
        if "Close" in raw.columns.get_level_values(0):
            return raw["Close"]
        # older yfinance: (ticker, price_type) ordering
        close_level = [i for i, lvl in enumerate(raw.columns.levels)
                       if "Close" in lvl]
        if close_level:
            return raw.xs("Close", axis=1, level=close_level[0])
        return pd.DataFrame()
    else:
        # Single ticker download — plain DataFrame
        col = "Close" if "Close" in raw.columns else raw.columns[0]
        return raw[[col]].rename(columns={col: tickers[0]})
