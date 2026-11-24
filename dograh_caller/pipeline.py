"""
The lead pipeline.

    submit(lead) ─► [Number verifier] ─► [Decision maker (Gemini)] ─► per-service list
                         │ dropped              │ not interested          │
                         ▼                      ▼                         ▼  (when input ends)
                    results .xlsx          results .xlsx      one Dograh campaign per service
                                                              "<service>_<YYYY-MM-DD>" with its
                                                              agent ─► call results ─► results .xlsx

Verification and Gemini scoring run concurrently while leads are being submitted.
Dograh campaigns can't receive numbers after they are created, so each
service's campaign is created once all leads of the run have been scored
(or whenever flush() is called), then followed until every call has a result.

Usage from other code (e.g. the AI-powered scraper):

    from pipeline import LeadPipeline

    async with LeadPipeline() as pipeline:
        async for lead in scraper():          # each lead is a plain dict
            await pipeline.submit(lead)
    # leaving the block creates the campaigns and waits for all calls

Queues live in memory only: if the process stops, leads still in flight are lost
(streamer batches stay on disk and are resumed; see pull_leads.BatchStore).
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterable, Callable, Dict, Iterable, List, Optional, Union

import config
from campaign_manager import CampaignJob, CampaignLead
from decision_maker import Decision, DecisionMaker, Service, load_services
from dograh_client import Agent, CallOutcome, DograhClient, is_not_reached, is_transferred, outcome_from_run
from number_verifier import NumberVerifier, Verification, load_called_numbers
from pipeline_tracker import PipelineTracker
from results_store import TRANSFER_COLUMNS, TRANSFERS_SHEET_NAME, ResultsStore, transfer_row

_STOP_TIMEOUT = 5
# Bookkeeping this program stores inside a lead saved for retry: {"retries": {"decision": 1, "call": 2}}.
# It is removed again when the lead is submitted, so Gemini, Dograh and the sheets never see it.
LEAD_META_KEY = "_dograh_caller"
_MAX_RECOVER_CAMPAIGNS = 50


def log(stage: str, message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {stage:<8} {message}", flush=True)


def _never_dialed(outcome: Optional[CallOutcome]) -> bool:
    """call_failed before any call was placed (campaign not created/started, or never dialed)."""
    if outcome is None:
        return True
    return outcome.run_id is None and str(outcome.error or "").startswith("Not dialed")


class _Item:
    """A lead travelling through the pipeline."""
    __slots__ = (
        "agent",
        "campaign_id",
        "campaign_name",
        "decision",
        "detail",
        "dialed",
        "lead",
        "lead_id",
        "outcome",
        "position",
        "retries",
        "service",
        "status",
        "verification",
    )

    def __init__(self, lead: Dict[str, Any], position: Optional[int] = None,
                 retries: Optional[Dict[str, int]] = None):
        self.lead = lead
        self.lead_id: str = ""
        self.verification: Optional[Verification] = None
        self.decision: Optional[Decision] = None
        self.service: Optional[Service] = None
        self.agent: Optional[Agent] = None
        self.outcome: Optional[CallOutcome] = None
        self.campaign_id: Optional[int] = None
        self.campaign_name: Optional[str] = None
        self.status = ""
        self.detail = ""
        self.dialed = False                       # Dograh started at least one call for it
        self.position = position                  # index in the submitted sequence (run_leads)
        self.retries: Dict[str, int] = dict(retries or {})

    @property
    def label(self) -> str:
        for key in ("company", "company_name", "business_name", "name", "title"):
            for k, v in self.lead.items():
                if str(k).strip().lower().replace(" ", "_") == key and v:
                    return str(v)[:40]
        return "(no company name)"


class LeadPipeline:
    def __init__(
        self,
        *,
        results_file: Optional[Union[str, Path]] = None,
        threshold: Optional[float] = None,
        services_file: Optional[Union[str, Path]] = None,
        score_only: bool = False,
        tracker_file: Optional[Union[str, Path]] = None,
        source_label: str = "python",
        batch_id: Optional[str] = None,
        stop_event: Optional[Any] = None,
        retry_failed_calls: Optional[bool] = None,
        on_lead_final: Optional[Callable[[int, str], None]] = None,
    ):
        self.score_only = bool(score_only)
        self.stop_event = stop_event
        # Re-queue leads whose call reached nobody (busy, no answer, failed, ...)
        self.retry_failed_calls = config.RETRY_FAILED_CALLS if retry_failed_calls is None else bool(retry_failed_calls)
        # Called with (position, status) when a submitted lead is finished for good
        # (not when it was interrupted or saved for a retry)
        self.on_lead_final = on_lead_final
        self.batch_id = str(batch_id or "").strip()
        if results_file:
            self.results_file = Path(results_file)
        elif self.score_only:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.results_file = config.RESULTS_FILE.parent / f"score_only_{stamp}.xlsx"
        else:
            self.results_file = config.RESULTS_FILE
        if self.score_only and self.results_file.resolve() == config.RESULTS_FILE.resolve():
            raise ValueError("Score-only mode must not write to call_results.xlsx")
        if tracker_file:
            self.tracker_file = Path(tracker_file)
        elif self.score_only:
            self.tracker_file = self.results_file.with_name(f"{self.results_file.stem}_tracker.xlsx")
        else:
            self.tracker_file = config.PIPELINE_TRACKER_FILE
        self.source_label = source_label
        self.threshold = config.INTEREST_THRESHOLD if threshold is None else threshold
        self.services_file = Path(services_file) if services_file else config.SERVICES_FILE
        self.stats: Counter = Counter()
        self.failed_decision_leads: List[Dict[str, Any]] = []   # Gemini failed: score again later
        self.retry_call_leads: List[Dict[str, Any]] = []        # call failed: call again later
        self.store: Optional[ResultsStore] = None
        self.decider: Optional[DecisionMaker] = None
        self._warned_monitor = False

        self._verify_q: asyncio.Queue = asyncio.Queue(maxsize=config.QUEUE_MAXSIZE)
        self._decision_q: asyncio.Queue = asyncio.Queue(maxsize=config.QUEUE_MAXSIZE)
        self._results_q: asyncio.Queue = asyncio.Queue()
        self._pending: Dict[str, List[_Item]] = {}     # service id -> interested leads awaiting a campaign
        self._flush_lock = asyncio.Lock()
        self._store_save_lock = threading.Lock()
        self._live_campaigns: Dict[int, str] = {}       # campaign id -> name, started and not yet stopped
        self._stage_tasks: List[asyncio.Task] = []
        self._writer_task: Optional[asyncio.Task] = None
        self._tracker_task: Optional[asyncio.Task] = None
        self._started = False
        self._warned_tracker = False
        self._results_dirty = False
        self.dograh: Optional[DograhClient] = None
        self.transfers: Optional[ResultsStore] = None
        self.agents: Dict[str, Agent] = {}
        self.tracker = PipelineTracker(self.tracker_file)

    # ------------------------------------------------------------------ lifecycle

    def _track(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if not self._warned_tracker:
                log("tracker", f"WARNING: tracker update failed ({e}); continuing without blocking calls")
                self._warned_tracker = True
            return None

    async def start(self) -> None:
        if self._started:
            return
        await asyncio.to_thread(self.tracker.open)
        try:
            self.services = load_services(self.services_file)
            self.decider = DecisionMaker(self.services, self.threshold, config.GEMINI_API_KEY, config.GEMINI_MODEL)
            self.store = await asyncio.to_thread(ResultsStore, self.results_file)
            # Only businesses whose call was transferred; the file is created on the first transfer
            self.transfers = (
                None if self.score_only
                else await asyncio.to_thread(
                    lambda: ResultsStore(config.TRANSFERS_FILE, fixed_columns=TRANSFER_COLUMNS,
                                         sheet_name=TRANSFERS_SHEET_NAME, time_column="transferred_at"))
            )
            self.dograh = None if self.score_only else DograhClient()
            self.agents = {}

            # Calls still going when an earlier run stopped: fetch their final result
            # first, so the already-called list below is complete.
            if not self.score_only:
                recovered = await asyncio.to_thread(self._recover_unfinished_calls)
                if recovered:
                    log("setup", f"Recovered the final result of {recovered} call(s) from earlier runs")

            if self.score_only or not config.SKIP_ALREADY_CALLED:
                already_called = set()
            else:
                # The main results file always counts, also when this run writes elsewhere (--output)
                result_files = [self.results_file]
                if self.results_file.resolve() != config.RESULTS_FILE.resolve():
                    result_files.append(config.RESULTS_FILE)
                already_called = set()
                for path in result_files:
                    already_called |= await asyncio.to_thread(load_called_numbers, path)
                already_called |= self.tracker.called_numbers()
            self.verifier = NumberVerifier(already_called=already_called)
            # Campaign names use the date the run started ("Draftliner_<start date>")
            self.run_started = datetime.now()

            if self.score_only:
                log("setup", f"Services: {', '.join(s.name for s in self.services)}")
                log("setup", "Mode: SCORE ONLY (no Dograh campaigns or calls will be created)")
            else:
                log("setup", f"Services: {', '.join(f'{s.name} (agent {s.agent_id})' for s in self.services)}")
            log("setup", f"Gemini model {config.GEMINI_MODEL} (thinking {config.GEMINI_THINKING_LEVEL}), "
                         f"threshold {self.threshold:.0%}")
            log("setup", f"Phone regions: {', '.join(sorted(self.verifier.allowed_regions))} | "
                         f"already called in earlier runs: {len(already_called)} number(s)")
            if not self.score_only and self.dograh is not None:
                for s in self.services:
                    agent = await asyncio.to_thread(self.dograh.get_agent, s.agent_id)
                    self.agents[s.id] = agent
                    note = ""
                    if agent.version_status and agent.version_status != "published":
                        note = " - WARNING: has unpublished changes; calls use the last PUBLISHED version"
                    log("setup", f"{s.name} -> agent {agent.id} '{agent.name}'{note}")
            log("setup", f"Results file: {self.results_file}")
            log("setup", f"Tracker file: {self.tracker_file}")
            print("-" * 100, flush=True)

            self._stage_tasks.append(asyncio.create_task(self._verifier_worker()))
            for _ in range(max(1, config.DECISION_CONCURRENCY)):
                self._stage_tasks.append(asyncio.create_task(self._decision_worker()))
            self._writer_task = asyncio.create_task(self._writer_worker())
            self._tracker_task = asyncio.create_task(self._tracker_worker())
            self._started = True
        except BaseException:
            try:
                self.tracker.close()
            except Exception:
                pass
            if self.dograh is not None:
                try:
                    self.dograh.close()
                except Exception:
                    pass
            if self.decider is not None:
                try:
                    await self.decider.close()
                except Exception:
                    pass
            raise

    def _should_stop(self) -> bool:
        return bool(self.stop_event is not None and self.stop_event.is_set())

    def _stop_reason(self) -> str:
        return "Stopped from dashboard" if self._should_stop() else "Run stopped before completion"

    # ------------------------------------------------------------------ recovery

    def _recover_unfinished_calls(self) -> int:
        """
        Tracker rows of earlier runs whose call was still going (or not yet seen)
        when that run stopped: look the call up in its Dograh campaign. A finished
        call gets its real result (tracker + a new results row); a call still
        going gets its run id recorded, so the number is never dialed twice.
        Best effort: errors are logged and ignored. Blocking (run in a thread).
        """
        if self.dograh is None or config.RECOVER_CALLS_DAYS <= 0:
            return 0
        get_runs = getattr(self.dograh, "get_campaign_runs", None)
        if not callable(get_runs):
            return 0
        try:
            rows = self.tracker.unfinished_call_rows(config.RECOVER_CALLS_DAYS)
        except Exception as e:
            log("setup", f"WARNING: could not look for unfinished calls ({e})")
            return 0
        by_campaign: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            by_campaign.setdefault(str(row.get("campaign_id")), []).append(row)

        recovered = 0
        for cid, campaign_rows in list(by_campaign.items())[:_MAX_RECOVER_CAMPAIGNS]:
            try:
                runs = get_runs(int(float(cid)))
            except Exception as e:
                log("setup", f"WARNING: could not check campaign {cid} for unfinished calls ({e})")
                continue
            attempts: Dict[str, List[Dict[str, Any]]] = {}
            for run in sorted(runs or [], key=lambda r: r.get("id") or 0):
                ref = str((run.get("initial_context") or {}).get("lead_ref") or "")
                if ref:
                    attempts.setdefault(ref, []).append(run)
            for row in campaign_rows:
                lead_id = str(row.get("lead_id") or "")
                tries = attempts.get(lead_id)
                if not tries:
                    continue
                last = tries[-1]
                if not last.get("is_completed"):
                    self.tracker.record_run_seen(lead_id, last.get("id"), len(tries))
                    continue
                outcome = outcome_from_run(last, attempts=len(tries))
                detail = "Result recovered after the run had stopped"
                self.tracker.record_finished(lead_id, outcome.status, outcome.error or detail, outcome)
                try:
                    self._append_result(self._recovered_row(row, outcome, outcome.error or detail),
                                        self._lead_from_json(row.get("lead_json")))
                except Exception as e:
                    log("results", f"ERROR writing recovered result for {lead_id}: {e}")
                recovered += 1
        return recovered

    @staticmethod
    def _lead_from_json(text: Any) -> Dict[str, Any]:
        try:
            lead = json.loads(str(text or ""))
        except ValueError:
            return {}
        return lead if isinstance(lead, dict) else {}

    @staticmethod
    def _recovered_row(trow: Dict[str, Any], o: CallOutcome, detail: str) -> Dict[str, Any]:
        def val(key: str) -> Any:
            v = trow.get(key)
            return None if v in (None, "") else v

        return {
            "processed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "pipeline_lead_id": val("lead_id"),
            "status": o.status,
            "detail": detail,
            "phone_raw": val("phone_raw"),
            "phone_e164": val("phone_e164"),
            "phone_location": val("phone_location"),
            "phone_timezone": val("phone_timezone"),
            "interested_service": val("assigned_service"),
            "interest_probability": val("best_probability"),
            "decision_verdict": val("decision_verdict"),
            "decision_summary": val("decision_summary"),
            "agent_id": val("agent_id"),
            "agent_name": val("agent_name"),
            "campaign_id": val("campaign_id"),
            "campaign_name": val("campaign_name"),
            "dograh_run_id": o.run_id,
            "call_attempts": o.attempts or None,
            "call_disposition": o.disposition,
            "call_duration_s": o.duration_seconds,
            "call_tags": ", ".join(map(str, o.tags)) if o.tags else None,
            "transcript_url": o.transcript_url,
            "recording_url": o.recording_url,
            "dograh_response": o.raw or None,
            "call_transferred": True if is_transferred(o.raw) else None,
        }

    async def _await_or_stop(self, awaitable: Any) -> bool:
        """Await `awaitable`, returning True if finished or False if stop_event fired."""
        if self.stop_event is None:
            await awaitable
            return True
        if self._should_stop():
            if asyncio.iscoroutine(awaitable):
                awaitable.close()
            return False
        task = asyncio.ensure_future(awaitable)
        try:
            while not task.done():
                if self._should_stop():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    return False
                done, _ = await asyncio.wait({task}, timeout=0.2)
                if done:
                    break
            await task
            return True
        except BaseException:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            raise

    def _append_result(self, row: Dict[str, Any], lead: Optional[Dict[str, Any]]) -> None:
        if self.store is not None:
            self.store.append(row, lead)
            self._results_dirty = True
        if self.transfers is not None and row.get("call_transferred"):
            t_row, rest = transfer_row(row, lead or {})
            self.transfers.append(t_row, rest)

    def _drain_results_queue(self) -> None:
        while True:
            try:
                row, lead = self._results_q.get_nowait()
            except asyncio.QueueEmpty:
                break
            try:
                self._append_result(row, lead)
            except Exception as e:
                log("results", f"ERROR writing row: {e}")
            finally:
                self._results_q.task_done()

    def _interrupt_queued_items(self, reason: str = "Stopped from dashboard") -> None:
        pending, self._pending = self._pending, {}
        for items in pending.values():
            for item in items:
                if not item.status:
                    self._finish_nowait(item, "interrupted", reason)
        for q in (self._verify_q, self._decision_q):
            while True:
                try:
                    item = q.get_nowait()
                except asyncio.QueueEmpty:
                    break
                try:
                    if not item.status:
                        self._finish_nowait(item, "interrupted", reason)
                finally:
                    q.task_done()

    async def submit(self, lead: Dict[str, Any], position: Optional[int] = None) -> None:
        """
        Hand one lead (a dict) to the pipeline. Waits if the pipeline is full.
        `position` (optional) is handed back to on_lead_final when the lead is done.
        """
        if not self._started:
            await self.start()
        if not isinstance(lead, dict):
            raise TypeError(f"Leads must be dicts, got {type(lead).__name__}")
        if self._should_stop():
            return
        self.stats["received"] += 1
        lead = dict(lead)
        meta = lead.pop(LEAD_META_KEY, None)
        retries = meta.get("retries") if isinstance(meta, dict) else None
        clean_retries = {}
        if isinstance(retries, dict):
            for kind, n in retries.items():
                try:
                    clean_retries[str(kind)] = int(n)
                except (TypeError, ValueError):
                    pass
        item = _Item(lead, position=position, retries=clean_retries)
        item.lead_id = self._track(self.tracker.register, item.lead, self.source_label, self.batch_id) or ""
        if not await self._await_or_stop(self._verify_q.put(item)):
            self._finish_nowait(item, "interrupted", "Stopped from dashboard")

    async def flush(self) -> None:
        """
        Finish scoring every lead submitted so far, then create one campaign per
        service that has interested leads and wait until all their calls are done.
        close() calls this automatically; long-running callers (a scraper that
        never stops) can call it periodically to dial what has been collected.
        """
        async with self._flush_lock:
            if not await self._await_or_stop(self._verify_q.join()):
                return
            if not await self._await_or_stop(self._decision_q.join()):
                return
            if self.score_only:
                self._pending.clear()
                await self._await_or_stop(self._results_q.join())
                return
            pending, self._pending = self._pending, {}
            if not pending:
                return
            await self._run_campaigns(pending)
            await self._await_or_stop(self._results_q.join())

    async def close(self, abort: bool = False) -> None:
        """
        Process every submitted lead (including all calls), then shut down.
        With abort=True (Ctrl+C or an error) nothing more is processed, though
        already-queued results are flushed to disk before exiting.
        Either way, every campaign this run started is stopped before exiting.
        """
        if not self._started:
            return
        stopped_by_event = self._should_stop()
        if stopped_by_event:
            abort = True
        try:
            if not abort:
                await self.flush()
        finally:
            try:
                for t in self._stage_tasks:
                    t.cancel()
                if self._stage_tasks:
                    await asyncio.gather(*self._stage_tasks, return_exceptions=True)
                self._stage_tasks.clear()

                if abort:
                    # Leads still queued or waiting for a campaign never finished: say so in the results
                    self._interrupt_queued_items(
                        "Stopped from dashboard" if stopped_by_event else "Run stopped before completion"
                    )

                if self._writer_task is not None and not self._writer_task.done():
                    try:
                        await self._results_q.join()
                    except BaseException:
                        self._drain_results_queue()
                    self._writer_task.cancel()
                    await asyncio.gather(self._writer_task, return_exceptions=True)
                if not self._results_q.empty():
                    self._drain_results_queue()
                self._writer_task = None
                if self._tracker_task is not None:
                    self._tracker_task.cancel()
                    await asyncio.gather(self._tracker_task, return_exceptions=True)
                self._tracker_task = None
            finally:
                self._stop_live_campaigns()
                stores = [s for s in (self.store, self.transfers) if s is not None]
                for store in stores:
                    try:
                        await self._final_save(store)
                    except BaseException:
                        with self._store_save_lock:
                            store.save()
                try:
                    self.tracker.mark_unfinished_interrupted(
                        detail="Stopped from dashboard" if stopped_by_event else ""
                    )
                    await asyncio.to_thread(self.tracker.save)
                except Exception as e:
                    log("tracker", f"WARNING: final tracker save failed ({e})")
                finally:
                    try:
                        self.tracker.close()
                    except Exception:
                        pass
                if self.decider is not None:
                    await self.decider.close()
                if self.dograh is not None:
                    self.dograh.close()
                self._started = False
                self._print_summary()

    async def __aenter__(self) -> "LeadPipeline":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close(abort=(exc_type is not None or self._should_stop()))

    def _stop_live_campaigns(self) -> None:
        """Pause every campaign of this run that is still running (blocking; used at exit)."""
        if self.dograh is None:
            return
        for cid, name in list(self._live_campaigns.items()):
            try:
                state = self.dograh.stop_campaign(cid)
                log("campaign", f"'{name}' (campaign {cid}) {'paused' if state == 'paused' else 'is ' + state}")
                del self._live_campaigns[cid]
            except Exception as e:
                log("campaign", f"WARNING: could not stop '{name}' (campaign {cid}) - pause it in Dograh: {e}")

    # ------------------------------------------------------------------ stages

    async def _verifier_worker(self) -> None:
        while True:
            item: _Item = await self._verify_q.get()
            try:
                item.verification = self.verifier.verify(item.lead)
                self._track(self.tracker.record_verification, item.lead_id, item.verification)
                if item.verification.ok:
                    await self._decision_q.put(item)
                else:
                    raw = item.verification.raw or "-"
                    log("verify", f"DROP {raw} ({item.verification.reason}) - {item.label}")
                    await self._finish(item, item.verification.reason)
            except asyncio.CancelledError:
                # Cancelled while working on this lead (Stop, Ctrl+C, error): record it
                if not item.status:
                    self._finish_nowait(item, "interrupted", self._stop_reason())
                raise
            except Exception as e:  # never let one bad lead kill the stage
                await self._finish(item, "error", f"verifier: {e}")
            finally:
                self._verify_q.task_done()

    async def _decision_worker(self) -> None:
        while True:
            item: _Item = await self._decision_q.get()
            try:
                item.decision = await self.decider.decide(item.lead)
                self._track(self.tracker.record_decision, item.lead_id, item.decision)
                probs = ", ".join(f"{k} {v:.0%}" for k, v in item.decision.probabilities.items()) or "no business details"
                phone = item.verification.e164
                if not item.decision.qualified:
                    log("decide", f"NON-QUALIFIED {phone} {item.label} ({probs})")
                    await self._finish(item, "not_interested")
                    continue

                item.service = item.decision.service
                if self.score_only:
                    log("decide", f"QUALIFIED {phone} {item.label} -> {item.service.name} ({probs}) [score-only]")
                    await self._finish(item, "scored_qualified")
                    continue
                item.agent = self.agents[item.service.id]
                self._pending.setdefault(item.service.id, []).append(item)
                log("decide", f"QUALIFIED {phone} {item.label} -> {item.service.name} ({probs})")
            except asyncio.CancelledError:
                if not item.status and item not in self._pending.get(getattr(item.service, "id", ""), []):
                    self._finish_nowait(item, "interrupted", self._stop_reason())
                raise
            except Exception as e:
                log("decide", f"ERROR {item.label}: {e}")
                self._track(self.tracker.record_decision_error, item.lead_id, str(e))
                await self._finish(item, "decision_error", str(e))
            finally:
                self._decision_q.task_done()

    async def _run_campaigns(self, pending: Dict[str, List[_Item]]) -> None:
        """Create, start and follow one campaign per service."""
        if self.score_only:
            raise RuntimeError("Campaigns are disabled in score-only mode")
        print("-" * 100, flush=True)
        jobs: List[CampaignJob] = []
        for idx_svc, service in enumerate(self.services):
            items = pending.get(service.id) or []
            if not items:
                continue
            if self._should_stop():
                for rem_service in self.services[idx_svc:]:
                    for item in pending.get(rem_service.id) or []:
                        if not item.status:
                            self._finish_nowait(item, "interrupted", "Stopped from dashboard")
                break
            leads = []
            for idx, item in enumerate(items, start=1):
                # The fallback prefix can never collide with a tracker ID (L000001, ...)
                ref = item.lead_id or f"untracked-{service.id}-{idx}"
                leads.append(CampaignLead(ref=ref, phone=item.verification.e164,
                                          lead=item.lead, payload=item,
                                          time_zone=item.verification.time_zone or ""))
            job = CampaignJob(service, self.agents[service.id], leads, name_date=self.run_started)
            try:
                await asyncio.to_thread(job.launch, self.dograh)
                error = None
            except Exception as e:
                error = e
            for note in job.warnings:
                log("campaign", f"NOTE '{job.name}': {note}")
            if job.campaign_id is not None:
                # Track it even if starting failed, so it is never left running on exit
                self._live_campaigns[job.campaign_id] = job.name

            # Leads left out for lacking a value the agent's prompt needs
            skipped_ids = set()
            for cl, reason in job.skipped:
                skipped_ids.add(id(cl.payload))
                log("campaign", f"SKIP {cl.phone} {cl.payload.label} - {reason}")
                await self._finish(cl.payload, "skipped_missing_data", reason)

            if error is not None:
                log("campaign", f"FAILED to start '{job.name}': {error}")
                for item in items:
                    if id(item) not in skipped_ids:
                        item.campaign_name = job.name
                        await self._finish(item, "call_failed", f"Campaign could not be started: {error}")
                continue

            if not job.leads:
                continue

            for cl in job.leads:
                cl.payload.campaign_id, cl.payload.campaign_name = job.campaign_id, job.name
                self._track(self.tracker.record_campaign, cl.payload.lead_id, job.agent, job.campaign_id, job.name)
            log("campaign", f"Started '{job.name}' (campaign {job.campaign_id}) with {len(job.leads)} "
                            f"number(s) via agent '{job.agent.name}'")
            jobs.append(job)

        if not jobs:
            return

        progress_clock = [time.monotonic()]
        finished_refs: set[str] = set()

        def on_started(cl: CampaignLead, run: Dict[str, Any], attempt: int) -> None:
            item: _Item = cl.payload
            item.dialed = True
            self._track(self.tracker.record_call_started, item.lead_id, run.get("id"), attempt)
            retry = f" (retry #{attempt - 1})" if attempt > 1 else ""
            log("call", f"-> Calling {cl.phone}{retry} ({item.label}) [{item.campaign_name}, run {run.get('id')}]")

        def on_result(cl: CampaignLead, outcome: CallOutcome) -> None:
            finished_refs.add(cl.ref)
            item: _Item = cl.payload
            item.outcome = outcome
            if outcome.status == "called":
                dur = f"{outcome.duration_seconds:.0f}s" if outcome.duration_seconds is not None else "-"
                tries = f" | attempts: {outcome.attempts}" if outcome.attempts > 1 else ""
                moved = " | TRANSFERRED" if is_transferred(outcome.raw) else ""
                log("call", f"<- {cl.phone} Result: {outcome.disposition or 'unknown'} | {dur} | "
                            f"tags: {', '.join(map(str, outcome.tags)) or '-'}{tries}{moved}")
            else:
                log("call", f"<- {cl.phone} {outcome.status.upper()}: {outcome.error}")
            self._finish_nowait(item, outcome.status, outcome.error or "")

        def interrupt_unfinished(job: CampaignJob, reason: str) -> None:
            for cl in job.leads:
                if cl.ref not in finished_refs and not cl.payload.status:
                    self._finish_nowait(cl.payload, "interrupted", reason)

        async def pause(job: CampaignJob, what: str) -> None:
            try:
                state = await asyncio.to_thread(self.dograh.stop_campaign, job.campaign_id)
                self._live_campaigns.pop(job.campaign_id, None)
                log("campaign", f"'{job.name}' (campaign {job.campaign_id}) {what} ({state})")
            except Exception as e:
                log("campaign", f"'{job.name}' (campaign {job.campaign_id}) {what} - "
                                f"could not pause it now, will retry on exit: {e}")

        async def follow(job: CampaignJob) -> None:
            try:
                await job.monitor(
                    self.dograh,
                    on_started,
                    on_result,
                    progress_clock=progress_clock,
                    stop_event=self.stop_event,
                )
            except Exception as e:
                # One campaign's monitor failing must not leave the others unwatched
                log("campaign", f"ERROR following '{job.name}' (campaign {job.campaign_id}): {e}")
                await pause(job, "monitoring failed - paused")
                interrupt_unfinished(job, f"Monitoring the campaign failed: {e}")
                return
            if self._should_stop():
                await pause(job, "stopped")
                # Last look after the pause: a call Dograh started at that moment is
                # recorded (never dialed again), and calls that already ended get their result.
                try:
                    if config.STOP_SYNC_SECONDS > 0:
                        await asyncio.sleep(config.STOP_SYNC_SECONDS)
                    await job.final_sync(self.dograh, on_started, on_result)
                except Exception as e:
                    log("campaign", f"'{job.name}': could not check its last calls after the stop ({e})")
                interrupt_unfinished(job, "Stopped from dashboard")
                return
            # Every lead has its final result: make sure Dograh isn't left "running"
            try:
                state = await asyncio.to_thread(self.dograh.stop_campaign, job.campaign_id)
                self._live_campaigns.pop(job.campaign_id, None)
                log("campaign", f"'{job.name}' (campaign {job.campaign_id}) finished - "
                                f"{'paused' if state == 'paused' else state}")
            except Exception as e:
                log("campaign", f"'{job.name}' (campaign {job.campaign_id}) finished - "
                                f"could not pause it now, will retry on exit: {e}")

        results = await asyncio.gather(*(follow(job) for job in jobs), return_exceptions=True)
        for job, res in zip(jobs, results):
            if isinstance(res, BaseException) and not isinstance(res, asyncio.CancelledError):
                log("campaign", f"ERROR in '{job.name}': {res}")
                interrupt_unfinished(job, f"Monitoring the campaign failed: {res}")
        if any(isinstance(r, asyncio.CancelledError) for r in results):
            raise asyncio.CancelledError()

    async def _writer_worker(self) -> None:
        """
        Adds result rows as they arrive and rewrites the results files at most
        every RESULTS_SAVE_SECONDS (each save rewrites the whole workbook, so not
        after every single row). close() always does a final save.
        """
        warned_locked = False
        warned_transfers_locked = False
        interval = float(config.RESULTS_SAVE_SECONDS)
        last_save = time.monotonic()
        while True:
            batch: List[Any] = []
            if self._results_dirty:
                wait = max(0.05, interval - (time.monotonic() - last_save))
                try:
                    batch.append(await asyncio.wait_for(self._results_q.get(), timeout=wait))
                except asyncio.TimeoutError:
                    pass
            else:
                batch.append(await self._results_q.get())
            while True:
                try:
                    batch.append(self._results_q.get_nowait())
                except asyncio.QueueEmpty:
                    break
            try:
                for row, lead in batch:
                    try:
                        self._append_result(row, lead)
                    except Exception as e:
                        log("results", f"ERROR writing row: {e}")
            finally:
                for _ in batch:
                    self._results_q.task_done()
            if not self._results_dirty or time.monotonic() - last_save < interval:
                continue
            try:
                ok = True
                if self.transfers is not None:
                    if not await asyncio.to_thread(self._save_store_locked, self.transfers):
                        ok = False
                        if not warned_transfers_locked:
                            log("results", f"WARNING: {self.transfers.path.name} is open in another program; "
                                           "will keep retrying - close it to let transfers save")
                            warned_transfers_locked = True
                    else:
                        warned_transfers_locked = False
                if not await asyncio.to_thread(self._save_store_locked, self.store):
                    ok = False
                    if not warned_locked:
                        log("results", f"WARNING: {self.store.path.name} is open in another program; "
                                       "will keep retrying - close it to let results save")
                        warned_locked = True
                else:
                    warned_locked = False
                last_save = time.monotonic()
                if ok:
                    self._results_dirty = False
            except Exception as e:
                last_save = time.monotonic()
                log("results", f"ERROR saving results: {e}")

    async def _tracker_worker(self) -> None:
        while True:
            interval = max(float(config.TRACKER_SAVE_SECONDS), 3.0 * self.tracker.last_save_duration, 0.2)
            await asyncio.sleep(interval)
            try:
                await asyncio.to_thread(self.tracker.save)
            except Exception as e:
                if not self._warned_tracker:
                    log("tracker", f"WARNING: periodic tracker save failed ({e})")
                    self._warned_tracker = True

    # ------------------------------------------------------------------ helpers

    def _finish_nowait(self, item: _Item, status: str, detail: str = "") -> None:
        item.status, item.detail = status, detail
        self.stats[status.split(" ")[0]] += 1
        if item.outcome is not None and is_transferred(item.outcome.raw):
            self.stats["transferred"] += 1
        if status in ("called", "call_timeout") and item.verification and item.verification.e164:
            self.verifier.mark_called(item.verification.e164)
        self._track(self.tracker.record_finished, item.lead_id, status, detail, item.outcome)
        self._results_q.put_nowait((self._row(item), item.lead))
        saved = self._save_for_retry(item, status)
        if (self.on_lead_final is not None and item.position is not None
                and status != "interrupted" and not saved):
            try:
                self.on_lead_final(item.position, status)
            except Exception as e:
                log("batch", f"WARNING: could not record batch progress ({e})")

    def _save_for_retry(self, item: _Item, status: str) -> bool:
        """
        Keep a lead to be processed again later:
          decision_error                         -> scored again (failed_decision_leads)
          call_failed, never dialed              -> called again (retry_call_leads), always
          call_failed, dialed but reached nobody -> called again, only with retry_failed_calls
        Each kind is retried at most RETRY_MAX_ATTEMPTS times. Returns True if saved.
        """
        if self.score_only and status != "decision_error":
            return False
        if status == "decision_error":
            kind, target = "decision", self.failed_decision_leads
        elif status == "call_failed":
            o = item.outcome
            if not item.dialed and _never_dialed(o):
                pass
            elif (self.retry_failed_calls and o is not None and o.run_id is not None
                  and is_not_reached(o.disposition)):
                pass
            else:
                return False
            kind, target = "call", self.retry_call_leads
        else:
            return False
        attempt = int(item.retries.get(kind, 0)) + 1
        if attempt > config.RETRY_MAX_ATTEMPTS:
            self.stats[f"retry_given_up_{kind}"] += 1
            log("retry", f"NOT saved again: {item.label} - {status} after {attempt - 1} retr"
                         f"{'y' if attempt == 2 else 'ies'} (RETRY_MAX_ATTEMPTS={config.RETRY_MAX_ATTEMPTS})")
            return False
        target.append({**item.lead, LEAD_META_KEY: {"retries": {**item.retries, kind: attempt}}})
        self.stats[f"retry_saved_{kind}"] += 1
        return True

    async def _finish(self, item: _Item, status: str, detail: str = "") -> None:
        self._finish_nowait(item, status, detail)

    def _row(self, item: _Item) -> Dict[str, Any]:
        """This program's columns for one lead (the lead's own fields are added by the results store)."""
        v, d, o = item.verification, item.decision, item.outcome
        row: Dict[str, Any] = {
            "processed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "pipeline_lead_id": item.lead_id or None,
            "status": item.status,
            "detail": item.detail,
            "phone_raw": v.raw if v else None,
            "phone_e164": v.e164 if v else None,
            "phone_location": (v.location or None) if v else None,
            "phone_timezone": (v.time_zone or None) if v else None,
        }
        if d:
            row["interested_service"] = item.service.name if item.service else None
            row["interest_probability"] = round(d.probability, 4) if d.probabilities else None
            row["decision_verdict"] = d.verdict or None
            row["decision_summary"] = d.summary or None
            for sid, p in d.probabilities.items():
                row[f"prob_{sid}"] = round(p, 4)
            for s in d.scores:
                row[f"decision_reason_{s.service_id}"] = s.reason
        if item.agent:
            row["agent_id"] = item.agent.id
            row["agent_name"] = item.agent.name
        if item.campaign_name:
            row["campaign_id"] = item.campaign_id
            row["campaign_name"] = item.campaign_name
        if o:
            row.update({
                "dograh_run_id": o.run_id,
                "call_attempts": o.attempts or None,
                "call_disposition": o.disposition,
                "call_duration_s": o.duration_seconds,
                "call_tags": ", ".join(map(str, o.tags)) if o.tags else None,
                "transcript_url": o.transcript_url,
                "recording_url": o.recording_url,
                "dograh_response": o.raw or None,
                "call_transferred": True if is_transferred(o.raw) else None,
            })
        return row

    def _save_store_locked(self, store: ResultsStore) -> bool:
        with self._store_save_lock:
            return store.save()

    async def _final_save(self, store: ResultsStore) -> None:
        for attempt in range(12):
            if await asyncio.to_thread(self._save_store_locked, store):
                return
            if attempt == 0:
                log("results", f"{store.path.name} is open in another program - close it to save "
                               "(retrying for 60s)")
            await asyncio.sleep(_STOP_TIMEOUT)
        fallback = store.path.with_name(
            f"{store.path.stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx")
        store.path = fallback
        await asyncio.to_thread(self._save_store_locked, store)
        log("results", f"Saved to {fallback} instead")

    def _print_summary(self) -> None:
        print("-" * 100, flush=True)
        s = self.stats
        dropped = sum(n for k, n in s.items() if k.startswith("dropped"))
        if self.score_only:
            print(f"Done (score-only). Leads received: {s['received']} | dropped by verifier: {dropped} | "
                  f"qualified: {s['scored_qualified']} | non-qualified: {s['not_interested']} | "
                  f"errors: {s['decision_error'] + s['error']}")
        else:
            print(f"Done. Leads received: {s['received']} | dropped by verifier: {dropped} | "
                  f"non-qualified: {s['not_interested']} | called: {s['called']} | "
                  f"call failed/timeout: {s['call_failed'] + s['call_timeout']} | "
                  f"skipped (missing data): {s['skipped_missing_data']} | "
                  f"errors: {s['decision_error'] + s['error']}")
        saved_d, saved_c = s["retry_saved_decision"], s["retry_saved_call"]
        if saved_d or saved_c:
            print(f"Saved for a later retry: {saved_d} lead(s) to score again, {saved_c} lead(s) to call again",
                  flush=True)
        given_up = s["retry_given_up_decision"] + s["retry_given_up_call"]
        if given_up:
            print(f"Not retried again (RETRY_MAX_ATTEMPTS={config.RETRY_MAX_ATTEMPTS} reached): {given_up} lead(s)",
                  flush=True)
        if self.store is not None:
            print(f"Results: {self.store.path}", flush=True)
        if self.transfers is not None and s["transferred"]:
            print(f"Transferred calls: {s['transferred']} -> {self.transfers.path}", flush=True)


async def run_leads(
    leads: Union[Iterable[Dict[str, Any]], AsyncIterable[Dict[str, Any]]],
    batch_size: Optional[int] = None,
    failed_leads: Optional[List[Dict[str, Any]]] = None,
    stop_event: Optional[Any] = None,
    retry_leads: Optional[List[Dict[str, Any]]] = None,
    **pipeline_kwargs: Any,
) -> Counter:
    """
    Run leads (list, generator or async generator of dicts) through the pipeline
    in batches of `batch_size` (default LEAD_BATCH_SIZE = 300): each batch is
    verified, scored, put into campaigns and fully called before the next one.

    failed_leads receives the leads Gemini could not score, retry_leads the
    leads whose call should be tried again - also when the run ends with an
    error or Ctrl+C. pipeline_kwargs go to LeadPipeline (e.g. on_lead_final,
    which is called with the lead's position in `leads`).
    """
    size = max(1, batch_size or config.LEAD_BATCH_SIZE)
    total = len(leads) if hasattr(leads, "__len__") else None  # type: ignore[arg-type]
    pipeline = LeadPipeline(stop_event=stop_event, **pipeline_kwargs)
    aborted = False
    await pipeline.start()
    try:
        count = 0

        async def add(lead: Dict[str, Any]) -> bool:
            nonlocal count, aborted
            if pipeline._should_stop():
                aborted = True
                return False
            if count % size == 0:
                last = count + size if total is None else min(count + size, total)
                log("batch", f"Batch {count // size + 1}: leads {count + 1}-{last}"
                             + (f" of {total}" if total is not None else ""))
            await pipeline.submit(lead, position=count)
            count += 1
            if pipeline._should_stop():
                aborted = True
                return False
            if count % size == 0:
                await pipeline.flush()      # call this batch completely before taking more
                if pipeline._should_stop():
                    aborted = True
                    return False
            return True

        if hasattr(leads, "__aiter__"):
            async for lead in leads:  # type: ignore[union-attr]
                if not await add(lead):
                    break
        else:
            for lead in leads:  # type: ignore[union-attr]
                if not await add(lead):
                    break
    except BaseException:
        aborted = True
        raise
    finally:
        try:
            await pipeline.close(abort=(aborted or pipeline._should_stop()))
        finally:
            # Handed back even when the run ended with an error or Ctrl+C
            if failed_leads is not None:
                failed_leads.extend(pipeline.failed_decision_leads)
            if retry_leads is not None:
                retry_leads.extend(pipeline.retry_call_leads)
    return pipeline.stats


def run_leads_sync(leads: Iterable[Dict[str, Any]], batch_size: Optional[int] = None,
                   failed_leads: Optional[List[Dict[str, Any]]] = None,
                   stop_event: Optional[Any] = None,
                   retry_leads: Optional[List[Dict[str, Any]]] = None,
                   **pipeline_kwargs: Any) -> Counter:
    """Blocking version of run_leads() for plain (non-async) scripts."""
    return asyncio.run(
        run_leads(leads, batch_size=batch_size, failed_leads=failed_leads, stop_event=stop_event,
                  retry_leads=retry_leads, **pipeline_kwargs)
    )
