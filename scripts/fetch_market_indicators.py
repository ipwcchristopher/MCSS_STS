"""
Fetch SPY, QQQ, VIX data for Gate 0 market direction check.

SPY/QQQ use Alpaca daily bars (no rate-limit issues) when ALPACA_API_KEY and
ALPACA_API_SECRET are set.  VIX stays on yfinance (Alpaca doesn't carry it);
VIX failures are non-fatal and already handled in market_gate.py.

Usage: python scripts/fetch_market_indicators.py [--output data/market_indicators.json]
"""
import argparse
import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import yfinance as yf

ALPACA_KEY    = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET = os.environ.get("ALPACA_API_SECRET", "")


def _fetch_indicator_alpaca(ticker: str) -> dict:
    """Fetch close + EMAs via Alpaca daily bars — immune to yfinance rate limits."""
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    client = StockHistoricalDataClient(ALPACA_KEY, ALPACA_SECRET)
    start  = datetime.now() - timedelta(days=420)
    req    = StockBarsRequest(
        symbol_or_symbols=ticker,
        timeframe=TimeFrame.Day,
        start=start,
        feed="iex",
    )
    try:
        bars = client.get_stock_bars(req).df
    except Exception as exc:
        return {"ticker": ticker, "error": f"alpaca: {exc}"}

    if bars.empty:
        return {"ticker": ticker, "error": "alpaca: no data"}

    # Alpaca returns MultiIndex (symbol, timestamp) for single-ticker requests too
    import pandas as pd
    if isinstance(bars.index, pd.MultiIndex):
        bars = bars.xs(ticker, level="symbol")

    close  = bars["close"]
    ema50  = close.ewm(span=50,  adjust=False).mean()
    ema200 = close.ewm(span=200, adjust=False).mean()
    return {
        "ticker": ticker,
        "close":          float(close.iloc[-1]),
        "ema50":          float(ema50.iloc[-1]),
        "ema200":         float(ema200.iloc[-1]),
        "ema200_21d_ago": float(ema200.iloc[-22]) if len(ema200) > 22 else None,
        "as_of":          str(close.index[-1].date()),
    }


def fetch_indicator(ticker: str, period: str = "1y") -> dict:
    """Fetch close price + key EMAs for a market indicator ticker.

    SPY/QQQ: Alpaca primary (no rate limit), yfinance retry as fallback.
    VIX (^VIX): yfinance only — Alpaca doesn't carry index volatility tickers.
    """
    # Alpaca path for SPY/QQQ
    if ALPACA_KEY and ALPACA_SECRET and ticker in ("SPY", "QQQ"):
        result = _fetch_indicator_alpaca(ticker)
        if "error" not in result:
            return result
        print(f"  [market_indicators] Alpaca failed for {ticker}: {result['error']} — falling back to yfinance")

    # yfinance path (VIX, or Alpaca fallback)
    for attempt in range(3):
        try:
            df = yf.download(ticker, period=period, auto_adjust=True, progress=False)
            if not df.empty:
                close  = df["Close"].squeeze()
                ema50  = close.ewm(span=50,  adjust=False).mean()
                ema200 = close.ewm(span=200, adjust=False).mean()
                return {
                    "ticker":         ticker,
                    "close":          float(close.iloc[-1]),
                    "ema50":          float(ema50.iloc[-1]),
                    "ema200":         float(ema200.iloc[-1]),
                    "ema200_21d_ago": float(ema200.iloc[-22]) if len(ema200) > 22 else None,
                    "as_of":          str(close.index[-1].date()),
                }
        except Exception:
            pass
        if attempt < 2:
            time.sleep(5 * (attempt + 1))   # 5 s, then 10 s

    return {"ticker": ticker, "error": "no data"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch market direction indicators")
    parser.add_argument("--output", default="data/market_indicators.json")
    args = parser.parse_args()

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)

    result = {
        "SPY": fetch_indicator("SPY"),
        "QQQ": fetch_indicator("QQQ"),
        "VIX": fetch_indicator("^VIX"),
    }

    with open(args.output, "w") as f:
        json.dump(result, f, indent=2)
    print(f"Market indicators saved → {args.output}")


if __name__ == "__main__":
    main()
