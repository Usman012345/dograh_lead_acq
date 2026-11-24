"""
Excel-backed per-lead pipeline tracker (output/pipeline_tracker.xlsx).

One row per lead, updated in memory as each stage progresses and flushed to
disk atomically in a background thread. Only the pipeline process writes to it;
the dashboard server reads snapshots without locking it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from openpyxl import Workbook, load_workbook
from openpyxl.cell import WriteOnlyCell

import config
from decision_maker import Decision
from dograh_client import Agent, CallOutcome, is_transferred
from number_verifier import Verification
from results_store import EXCEL_CELL_LIMIT, clean_excel_text

logger = logging.getLogger(__name__)

TRACKER_SHEET_NAME = "leads"
# Excel counts UTF-16 units, so 16,000 characters stays under its cell limit even for emoji.
JSON_CHUNK_SIZE = 16_000

BASE_COLUMNS: List[str] = [
    # Identity
    "lead_id",
    "source_key",
    "source_lead_id",
    "source_label",
    "batch_id",
    "previous_lead_id",
    "times_processed",
    # Summary
    "business",
    "stage",
    "status",
    "status_detail",
    "updated_at",
    # Timestamps
    "received_at",
    "verified_at",
    "decided_at",
    "queued_at",
    "call_started_at",
    "finished_at",
    # Number check
    "phone_raw",
    "phone_e164",
    "phone_location",
    "phone_timezone",
    "verification_result",
    "verification_reason",
    # Decision
    "decision_result",
    "assigned_service_id",
    "assigned_service",
    "best_service_id",
    "best_service",
    "best_probability",
    "threshold_used",
    "decision_model",
    "decision_summary",
    "decision_verdict",
    "service_scores_json",
    # Call
    "call_reason",
    "agent_id",
    "agent_name",
    "campaign_id",
    "campaign_name",
    "dograh_run_id",
    "call_attempts",
    "call_status",
    "call_disposition",
    "call_duration_s",
    "call_tags",
    "call_transferred",
    "transcript_url",
    "recording_url",
    # Full data
    "lead_json",
    "dograh_json",
]

FINAL_STAGES = frozenset({
    "number_rejected",
    "non_qualified",
    "decision_error",
    "not_sent",
    "scored_qualified",
    "call_finished",
    "interrupted",
})

_LIFECYCLE_CLEAR_KEYS = (
    "status_detail",
    "verified_at",
    "decided_at",
    "queued_at",
    "call_started_at",
    "finished_at",
    "phone_raw",
    "phone_e164",
    "phone_location",
    "phone_timezone",
    "verification_result",
    "verification_reason",
    "decision_result",
    "assigned_service_id",
    "assigned_service",
    "best_service_id",
    "best_service",
    "best_probability",
    "threshold_used",
    "decision_model",
    "decision_summary",
    "decision_verdict",
    "service_scores_json",
    "call_reason",
    "agent_id",
    "agent_name",
    "campaign_id",
    "campaign_name",
    "dograh_run_id",
    "call_attempts",
    "call_status",
    "call_disposition",
    "call_duration_s",
    "call_tags",
    "call_transferred",
    "transcript_url",
    "recording_url",
    "dograh_json",
)

_TOP_LEVEL_STRIP_KEYS = frozenset({
    "public_access_token",
    "initial_context",
    "cost_info",
    "usage_info",
    "logs",
})

_SENSITIVE_KEY_RE = re.compile(r"(?i)token|secret|password|api[_-]?key|authorization|credential")
_LEAD_ID_NUM_RE = re.compile(r"^L(\d+)$")
_MISSING_VALUE_RE = re.compile(r"Missing value for (.+?) \(used in", re.IGNORECASE)

PLAIN_ENGLISH_LABELS: Dict[str, str] = {
    "ok": "Valid US number",
    "dropped_no_phone": "No phone number found",
    "dropped_fax_only": "Only a fax number was found",
    "dropped_invalid_number": "The phone number is not valid",
    "dropped_unreadable_number": "The phone number could not be read",
    "dropped_not_us": "The number is outside the allowed region",
    "dropped_special_number": "Special number type that cannot be called",
    "dropped_already_called": "This number was already called",
    "dropped_duplicate": "Duplicate of another lead in this run",
    "error": "The number could not be checked",
    "not_interested": "Non-Qualified",
    "decision_error": "Could not be assessed",
    "skipped_missing_data": "Not sent: required details are missing",
    "called": "Call completed",
    "call_failed": "Call failed",
    "call_timeout": "Call timed out",
    "scored_qualified": "Qualified (test run, not called)",
    "interrupted": "Run stopped before completion",
    "incoming": "Incoming",
    "awaiting_decision": "Awaiting decision",
    "qualified_waiting": "Waiting for campaign",
    "calling": "Calling",
}


class TrackerLockedError(RuntimeError):
    """Another pipeline process already holds the tracker lock."""


def plain_english_label(code: Any) -> str:
    text = str(code or "").strip()
    if not text:
        return ""
    head = text.split(" ", 1)[0]
    if head in PLAIN_ENGLISH_LABELS:
        return PLAIN_ENGLISH_LABELS[head]
    return head.replace("_", " ").capitalize()


def _missing_details_text(detail: str) -> str:
    """'Missing value for city (used in agent ...)' -> client-readable wording."""
    match = _MISSING_VALUE_RE.search(detail or "")
    if match:
        return f"required details are missing ({match.group(1).replace('_', ' ')})"
    return "required details are missing"


def _qualified_phrase(row: Dict[str, Any]) -> str:
    service = row.get("assigned_service") or "a service"
    best = row.get("best_probability")
    if best in (None, ""):
        return f"it qualified for {service}"
    return f"it qualified for {service} at {float(best):.0%}"


def _sanitize_recursive(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: _sanitize_recursive(v)
            for k, v in value.items()
            if not _SENSITIVE_KEY_RE.search(str(k))
        }
    if isinstance(value, list):
        return [_sanitize_recursive(item) for item in value]
    return value


def sanitize_dograh_data(data: Any) -> Dict[str, Any]:
    """Strip sensitive tokens/context fields from a Dograh run response."""
    if not isinstance(data, dict):
        return {}
    stripped = {k: v for k, v in data.items() if k not in _TOP_LEVEL_STRIP_KEYS}
    return _sanitize_recursive(stripped)


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _norm_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(key).strip().lower()).strip("_")


def _business_name(lead: Dict[str, Any]) -> str:
    for wanted in ("company", "company_name", "business_name", "business",
                   "organization_name", "organization", "org_name", "name", "title"):
        for k, v in lead.items():
            if _norm_key(k) == wanted and v not in (None, ""):
                return str(v).strip()[:120]
    return "(no company name)"


def _source_identity(lead: Dict[str, Any]) -> Tuple[str, str]:
    for k, v in lead.items():
        if _norm_key(k) == "lead_id" and v not in (None, ""):
            sid = str(v).strip()
            if sid:
                return f"lead:{sid}", sid
    canonical = json.dumps(lead, sort_keys=True, default=str, ensure_ascii=False)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"hash:{digest}", ""


def _split_chunks(text: str, size: int = JSON_CHUNK_SIZE) -> List[str]:
    if not text:
        return [""]
    return [text[i:i + size] for i in range(0, len(text), size)]


def _parse_lead_num(lead_id: Any) -> int:
    m = _LEAD_ID_NUM_RE.match(str(lead_id or "").strip())
    return int(m.group(1)) if m else 0


def _safe_int(val: Any, default: int = 1) -> int:
    """Parse an integer count safely (accepting float strings like '1.0')."""
    if val in (None, "") or isinstance(val, bool):
        return default
    try:
        return int(float(str(val).strip()))
    except (TypeError, ValueError, OverflowError):
        return default


def _parse_dt(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.strptime(str(value or "").strip()[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _row_blocks_number(row: Dict[str, Any]) -> bool:
    """
    True if this row's number must never be called again: the call was answered
    (called), its result never arrived (call_timeout), or Dograh dialed it and
    the outcome is unknown. A call that is known to have reached nobody (busy,
    no answer, failed, voicemail - config.NOT_REACHED_DISPOSITIONS) does not block.
    """
    phone = str(row.get("phone_e164") or "").strip()
    if not phone:
        return False
    call_status = str(row.get("call_status") or "").strip()
    if call_status in ("called", "call_timeout"):
        return True
    run_id = row.get("dograh_run_id")
    if run_id is None or str(run_id).strip() == "":
        return False
    disposition = str(row.get("call_disposition") or "").strip().lower()
    if call_status == "call_failed" and disposition in config.NOT_REACHED_DISPOSITIONS:
        return False
    return True


def _has_call_activity(row: Dict[str, Any]) -> bool:
    """True if this tracker row has ever been assigned to a campaign or dialed."""
    return bool(
        str(row.get("campaign_id") or "").strip()
        or str(row.get("dograh_run_id") or "").strip()
    )


def _acquire_os_lock(lock_path: Path) -> Any:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+b")
    try:
        fh.seek(0)
        if fh.tell() == 0 and os.path.getsize(lock_path) == 0:
            fh.write(b"1")
            fh.flush()
        fh.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except OSError as e:
        fh.close()
        raise TrackerLockedError(
            f"Tracker is locked by another running pipeline ({lock_path})."
        ) from e


def _release_os_lock(fh: Any) -> None:
    if fh is None:
        return
    try:
        fh.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        try:
            fh.close()
        except OSError:
            pass


def _read_rows(path: Path) -> List[Dict[str, Any]]:
    """Rows of a tracker-format workbook, with split lead_json / dograh_json chunks joined again."""
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb[TRACKER_SHEET_NAME] if TRACKER_SHEET_NAME in wb.sheetnames else wb.active
        rows_iter = ws.iter_rows(values_only=True)
        header = [str(h).strip() if h is not None else "" for h in next(rows_iter, [])]
        if not header or "lead_id" not in header:
            raise ValueError("Missing 'lead_id' header in tracker workbook")
        loaded: List[Dict[str, Any]] = []
        for vals in rows_iter:
            if not vals or all(v in (None, "") for v in vals):
                continue
            raw_row = {h: v for h, v in zip(header, vals) if h}
            lead_chunks = []
            dograh_chunks = []
            row: Dict[str, Any] = {}
            for k, v in raw_row.items():
                if k == "lead_json" or re.fullmatch(r"lead_json_\d+", k):
                    lead_chunks.append((1 if k == "lead_json" else int(k.rsplit("_", 1)[1]), str(v or "")))
                elif k == "dograh_json" or re.fullmatch(r"dograh_json_\d+", k):
                    dograh_chunks.append((1 if k == "dograh_json" else int(k.rsplit("_", 1)[1]), str(v or "")))
                else:
                    row[k] = "" if v is None else v
            row["lead_json"] = "".join(c for _, c in sorted(lead_chunks))
            row["dograh_json"] = "".join(c for _, c in sorted(dograh_chunks))
            if not str(row.get("lead_id") or "").strip():
                continue
            loaded.append(row)
        return loaded
    finally:
        wb.close()


def _write_tmp_workbook(path: Path, rows: List[Dict[str, Any]]) -> Path:
    """Write rows in tracker format to a temporary file next to `path`; returns the temp file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.stem}.{os.getpid()}.tmp{path.suffix}")

    max_lead_chunks = 1
    max_dograh_chunks = 1
    split_rows: List[Tuple[Dict[str, Any], List[str], List[str]]] = []
    for row in rows:
        lc = _split_chunks(str(row.get("lead_json") or ""))
        dc = _split_chunks(str(row.get("dograh_json") or ""))
        max_lead_chunks = max(max_lead_chunks, len(lc))
        max_dograh_chunks = max(max_dograh_chunks, len(dc))
        split_rows.append((row, lc, dc))

    columns = list(BASE_COLUMNS)
    for i in range(2, max_lead_chunks + 1):
        columns.append(f"lead_json_{i}")
    for i in range(2, max_dograh_chunks + 1):
        columns.append(f"dograh_json_{i}")

    wb = Workbook(write_only=True)
    ws = wb.create_sheet(title=TRACKER_SHEET_NAME)
    ws.append(columns)

    for row, lc, dc in split_rows:
        values: List[Any] = []
        for col in columns:
            if col == "lead_json":
                val: Any = lc[0]
            elif col.startswith("lead_json_"):
                idx = int(col.rsplit("_", 1)[1]) - 1
                val = lc[idx] if idx < len(lc) else ""
            elif col == "dograh_json":
                val = dc[0]
            elif col.startswith("dograh_json_"):
                idx = int(col.rsplit("_", 1)[1]) - 1
                val = dc[idx] if idx < len(dc) else ""
            else:
                val = row.get(col, "")

            if isinstance(val, str):
                cleaned = clean_excel_text(val)[:EXCEL_CELL_LIMIT]
                if cleaned.startswith("="):
                    cell = WriteOnlyCell(ws, value=cleaned)
                    cell.data_type = "s"
                    values.append(cell)
                else:
                    values.append(cleaned)
            else:
                values.append(val)
        ws.append(values)

    try:
        wb.save(tmp_path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    finally:
        wb.close()
    return tmp_path


def _write_rows(path: Path, rows: List[Dict[str, Any]]) -> None:
    """Write rows in tracker format, atomically."""
    tmp_path = _write_tmp_workbook(path, rows)
    for attempt in range(5):
        try:
            os.replace(tmp_path, path)
            return
        except PermissionError:
            if attempt == 4:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
            time.sleep(0.2)


class PipelineTracker:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock_path = self.path.with_name(f".{self.path.stem}.lock")
        self._lock_fh: Any = None
        self._mem_lock = threading.RLock()
        # A cancelled background save keeps running in its thread; never let two saves overlap.
        self._save_lock = threading.Lock()
        self._rows: List[Dict[str, Any]] = []
        self._by_lead_id: Dict[str, Dict[str, Any]] = {}
        self._by_source_key: Dict[str, Dict[str, Any]] = {}
        self._called_source_keys: Set[str] = set()
        self._max_times_by_source_key: Dict[str, int] = {}
        self._registered_this_run: Set[str] = set()
        self._next_id = 1
        self._dirty = False
        self._dirty_version = 0
        self._warned_rows = False
        self._warned_perm = False
        self.last_save_duration: float = 0.0
        # Old finished rows are moved to <stem>_archive.xlsx (TRACKER_ARCHIVE_DAYS); the index
        # keeps their blocked numbers and the next free lead number, so nothing is lost.
        self.archive_path = self.path.with_name(f"{self.path.stem}_archive{self.path.suffix}")
        self._archive_index_path = self.path.with_name(f".{self.path.stem}_archive_index.json")
        self._archived_called: Set[str] = set()

    # ------------------------------------------------------------------ lifecycle

    def assert_unlocked(self) -> None:
        """Raise TrackerLockedError if another pipeline currently holds this tracker's lock."""
        if self._lock_fh is not None:
            return
        fh = _acquire_os_lock(self._lock_path)
        _release_os_lock(fh)

    def open(self) -> None:
        if self._lock_fh is not None:
            return
        self._lock_fh = _acquire_os_lock(self._lock_path)
        try:
            self._load_existing()
            self._load_archive_index()
            try:
                self._archive_old_rows()
            except Exception as e:      # archiving is housekeeping: never block a run
                logger.warning("Could not archive old tracker rows (%s); keeping them in %s.", e, self.path)
            self.mark_unfinished_interrupted()
        except Exception:
            _release_os_lock(self._lock_fh)
            self._lock_fh = None
            raise

    def _load_existing(self) -> None:
        if not self.path.exists():
            return
        try:
            loaded = _read_rows(self.path)
        except Exception as e:
            self._quarantine_corrupt(e)
            return
        max_num = max((_parse_lead_num(r.get("lead_id")) for r in loaded), default=0)

        with self._mem_lock:
            self._rows = loaded
            self._by_lead_id = {str(r["lead_id"]): r for r in loaded}
            self._by_source_key = {}
            self._called_source_keys = set()
            self._max_times_by_source_key = {}
            for r in loaded:
                sk = str(r.get("source_key") or "").strip()
                if sk:
                    self._by_source_key[sk] = r
                    if _has_call_activity(r):
                        self._called_source_keys.add(sk)
                    tp = _safe_int(r.get("times_processed"), default=1)
                    self._max_times_by_source_key[sk] = max(
                        self._max_times_by_source_key.get(sk, 0),
                        tp,
                    )
            self._next_id = max_num + 1

    # ------------------------------------------------------------------ archive

    def _load_archive_index(self) -> None:
        if not self._archive_index_path.is_file():
            return
        try:
            data = json.loads(self._archive_index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.warning("Could not read %s (%s).", self._archive_index_path, e)
            return
        if not isinstance(data, dict):
            return
        with self._mem_lock:
            self._archived_called = {str(n) for n in data.get("called_numbers") or [] if n}
            self._next_id = max(self._next_id, _safe_int(data.get("next_lead_num"), default=1))

    def _archive_old_rows(self) -> int:
        """Move finished rows older than TRACKER_ARCHIVE_DAYS to the archive workbook. Returns how many."""
        days = config.TRACKER_ARCHIVE_DAYS
        if days <= 0:
            return 0
        cutoff = datetime.now() - timedelta(days=days)
        with self._mem_lock:
            old = []
            for r in self._rows:
                if str(r.get("stage") or "").strip() not in FINAL_STAGES:
                    continue
                when = _parse_dt(r.get("updated_at")) or _parse_dt(r.get("finished_at"))
                if when is not None and when < cutoff:
                    old.append(r)
            if not old:
                return 0
            old_snapshot = [dict(r) for r in old]
            called = set(self._archived_called) | {
                str(r.get("phone_e164")).strip() for r in old_snapshot if _row_blocks_number(r)
            }
            next_num = self._next_id

        # 1. archive workbook (rows already archived by an interrupted earlier attempt are not repeated)
        existing = _read_rows(self.archive_path) if self.archive_path.exists() else []
        known = {str(r.get("lead_id")) for r in existing}
        _write_rows(self.archive_path, existing + [r for r in old_snapshot if str(r.get("lead_id")) not in known])
        # 2. index: blocked numbers and the next lead number survive the move
        tmp = self._archive_index_path.with_name(self._archive_index_path.name + ".tmp")
        tmp.write_text(json.dumps({"called_numbers": sorted(called), "next_lead_num": next_num}), encoding="utf-8")
        os.replace(tmp, self._archive_index_path)

        # 3. drop them from the live tracker
        moved_ids = {str(r.get("lead_id")) for r in old_snapshot}
        with self._mem_lock:
            self._archived_called = called
            self._rows = [r for r in self._rows if str(r.get("lead_id")) not in moved_ids]
            self._by_lead_id = {str(r["lead_id"]): r for r in self._rows}
            self._by_source_key = {k: v for k, v in self._by_source_key.items()
                                   if str(v.get("lead_id")) not in moved_ids}
            self._mark_dirty()
        self.save()
        logger.info("Moved %d finished tracker row(s) older than %d days to %s.", len(moved_ids), days,
                    self.archive_path)
        return len(moved_ids)

    def _quarantine_corrupt(self, exc: Exception) -> None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        corrupt = self.path.with_name(f"{self.path.stem}.corrupt_{stamp}{self.path.suffix}")
        try:
            os.replace(self.path, corrupt)
            logger.warning("Corrupt tracker file %s moved to %s (%s). Starting a new tracker.",
                           self.path, corrupt, exc)
        except OSError as move_err:
            logger.warning("Corrupt tracker file %s could not be moved (%s; original: %s). Starting fresh.",
                           self.path, move_err, exc)
        with self._mem_lock:
            self._rows = []
            self._by_lead_id = {}
            self._by_source_key = {}
            self._called_source_keys = set()
            self._max_times_by_source_key = {}
            self._next_id = 1
            self._dirty = True
            self._dirty_version += 1

    def called_numbers(self) -> Set[str]:
        """Numbers never to call again (SKIP_ALREADY_CALLED), including archived rows."""
        with self._mem_lock:
            called: Set[str] = set(self._archived_called)
            for r in self._rows:
                if _row_blocks_number(r):
                    called.add(str(r.get("phone_e164")).strip())
            return called

    def unfinished_call_rows(self, max_age_days: int) -> List[Dict[str, Any]]:
        """
        Rows (copies) whose call result never arrived: put in a Dograh campaign
        but stopped (interrupted) or timed out, without an outcome, updated
        within max_age_days.
        """
        cutoff = datetime.now() - timedelta(days=max_age_days)
        out: List[Dict[str, Any]] = []
        with self._mem_lock:
            for r in self._rows:
                if not str(r.get("campaign_id") or "").strip() or str(r.get("call_disposition") or "").strip():
                    continue
                stage = str(r.get("stage") or "").strip()
                if stage != "interrupted" and str(r.get("call_status") or "").strip() != "call_timeout":
                    continue
                updated = _parse_dt(r.get("updated_at"))
                if updated is None or updated < cutoff:
                    continue
                out.append(dict(r))
        return out

    def record_run_seen(self, lead_id: str, run_id: Any, attempts: int) -> None:
        """Remember that Dograh dialed this lead (its number is then never dialed again); the stage is unchanged."""
        if run_id is None:
            return
        with self._mem_lock:
            row = self._by_lead_id.get(lead_id)
            if row is None:
                return
            row["dograh_run_id"] = run_id
            row["call_attempts"] = attempts
            if not row.get("call_started_at"):
                row["call_started_at"] = row.get("updated_at") or _now_str()
            row["updated_at"] = _now_str()
            row["call_reason"] = "Call status unknown: the call was still going when the run stopped."
            self._record_call_source_key(row)
            self._mark_dirty()

    def mark_unfinished_interrupted(self, detail: str = "") -> None:
        now = _now_str()
        with self._mem_lock:
            changed = False
            for r in self._rows:
                stage = str(r.get("stage") or "").strip()
                if stage in FINAL_STAGES:
                    continue
                has_campaign = bool(r.get("campaign_id") or r.get("campaign_name") or stage == "calling")
                r["stage"] = "interrupted"
                r["status"] = "interrupted"
                if detail and not r.get("status_detail"):
                    r["status_detail"] = detail
                r["updated_at"] = now
                r["finished_at"] = r.get("finished_at") or now
                r["call_reason"] = (
                    detail if detail
                    else (
                        "Call status unknown: the run stopped while this campaign was active."
                        if has_campaign
                        else "Not called: the run stopped before this lead was finished."
                    )
                )
                changed = True
            if changed:
                self._mark_dirty()

    # ------------------------------------------------------------------ registration & stage updates

    def _mark_dirty(self) -> None:
        self._dirty = True
        self._dirty_version += 1

    def _next_lead_id(self) -> str:
        lid = f"L{self._next_id:06d}"
        self._next_id += 1
        return lid

    def register(self, lead: Dict[str, Any], source_label: str, batch_id: str = "") -> str:
        now = _now_str()
        clean_batch_id = str(batch_id or "").strip()
        source_key, source_lead_id = _source_identity(lead)
        lead_json = json.dumps(lead, ensure_ascii=False, default=str)
        business = _business_name(lead)

        with self._mem_lock:
            earlier = self._by_source_key.get(source_key)
            can_reuse = False
            prev_id = ""
            max_times = self._max_times_by_source_key.get(source_key, 0)
            if earlier is not None:
                max_times = max(max_times, _safe_int(earlier.get("times_processed"), default=1))
            times = 1

            if earlier is not None:
                earlier_id = str(earlier["lead_id"])
                earlier_batch = str(earlier.get("batch_id") or "").strip()
                has_call_activity = (
                    source_key in self._called_source_keys
                    or _has_call_activity(earlier)
                )
                times = max_times + 1
                if earlier_id in self._registered_this_run or has_call_activity or (clean_batch_id and earlier_batch != clean_batch_id):
                    prev_id = earlier_id
                else:
                    can_reuse = True

            if can_reuse and earlier is not None:
                lead_id = str(earlier["lead_id"])
                for k in _LIFECYCLE_CLEAR_KEYS:
                    earlier[k] = ""
                earlier["source_lead_id"] = source_lead_id
                earlier["source_label"] = source_label
                earlier["batch_id"] = clean_batch_id
                earlier["previous_lead_id"] = str(earlier.get("previous_lead_id") or "")
                earlier["times_processed"] = times
                earlier["business"] = business
                earlier["stage"] = "incoming"
                earlier["status"] = "incoming"
                earlier["updated_at"] = now
                earlier["received_at"] = now
                earlier["lead_json"] = lead_json
                row = earlier
            else:
                lead_id = self._next_lead_id()
                row = {col: "" for col in BASE_COLUMNS}
                row.update({
                    "lead_id": lead_id,
                    "source_key": source_key,
                    "source_lead_id": source_lead_id,
                    "source_label": source_label,
                    "batch_id": clean_batch_id,
                    "previous_lead_id": prev_id,
                    "times_processed": times,
                    "business": business,
                    "stage": "incoming",
                    "status": "incoming",
                    "updated_at": now,
                    "received_at": now,
                    "lead_json": lead_json,
                })
                self._rows.append(row)
                self._by_lead_id[lead_id] = row

            self._by_source_key[source_key] = row
            self._max_times_by_source_key[source_key] = times
            self._registered_this_run.add(lead_id)
            if not self._warned_rows and len(self._rows) >= config.TRACKER_WARN_ROWS:
                self._warned_rows = True
                logger.warning(
                    "Pipeline tracker has %d rows (threshold %d).",
                    len(self._rows),
                    config.TRACKER_WARN_ROWS,
                )
            self._mark_dirty()
            return lead_id

    def record_verification(self, lead_id: str, verification: Verification) -> None:
        now = _now_str()
        with self._mem_lock:
            row = self._by_lead_id.get(lead_id)
            if row is None:
                return
            row["verified_at"] = now
            row["updated_at"] = now
            row["phone_raw"] = verification.raw or ""
            row["phone_e164"] = verification.e164 or ""
            row["phone_location"] = verification.location or ""
            row["phone_timezone"] = verification.time_zone or ""
            row["verification_reason"] = verification.reason
            if verification.ok:
                row["verification_result"] = "passed"
                row["stage"] = "awaiting_decision"
                row["status"] = "awaiting_decision"
            else:
                row["verification_result"] = "rejected"
                row["stage"] = "number_rejected"
                row["status"] = verification.reason
                row["finished_at"] = now
                label = plain_english_label(verification.reason).rstrip(".")
                row["call_reason"] = f"Not called: {label}."
            self._mark_dirty()

    def record_decision(self, lead_id: str, decision: Decision) -> None:
        now = _now_str()
        with self._mem_lock:
            row = self._by_lead_id.get(lead_id)
            if row is None:
                return
            row["decided_at"] = now
            row["updated_at"] = now
            threshold = decision.threshold if decision.threshold is not None else config.INTEREST_THRESHOLD
            row["threshold_used"] = threshold
            row["decision_model"] = decision.model or ""
            row["decision_summary"] = decision.summary or ""
            row["decision_verdict"] = decision.verdict or ""
            row["service_scores_json"] = json.dumps(
                [
                    {
                        "service_id": s.service_id,
                        "service_name": s.service_name,
                        "probability": round(s.probability, 4),
                        "reason": s.reason,
                    }
                    for s in decision.scores
                ],
                ensure_ascii=False,
            )
            best_svc = decision.best_service
            if best_svc is not None:
                row["best_service_id"] = best_svc.id
                row["best_service"] = best_svc.name
                row["best_probability"] = round(decision.probability, 4)
            elif decision.scores:
                top = max(decision.scores, key=lambda s: s.probability)
                row["best_service_id"] = top.service_id
                row["best_service"] = top.service_name
                row["best_probability"] = round(top.probability, 4)
            else:
                row["best_service_id"] = ""
                row["best_service"] = ""
                row["best_probability"] = round(decision.probability, 4)

            if decision.qualified and decision.service is not None:
                row["decision_result"] = "qualified"
                row["assigned_service_id"] = decision.service.id
                row["assigned_service"] = decision.service.name
                row["stage"] = "qualified_waiting"
                row["status"] = "qualified_waiting"
                row["queued_at"] = now
                row["call_reason"] = f"Qualified for {decision.service.name}; waiting for the calling campaign."
            else:
                row["decision_result"] = "non_qualified"
                row["assigned_service_id"] = ""
                row["assigned_service"] = ""
                row["stage"] = "non_qualified"
                row["status"] = "not_interested"
                row["finished_at"] = now
                row["call_reason"] = f"Not called: no service reached {threshold:.0%}."
            self._mark_dirty()

    def record_decision_error(self, lead_id: str, message: str) -> None:
        now = _now_str()
        with self._mem_lock:
            row = self._by_lead_id.get(lead_id)
            if row is None:
                return
            row["decided_at"] = now
            row["updated_at"] = now
            row["finished_at"] = now
            row["decision_result"] = "error"
            row["stage"] = "decision_error"
            row["status"] = "decision_error"
            row["status_detail"] = message
            row["call_reason"] = "Not called: the lead could not be assessed and is saved for retry."
            self._mark_dirty()

    def _record_call_source_key(self, row: Dict[str, Any]) -> None:
        if _has_call_activity(row):
            sk = str(row.get("source_key") or "").strip()
            if sk:
                self._called_source_keys.add(sk)

    def record_campaign(self, lead_id: str, agent: Agent, campaign_id: int, campaign_name: str) -> None:
        now = _now_str()
        with self._mem_lock:
            row = self._by_lead_id.get(lead_id)
            if row is None:
                return
            row["updated_at"] = now
            row["agent_id"] = agent.id
            row["agent_name"] = agent.name
            row["campaign_id"] = campaign_id
            row["campaign_name"] = campaign_name
            row["stage"] = "calling"
            row["status"] = "calling"
            row["call_reason"] = f"In the calling queue because {_qualified_phrase(row)}."
            self._record_call_source_key(row)
            self._mark_dirty()

    def record_call_started(self, lead_id: str, run_id: Any, attempt: int) -> None:
        now = _now_str()
        with self._mem_lock:
            row = self._by_lead_id.get(lead_id)
            if row is None:
                return
            row["updated_at"] = now
            if not row.get("call_started_at"):
                row["call_started_at"] = now
            row["dograh_run_id"] = run_id if run_id is not None else ""
            row["call_attempts"] = attempt
            row["stage"] = "calling"
            row["status"] = "calling"
            row["call_reason"] = f"Called because {_qualified_phrase(row)}."
            self._record_call_source_key(row)
            self._mark_dirty()

    def record_finished(
        self,
        lead_id: str,
        status: str,
        detail: str = "",
        outcome: Optional[CallOutcome] = None,
    ) -> None:
        now = _now_str()
        with self._mem_lock:
            row = self._by_lead_id.get(lead_id)
            if row is None:
                return
            row["updated_at"] = now
            row["finished_at"] = now
            row["status"] = status
            row["status_detail"] = detail or ""

            if status.startswith("dropped") or status in ("invalid", "duplicate"):
                if not row.get("verified_at"):
                    row["verified_at"] = now
                row["stage"] = "number_rejected"
                row["verification_result"] = "rejected"
                if not row.get("verification_reason"):
                    row["verification_reason"] = status
                label = plain_english_label(status).rstrip(".")
                row["call_reason"] = f"Not called: {label}."
            elif status == "error":
                if not row.get("verification_result") and not row.get("verified_at"):
                    row["verified_at"] = now
                row["stage"] = "number_rejected"
                row["verification_result"] = "error"
                row["verification_reason"] = detail or "error"
                label = plain_english_label("error").rstrip(".")
                row["call_reason"] = f"Not called: {label}."
            elif status == "not_interested":
                row["stage"] = "non_qualified"
                row["decision_result"] = "non_qualified"
                if not row.get("call_reason"):
                    t = row.get("threshold_used")
                    thresh = float(t) if t not in (None, "") else config.INTEREST_THRESHOLD
                    row["call_reason"] = f"Not called: no service reached {thresh:.0%}."
            elif status == "decision_error":
                row["stage"] = "decision_error"
                row["decision_result"] = "error"
                row["call_reason"] = "Not called: the lead could not be assessed and is saved for retry."
            elif status == "skipped_missing_data":
                row["stage"] = "not_sent"
                row["call_reason"] = f"Not called: {_missing_details_text(detail)}."
            elif status == "scored_qualified":
                row["stage"] = "scored_qualified"
                row["call_reason"] = "Not called: this was a score-only test run."
            elif status == "interrupted":
                row["stage"] = "interrupted"
                has_campaign = bool(row.get("campaign_id") or row.get("campaign_name"))
                if detail:
                    row["call_reason"] = detail
                elif has_campaign:
                    row["call_reason"] = "Call status unknown: the run stopped while this campaign was active."
                else:
                    row["call_reason"] = "Not called: the run stopped before this lead was finished."
            elif status in ("called", "call_failed", "call_timeout"):
                row["stage"] = "call_finished"
                row["call_status"] = status
                dialed = bool(str(row.get("dograh_run_id") or "").strip()) or (
                    outcome is not None and outcome.run_id is not None
                )
                if not dialed:
                    row["call_reason"] = "Not called: the call could not be started."
                elif status == "called":
                    row["call_reason"] = f"Called because {_qualified_phrase(row)}."
                elif status == "call_failed":
                    row["call_reason"] = f"Called because {_qualified_phrase(row)}, but the call did not complete."
                else:
                    row["call_reason"] = (
                        f"Called because {_qualified_phrase(row)}, but its final result did not arrive in time."
                    )
                if outcome is not None:
                    if outcome.run_id is not None:
                        row["dograh_run_id"] = outcome.run_id
                    if outcome.attempts:
                        row["call_attempts"] = outcome.attempts
                    row["call_disposition"] = outcome.disposition or ""
                    row["call_duration_s"] = outcome.duration_seconds if outcome.duration_seconds is not None else ""
                    row["call_tags"] = ", ".join(map(str, outcome.tags)) if outcome.tags else ""
                    row["call_transferred"] = bool(is_transferred(outcome.raw))
                    row["transcript_url"] = outcome.transcript_url or ""
                    row["recording_url"] = outcome.recording_url or ""
                    if outcome.raw:
                        sanitized = sanitize_dograh_data(outcome.raw)
                        row["dograh_json"] = json.dumps(sanitized, ensure_ascii=False, default=str)
                self._record_call_source_key(row)
            self._mark_dirty()

    # ------------------------------------------------------------------ persistence

    def save(self) -> bool:
        with self._save_lock:
            return self._save_unlocked()

    def _save_unlocked(self) -> bool:
        with self._mem_lock:
            if not self._dirty and self.path.exists():
                return True
            if not self._rows and not self._dirty:
                return True
            version = self._dirty_version
            snapshot = [dict(r) for r in self._rows]

        started = time.monotonic()
        tmp_path = _write_tmp_workbook(self.path, snapshot)

        last_err: Optional[PermissionError] = None
        for attempt in range(5):
            try:
                os.replace(tmp_path, self.path)
                self.last_save_duration = time.monotonic() - started
                self._warned_perm = False
                with self._mem_lock:
                    if self._dirty_version == version:
                        self._dirty = False
                return True
            except PermissionError as e:
                last_err = e
                if attempt < 4:
                    time.sleep(0.2)

        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        if not self._warned_perm:
            self._warned_perm = True
            logger.warning("Could not save tracker %s (%s); will retry on next save.", self.path, last_err)
        return False

    def close(self) -> None:
        _release_os_lock(self._lock_fh)
        self._lock_fh = None
