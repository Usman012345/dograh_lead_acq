#!/usr/bin/env python3
"""
Entry point: number verifier -> Gemini decision maker -> Dograh calling agent -> results .xlsx

HOW TO USE
==========
1) Recommended: start the dashboard (`python dashboard_server.py`) and click
   "Start Batch" when you are ready to pull leads directly from the reverse proxy
   (`LEAD_SOURCE_URL` = `https://ai-sdr.grobots.ai`) and call them. Nothing is
   pulled or called on startup. Or run `python pull_leads.py` (waits for Enter
   before pulling).
2) Or edit the INPUT section below for a file / Python list run:
     - INPUT_FILE_PATH : path to your leads spreadsheet (.xlsx / .csv / .json), or None
     - INPUT_LEADS     : lead dicts (e.g. from the scraper), or an empty list
     - USE_STREAMER    : False by default (never auto-pulls on startup); set True
                         only if you explicitly want unattended polling
   Then run:   python main.py      (always LIVE: qualified leads are called)

From other code (e.g. the AI-powered scraper), call run() with a file path,
one dict, or a list of dicts:

    from main import run
    run({"company": "...", "category": "...", "phone": "+1 512 555 0101"})
    run([lead1, lead2, ...])
    run(r"C:\\path\\to\\leads.xlsx")

(Command-line arguments still work and override the variables. A run with a
 file argument or --score-only processes that input once and does not use the
 streamer: python main.py leads.xlsx --limit 5)
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Union

import config
from pipeline import run_leads_sync

# ==============================================================================
# INPUT - edit these
# ==============================================================================

# Path to a leads file: .xlsx (first row = column names), .csv or .json.
# Relative paths are relative to this folder. None = no file.
# Every run is LIVE: qualified leads in this file are called when you run main.py.
# Don't use examples/sample_leads.xlsx here - its numbers are fictional.
INPUT_FILE_PATH: Optional[str] = None

# Worksheet to read from INPUT_FILE_PATH (None = first sheet).
INPUT_SHEET: Optional[str] = None

# Leads as Python dicts, e.g. data points coming from the AI-powered scraper.
# Each dict needs a phone field (phone / phone_number / mobile / ...) plus
# fields describing the business (company, category, description, location, ...).
# If both INPUT_FILE_PATH and INPUT_LEADS are set, both are processed.
INPUT_LEADS: List[Dict[str, Any]] = [
    # {
    #     "company": "Summit Concrete Co",
    #     "category": "Concrete subcontractor",
    #     "description": "Commercial flatwork and foundations, bids public work",
    #     "location": "Denver, CO",
    #     "phone": "(303) 555-0142",
    # },
]

# Only process the first N leads of the file (None = all). Handy for a first real test.
LIMIT: Optional[int] = None

# False (default): never auto-pull or auto-call from the lead streamer on startup.
# Start the dashboard (`python dashboard_server.py`) and click "Start Batch" to
# pull and call leads directly from the reverse proxy on demand, or set
# USE_STREAMER = True only if you explicitly want unattended polling after any file.
USE_STREAMER: bool = False

# Seconds between streamer checks while it is not attached or has no new leads.
STREAMER_CHECK_SECONDS: int = 60

# ==============================================================================

LeadSource = Union[str, Path, Dict[str, Any], Iterable[Dict[str, Any]]]


def _clean(value: Any) -> Any:
    if isinstance(value, float) and value.is_integer():
        return int(value)          # 14155550199.0 -> 14155550199
    if isinstance(value, str):
        return value.strip()
    return value


def read_leads(path: Union[str, Path], sheet: Optional[str] = None) -> List[Dict[str, Any]]:
    """Read leads from an .xlsx, .csv or .json file into a list of dicts."""
    path = Path(path).expanduser()
    if not path.is_absolute():
        path = config.PROJECT_DIR / path
    if not path.is_file():
        raise FileNotFoundError(f"Input file not found: {path}")

    ext = path.suffix.lower()
    if ext == ".xls":
        raise ValueError("Old .xls files are not supported - open it in Excel and save as .xlsx.")
    if ext in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook
        wb = load_workbook(path, read_only=True, data_only=True)
        try:
            ws = wb[sheet] if sheet else wb.active
            rows = ws.iter_rows(values_only=True)
            header = [str(h).strip() if h is not None else "" for h in next(rows, [])]
            leads = []
            for values in rows:
                lead = {h: _clean(v) for h, v in zip(header, values) if h and v not in (None, "")}
                if lead:
                    leads.append(lead)
            return leads
        finally:
            wb.close()
    if ext == ".csv":
        with open(path, newline="", encoding="utf-8-sig") as f:
            leads = []
            for row in csv.DictReader(f):
                lead = {k.strip(): v.strip() for k, v in row.items() if k and v and v.strip()}
                if lead:
                    leads.append(lead)
            return leads
    if ext == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list) or not all(isinstance(d, dict) for d in data):
            raise ValueError("JSON input must be an object or a list of objects.")
        return data
    raise ValueError(f"Unsupported input type '{ext}'. Use .xlsx, .csv or .json.")


def collect_leads(source: LeadSource, sheet: Optional[str] = None) -> List[Dict[str, Any]]:
    """Turn a file path, a single dict, or an iterable of dicts into a list of leads."""
    if source is None:
        return []
    if isinstance(source, (str, Path)):
        return read_leads(source, sheet)
    if isinstance(source, dict):
        return [source]
    leads = list(source)
    bad = [type(x).__name__ for x in leads if not isinstance(x, dict)]
    if bad:
        raise TypeError(f"Leads must be dicts; got {', '.join(sorted(set(bad)))}")
    return leads


def run(
    source: LeadSource,
    *,
    sheet: Optional[str] = None,
    limit: Optional[int] = None,
    threshold: Optional[float] = None,
    output: Optional[str] = None,
    score_only: bool = False,
    source_label: Optional[str] = None,
    on_failed: Optional[Callable[[List[Dict[str, Any]]], Any]] = None,
    retry_failed_calls: Optional[bool] = None,
    on_call_retry: Optional[Callable[[List[Dict[str, Any]]], Any]] = None,
) -> int:
    """
    Run leads through the pipeline.
    When score_only=True, leads are verified and scored without placing any calls.
    `source` can be a file path, one lead dict, or a list of lead dicts.
    retry_failed_calls: also keep leads whose call reached nobody, to call them
    again (default RETRY_FAILED_CALLS); never-dialed leads are always kept.
    Leads to retry go to on_failed / on_call_retry, or else to
    output/decision_retry_<time>.json / output/call_retry_<time>.json - also
    when the run ends with an error or Ctrl+C.
    Returns 0 on success, 1 on error, 130 if interrupted.
    """
    active_threshold = config.INTEREST_THRESHOLD if threshold is None else threshold
    if not 0 < active_threshold <= 1:
        print(f"The threshold must be above 0 and at most 1 (0.80 = 80%), got {active_threshold}.")
        return 1
    if source_label is None:
        if isinstance(source, (str, Path)):
            source_label = f"file:{Path(source).name}"
        else:
            source_label = "python"
    try:
        leads = collect_leads(source, sheet)
    except Exception as e:
        print(f"Could not load leads: {e}")
        return 1
    if limit is not None:
        if limit <= 0:
            print("Limit must be a positive integer.")
            return 1
        leads = leads[:limit]
    if not leads:
        print("No leads to process.")
        return 1

    if score_only:
        if output is None:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output = str(config.PROJECT_DIR / "output" / f"score_only_{stamp}.xlsx")
        print(f"Loaded {len(leads)} lead(s) - SCORE ONLY: no calls will be placed")
    else:
        print(f"Loaded {len(leads)} lead(s) - LIVE: qualified leads will be called")
    failed: List[Dict[str, Any]] = []
    call_retry: List[Dict[str, Any]] = []
    pipeline_kwargs: Dict[str, Any] = {}
    if retry_failed_calls is not None:
        pipeline_kwargs["retry_failed_calls"] = retry_failed_calls
    code = 0
    try:
        run_leads_sync(
            leads,
            threshold=threshold,
            results_file=output,
            score_only=score_only,
            failed_leads=failed,
            retry_leads=call_retry,
            source_label=source_label,
            **pipeline_kwargs,
        )
    except KeyboardInterrupt:
        print("\nStopped by user. This run's campaigns were paused; leads still in progress were not saved.")
        code = 130
    except Exception as e:
        print(f"\nERROR: {e}")
        code = 1
    # Saved whatever way the run ended, so these leads are never lost
    flag = " --score-only" if score_only else ""
    _save_retry(failed, on_failed, "decision_retry",
                "lead(s) failed Gemini scoring and were saved to", flag)
    _save_retry(call_retry, on_call_retry, "call_retry",
                "lead(s) whose call should be tried again were saved to", flag)
    return code


def _save_retry(leads: List[Dict[str, Any]], handler: Optional[Callable[[List[Dict[str, Any]]], Any]],
                prefix: str, what: str, flag: str) -> None:
    if not leads:
        return
    if handler is not None:
        handler(leads)
        return
    retry_dir = config.PROJECT_DIR / "output"
    retry_dir.mkdir(parents=True, exist_ok=True)
    retry_path = retry_dir / f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    retry_path.write_text(json.dumps(leads, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"{len(leads)} {what} {retry_path}")
    print(f"Re-run them with: python main.py {retry_path}{flag}")


def run_configured(
    *,
    sheet: Optional[str] = None,
    limit: Optional[int] = None,
    threshold: Optional[float] = None,
    output: Optional[str] = None,
    retry_failed_calls: Optional[bool] = None,
) -> int:
    """`python main.py`: run INPUT_FILE_PATH / INPUT_LEADS configured in main.py (polls streamer only if USE_STREAMER = True)."""
    active_threshold = config.INTEREST_THRESHOLD if threshold is None else threshold
    if not 0 < active_threshold <= 1:
        print(f"The threshold must be above 0 and at most 1 (0.80 = 80%), got {active_threshold}.")
        return 1
    try:
        leads = collect_leads(INPUT_FILE_PATH, sheet) + collect_leads(INPUT_LEADS)
    except Exception as e:
        print(f"Could not load leads: {e}")
        print("Fix INPUT_FILE_PATH at the top of main.py (or set it to None).")
        return 1
    if not leads and not USE_STREAMER:
        if INPUT_FILE_PATH is None:
            print("No input set. Start the dashboard with `python dashboard_server.py` and click 'Start Batch' "
                  "to pull and call leads from the reverse proxy on demand,\n"
                  "or run `python pull_leads.py` (waits for Enter before pulling),\n"
                  "or set INPUT_FILE_PATH (or INPUT_LEADS) at the top of main.py,\n"
                  "or run: python main.py path\\to\\leads.xlsx")
        else:
            print("No leads found in the input.")
        return 1

    import pull_leads

    problem = pull_leads.preflight(need_streamer=USE_STREAMER)
    if problem:
        print(problem)
        return 1
    from pipeline_tracker import PipelineTracker, TrackerLockedError
    try:
        PipelineTracker(config.PIPELINE_TRACKER_FILE).assert_unlocked()
    except TrackerLockedError as e:
        print(e)
        return 1
    pull_leads.stop_signals_act_like_ctrl_c()

    if leads:
        label = f"file:{Path(INPUT_FILE_PATH).name}" if INPUT_FILE_PATH and not INPUT_LEADS else "python"
        code = run(leads, limit=limit, threshold=threshold, output=output, source_label=label,
                   retry_failed_calls=retry_failed_calls,
                   on_failed=pull_leads.save_failed_for_retry if USE_STREAMER else None,
                   on_call_retry=(lambda ls: pull_leads.save_failed_for_retry(ls, source="call_retry"))
                   if USE_STREAMER else None)
        if code != 0:
            return code
    if not USE_STREAMER:
        return 0

    puller = pull_leads.Puller(
        source_url=config.LEAD_SOURCE_URL,
        token=config.LEAD_SOURCE_TOKEN,
        timeout=config.LEAD_SOURCE_TIMEOUT,
        store=pull_leads.BatchStore(config.PULLED_BATCHES_DIR),
        process=lambda batch, **kw: pull_leads.call_with_supported_kwargs(
            pull_leads.default_process, batch, threshold, output,
            retry_failed_calls=retry_failed_calls, **kw),
    )
    return puller.run_auto(STREAMER_CHECK_SECONDS)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Qualify leads with Gemini and call them with Dograh. "
                    "Without arguments, uses INPUT_FILE_PATH / INPUT_LEADS from main.py.")
    parser.add_argument("input", nargs="?", help="Leads file (.xlsx, .csv or .json); overrides INPUT_FILE_PATH")
    parser.add_argument("--sheet", help="Worksheet name (default: INPUT_SHEET or first sheet)")
    parser.add_argument("--threshold", type=float, help=f"Call threshold 0-1 (default {config.INTEREST_THRESHOLD})")
    parser.add_argument("--output", help=f"Results .xlsx (default {config.RESULTS_FILE})")
    parser.add_argument("--limit", type=int, help="Only process the first N leads")
    parser.add_argument("--score-only", action="store_true",
                        help="Verify and score leads without creating Dograh campaigns or placing calls")
    parser.add_argument("--retry-failed-calls", action=argparse.BooleanOptionalAction, default=None,
                        help="Save leads whose call reached nobody (busy, no answer, failed, voicemail) to call "
                             f"them again (default RETRY_FAILED_CALLS={config.RETRY_FAILED_CALLS})")
    args = parser.parse_args()

    sheet = args.sheet or INPUT_SHEET

    if not args.input and not args.score_only:
        return run_configured(sheet=sheet, limit=args.limit if args.limit is not None else LIMIT,
                              threshold=args.threshold, output=args.output,
                              retry_failed_calls=args.retry_failed_calls)

    try:
        if args.input:
            leads = collect_leads(args.input, sheet)
            source_label = f"file:{Path(args.input).name}"
        else:
            leads = collect_leads(INPUT_FILE_PATH, sheet) + collect_leads(INPUT_LEADS)
            if INPUT_FILE_PATH and not INPUT_LEADS:
                source_label = f"file:{Path(INPUT_FILE_PATH).name}"
            else:
                source_label = "python"
    except Exception as e:
        print(f"Could not load leads: {e}")
        return 1
    if not leads:
        if not args.input and INPUT_FILE_PATH is None and not INPUT_LEADS:
            print("No input set. Set INPUT_FILE_PATH (or INPUT_LEADS) at the top of main.py,\n"
                  "or run: python main.py path\\to\\leads.xlsx")
        else:
            print("No leads found in the input.")
        return 1

    limit = args.limit if args.limit is not None else LIMIT
    import pull_leads
    # kill / systemctl stop / a dropped SSH session act like Ctrl+C: campaigns are paused, results saved
    pull_leads.stop_signals_act_like_ctrl_c()
    return run(leads, limit=limit,
               threshold=args.threshold, output=args.output,
               score_only=args.score_only, source_label=source_label,
               retry_failed_calls=args.retry_failed_calls)


if __name__ == "__main__":
    sys.exit(main())
