"""MCSS Phase 5 — Gate 0 Market Direction Check.

Downloads fresh SPY/QQQ/VIX data and evaluates 4 conditions.
Always exits with code 0 — run_pipeline.py reads the JSON result to
decide whether to continue or halt the pipeline.

Output: data/market_gate_result.json
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any

import yaml

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from fetch_market_indicators import fetch_indicator


def _check_gate(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Download market data and evaluate all Gate 0 conditions."""
    gate_cfg = cfg.get("gate_0", {})
    vix_max = float(gate_cfg.get("vix_max", 30))

    spy = fetch_indicator("SPY")
    qqq = fetch_indicator("QQQ")
    vix = fetch_indicator("^VIX")

    conditions: Dict[str, Any] = {}
    fail_reason = ""

    # Condition 1: SPY above EMA200
    if "error" in spy:
        conditions["spy_above_ema200"] = False
        fail_reason = f"SPY data error: {spy['error']}"
    else:
        cond = spy["close"] > spy["ema200"]
        conditions["spy_above_ema200"] = cond
        conditions["spy_close"] = round(spy["close"], 2)
        conditions["spy_ema200"] = round(spy["ema200"], 2)
        if not cond and not fail_reason:
            fail_reason = f"SPY ({spy['close']:.2f}) below EMA200 ({spy['ema200']:.2f})"

    # Condition 2: QQQ above EMA200
    if "error" in qqq:
        conditions["qqq_above_ema200"] = False
        fail_reason = fail_reason or f"QQQ data error: {qqq['error']}"
    else:
        cond = qqq["close"] > qqq["ema200"]
        conditions["qqq_above_ema200"] = cond
        conditions["qqq_close"] = round(qqq["close"], 2)
        conditions["qqq_ema200"] = round(qqq["ema200"], 2)
        if not cond and not fail_reason:
            fail_reason = f"QQQ ({qqq['close']:.2f}) below EMA200 ({qqq['ema200']:.2f})"

    # Condition 3: SPY EMA50 above EMA200
    if "error" not in spy:
        cond = spy["ema50"] > spy["ema200"]
        conditions["spy_ema50_above_ema200"] = cond
        conditions["spy_ema50"] = round(spy["ema50"], 2)
        if not cond and not fail_reason:
            fail_reason = f"SPY EMA50 ({spy['ema50']:.2f}) below EMA200 ({spy['ema200']:.2f})"

    # Condition 4: VIX below max
    if "error" in vix:
        # VIX data failure is non-fatal — assume OK to not block in data outage
        conditions["vix_below_max"] = True
        conditions["vix_close"] = None
        conditions["vix_max"] = vix_max
    else:
        cond = vix["close"] < vix_max
        conditions["vix_below_max"] = cond
        conditions["vix_close"] = round(vix["close"], 2)
        conditions["vix_max"] = vix_max
        if not cond and not fail_reason:
            fail_reason = f"VIX ({vix['close']:.1f}) above max ({vix_max})"

    all_pass = all([
        conditions.get("spy_above_ema200", False),
        conditions.get("qqq_above_ema200", False),
        conditions.get("spy_ema50_above_ema200", False),
        conditions.get("vix_below_max", False),
    ])

    return {
        "gate_0_status": "PASS" if all_pass else "HALT",
        "reason": "" if all_pass else fail_reason,
        "conditions": conditions,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="data")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    out_dir = ROOT / args.output_dir
    out_dir.mkdir(exist_ok=True)

    config_path = ROOT / "config" / "criteria.yaml"
    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    print("Checking Gate 0 market conditions...")
    result = _check_gate(cfg)

    # Save result for run_pipeline.py check_halt()
    result_path = out_dir / "market_gate_result.json"
    with open(result_path, "w") as f:
        json.dump(result, f, indent=2)

    status = result["gate_0_status"]
    conds = result["conditions"]

    print(f"  SPY:  {conds.get('spy_close', '?'):>8} vs EMA200 {conds.get('spy_ema200', '?'):>8}  {'✓' if conds.get('spy_above_ema200') else '✗'}")
    print(f"  QQQ:  {conds.get('qqq_close', '?'):>8} vs EMA200 {conds.get('qqq_ema200', '?'):>8}  {'✓' if conds.get('qqq_above_ema200') else '✗'}")
    print(f"  SPY EMA50: {conds.get('spy_ema50', '?'):>8} vs EMA200 {conds.get('spy_ema200', '?'):>8}  {'✓' if conds.get('spy_ema50_above_ema200') else '✗'}")
    vix_val = conds.get('vix_close', '?')
    print(f"  VIX:  {vix_val if vix_val else '?':>8} vs max    {conds.get('vix_max', 30):>8}  {'✓' if conds.get('vix_below_max') else '✗'}")
    print(f"Gate 0 → {status}" + (f": {result['reason']}" if result.get("reason") else ""))

    # On HALT, messaging is handled downstream: run_pipeline.py runs
    # market_brief.py + report_agent.py --halt so the user gets a full
    # market brief instead of a bare warning (single message per run).

    # Always exit 0 — run_pipeline.py reads the JSON to decide halt vs continue
    sys.exit(0)


if __name__ == "__main__":
    main()
