"""VCP (Volatility Contraction Pattern) detection — optional L3 scoring."""

from typing import Dict, Tuple
import pandas as pd


def compute_vcp_score(ohlcv: pd.DataFrame) -> Tuple[bool, int]:
    """
    Score VCP quality for a single ticker (0–100). Non-blocking — used for L4 scoring.

    Checks over the last 60 trading days:
    - Price range contraction across 3 x 20-day windows  (0–60 pts)
    - Volume dry-up across the same windows               (0–40 pts)

    Returns:
        (vcp_detected, score): detected if score >= 60.
    """
    if len(ohlcv) < 60:
        return False, 0

    last_60 = ohlcv.tail(60)
    price_ref = float(last_60["Close"].iloc[-1])
    if price_ref <= 0:
        return False, 0

    p1 = last_60.iloc[:20]
    p2 = last_60.iloc[20:40]
    p3 = last_60.iloc[40:]

    def price_range(chunk: pd.DataFrame) -> float:
        h = chunk["High"].max()
        l = chunk["Low"].min()
        return (h - l) / price_ref if price_ref else 0.0

    r1, r2, r3 = price_range(p1), price_range(p2), price_range(p3)
    v1 = float(p1["Volume"].mean())
    v2 = float(p2["Volume"].mean())
    v3 = float(p3["Volume"].mean())

    score = 0

    # Price contraction (0–60 pts)
    if r2 < r1:
        score += 20
    if r3 < r2:
        score += 20
    if r1 > 0 and r3 < r1 * 0.5:
        score += 20

    # Volume dry-up (0–40 pts)
    if v1 > 0 and v2 < v1:
        score += 15
    if v2 > 0 and v3 < v2:
        score += 15
    if v1 > 0 and v3 < v1 * 0.6:
        score += 10

    return score >= 60, min(score, 100)


def compute_vcp_batch(
    ticker_ohlcv: Dict[str, pd.DataFrame],
) -> Dict[str, Tuple[bool, int]]:
    """Compute VCP for a dict of {ticker: ohlcv_df}. Silently skips failures."""
    results: Dict[str, Tuple[bool, int]] = {}
    for ticker, ohlcv in ticker_ohlcv.items():
        try:
            results[ticker] = compute_vcp_score(ohlcv)
        except Exception:
            results[ticker] = (False, 0)
    return results
