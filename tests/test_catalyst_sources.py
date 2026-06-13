"""Unit tests for catalyst_sources.py pure functions (no network)."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from catalyst_sources import (
    _within_window,
    dedupe_headlines,
    headlines_to_strings,
    normalize_title,
    parse_rss_items,
)


# ── normalize_title ────────────────────────────────────────────────────────────

def test_normalize_title_strips_punctuation_and_case():
    assert normalize_title("NVIDIA's Q1 Beat — Huge!") == "nvidias q1 beat huge"


def test_normalize_title_collapses_whitespace():
    assert normalize_title("  A   B\tC  ") == "a b c"


# ── dedupe_headlines ───────────────────────────────────────────────────────────

def test_dedupe_drops_same_title_different_punctuation():
    items = [
        {"title": "NVIDIA beats Q1 estimates", "source": "A"},
        {"title": "NVIDIA Beats Q1 Estimates!", "source": "B"},
        {"title": "Different story", "source": "C"},
    ]
    result = dedupe_headlines(items)
    assert len(result) == 2
    assert result[0]["source"] == "A"  # first occurrence kept


def test_dedupe_drops_empty_titles():
    assert dedupe_headlines([{"title": ""}, {"title": "  "}]) == []


# ── headlines_to_strings ───────────────────────────────────────────────────────

def test_headlines_to_strings_with_and_without_summary():
    items = [
        {"title": "T1", "summary": "S1"},
        {"title": "T2", "summary": ""},
    ]
    assert headlines_to_strings(items) == ["T1 — S1", "T2"]


# ── _within_window ─────────────────────────────────────────────────────────────

def test_within_window_recent_passes():
    recent = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    assert _within_window(recent, window_days=7) is True


def test_within_window_old_fails():
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    assert _within_window(old, window_days=7) is False


def test_within_window_undated_kept():
    assert _within_window("", window_days=7) is True


def test_within_window_zulu_suffix_parsed():
    recent = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert _within_window(recent, window_days=7) is True


# ── parse_rss_items ────────────────────────────────────────────────────────────

RSS_FIXTURE = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>Google News</title>
  <item>
    <title>Stock A surges on earnings</title>
    <pubDate>Wed, 10 Jun 2026 16:07:51 GMT</pubDate>
    <source url="https://example.com">Example Wire</source>
  </item>
  <item>
    <title>Stock B drops</title>
    <pubDate>not a date</pubDate>
  </item>
  <item><title></title></item>
</channel></rss>"""


def test_parse_rss_items_extracts_title_source_date():
    items = parse_rss_items(RSS_FIXTURE)
    assert len(items) == 2  # empty-title item dropped
    assert items[0]["title"] == "Stock A surges on earnings"
    assert items[0]["source"] == "Example Wire"
    assert items[0]["published_at"].startswith("2026-06-10")


def test_parse_rss_items_bad_date_kept_with_empty_published():
    items = parse_rss_items(RSS_FIXTURE)
    assert items[1]["title"] == "Stock B drops"
    assert items[1]["published_at"] == ""


def test_parse_rss_items_invalid_xml_returns_empty():
    assert parse_rss_items(b"this is not xml") == []


def test_parse_rss_items_respects_limit():
    assert len(parse_rss_items(RSS_FIXTURE, limit=1)) == 1


def test_parse_rss_items_entity_bomb_rejected():
    # defusedxml must refuse DTD/entity expansion instead of expanding it
    bomb = (b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
            b'<!ENTITY lol2 "&lol;&lol;&lol;">]>'
            b'<rss><channel><item><title>&lol2;</title></item></channel></rss>')
    assert parse_rss_items(bomb) == []
