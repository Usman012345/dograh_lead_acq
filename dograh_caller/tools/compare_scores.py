#!/usr/bin/env python3
"""
Compare Gemini score-only results against stored historical call_results.xlsx scores.

Usage:
    python tools/compare_scores.py <score_only.xlsx> <call_results.xlsx>
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from openpyxl import load_workbook

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
from decision_maker import load_services  # noqa: E402
from results_store import SHEET_NAME  # noqa: E402


def mask_phone(phone: Any) -> str:
    text = str(phone or "").strip()
    if len(text) <= 4:
        return "***"
    if len(text) <= 8:
        return text[:2] + "***" + text[-2:]
    return text[:5] + "***" + text[-4:]


def _processed_at_key(row: Dict[str, Any]) -> str:
    val = row.get("processed_at")
    if isinstance(val, datetime):
        return val.strftime("%Y-%m-%d %H:%M:%S")
    return str(val or "").strip()


def _load_rows_by_phone(path: Path) -> Dict[str, Dict[str, Any]]:
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb[SHEET_NAME] if SHEET_NAME in wb.sheetnames else wb.active
        rows = ws.iter_rows(values_only=True)
        header = [str(h).strip() if h is not None else "" for h in next(rows, [])]
        if "phone_e164" not in header:
            return {}
        out: Dict[str, Dict[str, Any]] = {}
        for values in rows:
            row = {h: v for h, v in zip(header, values) if h}
            phone = str(row.get("phone_e164") or "").strip()
            if not phone:
                continue
            if phone not in out:
                out[phone] = row
            else:
                existing_ts = _processed_at_key(out[phone])
                new_ts = _processed_at_key(row)
                if new_ts and (not existing_ts or new_ts > existing_ts):
                    out[phone] = row
        return out
    finally:
        wb.close()


def _service_order() -> List[str]:
    try:
        return [s.id for s in load_services(config.SERVICES_FILE)]
    except Exception:
        return []


def _order_prob_cols(
    prob_cols: Iterable[str],
    service_ids: Optional[List[str]] = None,
) -> List[str]:
    cols = list(dict.fromkeys(prob_cols))
    if service_ids is None:
        service_ids = _service_order()
    rank = {f"prob_{sid}": idx for idx, sid in enumerate(service_ids)}
    sheet_pos = {col: idx for idx, col in enumerate(cols)}
    return sorted(cols, key=lambda c: (0, rank[c]) if c in rank else (1, sheet_pos[c]))


def _best_service_prob(row: Dict[str, Any], prob_cols: List[str]) -> Tuple[Optional[str], Optional[float]]:
    best_col: Optional[str] = None
    best_val: Optional[float] = None
    for col in _order_prob_cols(prob_cols):
        val = row.get(col)
        if val is None or val == "":
            continue
        try:
            f = float(val)
        except (TypeError, ValueError):
            continue
        if best_val is None or f > best_val:
            best_val = f
            best_col = col.removeprefix("prob_")
    return best_col, best_val


def compare_workbooks(
    score_only_path: Path,
    call_results_path: Path,
    threshold: float = config.INTEREST_THRESHOLD,
) -> List[Dict[str, Any]]:
    new_rows = _load_rows_by_phone(score_only_path)
    old_rows = _load_rows_by_phone(call_results_path)
    service_ids = _service_order()
    comparisons: List[Dict[str, Any]] = []

    for phone, new_row in new_rows.items():
        if phone not in old_rows:
            continue
        old_row = old_rows[phone]
        raw_prob_cols = [
            k for k in dict.fromkeys((*old_row.keys(), *new_row.keys()))
            if k.startswith("prob_")
        ]
        prob_cols = _order_prob_cols(raw_prob_cols, service_ids)
        old_best, old_prob = _best_service_prob(old_row, prob_cols)
        new_best, new_prob = _best_service_prob(new_row, prob_cols)
        old_qual = (old_prob is not None and old_prob >= threshold)
        new_qual = (new_prob is not None and new_prob >= threshold)
        comparisons.append({
            "phone_masked": mask_phone(phone),
            "old_probs": {c.removeprefix("prob_"): old_row.get(c) for c in prob_cols},
            "new_probs": {c.removeprefix("prob_"): new_row.get(c) for c in prob_cols},
            "old_best": old_best,
            "old_qualified": old_qual,
            "new_best": new_best,
            "new_qualified": new_qual,
            "changed": (old_qual != new_qual) or (old_qual and new_qual and old_best != new_best),
        })
    return comparisons


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Compare Gemini score-only output with stored call_results.xlsx.")
    parser.add_argument("score_only", type=Path, help="Path to score_only_*.xlsx")
    parser.add_argument("call_results", type=Path, help="Path to call_results.xlsx")
    parser.add_argument("--threshold", type=float, default=config.INTEREST_THRESHOLD, help="Threshold (default 0.80)")
    args = parser.parse_args(argv)

    rows = compare_workbooks(args.score_only, args.call_results, threshold=args.threshold)
    if not rows:
        print("No matching phone_e164 rows found between the two workbooks.")
        return 0

    changed_count = sum(1 for r in rows if r["changed"])
    print(f"Compared {len(rows)} lead(s) at {args.threshold:.0%} threshold; {changed_count} decision(s) changed:")
    for r in rows:
        old_p = ", ".join(f"{k}={v:.0%}" if isinstance(v, (int, float)) else f"{k}=-" for k, v in r["old_probs"].items())
        new_p = ", ".join(f"{k}={v:.0%}" if isinstance(v, (int, float)) else f"{k}=-" for k, v in r["new_probs"].items())
        old_lbl = f"Qualified ({r['old_best']})" if r["old_qualified"] else "Non-Qualified"
        new_lbl = f"Qualified ({r['new_best']})" if r["new_qualified"] else "Non-Qualified"
        marker = " [CHANGED]" if r["changed"] else ""
        print(f"  {r['phone_masked']}: old [{old_p} -> {old_lbl}] | new [{new_p} -> {new_lbl}]{marker}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
