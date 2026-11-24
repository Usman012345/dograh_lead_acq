"""Background batch runner controlled by the dashboard Start/Stop API."""

from __future__ import annotations

import inspect
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import config
from dashboard.data import TrackerDataStore
from pipeline_tracker import PipelineTracker, TrackerLockedError
from pull_leads import (
    BATCH,
    EMPTY,
    FATAL,
    RETRY,
    BatchStore,
    PingResult,
    default_process,
    ping,
    preflight,
)


class RunnerBusyError(Exception):
    """Raised when attempting to start a batch run while one is already active."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_batch_id() -> str:
    return f"job_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"


def _call_ping(
    ping_fn: Callable[..., Any],
    source_url: str,
    token: str,
    timeout: float,
    limit: int,
) -> PingResult:
    try:
        sig = inspect.signature(ping_fn)
        params = sig.parameters
        accepts_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
        if accepts_var_kw or "limit" in params:
            return ping_fn(source_url, token, timeout, limit=limit)
        return ping_fn(source_url, token, timeout)
    except (ValueError, TypeError):
        try:
            return ping_fn(source_url, token, timeout, limit=limit)
        except TypeError:
            return ping_fn(source_url, token, timeout)


def _call_process(
    process_fn: Callable[..., Any],
    leads: Any,
    batch_size: int,
    stop_event: threading.Event,
    batches_dir: Optional[Path] = None,
    tracker_file: Optional[Path] = None,
    batch_id: Optional[str] = None,
    progress_path: Optional[Path] = None,
    lead_indexes: Optional[List[int]] = None,
    retry_failed_calls: Optional[bool] = None,
) -> Any:
    candidate_kwargs: Dict[str, Any] = {
        "threshold": None,
        "output": None,
        "batch_size": batch_size,
        "stop_event": stop_event,
    }
    if batches_dir is not None:
        candidate_kwargs["batches_dir"] = batches_dir
    if tracker_file is not None:
        candidate_kwargs["tracker_file"] = tracker_file
    if batch_id:
        candidate_kwargs["batch_id"] = batch_id
    if progress_path is not None:
        candidate_kwargs["progress_path"] = progress_path
        candidate_kwargs["lead_indexes"] = lead_indexes
    if retry_failed_calls is not None:
        candidate_kwargs["retry_failed_calls"] = retry_failed_calls
    try:
        sig = inspect.signature(process_fn)
        params = sig.parameters
        accepts_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
        if accepts_var_kw:
            return process_fn(leads, **candidate_kwargs)
        accepted = {
            k: v for k, v in candidate_kwargs.items()
            if k in params and params[k].kind != inspect.Parameter.POSITIONAL_ONLY
        }
        return process_fn(leads, **accepted)
    except (ValueError, TypeError):
        try:
            return process_fn(leads, **candidate_kwargs)
        except TypeError:
            try:
                return process_fn(leads, None, None)
            except TypeError:
                return process_fn(leads)


class BatchRunner:
    """Manages a single background thread pulling and processing lead batches."""

    def __init__(
        self,
        *,
        store: TrackerDataStore,
        demo: bool = False,
        source_url: Optional[str] = None,
        token: Optional[str] = None,
        timeout: Optional[float] = None,
        batches_dir: Optional[Path] = None,
        ping_fn: Optional[Callable[..., Any]] = None,
        process_fn: Optional[Callable[..., Any]] = None,
        preflight_fn: Optional[Callable[[], Optional[str]]] = None,
    ) -> None:
        self._store = store
        self.demo = demo
        self._source_url = source_url
        self._token = token
        self._timeout = timeout
        self._batches_dir = Path(batches_dir) if batches_dir is not None else None
        self._ping_fn = ping_fn
        self._process_fn = process_fn
        self._preflight_fn = preflight_fn

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._status: Dict[str, Any] = {
            "state": "idle",
            "active": False,
            "batch_id": None,
            "batch_size": 300,
            "total_batches": 1,
            "current_batch": 0,
            "batches_completed": 0,
            "leads_fetched": 0,
            "leads_processed": 0,
            "current_batch_leads": 0,
            "started_at": None,
            "finished_at": None,
            "message": "",
            "error": None,
            "retry_failed_calls": config.RETRY_FAILED_CALLS,
        }
        # pending file -> ((mtime_ns, size), number of leads): avoids re-reading big files on every poll
        self._pending_cache: Dict[Path, Tuple[Tuple[int, int], int]] = {}
        self._pending_lock = threading.Lock()

    def pending_summary(self) -> Dict[str, int]:
        """Saved (pending) batches and how many of their leads are not finished yet."""
        if self.demo and self._ping_fn is None and self._process_fn is None:
            return {"pending_batches": 0, "pending_leads": 0}
        store = BatchStore(self._resolved_batches_dir())
        batches = leads_left = 0
        with self._pending_lock:
            live = set()
            for path in store.unfinished():
                try:
                    st = path.stat()
                    sig = (st.st_mtime_ns, st.st_size)
                    cached = self._pending_cache.get(path)
                    if cached is None or cached[0] != sig:
                        cached = (sig, len(store.load(path)["leads"]))
                        self._pending_cache[path] = cached
                    left = max(0, cached[1] - len(store.load_progress(path)))
                except (OSError, ValueError):
                    continue
                live.add(path)
                if left:
                    batches += 1
                    leads_left += left
            for gone in set(self._pending_cache) - live:
                self._pending_cache.pop(gone, None)
        return {"pending_batches": batches, "pending_leads": leads_left}

    def _resolved_source_url(self) -> str:
        return self._source_url if self._source_url is not None else config.LEAD_SOURCE_URL

    def _resolved_token(self) -> str:
        return self._token if self._token is not None else config.LEAD_SOURCE_TOKEN

    def _resolved_timeout(self) -> float:
        return float(self._timeout if self._timeout is not None else config.LEAD_SOURCE_TIMEOUT)

    def _resolved_batches_dir(self) -> Path:
        return self._batches_dir if self._batches_dir is not None else Path(config.PULLED_BATCHES_DIR)

    def _snapshot_locked(self) -> Dict[str, Any]:
        snapshot = dict(self._status)
        snapshot["source_url"] = None if self.demo else self._resolved_source_url()
        return snapshot

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            snapshot = self._snapshot_locked()
        snapshot.update(self.pending_summary())
        return snapshot

    def start(self, batch_size: int = 300, num_batches: int = 1,
              retry_failed_calls: Optional[bool] = None) -> Dict[str, Any]:
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not (1 <= batch_size <= 1000):
            raise ValueError("batch_size must be an integer between 1 and 1000")
        if isinstance(num_batches, bool) or not isinstance(num_batches, int) or not (1 <= num_batches <= 100):
            raise ValueError("num_batches must be an integer between 1 and 100")
        retry_calls = config.RETRY_FAILED_CALLS if retry_failed_calls is None else bool(retry_failed_calls)

        is_demo_sim = self.demo and self._ping_fn is None and self._process_fn is None
        batch_id: Optional[str] = None if is_demo_sim else _new_batch_id()

        with self._lock:
            if self._status["active"]:
                raise RunnerBusyError("A batch run is already active.")
            stop_event = threading.Event()
            self._stop_event = stop_event
            self._status = {
                "state": "running",
                "active": True,
                "batch_id": batch_id,
                "batch_size": batch_size,
                "total_batches": num_batches,
                "current_batch": 0,
                "batches_completed": 0,
                "leads_fetched": 0,
                "leads_processed": 0,
                "current_batch_leads": 0,
                "started_at": _utc_now(),
                "finished_at": None,
                "message": (
                    f"Starting {num_batches} batch(es) of up to {batch_size} leads..."
                ),
                "error": None,
                "retry_failed_calls": retry_calls,
            }
            thread = threading.Thread(
                target=self._run_worker,
                args=(batch_size, num_batches, stop_event, batch_id, retry_calls),
                daemon=True,
                name="batch-runner",
            )
            self._thread = thread
            snapshot = self._snapshot_locked()

        thread.start()
        snapshot.update(self.pending_summary())
        return snapshot

    def stop(self) -> Dict[str, Any]:
        with self._lock:
            self._stop_event.set()
            if self._status["active"]:
                self._status["state"] = "stopping"
                self._status["message"] = "Stopping after current step..."
            return self._snapshot_locked()

    def close(self) -> None:
        with self._lock:
            self._stop_event.set()
            thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)

    def _update(self, **fields: Any) -> None:
        with self._lock:
            self._status.update(fields)

    def _finish(self, state: str, message: str, *, error: Optional[str] = None) -> None:
        with self._lock:
            self._status["state"] = state
            self._status["active"] = False
            self._status["finished_at"] = _utc_now()
            self._status["message"] = message
            self._status["error"] = error

    def _run_worker(
        self,
        batch_size: int,
        num_batches: int,
        stop_event: threading.Event,
        batch_id: Optional[str],
        retry_failed_calls: Optional[bool] = None,
    ) -> None:
        try:
            if self.demo and self._ping_fn is None and self._process_fn is None:
                self._run_demo(batch_size, num_batches, stop_event)
            else:
                self._run_live(batch_size, num_batches, stop_event, batch_id, retry_failed_calls)
        except Exception as exc:
            self._finish("error", f"Batch run failed: {exc}", error=str(exc))

    def _run_demo(self, batch_size: int, num_batches: int, stop_event: threading.Event) -> None:
        fetched = 0
        processed = 0
        completed = 0
        for b in range(1, num_batches + 1):
            if stop_event.is_set():
                self._finish("stopped", "Stopped by user.")
                return
            self._update(
                current_batch=b,
                current_batch_leads=batch_size,
                message=f"Demo: pulling batch {b} of {num_batches}...",
            )
            if stop_event.wait(0.8):
                self._finish("stopped", "Stopped by user.")
                return
            fetched += batch_size
            self._update(
                leads_fetched=fetched,
                message=f"Demo: processing batch {b} of {num_batches} ({batch_size} leads)...",
            )
            if stop_event.wait(1.5):
                self._finish("stopped", "Stopped by user.")
                return
            self._store.refresh(force=True)
            completed += 1
            processed += batch_size
            self._update(batches_completed=completed, leads_processed=processed)

        self._finish(
            "completed",
            f"Demo completed {completed} batch(es) ({processed} leads).",
        )

    def _run_live(
        self,
        batch_size: int,
        num_batches: int,
        stop_event: threading.Event,
        batch_id: Optional[str] = None,
        retry_failed_calls: Optional[bool] = None,
    ) -> None:
        pf = self._preflight_fn
        if pf is None and self._ping_fn is None and self._process_fn is None:
            pf = preflight
        if pf is not None:
            err = pf()
            if stop_event.is_set():
                self._finish("stopped", "Stopped by user.")
                return
            if err:
                self._finish("error", err, error=err)
                return

        if self._process_fn is None:
            try:
                PipelineTracker(self._store.path).assert_unlocked()
            except TrackerLockedError as exc:
                self._finish("error", str(exc), error=str(exc))
                return

        source_url = self._resolved_source_url()
        if not source_url:
            msg = "LEAD_SOURCE_URL is missing in .env (the reverse proxy URL)."
            self._finish("error", msg, error=msg)
            return
        token = self._resolved_token()
        timeout = self._resolved_timeout()
        batches_dir = self._resolved_batches_dir()
        batch_store = BatchStore(batches_dir)
        ping_fn = self._ping_fn or ping
        process_fn = self._process_fn or default_process

        fetched = 0
        processed = 0
        completed = 0
        decision_errors = 0
        call_retries = 0
        unreadable_pending: set[Path] = set()
        # Only batches saved BEFORE this job are resumed by it. Retry batches this job
        # writes (Gemini failures, failed calls) wait for the next job, so a job never
        # spends its batches re-running its own failures.
        resumable = set(batch_store.unfinished())

        for b in range(1, num_batches + 1):
            if stop_event.is_set():
                self._finish("stopped", "Stopped by user.")
                return

            leads: list[dict[str, Any]] = []
            indexes: List[int] = []
            pending_path: Optional[Path] = None
            resumed_total = 0
            while True:
                unfinished = [p for p in batch_store.unfinished()
                              if p in resumable and p not in unreadable_pending]
                if not unfinished:
                    pending_path = None
                    break
                candidate = unfinished[0]
                try:
                    doc, left = batch_store.remaining(candidate)
                except (OSError, ValueError):
                    unreadable_pending.add(candidate)
                    continue
                if not left:
                    batch_store.mark_done(candidate)
                    continue
                # A saved batch is processed in pieces of the size asked for (not all at once)
                pending_path = candidate
                indexes = left[:batch_size]
                leads = [doc["leads"][i] for i in indexes]
                resumed_total = len(left)
                break

            if pending_path is not None:
                fetched += len(leads)
                rest = resumed_total - len(leads)
                self._update(
                    current_batch=b,
                    current_batch_leads=len(leads),
                    leads_fetched=fetched,
                    message=(f"Processing {len(leads)} saved lead(s) from {pending_path.name}"
                             + (f" ({rest} more saved after these)..." if rest else "...")),
                )
            else:
                self._update(
                    current_batch=b,
                    current_batch_leads=0,
                    message=f"Pulling batch {b} of {num_batches} from {source_url} (up to {batch_size} leads)...",
                )
                result = _call_ping(ping_fn, source_url, token, timeout, batch_size)
                if result.kind in (FATAL, RETRY):
                    self._finish("error", result.message, error=result.message)
                    return
                if result.kind == EMPTY:
                    self._finish(
                        "completed",
                        result.message or "No new leads available.",
                    )
                    return
                if result.kind != BATCH:
                    msg = f"Unexpected ping result: {result.kind}"
                    self._finish("error", msg, error=msg)
                    return

                leads = list(result.leads)
                indexes = list(range(len(leads)))
                pending_path = batch_store.save(result, source_url)
                fetched += len(leads)
                self._update(
                    current_batch_leads=len(leads),
                    leads_fetched=fetched,
                    message=f"Processing batch {b} of {num_batches} ({len(leads)} leads)...",
                )
                if stop_event.is_set():
                    self._finish("stopped", "Stopped by user. Unfinished batch kept for resume.")
                    return

            custom_tracker: Optional[Path] = None
            if self._store.path.resolve() != config.PIPELINE_TRACKER_FILE.resolve():
                custom_tracker = self._store.path
            stats = _call_process(
                process_fn,
                leads,
                batch_size,
                stop_event,
                batches_dir=batches_dir,
                tracker_file=custom_tracker,
                batch_id=batch_id,
                progress_path=pending_path,
                lead_indexes=indexes,
                retry_failed_calls=retry_failed_calls,
            )
            if isinstance(stats, dict):
                saved = stats.get("retry_saved_decision")
                decision_errors += int((saved if saved is not None else stats.get("decision_error")) or 0)
                call_retries += int(stats.get("retry_saved_call") or 0)
            self._store.refresh(force=True)

            if stop_event.is_set():
                self._finish("stopped", "Stopped by user. Unfinished leads kept and resumed on the next start.")
                return

            batch_store.add_progress(pending_path, indexes)
            try:
                _doc, still_left = batch_store.remaining(pending_path)
            except (OSError, ValueError):
                still_left = []
            if not still_left:
                batch_store.mark_done(pending_path)
            completed += 1
            processed += len(leads)
            self._update(batches_completed=completed, leads_processed=processed)

        summary_msg = f"Completed {completed} batch(es) ({processed} leads processed)."
        notes = []
        if decision_errors:
            notes.append(f"{decision_errors} failed Gemini scoring and saved for retry")
        if call_retries:
            notes.append(f"{call_retries} call(s) saved to be tried again")
        if notes:
            summary_msg = (f"Completed {completed} batch(es) ({processed} leads processed; "
                           f"{'; '.join(notes)}).")
        self._finish("completed", summary_msg)
