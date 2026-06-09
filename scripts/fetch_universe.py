"""
Fetch raw market data for MCSS universe.
No screening logic — returns raw OHLCV + fundamental data.

Primary source (when ALPACA_API_KEY + ALPACA_API_SECRET are set):
  Alpaca /v2/assets → snapshots → 1-year bars (~10,000 → ~800 in seconds)

Fallback source (no Alpaca creds):
  NASDAQ Trader FTP (nasdaqlisted.txt + otherlisted.txt)
  Covers NYSE, NASDAQ, AMEX (~7000-9000 stocks). Free, no API key required.
  Falls back to S&P 500 Wikipedia if FTP is unreachable.

Fundamentals (market_cap, PE, sector, etc.) always come from yfinance.info —
Alpaca pre-filter reduces the candidate pool so only ~800 tickers need .info.

Usage:
    python scripts/fetch_universe.py [--output data/universe_raw.csv]
    python scripts/fetch_universe.py --tickers AAPL,NVDA,MSFT
    python scripts/fetch_universe.py --no-prefilter   # skip OHLCV batch step
"""
import argparse
import concurrent.futures
import json
import os
import threading
import time
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Dict, List, Optional, Set

import pandas as pd
import requests
import yaml
import yfinance as yf

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

ALPACA_KEY    = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET = os.environ.get("ALPACA_API_SECRET", "")


# ── Ticker list from NASDAQ FTP ────────────────────────────────────────────────

_NASDAQ_LISTED_URL  = "http://ftp.nasdaqtrader.com/SymbolDirectory/nasdaqlisted.txt"
_OTHER_LISTED_URL   = "http://ftp.nasdaqtrader.com/SymbolDirectory/otherlisted.txt"
_SEC_EXCHANGE_URL   = "https://www.sec.gov/files/company_tickers_exchange.json"
_SEC_USER_AGENT     = "MCSS-Screener/1.0 contact@example.com"


def _is_common_equity(symbol: str) -> bool:
    """Return False for warrants, rights, units, preferred shares."""
    if not symbol or len(symbol) > 5:
        return False
    # Symbols ending in these suffixes are non-common-equity instruments
    for suffix in ("W", "WS", "WD", "WI", "R", "RI", "U", "RT", "+"):
        if symbol.endswith(suffix):
            return False
    return True


def _get_tickers_from_nasdaq_ftp() -> Optional[List[str]]:
    """
    Source 1: NASDAQ Trader FTP — ~8000 stocks with ETF + test-issue flags.
    Returns None if FTP is unreachable.
    """
    headers = {"User-Agent": _SEC_USER_AGENT}
    tickers: Set[str] = set()

    try:
        resp = requests.get(_NASDAQ_LISTED_URL, timeout=20, headers=headers)
        resp.raise_for_status()
        df = pd.read_csv(StringIO(resp.text), sep="|")
        df = df[df["Symbol"].notna() & ~df["Symbol"].str.startswith("File")]
        mask = (df["ETF"].str.strip() == "N") & (df["Test Issue"].str.strip() == "N")
        syms = [s.strip() for s in df[mask]["Symbol"].tolist() if _is_common_equity(s.strip())]
        tickers.update(syms)
        print(f"  NASDAQ FTP: {len(syms)} NASDAQ equities")
    except Exception as exc:
        print(f"  NASDAQ FTP unavailable: {exc}")
        return None

    try:
        resp = requests.get(_OTHER_LISTED_URL, timeout=20, headers=headers)
        resp.raise_for_status()
        df = pd.read_csv(StringIO(resp.text), sep="|")
        df = df[df["ACT Symbol"].notna() & ~df["ACT Symbol"].str.startswith("File")]
        mask = (
            (df["ETF"].str.strip() == "N") &
            (df["Test Issue"].str.strip() == "N") &
            (df["Exchange"].str.strip().isin({"N", "A", "P", "Z"}))
        )
        syms = [s.strip() for s in df[mask]["ACT Symbol"].tolist() if _is_common_equity(s.strip())]
        tickers.update(syms)
        print(f"  NASDAQ FTP: +{len(syms)} NYSE/AMEX/ARCA equities")
    except Exception as exc:
        print(f"  Other listed FTP unavailable: {exc}")

    return sorted(tickers) if tickers else None


def _get_tickers_from_sec_edgar() -> Optional[List[str]]:
    """
    Source 2: SEC EDGAR company_tickers_exchange.json — ~7500 NYSE + NASDAQ stocks.
    More accessible than NASDAQ FTP; works reliably from GitHub Actions.
    Returns None if unavailable.
    """
    try:
        resp = requests.get(
            _SEC_EXCHANGE_URL,
            timeout=20,
            headers={"User-Agent": _SEC_USER_AGENT},
        )
        resp.raise_for_status()
        data = resp.json()
        fields = data.get("fields", [])
        entries = data.get("data", [])

        ticker_idx   = fields.index("ticker")
        exchange_idx = fields.index("exchange")

        # Keep NYSE and Nasdaq only (exclude OTC, CBOE)
        major_exchanges = {"Nasdaq", "NYSE"}
        tickers = [
            e[ticker_idx]
            for e in entries
            if e[exchange_idx] in major_exchanges and _is_common_equity(str(e[ticker_idx]))
        ]
        print(f"  SEC EDGAR: {len(tickers)} NYSE + NASDAQ equities")
        return sorted(set(tickers))
    except Exception as exc:
        print(f"  SEC EDGAR unavailable: {exc}")
        return None


def _get_sp500_fallback() -> List[str]:
    """Source 3: S&P 500 from Wikipedia (last-resort fallback only)."""
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    resp = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    tables = pd.read_html(StringIO(resp.text))
    tickers = tables[0]["Symbol"].str.replace(".", "-", regex=False).tolist()
    print(f"  S&P 500 fallback: {len(tickers)} tickers")
    return tickers


def _get_sp500_nasdaq100() -> List[str]:
    """
    Default local universe: S&P 500 + NASDAQ 100 from Wikipedia.
    ~560 unique liquid US stocks — fast to fetch, covers all major swing candidates.
    Replaces slow NASDAQ FTP + 8000-ticker OHLCV prefilter for local daily runs.
    """
    headers = {"User-Agent": "Mozilla/5.0"}
    tickers: set = set()

    # S&P 500
    try:
        resp = requests.get(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            timeout=30, headers=headers,
        )
        tables = pd.read_html(StringIO(resp.text))
        sp500 = tables[0]["Symbol"].str.replace(".", "-", regex=False).tolist()
        tickers.update(sp500)
        print(f"  S&P 500 (Wikipedia): {len(sp500)} tickers")
    except Exception as exc:
        print(f"  S&P 500 scrape failed: {exc}")

    # NASDAQ 100
    try:
        resp = requests.get(
            "https://en.wikipedia.org/wiki/Nasdaq-100",
            timeout=30, headers=headers,
        )
        tables = pd.read_html(StringIO(resp.text))
        # Find table with a "Ticker" or "Symbol" column
        ndx = []
        for t in tables:
            for col in t.columns:
                if str(col).strip().lower() in ("ticker", "symbol"):
                    ndx = t[col].dropna().str.replace(".", "-", regex=False).tolist()
                    break
            if ndx:
                break
        tickers.update(ndx)
        print(f"  NASDAQ 100 (Wikipedia): {len(ndx)} tickers")
    except Exception as exc:
        print(f"  NASDAQ 100 scrape failed: {exc}")

    # S&P 400 MidCap — captures growth mid-caps common in swing trading
    try:
        resp = requests.get(
            "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies",
            timeout=30, headers=headers,
        )
        tables = pd.read_html(StringIO(resp.text))
        sp400 = []
        for t in tables:
            for col in t.columns:
                if str(col).strip().lower() in ("ticker", "symbol"):
                    sp400 = t[col].dropna().str.replace(".", "-", regex=False).tolist()
                    break
            if sp400:
                break
        tickers.update(sp400)
        print(f"  S&P 400 MidCap (Wikipedia): {len(sp400)} tickers")
    except Exception as exc:
        print(f"  S&P 400 scrape failed (non-fatal): {exc}")

    result = sorted(t for t in tickers if _is_common_equity(t))
    print(f"  Total local universe: {len(result)} unique tickers")
    return result


def get_all_us_tickers() -> List[str]:
    """
    Fetch all active US common equity tickers.
    Source priority:
      1. NASDAQ Trader FTP (~8000 stocks, best ETF filtering)
      2. SEC EDGAR exchange JSON (~7500 NYSE+NASDAQ stocks, very reliable)
      3. S&P 500 Wikipedia (503 stocks, last resort)
    """
    result = _get_tickers_from_nasdaq_ftp()
    if result:
        print(f"  Total unique US tickers: {len(result)}")
        return result

    print("  Trying SEC EDGAR fallback...")
    result = _get_tickers_from_sec_edgar()
    if result:
        print(f"  Total unique US tickers: {len(result)}")
        return result

    print("  Trying S&P 500 Wikipedia fallback...")
    return _get_sp500_fallback()


# ── Alpaca primary source ─────────────────────────────────────────────────────

def _alpaca_get_all_tickers() -> List[str]:
    """Get all active tradable US equity symbols via Alpaca Trading API."""
    from alpaca.trading.client import TradingClient

    client = TradingClient(ALPACA_KEY, ALPACA_SECRET, paper=True)
    assets = client.get_all_assets()
    symbols = [
        a.symbol for a in assets
        if a.asset_class.value == "us_equity"
        and a.status.value == "active"
        and a.tradable
        and _is_common_equity(a.symbol)
    ]
    print(f"  Alpaca assets: {len(symbols)} active US equities")
    return sorted(symbols)


def _alpaca_snapshot_prefilter(
    tickers: List[str],
    min_price: float,
    min_volume: int,
) -> List[str]:
    """Bulk snapshot filter via Alpaca — replaces slow yfinance.download loop."""
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockSnapshotRequest

    client = StockHistoricalDataClient(ALPACA_KEY, ALPACA_SECRET)
    survivors: List[str] = []
    for i in range(0, len(tickers), 1000):
        chunk = tickers[i:i + 1000]
        try:
            req   = StockSnapshotRequest(symbol_or_symbols=chunk)
            snaps = client.get_stock_snapshot(req)
            for sym, snap in snaps.items():
                if snap.daily_bar:
                    if snap.daily_bar.close >= min_price and snap.daily_bar.volume >= min_volume:
                        survivors.append(sym)
        except Exception as exc:
            print(f"  Alpaca snapshot chunk error (passing through): {exc}")
            survivors.extend(chunk)
    print(f"  Alpaca snapshot filter: {len(tickers)} → {len(survivors)}")
    return survivors


def _alpaca_bars_filter(
    tickers: List[str],
    max_drop_pct: float,
    min_price: float = 10.0,
    min_volume: int = 2_000_000,
) -> Dict[str, dict]:
    """1-year daily bars: compute 52w high + avg_vol_20d, apply price/volume/52w filters."""
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    client = StockHistoricalDataClient(ALPACA_KEY, ALPACA_SECRET)
    start  = datetime.now() - timedelta(days=380)
    result: Dict[str, dict] = {}

    for i in range(0, len(tickers), 200):
        chunk = tickers[i:i + 200]
        if i % 2000 == 0:
            print(f"    bars chunk {i//200 + 1}/{(len(tickers) - 1)//200 + 1}...")
        try:
            req  = StockBarsRequest(
                symbol_or_symbols=chunk,
                timeframe=TimeFrame.Day,
                start=start,
            )
            bars = client.get_stock_bars(req).df   # MultiIndex: symbol / timestamp
            for ticker in chunk:
                try:
                    df = bars.xs(ticker, level="symbol")
                except KeyError:
                    continue
                hi_52w     = float(df["high"].max())
                avg_vol    = float(df["volume"].tail(20).mean())
                last_price = float(df["close"].iloc[-1])
                if (
                    last_price >= min_price
                    and avg_vol >= min_volume
                    and hi_52w > 0
                    and (last_price / hi_52w) >= (1 - max_drop_pct / 100)
                ):
                    result[ticker] = {
                        "fifty_two_week_high": hi_52w,
                        "fifty_two_week_low":  float(df["low"].min()),
                        "avg_volume_20d":      avg_vol,
                        "price":               last_price,
                    }
        except Exception as exc:
            print(f"  Alpaca bars chunk error: {exc}")

    print(f"  Alpaca bars filter: {len(tickers)} → {len(result)}")
    return result


# ── Batch OHLCV pre-filter ────────────────────────────────────────────────────

def _batch_ohlcv_prefilter(
    tickers: List[str],
    min_price: float = 10.0,
    min_avg_volume: int = 2_000_000,
    chunk_size: int = 200,
) -> List[str]:
    """
    Batch download 1-month OHLCV, apply price + volume pre-filter.
    Reduces ~8000 tickers to ~1500 before expensive .info calls.
    Chunks that fail entirely are passed through (conservative fallback).
    """
    survivors: List[str] = []
    chunks = [tickers[i:i + chunk_size] for i in range(0, len(tickers), chunk_size)]
    print(f"  OHLCV pre-filter: {len(tickers)} tickers → {len(chunks)} chunks...")

    for i, chunk in enumerate(chunks):
        if i % 10 == 0:
            print(f"    chunk {i+1}/{len(chunks)} ({i * chunk_size}/{len(tickers)})...")
        try:
            raw = yf.download(
                chunk,
                period="1mo",
                interval="1d",
                auto_adjust=True,
                progress=False,
                threads=True,
            )
            if raw.empty:
                survivors.extend(chunk)
                continue

            # yfinance returns MultiIndex columns for multiple tickers
            if isinstance(raw.columns, pd.MultiIndex):
                close  = raw["Close"]
                volume = raw["Volume"]
            else:
                # Single-ticker fallback (chunk_size=1 or only 1 ticker traded)
                if len(chunk) == 1:
                    close  = raw[["Close"]].rename(columns={"Close": chunk[0]})
                    volume = raw[["Volume"]].rename(columns={"Volume": chunk[0]})
                else:
                    survivors.extend(chunk)
                    continue

            for ticker in chunk:
                if ticker not in close.columns:
                    continue
                c = close[ticker].dropna()
                v = volume[ticker].dropna()
                if len(c) < 3 or len(v) < 3:
                    continue
                last_price = float(c.iloc[-1])
                avg_vol    = float(v.tail(20).mean())
                if last_price >= min_price and avg_vol >= min_avg_volume:
                    survivors.append(ticker)

        except Exception:
            # On chunk failure, pass through to avoid losing valid tickers
            survivors.extend(chunk)

    print(f"  OHLCV pre-filter result: {len(tickers)} → {len(survivors)}")
    return survivors


# ── Parallel fundamentals fetch ───────────────────────────────────────────────

def fetch_fundamentals(ticker: str, max_retries: int = 3) -> dict:
    """
    Fetch fundamental data for a single ticker via yfinance.
    Retries on 401/rate-limit errors with exponential backoff.
    All fields may be None on data unavailability — never raises.
    """
    for attempt in range(max_retries):
        try:
            info = yf.Ticker(ticker).info
            # Distinguish a real empty response (only quoteType etc.) from a failed one
            if not info or len(info) <= 2:
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                return {"ticker": ticker}
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
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
            else:
                return {"ticker": ticker}
    return {"ticker": ticker}


def fetch_fundamentals_parallel(tickers: List[str], max_workers: int = 5) -> List[dict]:
    """Fetch fundamentals for multiple tickers in parallel.

    Uses 5 workers (vs 20) to avoid yfinance 401 rate-limit errors on .info calls.
    """
    lock = threading.Lock()
    completed = [0]

    def _worker(ticker: str) -> dict:
        result = fetch_fundamentals(ticker)
        with lock:
            completed[0] += 1
            if completed[0] % 100 == 0:
                print(f"    .info fetched: {completed[0]}/{len(tickers)}...")
        return result

    print(f"  Fetching fundamentals: {len(tickers)} tickers, {max_workers} parallel workers...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        results = list(executor.map(_worker, tickers))

    # Data quality check
    non_null = sum(1 for r in results if r.get("market_cap") is not None)
    pct = non_null / len(results) * 100 if results else 0
    print(f"  Data quality: {non_null}/{len(results)} ({pct:.0f}%) have market_cap")
    if pct < 30:
        print("  WARNING: Less than 30% of tickers have market_cap — likely rate limited.")
    return results


# ── Config + Main ─────────────────────────────────────────────────────────────

def load_config(config_path: str = "config/criteria.yaml") -> dict:
    with open(config_path) as f:
        return yaml.safe_load(f)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch MCSS universe raw data")
    parser.add_argument("--output", default="data/universe_raw.csv")
    parser.add_argument("--tickers", default=None, help="Comma-separated override (skips all universe discovery)")
    parser.add_argument("--config", default="config/criteria.yaml")
    parser.add_argument("--no-prefilter", action="store_true", help="Skip batch OHLCV pre-filter (full-universe path only)")
    parser.add_argument("--full-universe", action="store_true",
                        help="Use full NASDAQ FTP universe (~8000 tickers) instead of S&P500+NDX default. "
                             "Designed for GitHub Actions with Alpaca. Very slow locally.")
    parser.add_argument("--workers", type=int, default=3,
                        help="Parallel workers for yfinance .info (default 3 — Yahoo rate-limit safe)")
    args = parser.parse_args()

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    cfg = load_config(args.config)
    l1 = cfg.get("l1_universe", {})
    min_price  = float(l1.get("min_price", 10.0))
    min_volume = int(l1.get("min_avg_volume_20d", 2_000_000))

    alpaca_data: Dict[str, dict] = {}

    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",")]
        print(f"Using {len(tickers)} specified tickers (skipping universe discovery)")

    elif ALPACA_KEY and ALPACA_SECRET:
        print("Step 1 — Alpaca: fetching all active US equity symbols...")
        tickers = _alpaca_get_all_tickers()

        print("\nStep 2 — Alpaca: bars filter (price + volume + 52w distance)...")
        alpaca_data = _alpaca_bars_filter(
            tickers,
            max_drop_pct=float(l1.get("max_distance_from_52w_high_pct", 35)),
            min_price=min_price,
            min_volume=min_volume,
        )
        tickers = list(alpaca_data.keys())
        print(f"  After bars filter: {len(tickers)} tickers")

    elif args.full_universe:
        print("Step 1 — NASDAQ FTP full universe (--full-universe flag)...")
        tickers = get_all_us_tickers()

        if not args.no_prefilter:
            print("\nStep 2 — OHLCV batch pre-filter (price + volume)...")
            tickers = _batch_ohlcv_prefilter(tickers, min_price=min_price, min_avg_volume=min_volume)

    else:
        # Default for local daily runs: S&P 500 + NASDAQ 100 + S&P 400 (~1100 stocks)
        # Much faster than full NASDAQ FTP + OHLCV pre-filter, avoids Yahoo 401 rate limits.
        # Use --full-universe for the broader 8000-ticker screen (or set Alpaca keys).
        print("Step 1 — S&P 500 + NASDAQ 100 + S&P 400 (default local universe)...")
        tickers = _get_sp500_nasdaq100()

    print(f"\nFetching fundamentals for {len(tickers)} tickers ({args.workers} workers)...")
    records = fetch_fundamentals_parallel(tickers, max_workers=args.workers)

    df = pd.DataFrame(records)

    # Merge Alpaca prefilled price/52w fields into any rows where yfinance.info
    # returned None — reduces downstream NaN failures on critical L1 columns.
    if alpaca_data:
        for col, src_key in [
            ("price",               "price"),
            ("avg_volume_20d",      "avg_volume_20d"),
            ("fifty_two_week_high", "fifty_two_week_high"),
            ("fifty_two_week_low",  "fifty_two_week_low"),
        ]:
            if col not in df.columns:
                df[col] = None
            mask = df[col].isna()
            vals = df.loc[mask, "ticker"].map(
                lambda t: alpaca_data.get(t, {}).get(src_key)
            )
            df[col] = df[col].astype(object)
            df.loc[mask, col] = vals

    _non_null = int(df["market_cap"].notna().sum()) if "market_cap" in df.columns else 0
    _pct = round(_non_null / len(df) * 100) if len(df) else 0
    _quality_path = Path(args.output).parent / "universe_quality.json"
    with open(_quality_path, "w") as _qf:
        json.dump({"data_quality_pct": _pct, "yfinance_degraded": _pct < 70}, _qf)

    df.to_csv(args.output, index=False)
    print(f"\nDone — saved {len(df)} records → {args.output}")


if __name__ == "__main__":
    main()
