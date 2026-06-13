#!/usr/bin/env python3
"""MCSS Pipeline Orchestrator — GitHub Actions entry point.

Runs each pipeline script in sequence. Designed for headless/CI use.
Local development uses run_mcss.sh (claude -p) instead.

Usage:
    python scripts/run_pipeline.py [--dry-run] [--session=pre-market|post-market]

Required env vars (GitHub Actions secrets):
    GEMINI_API_KEY       — used by ai_catalyst.py (Phase 4)
    TELEGRAM_BOT_TOKEN   — Telegram push (leave unset for dry-run)
    TELEGRAM_CHAT_ID     — Telegram push (leave unset for dry-run)
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional


ROOT = Path(__file__).parent.parent
DATA_DIR = ROOT / "data"
SCRIPTS_DIR = ROOT / "scripts"


def log(stage: str, status: str, msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] [{stage}] [{status}] {msg}", flush=True)


def run_script(script: str, extra_args: Optional[List[str]] = None) -> bool:
    """Run a Python script from SCRIPTS_DIR. Returns True on success."""
    path = SCRIPTS_DIR / script
    if not path.exists():
        log(script, "SKIP", f"Script not yet implemented (pending phase)")
        return True  # non-fatal — future phases

    cmd = [sys.executable, str(path)] + (extra_args or [])
    log(script, "START", " ".join(cmd))
    try:
        result = subprocess.run(cmd, cwd=ROOT, check=True, text=True)
        log(script, "PASS", "completed successfully")
        return True
    except subprocess.CalledProcessError as e:
        log(script, "FAIL", f"exit code {e.returncode}")
        return False


def check_halt(gate_output_path: Path) -> bool:
    """Return True if market gate says HALT."""
    if not gate_output_path.exists():
        return False
    try:
        data = json.loads(gate_output_path.read_text())
        return data.get("gate_0_status") == "HALT"
    except Exception:
        return False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--session", default="post-market",
                        choices=["pre-market", "post-market"])
    args = parser.parse_args()

    DATA_DIR.mkdir(exist_ok=True)

    dry_run_flag = ["--dry-run"] if args.dry_run else []

    log("PIPELINE", "START", f"session={args.session} dry_run={args.dry_run} "
        f"date={datetime.now(timezone.utc).strftime('%Y-%m-%d')}")

    run_record: dict = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "session": args.session,
        "dry_run": args.dry_run,
        "stages": {},
    }

    # --- Stage 0: Market Gate ---
    ok = run_script("market_gate.py")
    run_record["stages"]["market_gate"] = "pass" if ok else "fail"
    if ok and check_halt(DATA_DIR / "market_gate_result.json"):
        log("PIPELINE", "HALT", "Gate 0 conditions not met — sending market brief only")
        # No stock screening on HALT, but the user still gets sector/news context
        ok = run_script("market_brief.py", dry_run_flag)
        run_record["stages"]["market_brief"] = "pass" if ok else "warn"
        ok = run_script("report_agent.py",
                        ["--halt", "--session", args.session] + dry_run_flag)
        run_record["stages"]["report_agent"] = "pass" if ok else "fail"
        if not ok and not args.dry_run:
            log("PIPELINE", "FAIL", "HALT brief delivery failed on a live run")
            run_record["result"] = "DELIVERY_FAILED"
            (DATA_DIR / "pipeline_run.json").write_text(json.dumps(run_record, indent=2))
            sys.exit(1)
        run_record["result"] = "HALT"
        (DATA_DIR / "pipeline_run.json").write_text(json.dumps(run_record, indent=2))
        sys.exit(0)

    # --- Stage 1: Fetch Universe ---
    ok = run_script("fetch_universe.py", ["--output", str(DATA_DIR / "universe_raw.csv")])
    run_record["stages"]["fetch_universe"] = "pass" if ok else "fail"
    if not ok:
        log("PIPELINE", "ABORT", "Data fetch failed — cannot continue")
        run_record["result"] = "ABORT"
        (DATA_DIR / "pipeline_run.json").write_text(json.dumps(run_record, indent=2))
        sys.exit(1)

    # --- Stage 2: Fundamental Filter (L1 + L2) ---
    ok = run_script("fundamental_filter.py",
                    ["--input", str(DATA_DIR / "universe_raw.csv"),
                     "--output-dir", str(DATA_DIR)])
    run_record["stages"]["fundamental_filter"] = "pass" if ok else "warn"

    # --- Stage 3: Technical Filter (L3) — Phase 2 ---
    ok = run_script("technical_filter.py",
                    ["--input", str(DATA_DIR / "l2_fundamental_passed.csv"),
                     "--output-dir", str(DATA_DIR)])
    run_record["stages"]["technical_filter"] = "pass" if ok else "warn"

    # --- Stage 4: Quant Scoring (L4) — Phase 3 ---
    ok = run_script("quant_scoring.py")
    run_record["stages"]["quant_scoring"] = "pass" if ok else "warn"

    # --- Stage 5: AI Catalyst (L5, Gemini) — Phase 4 ---
    ok = run_script("ai_catalyst.py", dry_run_flag)
    run_record["stages"]["ai_catalyst"] = "pass" if ok else "warn"

    # --- Stage 6: Day Trade Screen (orb post-market / gap pre-market) ---
    dt_mode = "gap" if args.session == "pre-market" else "orb"
    ok = run_script("day_trade_screen.py", ["--mode", dt_mode] + dry_run_flag)
    run_record["stages"]["day_trade_screen"] = "pass" if ok else "warn"

    # --- Stage 7: Market Brief (sector RS + headlines, all sessions) ---
    ok = run_script("market_brief.py", dry_run_flag)
    run_record["stages"]["market_brief"] = "pass" if ok else "warn"

    # --- Stage 8: Report + Telegram ---
    ok = run_script("report_agent.py", ["--session", args.session] + dry_run_flag)
    run_record["stages"]["report_agent"] = "pass" if ok else "fail"

    # On a LIVE run, a delivery failure must surface as RED — otherwise the run
    # finishes green with nothing delivered (the exact silent failure the
    # workflow's "Verify Telegram secrets" guard exists to prevent). Dry runs only
    # print, so a non-ok there is not a real delivery failure and stays green.
    if not ok and not args.dry_run:
        log("PIPELINE", "FAIL", "Report delivery failed on a live run — see traceback above")
        run_record["result"] = "DELIVERY_FAILED"
        (DATA_DIR / "pipeline_run.json").write_text(json.dumps(run_record, indent=2))
        sys.exit(1)

    run_record["result"] = "COMPLETE"
    (DATA_DIR / "pipeline_run.json").write_text(json.dumps(run_record, indent=2))
    log("PIPELINE", "COMPLETE", "All stages finished. See data/pipeline_run.json")


if __name__ == "__main__":
    main()
