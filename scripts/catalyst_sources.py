"""Free catalyst/news sources for MCSS.

Aggregates headlines from multiple free sources with graceful degradation:
  - yfinance Ticker.news        (no key required)
  - Google News RSS             (no key required)
  - Finnhub /company-news       (FINNHUB_API_KEY, free tier 60 calls/min)
  - NewsAPI /v2/everything      (NEWSAPI_KEY, free tier 100 req/day)

Every fetcher returns [] on any failure — a dead source never breaks the pipeline.

Headline dict shape: {"title": str, "summary": str, "source": str, "published_at": str}
(published_at is ISO-8601 UTC, or "" when the source gave no usable date)
"""

import os
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Dict, List

import requests

_HTTP_TIMEOUT = 15
_RSS_HEADERS = {"User-Agent": "Mozilla/5.0 (MCSS-Screener)"}


# ── Dedupe helpers (pure, unit-tested) ─────────────────────────────────────────

def normalize_title(title: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace — dedupe key."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", "", title.lower())).strip()


def dedupe_headlines(headlines: List[Dict]) -> List[Dict]:
    """Drop duplicate headlines by normalized title, keeping first occurrence."""
    seen: set = set()
    out: List[Dict] = []
    for h in headlines:
        key = normalize_title(str(h.get("title", "")))
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(h)
    return out


def headlines_to_strings(headlines: List[Dict]) -> List[str]:
    """Render headline dicts to 'title — summary' strings for AI prompts."""
    out = []
    for h in headlines:
        title = str(h.get("title", "")).strip()
        summary = str(h.get("summary", "")).strip()
        out.append(f"{title} — {summary}" if summary else title)
    return out


def _within_window(published_at: str, window_days: int) -> bool:
    """True if the ISO timestamp is recent enough (or undated — keep those)."""
    if not published_at:
        return True
    try:
        dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        return dt >= datetime.now(timezone.utc) - timedelta(days=window_days)
    except ValueError:
        return True


# ── yfinance (no key) ──────────────────────────────────────────────────────────

def _from_yfinance(ticker: str) -> List[Dict]:
    """yfinance>=0.2.5x nests article fields under item['content']."""
    try:
        import yfinance as yf
        items = yf.Ticker(ticker).news or []
    except Exception:
        return []
    out: List[Dict] = []
    for item in items:
        c = item.get("content", item)
        if not isinstance(c, dict):
            continue
        title = str(c.get("title", "")).strip()
        if not title:
            continue
        provider = c.get("provider")
        source = provider.get("displayName", "") if isinstance(provider, dict) else ""
        out.append({
            "title": title,
            "summary": str(c.get("summary", "") or "").strip(),
            "source": source or "Yahoo Finance",
            "published_at": str(c.get("pubDate", "") or ""),
        })
    return out


# ── Google News RSS (no key) ───────────────────────────────────────────────────

def parse_rss_items(xml_bytes: bytes, limit: int = 20) -> List[Dict]:
    """Parse Google News RSS <item> elements. Pure function — unit-tested.

    Uses defusedxml: RSS is external content, stdlib ElementTree is open to
    XXE / billion-laughs entity expansion.
    """
    try:
        import defusedxml.ElementTree as ET
        root = ET.fromstring(xml_bytes)
    except Exception:
        return []
    out: List[Dict] = []
    for item in root.findall(".//item")[:limit]:
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        published = ""
        raw_date = item.findtext("pubDate") or ""
        if raw_date:
            try:
                published = parsedate_to_datetime(raw_date).astimezone(timezone.utc).isoformat()
            except (ValueError, TypeError):
                pass
        out.append({
            "title": title,
            "summary": "",  # Google RSS descriptions are just links — no value
            "source": (item.findtext("source") or "Google News").strip(),
            "published_at": published,
        })
    return out


def _google_rss(url: str, limit: int) -> List[Dict]:
    try:
        resp = requests.get(url, timeout=_HTTP_TIMEOUT, headers=_RSS_HEADERS)
        if resp.status_code != 200:
            return []
        return parse_rss_items(resp.content, limit)
    except Exception:
        return []


def _from_google_rss_ticker(ticker: str, limit: int = 10) -> List[Dict]:
    url = f"https://news.google.com/rss/search?q={ticker}+stock&hl=en-US&gl=US&ceid=US:en"
    return _google_rss(url, limit)


def _from_google_rss_business(limit: int = 15) -> List[Dict]:
    url = "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=en-US&gl=US&ceid=US:en"
    return _google_rss(url, limit)


# ── Finnhub (FINNHUB_API_KEY, free tier) ───────────────────────────────────────

def _from_finnhub_company(ticker: str, window_days: int) -> List[Dict]:
    key = os.environ.get("FINNHUB_API_KEY", "")
    if not key:
        return []
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=window_days)
    try:
        resp = requests.get(
            "https://finnhub.io/api/v1/company-news",
            params={"symbol": ticker, "from": start.isoformat(), "to": end.isoformat(), "token": key},
            timeout=_HTTP_TIMEOUT,
        )
        if resp.status_code != 200:
            return []
        items = resp.json()
    except Exception:
        return []
    out: List[Dict] = []
    for item in items if isinstance(items, list) else []:
        title = str(item.get("headline", "")).strip()
        if not title:
            continue
        published = ""
        try:
            published = datetime.fromtimestamp(int(item.get("datetime", 0)), tz=timezone.utc).isoformat()
        except (ValueError, TypeError, OSError):
            pass
        out.append({
            "title": title,
            "summary": str(item.get("summary", "") or "").strip(),
            "source": str(item.get("source", "") or "Finnhub"),
            "published_at": published,
        })
    return out


def _from_finnhub_general(limit: int) -> List[Dict]:
    key = os.environ.get("FINNHUB_API_KEY", "")
    if not key:
        return []
    try:
        resp = requests.get(
            "https://finnhub.io/api/v1/news",
            params={"category": "general", "token": key},
            timeout=_HTTP_TIMEOUT,
        )
        if resp.status_code != 200:
            return []
        items = resp.json()
    except Exception:
        return []
    out: List[Dict] = []
    for item in (items if isinstance(items, list) else [])[:limit]:
        title = str(item.get("headline", "")).strip()
        if title:
            out.append({
                "title": title,
                "summary": str(item.get("summary", "") or "").strip(),
                "source": str(item.get("source", "") or "Finnhub"),
                "published_at": "",
            })
    return out


# ── NewsAPI (NEWSAPI_KEY, free tier) ───────────────────────────────────────────

def _from_newsapi(ticker: str, company_name: str, window_days: int) -> List[Dict]:
    key = os.environ.get("NEWSAPI_KEY", "")
    if not key:
        return []
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=window_days)
    query = f'{ticker} OR "{company_name}"' if company_name else ticker
    try:
        resp = requests.get(
            "https://newsapi.org/v2/everything",
            params={
                "q": query,
                "from": start.strftime("%Y-%m-%d"),
                "to": end.strftime("%Y-%m-%d"),
                "language": "en",
                "sortBy": "relevancy",
                "pageSize": 10,
                "apiKey": key,
            },
            timeout=_HTTP_TIMEOUT,
        )
        if resp.status_code != 200:
            return []
        articles = resp.json().get("articles", [])
    except Exception:
        return []
    out: List[Dict] = []
    for a in articles:
        title = str(a.get("title", "") or "").strip()
        if title:
            out.append({
                "title": title,
                "summary": str(a.get("description", "") or "").strip(),
                "source": (a.get("source") or {}).get("name", "NewsAPI"),
                "published_at": str(a.get("publishedAt", "") or ""),
            })
    return out


# ── Public API ─────────────────────────────────────────────────────────────────

def get_ticker_news(
    ticker: str,
    company_name: str = "",
    window_days: int = 7,
    max_headlines: int = 8,
) -> List[Dict]:
    """Merged + deduped recent headlines for one ticker, newest first."""
    merged = (
        _from_yfinance(ticker)
        + _from_finnhub_company(ticker, window_days)
        + _from_google_rss_ticker(ticker)
        + _from_newsapi(ticker, company_name, window_days)
    )
    merged = [h for h in merged if _within_window(h.get("published_at", ""), window_days)]
    merged = dedupe_headlines(merged)
    merged.sort(key=lambda h: h.get("published_at", ""), reverse=True)
    return merged[:max_headlines]


def get_market_news(limit: int = 5) -> List[Dict]:
    """Top market-level headlines (Google News business + Finnhub general)."""
    merged = _from_google_rss_business(limit * 3) + _from_finnhub_general(limit * 2)
    merged = dedupe_headlines(merged)
    merged.sort(key=lambda h: h.get("published_at", ""), reverse=True)
    return merged[:limit]


if __name__ == "__main__":
    # Smoke test: python scripts/catalyst_sources.py [TICKER]
    import sys
    tk = sys.argv[1] if len(sys.argv) > 1 else "NVDA"
    print(f"── Ticker news: {tk} ──")
    for h in get_ticker_news(tk):
        print(f"  [{h['source']}] {h['published_at'][:10]} {h['title'][:70]}")
    print("── Market news ──")
    for h in get_market_news():
        print(f"  [{h['source']}] {h['title'][:70]}")
