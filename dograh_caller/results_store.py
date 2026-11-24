"""
Results store - writes one row per lead to an .xlsx file, grouped by day.

Layout (newest day on top):

    row 1   header (processed_at, status, ...)
    row 2   2026-11-19                    <- date row: only the date, nothing else
    row 3   today's 1st result
    row 4   today's 2nd result            <- new results of the same day go BELOW
    row 5   2026-11-18                    <- previous day moved down
    row 6   yesterday's 1st result
    ...

When the first result of a new day arrives, a new date row is inserted
directly under the header and earlier days move down.

The file is created on first use and reused on later runs. Lead fields
(whatever the scraper or input sheet provides) become extra columns; new
fields simply add new columns. A file written in an older layout is
regrouped into this layout when it is opened.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from openpyxl import Workbook, load_workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

import config

SHEET_NAME = "results"
EXCEL_CELL_LIMIT = 32_000
_UNKNOWN_DATE_LABEL = "unknown date"

FIXED_COLUMNS: List[str] = [
    "processed_at",
    "pipeline_lead_id",
    "status",                # dropped_* | not_interested | called | call_failed | call_timeout | ...
    "detail",
    "phone_raw",
    "phone_e164",
    "phone_location",        # US state of the area code
    "phone_timezone",        # e.g. America/Chicago - for calling-hours rules
    "interested_service",
    "interest_probability",
    "decision_verdict",
    "decision_summary",
    "agent_id",
    "agent_name",
    "campaign_id",
    "campaign_name",
    "dograh_run_id",         # the lead's last call attempt
    "call_attempts",
    "call_disposition",
    "call_duration_s",
    "call_tags",
    "call_transferred",      # True when the agent transferred the call (also in TRANSFERS_FILE)
    "transcript_url",
    "recording_url",
    "dograh_response",       # the run as returned by Dograh (JSON)
    "other_lead_fields",     # lead fields beyond RESULTS_MAX_LEAD_COLUMNS (JSON)
]
OTHER_LEAD_FIELDS = "other_lead_fields"

# --- Transferred calls (config.TRANSFERS_FILE): one row per transferred business
TRANSFERS_SHEET_NAME = "transferred"
# Contact details first (taken from the lead, whatever its field names), then the call.
TRANSFER_CONTACT_COLUMNS: List[Tuple[str, Tuple[str, ...]]] = [
    ("business", ("company", "company_name", "business_name", "business", "organization_name",
                  "organization", "org_name", "name")),
    ("contact_name", ("contact_name", "contact", "full_name", "owner_name", "owner", "contact_person")),
    ("contact_title", ("contact_title", "job_title", "position", "role")),
    ("email", ("email", "email_address", "e_mail", "contact_email")),
    ("website", ("website", "web_site", "url", "site", "domain")),
    ("address", ("address", "street_address", "street", "address_line1")),
    ("city", ("city", "town")),
    ("state", ("state", "us_state", "province", "region")),
    ("postal_code", ("postal_code", "zip", "zip_code", "postcode")),
    ("category", ("category", "industry", "business_type", "type_of_business")),
    ("lead_id", ("lead_id", "id", "record_id")),
]
TRANSFER_COLUMNS: List[str] = (
    ["transferred_at", "phone_called"]
    + [name for name, _ in TRANSFER_CONTACT_COLUMNS]
    + ["interested_service", "campaign_name", "agent_name", "call_disposition", "call_duration_s",
       "collected_by_agent", "transcript_url", "recording_url", "dograh_run_id", OTHER_LEAD_FIELDS]
)
# gathered_context keys that are Dograh bookkeeping, not information about the business
_SYSTEM_CONTEXT_KEYS = {
    "provider", "call_id", "nodes_visited", "call_disposition", "mapped_call_disposition", "call_tags",
    "external_pbx_transferred", "transfer_state", "agent_transfers", "extracted_variables",
}


def _norm(key: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(key).strip().lower()).strip("_")


def _readable(values: Dict[str, Any]) -> Optional[str]:
    parts = [f"{k}: {v if not isinstance(v, (dict, list)) else json.dumps(v, default=str)}"
             for k, v in values.items() if v not in (None, "", [], {})]
    return "; ".join(parts) or None


def transfer_row(row: Dict[str, Any], lead: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    The transfers-sheet row for one result `row` (pipeline columns) and its `lead`.
    Returns (row, remaining lead fields): fields used for a contact column are
    left out of the remainder, so nothing appears twice.
    """
    by_norm: Dict[str, Any] = {}
    for key in lead:
        by_norm.setdefault(_norm(key), key)
    used = set()
    out: Dict[str, Any] = {
        "transferred_at": row.get("processed_at"),
        "phone_called": row.get("phone_e164"),
    }
    for column, candidates in TRANSFER_CONTACT_COLUMNS:
        for cand in candidates:
            key = by_norm.get(cand)
            if key is not None and key not in used and lead.get(key) not in (None, "", [], {}):
                out[column] = lead[key]
                used.add(key)
                break
    for name in ("interested_service", "campaign_name", "agent_name", "call_disposition",
                 "call_duration_s", "transcript_url", "recording_url", "dograh_run_id"):
        out[name] = row.get(name)

    run = row.get("dograh_response")
    if isinstance(run, str):
        try:
            run = json.loads(run)
        except ValueError:
            run = {}
    gathered = (run or {}).get("gathered_context") or {}
    collected = gathered.get("extracted_variables")
    if not isinstance(collected, dict):
        collected = {}
    collected = {**{k: v for k, v in gathered.items() if k not in _SYSTEM_CONTEXT_KEYS}, **collected}
    out["collected_by_agent"] = _readable(collected)

    remaining = {k: v for k, v in lead.items() if k not in used}
    return out, remaining


_HEADER_FONT = Font(bold=True, color="FFFFFF")
_HEADER_FILL = PatternFill("solid", fgColor="1F2937")
_DATE_FONT = Font(bold=True, color="111827")
_DATE_FILL = PatternFill("solid", fgColor="E5E7EB")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def clean_excel_text(text: str) -> str:
    """Replace characters disallowed in XML/Excel worksheets with spaces."""
    return ILLEGAL_CHARACTERS_RE.sub(" ", text)


def _cell(value: Any) -> Any:
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, (dict, list, tuple)):
        value = json.dumps(value, default=str, ensure_ascii=False)
    text = clean_excel_text(str(value))
    return text if len(text) <= EXCEL_CELL_LIMIT else text[:EXCEL_CELL_LIMIT] + "...[truncated]"


def _day_of(value: Any) -> str:
    """'2026-11-19 14:03:22' -> '2026-11-19'."""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    text = str(value or "")[:10]
    return text if _DATE_RE.match(text) else ""


def _sort_key(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value or "")


class ResultsStore:
    def __init__(self, path: Path, *, fixed_columns: Optional[List[str]] = None,
                 sheet_name: str = SHEET_NAME, time_column: str = "processed_at"):
        self.path = Path(path)
        self._fixed = list(fixed_columns or FIXED_COLUMNS)
        self._fixed_set = set(self._fixed)
        self._sheet_name = sheet_name
        self._time_col = time_column
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._groups: Dict[str, List[Dict[str, Any]]] = {}
        self.columns: List[str] = []
        needs_rewrite = False

        if self.path.exists():
            self.wb = load_workbook(self.path)
            self.ws = self.wb[sheet_name] if sheet_name in self.wb.sheetnames else self.wb.active
            self._sheet_title = self.ws.title
            needs_rewrite = self._load_existing_sheet()
        else:
            self.wb = Workbook()
            self.ws = self.wb.active
            self.ws.title = sheet_name
            self._sheet_title = sheet_name

        existing_cols = set(self.columns)
        for name in self._fixed:
            if name not in existing_cols:
                self.columns.append(name)
                existing_cols.add(name)
                if self.path.exists():
                    needs_rewrite = True

        self._dirty = needs_rewrite
        # Columns that hold lead fields (everything that isn't ours)
        self._lead_columns = {c for c in self.columns if not self._is_own_column(c)}

    # ------------------------------------------------------------------ layout

    def _load_existing_sheet(self) -> bool:
        """Read existing rows into day groups; returns True if the sheet layout needs normalizing."""
        all_rows = list(self.ws.iter_rows(values_only=True))
        if not all_rows:
            return False

        header_row = all_rows[0]
        col_positions: List[Tuple[int, str]] = []
        seen_cols: set[str] = set()
        needs_rewrite = False
        for idx, val in enumerate(header_row):
            if val is None:
                continue
            name = str(val).strip()
            if not name or name in seen_cols:
                needs_rewrite = True
                continue
            seen_cols.add(name)
            col_positions.append((idx, name))
        self.columns = [name for _, name in col_positions]

        seen_banners: List[str] = []
        banner_counts: Dict[str, int] = {}
        last_banner_day: Optional[str] = None

        for row_tuple in all_rows[1:]:
            if all(v in (None, "") for v in row_tuple):
                needs_rewrite = True
                continue
            first = row_tuple[0] if row_tuple else None
            is_banner = (
                isinstance(first, str)
                and (bool(_DATE_RE.match(first)) or first == _UNKNOWN_DATE_LABEL)
                and all(v in (None, "") for v in row_tuple[1:])
            )
            if is_banner:
                banner_day = "" if first == _UNKNOWN_DATE_LABEL else first
                seen_banners.append(banner_day)
                banner_counts[banner_day] = banner_counts.get(banner_day, 0) + 1
                last_banner_day = banner_day
                continue

            row_dict: Dict[str, Any] = {}
            for idx, col_name in col_positions:
                if idx < len(row_tuple):
                    val = row_tuple[idx]
                    if val is not None:
                        row_dict[col_name] = val
            if not row_dict:
                needs_rewrite = True
                continue

            day = _day_of(row_dict.get(self._time_col))
            if not day and last_banner_day:
                day = last_banner_day
            if last_banner_day is None or day != last_banner_day:
                needs_rewrite = True
            self._groups.setdefault(day, []).append(row_dict)

        if any(count > 1 for count in banner_counts.values()):
            needs_rewrite = True
        if any(day not in self._groups for day in seen_banners):
            needs_rewrite = True

        expected_days = sorted((d for d in self._groups if d), reverse=True) + ([""] if "" in self._groups else [])
        if seen_banners != expected_days:
            needs_rewrite = True

        for day, rows in list(self._groups.items()):
            keys = [_sort_key(r.get(self._time_col)) for r in rows]
            if keys != sorted(keys):
                self._groups[day] = sorted(rows, key=lambda r: _sort_key(r.get(self._time_col)))
                needs_rewrite = True

        return needs_rewrite

    def _rebuild_sheet(self) -> None:
        was_active = self.wb.active is not None and self.wb.active.title == self._sheet_title
        if self._sheet_title in self.wb.sheetnames:
            idx = self.wb.sheetnames.index(self._sheet_title)
            del self.wb[self._sheet_title]
            ws = self.wb.create_sheet(title=self._sheet_title, index=idx)
        else:
            ws = self.wb.create_sheet(title=self._sheet_title, index=0)
        if was_active:
            self.wb.active = ws
        self.ws = ws
        ws.freeze_panes = "A2"

        for c, name in enumerate(self.columns, start=1):
            cell = ws.cell(row=1, column=c, value=name)
            if isinstance(name, str) and name.startswith("="):
                cell.data_type = "s"
            cell.font = _HEADER_FONT
            cell.fill = _HEADER_FILL
            ws.column_dimensions[get_column_letter(c)].width = max(14, min(len(name) + 4, 40))

        days = sorted((d for d in self._groups if d), reverse=True) + ([""] if "" in self._groups else [])
        r = 2
        for day in days:
            label = day or _UNKNOWN_DATE_LABEL
            for c in range(1, len(self.columns) + 1):
                cell = ws.cell(row=r, column=c, value=label if c == 1 else None)
                cell.fill = _DATE_FILL
                if c == 1:
                    cell.font = _DATE_FONT
            r += 1
            for row_dict in self._groups[day]:
                for c, name in enumerate(self.columns, start=1):
                    val = row_dict.get(name)
                    if val is not None:
                        cell = ws.cell(row=r, column=c, value=val)
                        if isinstance(val, str) and val.startswith("="):
                            cell.data_type = "s"
                r += 1

    # ------------------------------------------------------------------ lead fields

    def _is_own_column(self, name: str) -> bool:
        """Columns written by this program (not copied from the lead)."""
        return name in self._fixed_set or name.startswith(("prob_", "decision_reason_"))

    def _lead_values(self, own: Dict[str, Any], lead: Dict[str, Any]) -> Dict[str, Any]:
        """
        Map a lead's fields to sheet columns without ever overwriting a value:
          - a field whose name is one of our columns ("status", "detail", ...) is
            renamed to "lead.<name>" ("lead.status") - always the same column, and
            a name real lead fields don't use; if even that is taken, "lead.status_2", ...
          - each field gets its own column while the sheet has fewer than
            RESULTS_MAX_LEAD_COLUMNS lead columns; further new fields are kept
            together (as JSON) in the "other_lead_fields" column.
        """
        lead_names = {clean_excel_text(str(k)) for k in lead}
        own_cols = set(own)
        taken = set(own_cols)
        values: Dict[str, Any] = {}
        overflow: Dict[str, Any] = {}
        for key, value in lead.items():
            name = clean_excel_text(str(key))
            if self._is_own_column(name) or name in own_cols:
                n, candidate = 1, f"lead.{name}"
                while (self._is_own_column(candidate) or candidate in taken
                       or (candidate in lead_names and candidate != name)):
                    n += 1
                    candidate = f"lead.{name}_{n}"
                name = candidate
            elif name in taken:
                n, candidate = 2, f"{name}_2"
                while (self._is_own_column(candidate) or candidate in taken
                       or (candidate in lead_names and candidate != name)):
                    n += 1
                    candidate = f"{name}_{n}"
                name = candidate
            taken.add(name)

            if name in self._lead_columns:
                values[name] = value
            elif len(self._lead_columns) < config.RESULTS_MAX_LEAD_COLUMNS:
                self._lead_columns.add(name)
                values[name] = value
            else:
                overflow[name] = value
        if overflow:
            values[OTHER_LEAD_FIELDS] = overflow
        return values

    # ------------------------------------------------------------------ API

    def append(self, row: Dict[str, Any], lead: Optional[Dict[str, Any]] = None) -> None:
        """
        Add one result under today's date (newest day block is at the top).
        `row` holds this program's columns; `lead` holds the original lead fields.
        """
        if lead:
            row = {**row, **self._lead_values(row, lead)}
        existing = set(self.columns)
        for name in row:
            if name not in existing:
                self.columns.append(name)
                existing.add(name)
        day = _day_of(row.get(self._time_col)) or datetime.now().strftime("%Y-%m-%d")
        stored = {k: _cell(v) for k, v in row.items()}
        self._groups.setdefault(day, []).append(stored)
        self._dirty = True

    @staticmethod
    def _unlink_quietly(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def save(self) -> bool:
        """Write the file atomically. Returns False if it is locked (e.g. open in Excel)."""
        if not self._dirty:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._rebuild_sheet()
        tmp_path = self.path.with_name(f".{self.path.stem}.{os.getpid()}.tmp{self.path.suffix}")
        try:
            self.wb.save(tmp_path)
            os.replace(tmp_path, self.path)
            self._dirty = False
            return True
        except PermissionError:
            self._unlink_quietly(tmp_path)
            return False
        except Exception:
            self._unlink_quietly(tmp_path)
            raise
