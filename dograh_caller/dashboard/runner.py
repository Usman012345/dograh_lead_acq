"""Background batch runner controlled by the dashboard Start/Stop API."""

from __future__ import annotations

import inspect
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

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
        }

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
            return self._snapshot_locked()

    def start(self, batch_size: int = 300, num_batches: int = 1) -> Dict[str, Any]:
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not (1 <= batch_size <= 1000):
            raise ValueError("batch_size must be an integer between 1 and 1000")
        if isinstance(num_batches, bool) or not isinstance(num_batches, int) or not (1 <= num_batches <= 100):
            raise ValueError("num_batches must be an integer between 1 and 100")

        with self._lock:
            if self._status["active"]:
                raise RunnerBusyError("A batch run is already active.")
            stop_event = threading.Event()
            self._stop_event = stop_event
            self._status = {
                "state": "running",
                "active": True,
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
            }
            thread = threading.Thread(
                target=self._run_worker,
                args=(batch_size, num_batches, stop_event),
                daemon=True,
                name="batch-runner",
            )
            self._thread = thread
            snapshot = self._snapshot_locked()

        thread.start()
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

    def _run_worker(self, batch_size: int, num_batches: int, stop_event: threading.Event) -> None:
        try:
            if self.demo and self._ping_fn is None and self._process_fn is None:
                self._run_demo(batch_size, num_batches, stop_event)
            else:
                self._run_live(batch_size, num_batches, stop_event)
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

    def _run_live(self, batch_size: int, num_batches: int, stop_event: threading.Event) -> None:
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
        unreadable_pending: set[Path] = set()

        for b in range(1, num_batches + 1):
            if stop_event.is_set():
                self._finish("stopped", "Stopped by user.")
                return

            leads: list[dict[str, Any]] = []
            pending_path: Optional[Path] = None
            while True:
                unfinished = [p for p in batch_store.unfinished() if p not in unreadable_pending]
                if not unfinished:
                    pending_path = None
                    break
                candidate = unfinished[0]
                try:
                    doc = batch_store.load(candidate)
                except (OSError, ValueError):
                    unreadable_pending.add(candidate)
                    continue
                if not doc["leads"]:
                    batch_store.mark_done(candidate)
                    continue
                pending_path = candidate
                leads = doc["leads"]
                break

            if pending_path is not None:
                fetched += len(leads)
                self._update(
                    current_batch=b,
                    current_batch_leads=len(leads),
                    leads_fetched=fetched,
                    message=f"Processing saved batch {pending_path.name} ({len(leads)} leads)...",
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
            )
            if isinstance(stats, dict):
                decision_errors += int(stats.get("decision_error") or 0)
            self._store.refresh(force=True)

            if stop_event.is_set():
                self._finish("stopped", "Stopped by user. Unfinished batch kept for resume.")
                return

            batch_store.mark_done(pending_path)
            completed += 1
            processed += len(leads)
            self._update(batches_completed=completed, leads_processed=processed)

        summary_msg = f"Completed {completed} batch(es) ({processed} leads processed)."
        if decision_errors:
            summary_msg = (
                f"Completed {completed} batch(es) ({processed} leads processed; "
                f"{decision_errors} failed Gemini scoring and saved for retry)."
            )
        self._finish("completed", summary_msg)
