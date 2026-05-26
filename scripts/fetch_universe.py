"""
Fetch raw market data for MCSS universe.
No screening logic — returns raw OHLCV + fundamental data.
Usage: python scripts/fetch_universe.py [--output data/universe_raw.csv] [--tickers AAPL,NVDA]
"""
import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests
import yaml
import yfinance as yf


def load_config(config_path: str = "config/criteria.yaml") -> dict:
    """Load MCSS criteria config."""
    with open(config_path) as f:
        return yaml.safe_load(f)


def get_sp500_tickers() -> list[str]:
    """Fetch current S&P 500 tickers from Wikipedia."""
    from io import StringIO
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    resp = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    tables = pd.read_html(StringIO(resp.text))
    return tables[0]["Symbol"].str.replace(".", "-", regex=False).tolist()


def fetch_fundamentals(ticker: str) -> dict:
    """
    Fetch fundamental data for a single ticker.
    Returns empty dict fields (not raises) on failure.
    """
    try:
        info = yf.Ticker(ticker).info
        return {
            "ticker": ticker,
            "market_cap": info.get("marketCap"),
            "price": info.get("currentPrice") or info.get("regularMarketPrice"),
            "avg_volume_20d": info.get("averageVolume"),
            "fifty_two_week_high": info.get("fiftyTwoWeekHigh"),
            "fifty_two_week_low": info.get("fiftyTwoWeekLow"),
            "revenue_growth": info.get("revenueGrowth"),
            "gross_margins": info.get("grossMargins"),
            "profit_margins": info.get("profitMargins"),
            "operating_margins": info.get("operatingMargins"),
            "return_on_equity": info.get("returnOnEquity"),
            "free_cashflow": info.get("freeCashflow"),
            "institutional_ownership": info.get("heldPercentInstitutions"),
            "trailing_pe": info.get("trailingPE"),
            "forward_pe": info.get("forwardPE"),
            "peg_ratio": info.get("pegRatio"),
            "price_to_sales": info.get("priceToSalesTrailing12Months"),
            "short_percent_of_float": info.get("shortPercentOfFloat"),
            "earnings_date": str(info.get("earningsDate", "")),
            "sector": info.get("sector", ""),
            "industry": info.get("industry", ""),
            "quote_type": info.get("quoteType", ""),
            "long_name": info.get("longName", ""),
        }
    except Exception:
        return {"ticker": ticker}


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch MCSS universe raw data")
    parser.add_argument("--output", default="data/universe_raw.csv", help="Output CSV path")
    parser.add_argument("--tickers", default=None, help="Comma-separated tickers (default: S&P500)")
    parser.add_argument("--config", default="config/criteria.yaml")
    args = parser.parse_args()

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)

    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",")]
    else:
        print("Fetching S&P 500 ticker list...")
        tickers = get_sp500_tickers()
        print(f"Found {len(tickers)} tickers")

    print(f"Fetching fundamental data for {len(tickers)} tickers...")
    records = []
    for i, ticker in enumerate(tickers):
        if i % 50 == 0:
            print(f"  Progress: {i}/{len(tickers)}")
        records.append(fetch_fundamentals(ticker))

    df = pd.DataFrame(records)
    df.to_csv(args.output, index=False)
    print(f"Saved {len(df)} records → {args.output}")


if __name__ == "__main__":
    main()
