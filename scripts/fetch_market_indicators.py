"""
Fetch SPY, QQQ, VIX data for Gate 0 market direction check.
Usage: python scripts/fetch_market_indicators.py [--output data/market_indicators.json]
"""
import argparse
import json
from pathlib import Path

import yfinance as yf
import pandas as pd


def fetch_indicator(ticker: str, period: str = "1y") -> dict:
    """Fetch close price + key EMAs for a market indicator ticker."""
    try:
        df = yf.download(ticker, period=period, auto_adjust=True, progress=False)
        if df.empty:
            return {"ticker": ticker, "error": "no data"}
        close = df["Close"].squeeze()
        ema50 = close.ewm(span=50, adjust=False).mean()
        ema200 = close.ewm(span=200, adjust=False).mean()
        return {
            "ticker": ticker,
            "close": float(close.iloc[-1]),
            "ema50": float(ema50.iloc[-1]),
            "ema200": float(ema200.iloc[-1]),
            "ema200_21d_ago": float(ema200.iloc[-22]) if len(ema200) > 22 else None,
            "as_of": str(close.index[-1].date()),
        }
    except Exception as e:
        return {"ticker": ticker, "error": str(e)}


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
