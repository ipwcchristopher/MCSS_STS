"""Unit tests for send_telegram.split_message (pure function)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from send_telegram import TELEGRAM_LIMIT, split_message

SEP = "━" * 22


def _block(rank: int, lines: int = 20) -> str:
    return "\n".join([SEP, f"<b>#{rank} TICK</b> — Company {rank}"]
                     + [f"  • metric line {i}" for i in range(lines)])


def test_short_message_unsplit_and_unprefixed():
    msg = "hello\nworld"
    assert split_message(msg) == [msg]


def test_exactly_at_limit_unsplit():
    msg = "x" * TELEGRAM_LIMIT
    assert split_message(msg) == [msg]


def test_long_report_splits_on_block_separators():
    header = "<b>📊 MCSS</b>\nfunnel line"
    msg = "\n".join([header] + [_block(i, lines=40) for i in range(1, 8)])
    assert len(msg) > TELEGRAM_LIMIT
    parts = split_message(msg)
    assert len(parts) >= 2
    for part in parts:
        assert len(part) <= TELEGRAM_LIMIT
    # every part after the first must start at a block boundary (after prefix line)
    for part in parts[1:]:
        body = part.split("\n", 1)[1]
        assert body.startswith(SEP)


def test_split_parts_are_numbered_and_lossless():
    msg = "\n".join([_block(i, lines=40) for i in range(1, 10)])
    parts = split_message(msg)
    n = len(parts)
    reassembled = []
    for i, part in enumerate(parts, 1):
        prefix, body = part.split("\n", 1)
        assert prefix == f"({i}/{n})"
        reassembled.append(body)
    assert "\n".join(reassembled) == msg


def test_oversized_single_block_hard_splits_at_newline():
    msg = "\n".join(f"line {i} " + "y" * 80 for i in range(100))  # no ━ separators
    parts = split_message(msg)
    assert len(parts) >= 2
    for part in parts:
        assert len(part) <= TELEGRAM_LIMIT
