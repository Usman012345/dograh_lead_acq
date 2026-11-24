#!/usr/bin/env python3
"""
Pull leads from the lead streamer reverse proxy one batch at a time.

    python dashboard_server.py  dashboard: starts idle; click Start Batch to pull & call
    python pull_leads.py        CLI: waits for you to press Enter before every ping (including the first)

    1. ping   GET https://ai-sdr.grobots.ai/leads/next   (Authorization: Bearer <token>)
    2. get    one batch of up to 300 lead dicts
    3. save   the batch to output/pulled_batches/ (the streamer has already marked
              these leads as sent, so this copy is the only one)
    4. run    the whole batch through the pipeline: number check -> Gemini -> Dograh
              campaigns -> every call finished -> output/call_results.xlsx
    5. wait   for a person: Enter = ping for the next batch, q = quit

In CLI mode (`python pull_leads.py`), every ping — including the first — requires a
human to press Enter. In the dashboard (`python dashboard_server.py`), the runner stays
idle until you click Start Batch. In unattended automatic mode (Puller.run_auto, used
only when explicitly enabled), it first confirms the real streamer answers /health,
then pings, finishes each batch completely, and checks again on a timer.

Settings (.env): LEAD_SOURCE_URL (default https://ai-sdr.grobots.ai),
LEAD_SOURCE_TOKEN (= the streamer's STREAM_TOKEN), LEAD_BATCH_SIZE (300).
The streamer's side of the agreement: lead_streamer/LEAD_STREAMER_SPEC.md §7, §12, §16.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import requests

import config

# ==============================================================================
# Ping the streamer
# ==============================================================================

BATCH = "batch"     # leads received
EMPTY = "empty"     # streamer has nothing new right now
RETRY = "retry"     # no leads this time (network, 404, 503, bad answer); a person may try again
FATAL = "fatal"     # configuration problem (401 token, 421 host); trying again won't help

MIN_AUTO_CHECK_SECONDS = 5
MAX_AUTO_BACKOFF_SECONDS = 3600


@dataclass
class PingResult:
    kind: str
    message: str = ""
    leads: List[Dict[str, Any]] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)


def _error_detail(resp: requests.Response) -> str:
    try:
        data = resp.json()
        if isinstance(data, dict):
            return str(data.get("error") or data.get("message") or data)[:200]
    except ValueError:
        pass
    return (resp.text or "").strip().replace("\n", " ")[:120]


def ping(base_url: str, token: str, timeout: float,
         session: Optional[requests.Session] = None,
         limit: Optional[int] = None) -> PingResult:
    """One GET <base_url>/leads/next. Never raises for HTTP or network problems."""
    url = f"{base_url.rstrip('/')}/leads/next"
    if limit is not None:
        url = f"{url}?limit={int(limit)}"
    http = session or requests
    try:
        resp = http.get(
            url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                     "Accept-Encoding": "gzip"},
            timeout=(15, timeout),
            allow_redirects=False,
        )
    except requests.exceptions.Timeout:
        return PingResult(RETRY, f"No answer from {url} within {timeout:.0f}s. If the streamer did "
                                 "hand out a batch, those leads are now marked as sent there; they can "
                                 "be re-sent with the SQL in lead_streamer/README.md §5.")
    except requests.exceptions.SSLError as e:
        return PingResult(RETRY, f"TLS/certificate problem with {url}: {e}")
    except requests.exceptions.RequestException as e:
        return PingResult(RETRY, f"Could not reach {url}: {e}")

    code = resp.status_code
    if code == 401:
        return PingResult(FATAL, "The streamer rejected the token (HTTP 401). LEAD_SOURCE_TOKEN in .env "
                                 "must equal STREAM_TOKEN in the streamer's .env.")
    if code == 421:
        return PingResult(FATAL, f"The streamer refused the host name (HTTP 421). LEAD_SOURCE_URL "
                                 f"({base_url}) must equal the streamer's PUBLIC_BASE_URL.")
    if code == 404:
        return PingResult(RETRY, f"{url} does not exist (HTTP 404): the lead streamer is not running "
                                 "behind this address yet, or the reverse proxy doesn't forward "
                                 "/leads/ to it.")
    if code == 503 and _error_detail(resp) == "schema_mismatch":
        return PingResult(FATAL, "The streamer is connected to a database whose tables don't match the "
                                 "scraper's (HTTP 503 schema_mismatch). Fix DATABASE_URL on the streamer; "
                                 "`python lead_streamer.py --check` there shows what is missing.")
    if code == 503:
        wait = None
        try:
            wait = resp.json().get("retry_after_seconds")
        except (ValueError, AttributeError):
            pass
        wait = wait or resp.headers.get("Retry-After") or "a few"
        return PingResult(RETRY, f"Streamer is busy or its database is unavailable (HTTP 503: "
                                 f"{_error_detail(resp)}). Wait {wait} seconds, then try again.",
                          meta={"retry_after": wait})
    if code != 200:
        return PingResult(RETRY, f"Unexpected answer HTTP {code} from {url}: {_error_detail(resp)}")

    try:
        data = resp.json()
    except ValueError:
        ctype = resp.headers.get("Content-Type", "?")
        return PingResult(RETRY, f"{url} answered with something that is not JSON ({ctype}). That is a "
                                 "web page, not the lead streamer: check where the address points.")
    if not isinstance(data, dict):
        return PingResult(RETRY, f"Unexpected JSON from {url}: expected an object, got {type(data).__name__}")

    status = data.get("status")
    meta = {k: data.get(k) for k in ("served_at", "count", "new", "updated")}
    if status == "no_new_leads":
        return PingResult(EMPTY, str(data.get("message") or "No new Leads available"), meta=meta)
    if status != "ok":
        return PingResult(RETRY, f"Unexpected status '{status}' from {url}: {_error_detail(resp)}")

    raw = data.get("leads")
    if not isinstance(raw, list):
        return PingResult(RETRY, f"Answer from {url} has status ok but no 'leads' list")
    leads = [x for x in raw if isinstance(x, dict)]
    skipped = len(raw) - len(leads)
    if not leads:
        return PingResult(EMPTY, "The streamer answered ok but sent no leads", meta=meta)
    note = f" ({skipped} item(s) that were not objects were ignored)" if skipped else ""
    return PingResult(BATCH, f"Received {len(leads)} lead(s){note}", leads=leads, meta=meta)


# ==============================================================================
# Received batches on disk
# ==============================================================================

class BatchStore:
    """
    output/pulled_batches/batch_<time>_<n>.pending.json   received, not fully processed yet
    output/pulled_batches/batch_<time>_<n>.done.json      fully processed (kept as a record)
    """

    def __init__(self, folder: Path):
        self.folder = Path(folder)

    def save(self, result: PingResult, source: str) -> Path:
        self.folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        n_leads = len(result.leads)
        path = self.folder / f"batch_{stamp}_{n_leads}.pending.json"
        seq = 1
        while path.exists() or path.with_name(path.name.replace(".pending.json", ".done.json")).exists():
            path = self.folder / f"batch_{stamp}_{seq}_{n_leads}.pending.json"
            seq += 1
        tmp = path.with_name(path.name + ".tmp")
        doc = {"received_at": datetime.now().isoformat(timespec="seconds"), "source": source,
               **result.meta, "leads": result.leads}
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, default=str)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        return path

    def unfinished(self) -> List[Path]:
        if not self.folder.is_dir():
            return []
        return sorted(self.folder.glob("batch_*.pending.json"))

    @staticmethod
    def load(path: Path) -> Dict[str, Any]:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
        doc["leads"] = [x for x in doc.get("leads") or [] if isinstance(x, dict)]
        return doc

    @staticmethod
    def mark_done(path: Path) -> Path:
        done = path.with_name(path.name.replace(".pending.json", ".done.json"))
        os.replace(path, done)
        return done


# ==============================================================================
# The loop: ping -> process fully -> wait for a person
# ==============================================================================

def ask(prompt: str) -> str:
    """Read one answer from the person at the keyboard. End of input or Ctrl+C = quit."""
    try:
        return input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return "q"


def say(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {'source':<8} {message}", flush=True)


def save_failed_for_retry(failed: List[Dict[str, Any]], batches_dir: Optional[Path] = None) -> Path:
    """Keep leads Gemini could not score as a pending batch, so they are processed again."""
    folder = Path(batches_dir) if batches_dir is not None else config.PULLED_BATCHES_DIR
    retry_path = BatchStore(folder).save(PingResult(BATCH, leads=failed), "decision_retry")
    say(f"Saved {len(failed)} failed-decision lead(s) for retry -> {retry_path.name}")
    return retry_path


def default_process(
    leads: List[Dict[str, Any]],
    threshold: Optional[float],
    output: Optional[str],
    *,
    batch_size: Optional[int] = None,
    stop_event: Optional[Any] = None,
    batches_dir: Optional[Path] = None,
    tracker_file: Optional[Path] = None,
) -> Counter:
    from pipeline import run_leads_sync
    failed: List[Dict[str, Any]] = []
    extra_kwargs: Dict[str, Any] = {}
    if tracker_file is not None:
        extra_kwargs["tracker_file"] = tracker_file
    stats = run_leads_sync(
        leads,
        batch_size=batch_size or config.LEAD_BATCH_SIZE,
        stop_event=stop_event,
        threshold=threshold,
        results_file=output,
        failed_leads=failed,
        source_label="streamer",
        **extra_kwargs,
    )
    if failed and not (stop_event is not None and stop_event.is_set()):
        save_failed_for_retry(failed, batches_dir=batches_dir)
    return stats


def streamer_is_ready(base_url: str, timeout: float = 20) -> bool:
    """
    True only when the real lead streamer answers /health with a working database.
    No token is sent, so nothing reaches another site while the proxy is not attached.
    """
    try:
        resp = requests.get(f"{base_url.rstrip('/')}/health", timeout=timeout, allow_redirects=False)
        data = resp.json()
    except (requests.RequestException, ValueError):
        return False
    return (resp.status_code == 200 and isinstance(data, dict)
            and data.get("status") == "alive" and data.get("database") == "ok")


def preflight(need_streamer: bool = True) -> Optional[str]:
    """Check the keys before taking leads from the streamer. Returns a problem, or None."""
    if need_streamer and not config.LEAD_SOURCE_URL:
        return "LEAD_SOURCE_URL is missing in .env (the lead streamer reverse proxy URL)."
    if need_streamer and not config.LEAD_SOURCE_TOKEN:
        return "LEAD_SOURCE_TOKEN is missing in .env (the streamer's STREAM_TOKEN)."
    if not config.GEMINI_API_KEY:
        return "Gemini API key not found. Put it in .env as GEMINI_API_KEY=... (or GEMINI_API=...)."
    if not 0 < config.INTEREST_THRESHOLD <= 1:
        return (
            f"INTEREST_THRESHOLD must be above 0 and at most 1 (0.80 = 80%), "
            f"got {config.INTEREST_THRESHOLD!r}."
        )
    try:
        from decision_maker import VALID_THINKING_LEVELS, check_gemini_access, load_services
        from dograh_client import DograhClient
        thinking_level = config.GEMINI_THINKING_LEVEL.upper()
        if thinking_level not in VALID_THINKING_LEVELS:
            return (
                f"GEMINI_THINKING_LEVEL must be one of {', '.join(VALID_THINKING_LEVELS)}, "
                f"got {thinking_level!r}."
            )
        check_gemini_access(config.GEMINI_API_KEY, config.GEMINI_MODEL)
        services = load_services(config.SERVICES_FILE)
        if not services:
            return f"No enabled services in {config.SERVICES_FILE.name}."
        client = DograhClient()
        try:
            for s in services:
                client.get_agent(s.agent_id)
        finally:
            client.close()
    except Exception as e:
        return f"Setup check failed, nothing was taken from the streamer: {e}"
    return None


class Puller:
    def __init__(
        self,
        *,
        source_url: str,
        token: str,
        timeout: float,
        store: BatchStore,
        process: Callable[[List[Dict[str, Any]]], Counter],
        ask_fn: Optional[Callable[[str], str]] = None,
        ping_fn: Optional[Callable[..., PingResult]] = None,
    ):
        self.source_url = source_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.store = store
        self.process = process
        self.ask = ask_fn if ask_fn is not None else ask
        self.ping = ping_fn if ping_fn is not None else ping
        self.batches_done = 0
        self.leads_done = 0
        self.last_stats: Any = {}

    def _run_batch(self, path: Path, auto: bool = False) -> str:
        """Process one saved batch completely. Returns 'done', 'later', 'failed' (auto) or 'quit'."""
        while True:
            doc = self.store.load(path)
            leads = doc["leads"]
            if len(leads) > config.LEAD_BATCH_SIZE:
                say(f"WARNING: the streamer sent {len(leads)} leads, more than LEAD_BATCH_SIZE="
                    f"{config.LEAD_BATCH_SIZE}. Set BATCH_SIZE={config.LEAD_BATCH_SIZE} in the streamer's "
                    f".env. All of them are processed now ({config.LEAD_BATCH_SIZE} at a time), because "
                    "the streamer has already marked them as sent.")
            say(f"Processing {len(leads)} lead(s) from {path.name} - LIVE: qualified leads will be called")
            try:
                self.last_stats = self.process(leads) or {}
            except KeyboardInterrupt:
                print()
                say(f"Stopped by user. This run's campaigns were paused. The batch is kept in {path} "
                    "and offered again on the next start (numbers already called are skipped).")
                return "quit"
            except Exception as e:
                say(f"ERROR while processing the batch: {e}")
                say(f"The batch is kept in {path}.")
                if auto:
                    return "failed"
                while True:
                    answer = self.ask("Enter = try this batch again, l = leave it for later, q = quit: ")
                    if answer in ("q", "quit", "exit", "n", "no"):
                        return "quit"
                    if answer == "l":
                        return "later"
                    if answer == "":
                        break
                continue
            done = self.store.mark_done(path)
            self.batches_done += 1
            self.leads_done += len(leads)
            say(f"Batch fully processed ({len(leads)} lead(s)) -> {done.name}")
            return "done"

    def run(self, *, confirm_first: bool = False) -> int:
        # 1. Batches received earlier but not fully processed (crash, Ctrl+C, error)
        for path in self.store.unfinished():
            try:
                n = len(self.store.load(path)["leads"])
            except (OSError, ValueError) as e:
                say(f"WARNING: cannot read {path.name} ({e}); leaving it alone")
                continue
            while True:
                answer = self.ask(f"Unfinished batch from an earlier run: {path.name} ({n} lead(s)).\n"
                                  "Enter = process it now, l = leave it for later, q = quit: ")
                if answer in ("q", "quit", "exit", "n", "no"):
                    return self._bye(0)
                if answer in ("l", ""):
                    break
            if answer == "l":
                continue
            if self._run_batch(path) == "quit":
                return self._bye(130)

        # 2. Ping -> process -> wait for a person -> ping ...
        # When confirm_first=True (CLI startup), require explicit confirmation before the first ping too.
        first = not confirm_first
        while True:
            if not first:
                while True:
                    answer = self.ask(f"\nPress Enter to ping {self.source_url} for the next batch "
                                      f"(up to {config.LEAD_BATCH_SIZE} leads), or type q to quit: ")
                    if answer in ("q", "quit", "exit", "n", "no"):
                        return self._bye(0)
                    if answer == "":
                        break
            first = False

            say(f"Pinging {self.source_url}/leads/next ...")
            try:
                result = self.ping(self.source_url, self.token, self.timeout)
            except KeyboardInterrupt:
                print()
                say("Stopped while waiting for the streamer. If it had already handed out a batch, "
                    "those leads are marked as sent there (lead_streamer/README.md §5 shows how to re-send).")
                return self._bye(130)

            if result.kind == FATAL:
                say(f"STOP: {result.message}")
                return self._bye(2)
            if result.kind == EMPTY:
                say(f"{result.message}. Nothing to process.")
                continue
            if result.kind == RETRY:
                say(f"No batch: {result.message}")
                continue

            meta = result.meta
            mix = ""
            if meta.get("new") is not None or meta.get("updated") is not None:
                mix = f" (new {meta.get('new') or 0}, updated {meta.get('updated') or 0})"
            say(f"{result.message}{mix}")
            path = self.store.save(result, self.source_url)
            if self._run_batch(path) == "quit":
                return self._bye(130)

    def run_auto(
        self,
        check_seconds: float,
        *,
        ready_fn: Callable[[str], bool] = streamer_is_ready,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> int:
        """
        Run unattended until stopped: finish saved batches, wait for the real streamer,
        take one batch, call it completely, repeat. New leads are never taken while a
        saved batch is unfinished, because the streamer marks leads as sent on hand-out.
        """
        check_seconds = max(MIN_AUTO_CHECK_SECONDS, check_seconds)
        last_note = ""
        connected = False
        failures = 0
        unreadable = set()

        def note(message: str) -> None:
            nonlocal last_note
            if message != last_note:
                say(message)
                last_note = message

        say(f"Automatic mode: checking {self.source_url} every {check_seconds:.0f}s. Press Ctrl+C to stop.")
        try:
            while True:
                pending = [p for p in self.store.unfinished() if p not in unreadable]
                if pending:
                    path = pending[0]
                    try:
                        self.store.load(path)
                    except (OSError, ValueError) as e:
                        say(f"WARNING: cannot read {path.name} ({e}); leaving it alone")
                        unreadable.add(path)
                        continue
                    last_note = ""
                    self.last_stats = {}
                    outcome = self._run_batch(path, auto=True)
                    if outcome == "quit":
                        return self._bye(130)
                    if outcome == "failed" or self.last_stats.get("decision_error"):
                        failures += 1
                        wait = min(check_seconds * 2 ** (failures - 1), MAX_AUTO_BACKOFF_SECONDS)
                        say(f"Retrying unfinished leads in {wait:.0f}s. No new leads are taken until then.")
                        sleep_fn(wait)
                    else:
                        failures = 0
                    continue

                if not ready_fn(self.source_url):
                    connected = False
                    note("Waiting for the lead streamer. Processing starts as soon as it is connected.")
                    sleep_fn(check_seconds)
                    continue
                if not connected:
                    connected = True
                    last_note = ""
                    say("Lead streamer connected.")

                result = self.ping(self.source_url, self.token, self.timeout)
                if result.kind == FATAL:
                    say(f"STOP: {result.message}")
                    return self._bye(2)
                if result.kind == EMPTY:
                    note(f"{result.message}. Checking again every {check_seconds:.0f}s.")
                    sleep_fn(check_seconds)
                    continue
                if result.kind == RETRY:
                    note(f"No batch: {result.message}")
                    try:
                        retry_after = float(result.meta.get("retry_after"))
                    except (TypeError, ValueError):
                        retry_after = 0
                    sleep_fn(max(check_seconds, retry_after))
                    continue

                meta = result.meta
                mix = ""
                if meta.get("new") is not None or meta.get("updated") is not None:
                    mix = f" (new {meta.get('new') or 0}, updated {meta.get('updated') or 0})"
                say(f"{result.message}{mix}")
                self.store.save(result, self.source_url)
        except KeyboardInterrupt:
            print()
            say("Stopped by user. Any unfinished batch is kept and continues on the next start.")
            return self._bye(130)

    def _bye(self, code: int) -> int:
        say(f"Finished: {self.batches_done} batch(es), {self.leads_done} lead(s) processed this session.")
        return code


def _silence_output() -> None:
    """Send further output to nowhere (after SIGHUP the terminal no longer exists)."""
    try:
        devnull = open(os.devnull, "w")
    except OSError:
        return
    sys.stdout = devnull
    sys.stderr = devnull


def stop_signals_act_like_ctrl_c() -> List[int]:
    """
    SIGTERM (kill, systemctl stop) and SIGHUP (SSH connection dropped, terminal
    closed) normally end the process on the spot, leaving this run's Dograh
    campaigns dialing. Turn them into Ctrl+C instead, so campaigns are paused
    and the batch is kept for the next start. Returns the signals handled.
    """
    def _as_ctrl_c(signum: int, _frame: Any) -> None:
        if signum == getattr(signal, "SIGHUP", None):
            _silence_output()    # the terminal is gone: printing would fail and cut the cleanup short
        signal.raise_signal(signal.SIGINT)

    handled = []
    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, _as_ctrl_c)
                handled.append(sig)
            except (OSError, ValueError):   # not in the main thread / not supported here
                pass
    return handled


def check_setup(source_url: str) -> int:
    """`python pull_leads.py --check`: test keys, agents and the streamer without taking any leads."""
    ok = True
    problem = preflight()
    if problem:
        print(f"FAIL caller setup: {problem}")
        ok = False
    else:
        print(
            f"OK   caller setup: Gemini ({config.GEMINI_MODEL}, threshold {config.INTEREST_THRESHOLD:.0%}), "
            f"{config.SERVICES_FILE.name} and Dograh agents at {config.DOGRAH_BASE_URL}"
        )

    base = source_url.rstrip("/")
    try:
        r = requests.get(f"{base}/health", timeout=20, allow_redirects=False)
        ctype = r.headers.get("Content-Type", "").lower()
        data = r.json() if ctype.startswith("application/json") else None
    except (requests.RequestException, ValueError) as e:
        print(f"FAIL streamer: cannot reach {base}/health: {e}")
        return 1
    if r.status_code != 200 or not isinstance(data, dict) or data.get("status") != "alive":
        print(f"FAIL streamer: {base}/health answered HTTP {r.status_code} {data if data is not None else '(not JSON)'}"
              " - that is not the lead streamer (its /health says \"alive\"). Point LEAD_SOURCE_URL, or the "
              "subdomain's DNS/proxy, at the server where lead_streamer runs.")
        return 1
    print(f"OK   streamer: {base} is alive, database {data.get('database')}")
    if data.get("database") != "ok":
        ok = False

    # /stats needs the token but hands out nothing, so it proves the token without taking leads
    if config.LEAD_SOURCE_TOKEN:
        try:
            r = requests.get(f"{base}/stats", headers={"Authorization": f"Bearer {config.LEAD_SOURCE_TOKEN}"},
                             timeout=60, allow_redirects=False)
        except requests.RequestException as e:
            print(f"FAIL streamer token: {e}")
            return 1
        if r.status_code == 200:
            try:
                s = r.json()
            except ValueError:
                s = None
            if not isinstance(s, dict):
                print(f"FAIL streamer /stats: HTTP 200 returned non-JSON or unexpected body ({r.headers.get('Content-Type', '?')})")
                ok = False
            else:
                print(f"OK   streamer token accepted: {s.get('never_sent', '?')} lead(s) never sent, "
                      f"{s.get('updated_since_sent', '?')} updated, {s.get('eligible', '?')} with a phone in total")
        elif r.status_code == 401:
            print("FAIL streamer token: rejected (HTTP 401) - LEAD_SOURCE_TOKEN must equal the streamer's STREAM_TOKEN")
            ok = False
        elif r.status_code == 421:
            print(f"FAIL streamer host: HTTP 421 - LEAD_SOURCE_URL ({base}) must equal the streamer's PUBLIC_BASE_URL")
            ok = False
        else:
            print(f"FAIL streamer /stats: HTTP {r.status_code} {_error_detail(r)}")
            ok = False
    print("All checks passed. Nothing was taken from the streamer." if ok else "Some checks failed.")
    return 0 if ok else 1


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Wait for you to press Enter before each ping to the lead streamer reverse proxy, "
                    "then process one batch at a time.")
    parser.add_argument("--url", help=f"Streamer address (default LEAD_SOURCE_URL = {config.LEAD_SOURCE_URL})")
    parser.add_argument("--threshold", type=float, help=f"Call threshold 0-1 (default {config.INTEREST_THRESHOLD})")
    parser.add_argument("--output", help=f"Results .xlsx (default {config.RESULTS_FILE})")
    parser.add_argument("--check", action="store_true",
                        help="Only test keys, Dograh agents and the streamer (token included); take no leads")
    args = parser.parse_args(argv)

    active_threshold = config.INTEREST_THRESHOLD if args.threshold is None else args.threshold
    if not 0 < active_threshold <= 1:
        print(f"The threshold must be above 0 and at most 1 (0.80 = 80%), got {active_threshold}.")
        return 1

    if args.check:
        return check_setup(args.url or config.LEAD_SOURCE_URL)

    problem = preflight()
    if problem:
        print(problem)
        return 1

    from pipeline_tracker import PipelineTracker, TrackerLockedError
    try:
        PipelineTracker(config.PIPELINE_TRACKER_FILE).assert_unlocked()
    except TrackerLockedError as e:
        print(e)
        return 1

    stop_signals_act_like_ctrl_c()
    puller = Puller(
        source_url=args.url or config.LEAD_SOURCE_URL,
        token=config.LEAD_SOURCE_TOKEN,
        timeout=config.LEAD_SOURCE_TIMEOUT,
        store=BatchStore(config.PULLED_BATCHES_DIR),
        process=lambda leads: default_process(leads, args.threshold, args.output),
    )
    print(f"Lead source (reverse proxy): {puller.source_url} | batch size {config.LEAD_BATCH_SIZE} | "
          "idle until you press Enter to pull a batch", flush=True)
    return puller.run(confirm_first=True)


if __name__ == "__main__":
    sys.exit(main())
