"""
Read-only reader and view builder for output/pipeline_tracker.xlsx.

Caches parsed rows by (mtime_ns, size), reads bytes quickly via Path.read_bytes(),
and refreshes in a background thread so API requests never wait for an Excel parse.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from openpyxl import load_workbook

import config
from dashboard.media import is_storage_key
from decision_maker import load_services
from pipeline_tracker import TRACKER_SHEET_NAME, plain_english_label, sanitize_dograh_data
from results_store import _SYSTEM_CONTEXT_KEYS

_LEAD_ID_NUM_RE = re.compile(r"^L(\d+)$")
# Only real script/data links: ordinary text such as "Data: 5 projects" must stay visible.
_UNSAFE_SCHEME_RE = re.compile(
    r"^\s*(?:javascript\s*:|vbscript\s*:|data\s*:\s*[a-z]+/[a-z0-9.+-]+\s*[;,])",
    re.IGNORECASE,
)
_MISSING_VALUE_RE = re.compile(r"Missing value for (.+?) \(used in", re.IGNORECASE)

# Files of a call: where each one is found in the tracker row / Dograh run
AUDIO_TRACKS = {
    "recording": ("Full call", ("recording_public_url", "recording_url")),
    "agent": ("AI agent", ("bot_recording_public_url", "bot_recording_url")),
    "customer": ("Customer", ("user_recording_public_url", "user_recording_url")),
}
_TRANSCRIPT_FIELDS = ("transcript_public_url", "transcript_url")

_CALL_RESULTS_SKIP_DOGRAH_KEYS = frozenset({
    "transcript_url",
    "transcript_public_url",
    "recording_url",
    "recording_public_url",
    "bot_recording_url",
    "bot_recording_public_url",
    "user_recording_url",
    "user_recording_public_url",
})

_OUTCOME_LABELS = {
    "user_qualified": "Qualified on the call",
    "user_hangup": "Customer hung up",
    "user_idle_max_duration_exceeded": "Customer stopped responding",
    "user_speech": "Customer spoke",
    "transfer_call": "Transferred to your team",
    "call_transferred": "Transferred to your team",
    "voicemail": "Reached voicemail",
    "machine": "Reached voicemail",
    "no-answer": "No answer",
    "no_answer": "No answer",
    "busy": "Line busy",
    "failed": "Call failed",
    "end_call_tool": "Call completed",
}
_OUTCOME_FIELD_NAMES = ("call_disposition", "mapped_call_disposition", "call_tags")

_FIELD_LABELS = {
    "gathered_context": "Call Details",
    "extracted_variables": "Collected by Agent",
    "call_disposition": "Outcome",
    "mapped_call_disposition": "Mapped Outcome",
    "call_tags": "Tags",
    "nodes_visited": "Conversation Steps",
    "call_id": "Phone Call ID",
    "provider": "Phone Provider",
    "external_pbx_transferred": "Transferred by Phone System",
    "transfer_state": "Transfer State",
    "agent_transfers": "Agent Transfers",
    "annotations": "Notes",
    "call_type": "Call Type",
    "created_at": "Started At",
    "definition_id": "Agent Version",
    "id": "Run ID",
    "is_completed": "Completed",
    "mode": "Call Mode",
    "name": "Run Name",
    "workflow_id": "Agent ID",
}


def outcome_label(value: Any) -> str:
    """A Dograh disposition or tag in words a client understands."""
    text = str(value or "").strip()
    if not text:
        return ""
    key = text.lower()
    if key in _OUTCOME_LABELS:
        return _OUTCOME_LABELS[key]
    if "not_interested" in key or "not interested" in key:
        return "Declined on the call"
    if "interested" in key:
        return "Positive response on the call"
    return text.replace("_", " ").replace("-", " ").capitalize()


def row_outcome(row: Dict[str, Any]) -> str:
    """A transferred call always reads as transferred, however the line finally closed."""
    if _as_bool(row.get("call_transferred")):
        return _OUTCOME_LABELS["transfer_call"]
    return outcome_label(row.get("call_disposition"))


def outcome_list(value: Any) -> str:
    parts = value if isinstance(value, list) else str(value or "").split(",")
    labels: List[str] = []
    for part in parts:
        label = outcome_label(part)
        if label and label not in labels:
            labels.append(label)
    return ", ".join(labels)


def field_label(path: str) -> str:
    """'gathered_context > call_tags' -> 'Call Details > Tags'."""
    labels = []
    for part in path.split(" > "):
        labels.append(_FIELD_LABELS.get(part) or part.replace("_", " ").strip().title())
    return " > ".join(labels)


def source_label(value: Any) -> str:
    text = str(value or "").strip()
    if text == "streamer":
        return "Lead feed"
    if text == "python":
        return "Direct import"
    if text.startswith("file:"):
        return f"File: {text[5:]}"
    return text


def friendly_detail(row: Dict[str, Any]) -> str:
    """Why a lead stopped, without internal error text, addresses or stack details."""
    stage = str(row.get("stage") or "")
    status = str(row.get("status") or "")
    if stage == "decision_error":
        return "The AI review could not be completed. This lead is saved and can be reviewed again."
    if stage == "not_sent":
        match = _MISSING_VALUE_RE.search(str(row.get("status_detail") or ""))
        if match:
            return f"Required details are missing: {match.group(1).replace('_', ' ')}."
        return "Required details are missing."
    if stage == "interrupted":
        return "The run stopped before this lead was finished."
    if status == "error":
        return "The phone number could not be checked."
    if status == "call_timeout":
        return "The final call result did not arrive in time."
    if status == "call_failed":
        if str(row.get("dograh_run_id") or "").strip():
            return "The call was attempted but did not complete."
        return "The call could not be started."
    return ""


def safe_http_url(value: Any) -> str:
    """Return the URL only when it starts with http:// or https://; otherwise ''."""
    text = str(value or "").strip()
    if not text:
        return ""
    lower = text.lower()
    if lower.startswith(("http://", "https://")):
        return text
    return ""


def _sanitize_links_recursive(value: Any) -> Any:
    """Remove script links; a website without http:// is ordinary lead data and stays."""
    if isinstance(value, dict):
        return {str(k): _sanitize_links_recursive(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_links_recursive(item) for item in value]
    if isinstance(value, str) and _UNSAFE_SCHEME_RE.match(value):
        return ""
    return value


def _safe_media_value(value: Any) -> str:
    """A private Dograh file key or a public http(s) link; anything else is dropped."""
    text = str(value or "").strip()
    return text if is_storage_key(text) or safe_http_url(text) else ""


def _media_sources(row: Dict[str, Any], run: Dict[str, Any]) -> Dict[str, str]:
    media: Dict[str, str] = {}
    transcript = next(
        (v for v in (_safe_media_value(run.get(k)) for k in _TRANSCRIPT_FIELDS) if v),
        _safe_media_value(row.get("transcript_url")),
    )
    if transcript:
        media["transcript"] = transcript
    for track, (_label, fields) in AUDIO_TRACKS.items():
        found = next((v for v in (_safe_media_value(run.get(k)) for k in fields) if v), "")
        if not found and track == "recording":
            found = _safe_media_value(row.get("recording_url"))
        if found:
            media[track] = found
    return media


def _parse_json_dict(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except ValueError:
        return {}


def _parse_json_list(raw: Any) -> List[Dict[str, Any]]:
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    text = str(raw or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
        return [x for x in parsed if isinstance(x, dict)] if isinstance(parsed, list) else []
    except ValueError:
        return []


def _flatten_dict(data: Any, prefix: str = "") -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if isinstance(data, dict):
        for k, v in data.items():
            key = f"{prefix} > {k}" if prefix else str(k)
            if isinstance(v, dict):
                out.update(_flatten_dict(v, key))
            elif isinstance(v, list):
                if all(not isinstance(item, (dict, list)) for item in v):
                    out[key] = ", ".join(str(item) for item in v if item not in (None, ""))
                else:
                    out[key] = json.dumps(v, ensure_ascii=False, default=str)
            else:
                out[key] = v
    return out


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("true", "1", "yes")


def _as_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _lead_num(lead_id: Any) -> int:
    m = _LEAD_ID_NUM_RE.match(str(lead_id or "").strip())
    return int(m.group(1)) if m else 0


def _format_duration(seconds: Any) -> str:
    val = _as_float(seconds)
    if val is None:
        return ""
    total = round(val)
    mins, secs = divmod(total, 60)
    if mins > 0:
        return f"{mins}m {secs}s"
    return f"{secs}s"


def _format_pct(prob: Any) -> str:
    val = _as_float(prob)
    if val is None:
        return ""
    return f"{val:.0%}"


@dataclass(frozen=True)
class TrackerSnapshot:
    tracker_found: bool
    stale: bool
    data_as_of: Optional[str]
    file_sig: Optional[Tuple[int, int]]
    rows: Tuple[Dict[str, Any], ...]
    summary: Dict[str, Any] = field(default_factory=dict)


def _parse_tracker_bytes(raw_bytes: bytes) -> List[Dict[str, Any]]:
    wb = load_workbook(BytesIO(raw_bytes), read_only=True, data_only=True)
    try:
        ws = wb[TRACKER_SHEET_NAME] if TRACKER_SHEET_NAME in wb.sheetnames else wb.active
        rows_iter = ws.iter_rows(values_only=True)
        header = [str(h).strip() if h is not None else "" for h in next(rows_iter, [])]
        if not header or "lead_id" not in header:
            return []

        parsed_rows: List[Dict[str, Any]] = []
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

            lid = str(row.get("lead_id") or "").strip()
            if not lid:
                continue

            lead_dict = _sanitize_links_recursive(_parse_json_dict("".join(c for _, c in sorted(lead_chunks))))
            dograh_raw = _parse_json_dict("".join(c for _, c in sorted(dograh_chunks)))
            dograh_clean = _sanitize_links_recursive(sanitize_dograh_data(dograh_raw))

            row["_lead_fields"] = lead_dict
            row["_dograh_clean"] = dograh_clean
            row["_dograh_flat"] = {
                k: v for k, v in _flatten_dict(dograh_clean).items()
                if k not in _CALL_RESULTS_SKIP_DOGRAH_KEYS
            }
            row["_scores"] = _parse_json_list(row.get("service_scores_json"))
            row["_media"] = _media_sources(row, dograh_raw)
            parsed_rows.append(row)
        return parsed_rows
    finally:
        wb.close()


class TrackerDataStore:
    def __init__(
        self,
        tracker_path: Path,
        *,
        services_file: Optional[Path] = None,
        threshold: Optional[float] = None,
        refresh_seconds: Optional[int] = None,
        start_watcher: bool = True,
        demo: bool = False,
    ):
        self.path = Path(tracker_path)
        self.demo = demo
        self._services_cache: Tuple[Optional[Tuple[int, int]], List[Dict[str, str]]] = (None, [])
        self.services_file = Path(services_file) if services_file else config.SERVICES_FILE
        self.threshold = config.INTEREST_THRESHOLD if threshold is None else threshold
        self.refresh_seconds = config.DASHBOARD_REFRESH_SECONDS if refresh_seconds is None else refresh_seconds
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._snapshot = TrackerSnapshot(
            tracker_found=False,
            stale=False,
            data_as_of=None,
            file_sig=None,
            rows=(),
        )
        self.refresh()
        self._thread: Optional[threading.Thread] = None
        if start_watcher:
            self._thread = threading.Thread(target=self._watch_loop, daemon=True, name="tracker-reader")
            self._thread.start()

    def close(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.0)

    def _watch_loop(self) -> None:
        poll_interval = max(0.5, min(float(self.refresh_seconds), 2.0))
        while not self._stop_event.is_set():
            self._wake_event.wait(timeout=poll_interval)
            self._wake_event.clear()
            if self._stop_event.is_set():
                break
            self.refresh()

    def refresh(self, force: bool = False) -> None:
        with self._refresh_lock:
            try:
                if not self.path.exists():
                    summary = self._build_summary(False, False, None, ())
                    with self._lock:
                        self._snapshot = TrackerSnapshot(
                            tracker_found=False,
                            stale=False,
                            data_as_of=None,
                            file_sig=None,
                            rows=(),
                            summary=summary,
                        )
                    return
                st = self.path.stat()
                sig = (st.st_mtime_ns, st.st_size)
                with self._lock:
                    current = self._snapshot
                if not force and current.tracker_found and not current.stale and current.file_sig == sig:
                    return
                raw_bytes = self.path.read_bytes()
                rows = tuple(_parse_tracker_bytes(raw_bytes))
                data_as_of = datetime.fromtimestamp(st.st_mtime).isoformat(timespec="milliseconds")
                summary = self._build_summary(True, False, data_as_of, rows)
                with self._lock:
                    self._snapshot = TrackerSnapshot(
                        tracker_found=True,
                        stale=False,
                        data_as_of=data_as_of,
                        file_sig=sig,
                        rows=rows,
                        summary=summary,
                    )
            except Exception:
                with self._lock:
                    prev = self._snapshot
                    stale_summary = dict(prev.summary) if prev.summary else self._build_summary(
                        prev.tracker_found, True, prev.data_as_of, prev.rows
                    )
                    stale_summary["stale"] = True
                    self._snapshot = TrackerSnapshot(
                        tracker_found=prev.tracker_found,
                        stale=True,
                        data_as_of=prev.data_as_of,
                        file_sig=prev.file_sig,
                        rows=prev.rows,
                        summary=stale_summary,
                    )

    def snapshot(self) -> TrackerSnapshot:
        """Return the last good snapshot immediately; trigger background refresh if file changed."""
        with self._lock:
            snap = self._snapshot
        try:
            exists = self.path.exists()
            if not exists and snap.tracker_found:
                self._wake_event.set()
            elif exists:
                st = self.path.stat()
                if snap.file_sig != (st.st_mtime_ns, st.st_size):
                    self._wake_event.set()
        except OSError:
            self._wake_event.set()
        return snap

    def _configured_services(self) -> List[Dict[str, str]]:
        """services.yaml, re-read only when the file changes (the page polls every few seconds)."""
        try:
            st = self.services_file.stat()
            sig = (st.st_mtime_ns, st.st_size)
            if self._services_cache[0] != sig:
                loaded = [{"id": s.id, "name": s.name} for s in load_services(self.services_file)]
                self._services_cache = (sig, loaded)
        except Exception:
            pass
        return self._services_cache[1]

    def _services_list(self, rows: Tuple[Dict[str, Any], ...]) -> List[Dict[str, str]]:
        services: List[Dict[str, str]] = [dict(s) for s in self._configured_services()]
        seen: set[str] = {s["id"] for s in services}
        for r in rows:
            sid = str(r.get("assigned_service_id") or "").strip()
            sname = str(r.get("assigned_service") or "").strip()
            if sid and sid not in seen:
                services.append({"id": sid, "name": sname or sid})
                seen.add(sid)
        return services

    # ------------------------------------------------------------------ /api/health & /api/summary

    def get_health(self) -> Dict[str, Any]:
        snap = self.snapshot()
        return {
            "status": "ok",
            "tracker_found": snap.tracker_found,
            "stale": snap.stale,
        }

    def _build_summary(
        self,
        tracker_found: bool,
        stale: bool,
        data_as_of: Optional[str],
        rows: Tuple[Dict[str, Any], ...],
    ) -> Dict[str, Any]:
        services = self._services_list(rows)
        by_service: Dict[str, int] = {s["id"]: 0 for s in services}

        incoming_total = len(rows)
        v_passed = v_rejected = v_errors = v_waiting = 0
        q_total = nq_total = d_errors = d_waiting = 0
        ca_waiting = ca_calling = ca_not_sent = ca_not_called = ca_interrupted = 0
        cr_total = cr_called = cr_failed = cr_timeout = 0
        transferred_total = 0
        act_verifying = act_deciding = act_interrupted = 0

        active_threshold = self.threshold
        best_decided_at = ""
        found_threshold = False

        for r in rows:
            vr = str(r.get("verification_result") or "").strip()
            if vr == "passed":
                v_passed += 1
            elif vr == "rejected":
                v_rejected += 1
            elif vr == "error":
                v_errors += 1
            else:
                v_waiting += 1

            dr = str(r.get("decision_result") or "").strip()
            stage = r.get("stage")
            if dr == "qualified":
                q_total += 1
                sid = str(r.get("assigned_service_id") or "").strip()
                if sid:
                    by_service[sid] = by_service.get(sid, 0) + 1
                if stage == "qualified_waiting":
                    ca_waiting += 1
                elif stage == "calling":
                    ca_calling += 1
                elif stage == "not_sent":
                    ca_not_sent += 1
                elif stage == "scored_qualified":
                    ca_not_called += 1
                elif stage == "interrupted":
                    ca_interrupted += 1
            elif dr == "non_qualified":
                nq_total += 1
            elif dr == "error":
                d_errors += 1
            elif vr == "passed":
                d_waiting += 1

            if stage == "incoming":
                act_verifying += 1
            elif stage == "awaiting_decision":
                act_deciding += 1
            elif stage == "call_finished":
                cr_total += 1
                cs = r.get("call_status")
                if cs == "called":
                    cr_called += 1
                elif cs == "call_failed":
                    cr_failed += 1
                elif cs == "call_timeout":
                    cr_timeout += 1

            if stage == "interrupted":
                act_interrupted += 1

            if _as_bool(r.get("call_transferred")):
                transferred_total += 1

            t = _as_float(r.get("threshold_used"))
            if t is not None:
                decided_at = str(r.get("decided_at") or "")
                if not found_threshold or decided_at > best_decided_at:
                    best_decided_at = decided_at
                    active_threshold = t
                    found_threshold = True

        return {
            "tracker_found": tracker_found,
            "stale": stale,
            "data_as_of": data_as_of,
            "refresh_seconds": self.refresh_seconds,
            "threshold": active_threshold,
            "demo": self.demo,
            "services": services,
            "activity": {
                "verifying": act_verifying,
                "deciding": act_deciding,
                "queued": ca_waiting,
                "calling": ca_calling,
                "interrupted": act_interrupted,
            },
            "nodes": {
                "incoming": {"total": incoming_total},
                "number_verifier": {
                    "passed": v_passed,
                    "rejected": v_rejected,
                    "errors": v_errors,
                    "waiting": v_waiting,
                },
                "decision_maker": {
                    "reviewed": q_total + nq_total,
                    "errors": d_errors,
                    "waiting": d_waiting,
                },
                "non_qualified": {"total": nq_total},
                "qualified": {
                    "total": q_total,
                    "by_service": by_service,
                },
                "calling_agent": {
                    "waiting": ca_waiting,
                    "calling": ca_calling,
                    "not_sent": ca_not_sent,
                    "not_called": ca_not_called,
                    "interrupted": ca_interrupted,
                },
                "call_results": {
                    "total": cr_total,
                    "called": cr_called,
                    "failed": cr_failed,
                    "timeout": cr_timeout,
                },
                "transferred": {"total": transferred_total},
            },
        }

    def get_summary(self) -> Dict[str, Any]:
        snap = self.snapshot()
        if snap.summary:
            return dict(snap.summary)
        return self._build_summary(snap.tracker_found, snap.stale, snap.data_as_of, snap.rows)

    # ------------------------------------------------------------------ /api/leads

    def _view_spec(
        self,
        view: str,
        all_rows: Tuple[Dict[str, Any], ...],
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
        if view == "incoming":
            rows = list(all_rows)
            cols = [
                {"key": "lead_id", "label": "Lead ID"},
                {"key": "business", "label": "Name"},
                {"key": "phone", "label": "Phone"},
                {"key": "source_label", "label": "Source"},
                {"key": "received_at", "label": "Received At"},
            ]
            return rows, cols

        if view == "number_verifier":
            rows = list(all_rows)
            cols = [
                {"key": "lead_id", "label": "Lead ID"},
                {"key": "business", "label": "Name"},
                {"key": "phone", "label": "Phone"},
                {"key": "verification_result_label", "label": "Result"},
                {"key": "verification_reason_label", "label": "Reason"},
            ]
            return rows, cols

        if view == "decision_maker":
            rows = [r for r in all_rows if r.get("verification_result") == "passed"]
            cols = [
                {"key": "lead_id", "label": "Lead ID"},
                {"key": "business", "label": "Name"},
                {"key": "best_service", "label": "Best Match"},
                {"key": "best_probability_pct", "label": "Probability"},
                {"key": "decision_result_label", "label": "Result"},
            ]
            return rows, cols

        if view == "non_qualified":
            rows = [r for r in all_rows if r.get("decision_result") == "non_qualified"]
            cols = [
                {"key": "lead_id", "label": "Lead ID"},
                {"key": "decision_summary", "label": "Why It Was Not Qualified"},
            ]
            return rows, cols

        if view == "qualified":
            rows = [r for r in all_rows if r.get("decision_result") == "qualified"]
            cols = [
                {"key": "lead_id", "label": "Lead ID"},
                {"key": "assigned_service", "label": "Qualified Service"},
                {"key": "decision_summary", "label": "Why It Qualified"},
            ]
            return rows, cols

        if view == "calling_agent":
            rows = [
                r for r in all_rows
                if r.get("decision_result") == "qualified"
                and r.get("stage") in ("qualified_waiting", "calling", "not_sent", "scored_qualified", "interrupted")
            ]
            cols = [
                {"key": "lead_id", "label": "Lead ID"},
                {"key": "business", "label": "Name"},
                {"key": "assigned_service", "label": "Service"},
                {"key": "agent_name", "label": "Agent"},
                {"key": "campaign_name", "label": "Campaign"},
                {"key": "call_progress_label", "label": "Call Progress"},
            ]
            return rows, cols

        if view == "call_results":
            rows = [r for r in all_rows if r.get("stage") == "call_finished"]
            extra_keys: List[str] = []
            seen_extra: set[str] = set()
            for r in rows:
                for k in r.get("_dograh_flat", {}):
                    if k not in seen_extra:
                        seen_extra.add(k)
                        extra_keys.append(k)
            cols = [
                {"key": "lead_id", "label": "Lead ID"},
                {"key": "business", "label": "Name"},
                {"key": "assigned_service", "label": "Service"},
                {"key": "call_status_label", "label": "Call Status"},
                {"key": "call_disposition", "label": "Outcome"},
                {"key": "duration_label", "label": "Duration"},
                {"key": "call_attempts", "label": "Attempts"},
                {"key": "transferred_label", "label": "Transferred"},
                {"key": "call_tags", "label": "Tags"},
                {"key": "transcript_url", "label": "Transcript"},
                {"key": "recording_url", "label": "Recording"},
            ]
            for ek in sorted(extra_keys):
                cols.append({"key": f"dograh:{ek}", "label": field_label(ek)})
            return rows, cols

        if view == "transferred":
            rows = [r for r in all_rows if _as_bool(r.get("call_transferred"))]
            cols = [
                {"key": "lead_id", "label": "Lead ID"},
                {"key": "business", "label": "Name"},
                {"key": "assigned_service", "label": "Service"},
                {"key": "transferred_at", "label": "Transferred At"},
                {"key": "duration_label", "label": "Duration"},
                {"key": "call_disposition", "label": "Outcome"},
            ]
            return rows, cols

        raise ValueError(f"Unknown view: {view!r}")

    def _project_row(self, row: Dict[str, Any], columns: List[Dict[str, str]]) -> Dict[str, Any]:
        vr = str(row.get("verification_result") or "").strip()
        vr_label = {"passed": "Passed", "rejected": "Rejected", "error": "Error"}.get(vr, "Waiting")

        dr = str(row.get("decision_result") or "").strip()
        dr_label = {"qualified": "Qualified", "non_qualified": "Non-Qualified", "error": "Error"}.get(dr, "Waiting")

        stage = str(row.get("stage") or "").strip()
        progress_map = {
            "qualified_waiting": "Waiting for campaign",
            "calling": "Calling",
            "not_sent": "Not sent",
            "scored_qualified": "Qualified (test run, not called)",
            "interrupted": "Interrupted",
        }

        phone = str(row.get("phone_e164") or row.get("phone_raw") or "").strip()
        cs = str(row.get("call_status") or "").strip()
        media = row.get("_media", {})
        lead_id = str(row.get("lead_id") or "")

        values: Dict[str, Any] = {
            "lead_id": row.get("lead_id", ""),
            "business": row.get("business", ""),
            "phone": phone,
            "source_label": source_label(row.get("source_label")),
            "received_at": row.get("received_at", ""),
            "verification_result_label": vr_label,
            "verification_reason_label": (
                plain_english_label(row.get("verification_reason"))
                if row.get("verification_reason") else ("Waiting" if not vr else "")
            ),
            "best_service": row.get("best_service", ""),
            "best_probability_pct": _format_pct(row.get("best_probability")),
            "best_probability_num": _as_float(row.get("best_probability")),
            "decision_result_label": dr_label,
            "decision_summary": row.get("decision_summary", ""),
            "assigned_service": row.get("assigned_service", ""),
            "assigned_service_id": row.get("assigned_service_id", ""),
            "agent_name": row.get("agent_name", ""),
            "campaign_name": row.get("campaign_name", ""),
            "call_progress_label": progress_map.get(stage, plain_english_label(row.get("status") or stage)),
            "call_status_label": plain_english_label(cs) if cs else "",
            "call_disposition": row_outcome(row),
            "duration_label": _format_duration(row.get("call_duration_s")),
            "call_duration_num": _as_float(row.get("call_duration_s")),
            "call_attempts": _as_int(row.get("call_attempts")) if row.get("call_attempts") not in (None, "") else "",
            "transferred_label": "Yes" if _as_bool(row.get("call_transferred")) else "No",
            "call_tags": outcome_list(row.get("call_tags")),
            "transcript_url": self._media_link(lead_id, "transcript", media),
            "recording_url": self._media_link(lead_id, "recording", media),
            "transferred_at": row.get("finished_at") or row.get("updated_at") or "",
        }
        dograh_flat = row.get("_dograh_flat", {})
        out: Dict[str, Any] = {}
        for col in columns:
            key = col["key"]
            if key.startswith("dograh:"):
                dk = key[7:]
                out[key] = self._display_call_field(dk, dograh_flat.get(dk, ""))
            else:
                out[key] = values.get(key, "")
        # Internal sort helpers kept out of response unless requested, so return clean dict
        out["_sort_prob"] = values["best_probability_num"]
        out["_sort_dur"] = values["call_duration_num"]
        return out

    @staticmethod
    def _display_call_field(path: str, value: Any) -> Any:
        if isinstance(value, bool):
            return "Yes" if value else "No"
        if value is None:
            return ""
        if path.rsplit(" > ", 1)[-1] in _OUTCOME_FIELD_NAMES:
            return outcome_list(value)
        return value

    @staticmethod
    def _media_link(lead_id: str, kind: str, media: Dict[str, str]) -> str:
        value = media.get(kind, "")
        if not value:
            return ""
        if not is_storage_key(value):
            return value                       # already a public http(s) link
        if kind == "transcript":
            return f"/api/leads/{lead_id}/transcript"
        return f"/api/leads/{lead_id}/audio/{kind}"

    def media_key(self, lead_id: str, kind: str) -> Optional[str]:
        """The private Dograh file recorded on this lead, if any."""
        snap = self.snapshot()
        row = next((r for r in snap.rows if str(r.get("lead_id")) == lead_id), None)
        if row is None:
            return None
        value = row.get("_media", {}).get(kind, "")
        return value if is_storage_key(value) else None

    @staticmethod
    def _compute_filter_counts(view: str, rows: List[Dict[str, Any]]) -> Dict[str, int]:
        total = len(rows)
        if view == "number_verifier":
            passed = rejected = errors = waiting = 0
            for r in rows:
                vr = str(r.get("verification_result") or "").strip()
                if vr == "passed":
                    passed += 1
                elif vr == "rejected":
                    rejected += 1
                elif vr == "error":
                    errors += 1
                else:
                    waiting += 1
            return {
                "": total,
                "passed": passed,
                "rejected": rejected,
                "errors": errors,
                "waiting": waiting,
            }
        if view == "decision_maker":
            qualified = non_qualified = errors = waiting = 0
            for r in rows:
                dr = str(r.get("decision_result") or "").strip()
                if dr == "qualified":
                    qualified += 1
                elif dr == "non_qualified":
                    non_qualified += 1
                elif dr == "error":
                    errors += 1
                else:
                    waiting += 1
            return {
                "": total,
                "qualified": qualified,
                "non_qualified": non_qualified,
                "errors": errors,
                "waiting": waiting,
            }
        if view == "calling_agent":
            waiting = calling = not_sent = not_called = interrupted = 0
            for r in rows:
                st = r.get("stage")
                if st == "qualified_waiting":
                    waiting += 1
                elif st == "calling":
                    calling += 1
                elif st == "not_sent":
                    not_sent += 1
                elif st == "scored_qualified":
                    not_called += 1
                elif st == "interrupted":
                    interrupted += 1
            return {
                "": total,
                "waiting": waiting,
                "calling": calling,
                "not_sent": not_sent,
                "not_called": not_called,
                "interrupted": interrupted,
            }
        if view == "call_results":
            called = failed = timeout = 0
            for r in rows:
                cs = r.get("call_status")
                if cs == "called":
                    called += 1
                elif cs == "call_failed":
                    failed += 1
                elif cs == "call_timeout":
                    timeout += 1
            return {
                "": total,
                "called": called,
                "not_completed": failed + timeout,
                "failed": failed,
                "timeout": timeout,
            }
        if view == "incoming":
            interrupted = sum(1 for r in rows if r.get("stage") == "interrupted")
            return {"": total, "interrupted": interrupted}
        return {"": total}

    def get_leads(
        self,
        *,
        view: str,
        service: Optional[str] = None,
        filter_val: Optional[str] = None,
        q: Optional[str] = None,
        sort: str = "lead_id",
        order: str = "asc",
        page: int = 1,
        page_size: int = 50,
    ) -> Dict[str, Any]:
        snap = self.snapshot()
        rows, columns = self._view_spec(view, snap.rows)

        if service:
            s_norm = service.strip()
            rows = [r for r in rows if str(r.get("assigned_service_id") or "").strip() == s_norm]

        filter_counts = self._compute_filter_counts(view, rows)

        if filter_val:
            f = filter_val.strip().lower().replace("-", "_").replace(" ", "_")
            if view == "number_verifier":
                if f == "passed":
                    rows = [r for r in rows if r.get("verification_result") == "passed"]
                elif f == "rejected":
                    rows = [r for r in rows if r.get("verification_result") == "rejected"]
                elif f in ("error", "errors"):
                    rows = [r for r in rows if r.get("verification_result") == "error"]
                elif f == "waiting":
                    rows = [r for r in rows if not str(r.get("verification_result") or "").strip()]
            elif view == "decision_maker":
                if f == "qualified":
                    rows = [r for r in rows if r.get("decision_result") == "qualified"]
                elif f == "non_qualified":
                    rows = [r for r in rows if r.get("decision_result") == "non_qualified"]
                elif f in ("error", "errors"):
                    rows = [r for r in rows if r.get("decision_result") == "error"]
                elif f == "waiting":
                    rows = [r for r in rows if not str(r.get("decision_result") or "").strip()]
            elif view == "calling_agent":
                if f == "waiting":
                    rows = [r for r in rows if r.get("stage") == "qualified_waiting"]
                elif f == "calling":
                    rows = [r for r in rows if r.get("stage") == "calling"]
                elif f == "not_sent":
                    rows = [r for r in rows if r.get("stage") == "not_sent"]
                elif f == "not_called":
                    rows = [r for r in rows if r.get("stage") == "scored_qualified"]
                elif f == "interrupted":
                    rows = [r for r in rows if r.get("stage") == "interrupted"]
            elif view == "incoming":
                if f == "interrupted":
                    rows = [r for r in rows if r.get("stage") == "interrupted"]
            elif view == "call_results":
                if f == "called":
                    rows = [r for r in rows if r.get("call_status") == "called"]
                elif f == "not_completed":
                    rows = [r for r in rows if r.get("call_status") in ("call_failed", "call_timeout")]
                elif f in ("failed", "call_failed"):
                    rows = [r for r in rows if r.get("call_status") == "call_failed"]
                elif f in ("timeout", "call_timeout"):
                    rows = [r for r in rows if r.get("call_status") == "call_timeout"]

        if q and q.strip():
            needle = q.strip().casefold()
            # "(512) 555-0101", "512-555-0101" and "5125550101" all find the same number
            allow_phone_digits = not any(ch.isalpha() for ch in needle)
            digits = re.sub(r"\D", "", needle)
            digits = digits[1:] if len(digits) == 11 and digits.startswith("1") else digits

            def phone_matches(row: Dict[str, Any]) -> bool:
                if not allow_phone_digits or len(digits) < 3:
                    return False
                return any(
                    digits in re.sub(r"\D", "", str(row.get(field) or ""))
                    for field in ("phone_raw", "phone_e164")
                )

            rows = [
                r for r in rows
                if (
                    needle in str(r.get("lead_id") or "").casefold()
                    or needle in str(r.get("business") or "").casefold()
                    or needle in str(r.get("phone_raw") or "").casefold()
                    or needle in str(r.get("phone_e164") or "").casefold()
                    or phone_matches(r)
                )
            ]

        projected = [self._project_row(r, columns) for r in rows]

        reverse = str(order or "asc").strip().lower() == "desc"
        sort_key = (sort or "lead_id").strip()
        valid_keys = {c["key"] for c in columns}
        if sort_key not in valid_keys:
            sort_key = "lead_id"

        def sort_value(item: Dict[str, Any]) -> Any:
            if sort_key == "lead_id":
                return _lead_num(item.get("lead_id"))
            if sort_key == "best_probability_pct":
                return item.get("_sort_prob")
            if sort_key == "duration_label":
                return item.get("_sort_dur")
            if sort_key == "call_attempts":
                value = item.get("call_attempts")
                return None if value == "" else int(value)
            value = item.get(sort_key, "")
            return None if value in ("", None) else str(value).casefold()

        # Empty values stay at the bottom whichever way the column is sorted
        with_value = [item for item in projected if sort_value(item) is not None]
        without_value = [item for item in projected if sort_value(item) is None]
        with_value.sort(key=sort_value, reverse=reverse)
        projected = with_value + without_value

        total = len(projected)
        page = max(1, page)
        page_size = max(1, min(200, page_size))
        start = (page - 1) * page_size
        page_rows = projected[start:start + page_size]
        for r in page_rows:
            r.pop("_sort_prob", None)
            r.pop("_sort_dur", None)

        return {
            "view": view,
            "columns": columns,
            "rows": page_rows,
            "total": total,
            "page": page,
            "page_size": page_size,
            "filter_counts": filter_counts,
        }

    # ------------------------------------------------------------------ /api/leads/{lead_id}

    def get_lead_detail(self, lead_id: str) -> Optional[Dict[str, Any]]:
        snap = self.snapshot()
        row = next((r for r in snap.rows if str(r.get("lead_id")) == lead_id), None)
        if row is None:
            return None

        stage = str(row.get("stage") or "").strip()
        vr = str(row.get("verification_result") or "").strip()
        dr = str(row.get("decision_result") or "").strip()
        threshold = _as_float(row.get("threshold_used"))
        if threshold is None:
            threshold = self.threshold

        # Journey steps
        received_at = str(row.get("received_at") or "").strip()
        verified_at = str(row.get("verified_at") or "").strip()
        decided_at = str(row.get("decided_at") or "").strip()
        queued_at = str(row.get("queued_at") or "").strip()
        call_started_at = str(row.get("call_started_at") or "").strip()
        finished_at = str(row.get("finished_at") or "").strip()

        def step_state(done_cond: bool, err_cond: bool = False, cur_cond: bool = False, skip_cond: bool = False) -> str:
            if err_cond:
                return "error"
            if cur_cond:
                return "current"
            if done_cond:
                return "done"
            if skip_cond:
                return "skipped"
            return "pending"

        has_call = bool(
            stage == "call_finished"
            or row.get("call_status")
            or row.get("dograh_run_id")
            or row.get("campaign_id")
        )
        call_ok = str(row.get("call_status") or "") == "called"
        journey = [
            {"stage": "Received", "at": received_at or None, "state": "done" if received_at else "pending"},
            {
                "stage": "Number Check",
                "at": verified_at or None,
                "state": step_state(
                    vr == "passed",
                    err_cond=vr in ("rejected", "error"),
                    cur_cond=stage == "incoming",
                ),
            },
            {
                "stage": "Decision",
                "at": decided_at or None,
                "state": step_state(
                    dr in ("qualified", "non_qualified"),
                    err_cond=dr == "error",
                    cur_cond=stage == "awaiting_decision",
                    skip_cond=vr in ("rejected", "error"),
                ),
            },
            {
                "stage": "Campaign",
                "at": queued_at or None,
                "state": step_state(
                    bool(row.get("campaign_id")),
                    err_cond=stage == "not_sent" or (stage == "call_finished" and not row.get("campaign_id")),
                    cur_cond=stage == "qualified_waiting",
                    skip_cond=dr != "qualified" or stage == "scored_qualified",
                ),
            },
            {
                "stage": "Call",
                "at": call_started_at or None,
                "state": step_state(
                    stage == "call_finished" and call_ok,
                    err_cond=stage == "call_finished" and not call_ok,
                    cur_cond=stage == "calling",
                    skip_cond=dr != "qualified" or stage in ("not_sent", "scored_qualified"),
                ),
            },
            {
                "stage": "Result",
                "at": finished_at or None,
                "state": step_state(
                    (stage == "call_finished" and call_ok) or stage in ("non_qualified", "scored_qualified"),
                    err_cond=(stage == "call_finished" and not call_ok)
                    or stage in ("number_rejected", "decision_error", "not_sent", "interrupted"),
                ),
            },
        ]

        scores_out = []
        for s in row.get("_scores", []):
            prob = _as_float(s.get("probability")) or 0.0
            scores_out.append({
                "service_id": s.get("service_id", ""),
                "service_name": s.get("service_name", ""),
                "probability": prob,
                "meets_threshold": prob >= threshold,
                "reason": s.get("reason", ""),
            })

        call_obj: Optional[Dict[str, Any]] = None
        if has_call:
            cs = str(row.get("call_status") or "").strip()
            media = row.get("_media", {})
            gathered = row.get("_dograh_clean", {}).get("gathered_context") or {}
            extracted = gathered.get("extracted_variables")
            collected = {k: v for k, v in gathered.items() if k not in _SYSTEM_CONTEXT_KEYS}
            if isinstance(extracted, dict):
                collected.update(extracted)
            collected = {k: v for k, v in collected.items() if v not in (None, "", [], {})}
            call_obj = {
                "status": cs or stage,
                "status_label": plain_english_label(cs or stage),
                "disposition": row_outcome(row),
                "duration_s": _as_float(row.get("call_duration_s")),
                "duration_label": _format_duration(row.get("call_duration_s")),
                "attempts": _as_int(row.get("call_attempts")),
                "tags": [t for t in outcome_list(row.get("call_tags")).split(", ") if t],
                "transferred": _as_bool(row.get("call_transferred")),
                "agent_name": row.get("agent_name") or "",
                "campaign_name": row.get("campaign_name") or "",
                "dograh_run_id": row.get("dograh_run_id") if row.get("dograh_run_id") not in (None, "") else None,
                "transcript_available": is_storage_key(media.get("transcript")),
                "transcript_url": safe_http_url(media.get("transcript")) or None,
                "recording_url": self._media_link(str(row.get("lead_id")), "recording", media) or None,
                "audio_tracks": [
                    {"id": track, "label": label, "url": self._media_link(str(row.get("lead_id")), track, media)}
                    for track, (label, _fields) in AUDIO_TRACKS.items()
                    if media.get(track)
                ],
                "collected": collected,
                "fields": {
                    field_label(k): self._display_call_field(k, v)
                    for k, v in row.get("_dograh_flat", {}).items()
                },
            }

        status_code = str(row.get("status") or stage)
        return {
            "lead_id": row.get("lead_id"),
            "business": row.get("business") or "(no company name)",
            "stage": stage,
            "status": status_code,
            "status_label": plain_english_label(status_code),
            "status_detail": friendly_detail(row),
            "journey": journey,
            "number_check": {
                "result": vr or "waiting",
                "reason": plain_english_label(row.get("verification_reason")) if row.get("verification_reason") else "Waiting",
                "raw_reason": row.get("verification_reason") or "",
                "phone": row.get("phone_e164") or row.get("phone_raw") or "",
                "phone_raw": row.get("phone_raw") or "",
                "location": row.get("phone_location") or "",
                "time_zone": row.get("phone_timezone") or "",
            },
            "decision": {
                "result": dr or "waiting",
                "assigned_service": row.get("assigned_service") or None,
                "assigned_service_id": row.get("assigned_service_id") or None,
                "best_service": row.get("best_service") or None,
                "best_probability": _as_float(row.get("best_probability")),
                "threshold": threshold,
                "model": row.get("decision_model") or "",
                "summary": row.get("decision_summary") or "",
                "verdict": row.get("decision_verdict") or "",
                "scores": scores_out,
                "call_reason": row.get("call_reason") or "",
            },
            "call": call_obj,
            "lead_fields": row.get("_lead_fields", {}),
        }
