"""
Campaign stage - one Dograh campaign per service per run.

For every service that has interested leads, a campaign named
"<service name>_<YYYY-MM-DD>" is created with that service's Dograh agent and
exactly those leads, then started. Its calls (including Dograh's automatic
retries) are followed until every lead has a final outcome.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set

import config
from decision_maker import Service
from dograh_client import (
    Agent, CallOutcome, DograhClient, DograhError, call_context, outcome_from_run, run_disposition,
)

RETRYABLE = {
    "retry_on_busy": {"busy"},
    "retry_on_no_answer": {"no-answer", "no_answer"},
    "retry_on_voicemail": {"voicemail", "machine"},
}
FINAL_CAMPAIGN_STATES = {"completed", "failed", "paused", "cancelled", "canceled", "stopped"}


@dataclass
class CampaignLead:
    ref: str                 # our id for the lead inside the campaign (CSV column lead_ref)
    phone: str               # E.164
    lead: Dict[str, Any]     # original lead fields
    payload: Any = None      # opaque object handed back in callbacks (the pipeline item)


def campaign_name(service: Service, when: Optional[datetime] = None) -> str:
    return f"{service.name}_{(when or datetime.now()).strftime(config.CAMPAIGN_NAME_DATE_FORMAT)}"


def _ts(value: Any) -> Optional[float]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


class CampaignJob:
    """One campaign: one service, one agent, a fixed list of leads."""

    def __init__(self, service: Service, agent: Agent, leads: List[CampaignLead],
                 name_date: Optional[datetime] = None):
        self.service = service
        self.agent = agent
        self.leads = leads
        self.name = campaign_name(service, name_date)
        self.campaign_id: Optional[int] = None
        # Leads left out because they lack a value the agent's prompt needs: (lead, reason)
        self.skipped: List[tuple] = []

    # ------------------------------------------------------------------ launch

    def launch(self, client: DograhClient) -> None:
        """Upload contacts, create and start the campaign (blocking; run in a thread)."""
        # Dograh refuses the WHOLE campaign if any row lacks a value for a
        # {{variable}} the agent uses, so leave out only the incomplete leads.
        required = client.get_required_variables(self.agent.id)
        rows, included = [], []
        for cl in self.leads:
            row = call_context(cl.lead, cl.phone, self.service.name)
            row["lead_ref"] = cl.ref
            missing = sorted(v for v in required if not str(row.get(v, "")).strip())
            if missing:
                self.skipped.append((cl, f"Missing value for {', '.join(missing)} "
                                         f"(used in agent '{self.agent.name}' prompt)"))
                continue
            rows.append(row)
            included.append(cl)
        self.leads = included
        if not rows:
            return
        file_key = client.upload_csv(rows, f"{self.name}.csv")
        campaign = client.create_campaign(self.name, self.agent.id, file_key)
        self.campaign_id = int(campaign["id"])
        client.start_campaign(self.campaign_id)

    # ------------------------------------------------------------------ monitor

    async def monitor(
        self,
        client: DograhClient,
        on_call_started: Callable[[CampaignLead, Dict[str, Any], int], None],
        on_result: Callable[[CampaignLead, CallOutcome], None],
        progress_clock: Optional[List[float]] = None,
        stop_event: Optional[Any] = None,
    ) -> None:
        """Follow the campaign until every lead has a final outcome."""
        assert self.campaign_id is not None
        by_ref = {cl.ref: cl for cl in self.leads}
        by_phone = {cl.phone: cl for cl in self.leads}
        announced: Set[Any] = set()
        finished: Set[str] = set()
        completed_at: Dict[Any, float] = {}
        last_progress = time.monotonic()
        if progress_clock is not None:
            progress_clock[0] = max(progress_clock[0], last_progress)
        errors = 0

        def touch_progress(ts: float) -> None:
            nonlocal last_progress
            if ts > last_progress:
                last_progress = ts
            if progress_clock is not None and ts > progress_clock[0]:
                progress_clock[0] = ts

        def finish(cl: CampaignLead, outcome: CallOutcome) -> None:
            finished.add(cl.ref)
            touch_progress(time.monotonic())
            on_result(cl, outcome)

        while True:
            if stop_event is not None and stop_event.is_set():
                return
            if stop_event is None:
                await asyncio.sleep(config.CAMPAIGN_POLL_SECONDS)
            else:
                deadline = time.monotonic() + float(config.CAMPAIGN_POLL_SECONDS)
                while True:
                    if stop_event.is_set():
                        return
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    await asyncio.sleep(min(0.2, remaining))
                if stop_event.is_set():
                    return
            try:
                details = await asyncio.to_thread(client.get_campaign, self.campaign_id)
                runs = await asyncio.to_thread(client.get_campaign_runs, self.campaign_id)
                errors = 0
            except DograhError as e:
                errors += 1
                if errors >= 10:
                    for cl in self.leads:
                        if cl.ref not in finished:
                            finish(cl, CallOutcome(status="call_failed", error=f"Lost contact with Dograh: {e}"))
                    return
                continue
            if stop_event is not None and stop_event.is_set():
                return

            # Group call attempts by lead
            attempts: Dict[str, List[Dict[str, Any]]] = {}
            for run in sorted(runs, key=lambda r: r.get("id") or 0):
                ctx = run.get("initial_context") or {}
                cl = by_ref.get(str(ctx.get("lead_ref") or "")) or by_phone.get(
                    str(ctx.get("phone_number") or ctx.get("called_number") or ""))
                if cl is None:
                    continue
                attempts.setdefault(cl.ref, []).append(run)
                if run.get("id") not in announced:
                    announced.add(run.get("id"))
                    touch_progress(time.monotonic())
                    on_call_started(cl, run, len(attempts[cl.ref]))

            now = time.monotonic()
            retry_cfg = details.get("retry_config") or {}
            for cl in self.leads:
                if cl.ref in finished or cl.ref not in attempts:
                    continue
                tries = attempts[cl.ref]
                latest = tries[-1]
                if not latest.get("is_completed"):
                    continue
                run_key = latest.get("id") if latest.get("id") is not None else (cl.ref, len(tries))
                if run_key not in completed_at:
                    completed_at[run_key] = now
                    touch_progress(now)
                # Dograh may write the outcome a moment after the call ends
                if not run_disposition(latest) and now - completed_at[run_key] < config.RESULT_WAIT_SECONDS:
                    continue
                if self._retry_pending(latest, len(tries), retry_cfg, completed_mono=completed_at[run_key]):
                    continue
                finish(cl, outcome_from_run(latest, attempts=len(tries)))

            if len(finished) == len(self.leads):
                return

            state = str(details.get("state") or "").lower()
            if state in FINAL_CAMPAIGN_STATES:
                for cl in self.leads:
                    if cl.ref in finished:
                        continue
                    tries = attempts.get(cl.ref) or []
                    if tries and not tries[-1].get("is_completed"):
                        continue  # still on the phone; wait for it
                    if tries:
                        finish(cl, outcome_from_run(tries[-1], attempts=len(tries)))
                    else:
                        finish(cl, CallOutcome(status="call_failed",
                                               error=f"Not dialed: campaign ended in state '{state}'"))
                if len(finished) == len(self.leads):
                    return

            idle_since = progress_clock[0] if progress_clock is not None else last_progress
            if time.monotonic() - idle_since > config.CAMPAIGN_IDLE_TIMEOUT_SECONDS:
                for cl in self.leads:
                    if cl.ref not in finished:
                        tries = attempts.get(cl.ref) or []
                        if tries:
                            outcome = outcome_from_run(tries[-1], attempts=len(tries))
                            outcome.status = "call_timeout"
                            outcome.error = (f"No progress in campaign for {config.CAMPAIGN_IDLE_TIMEOUT_SECONDS}s "
                                             f"(state '{state}')")
                        else:
                            outcome = CallOutcome(
                                status="call_failed",
                                error=(f"Not dialed: no progress in campaign for "
                                       f"{config.CAMPAIGN_IDLE_TIMEOUT_SECONDS}s (state '{state}')"),
                            )
                        finish(cl, outcome)
                return

    @staticmethod
    def _retry_pending(
        latest: Dict[str, Any],
        attempts: int,
        retry_cfg: Dict[str, Any],
        completed_mono: Optional[float] = None,
    ) -> bool:
        """True if Dograh is expected to call this lead again."""
        if not retry_cfg.get("enabled"):
            return False
        retryable = set()
        for flag, dispositions in RETRYABLE.items():
            if retry_cfg.get(flag):
                retryable |= dispositions
        if (run_disposition(latest) or "").lower() not in retryable:
            return False
        if attempts > int(retry_cfg.get("max_retries") or 0):
            return False
        window = int(retry_cfg.get("retry_delay_seconds") or 0) + config.RETRY_GRACE_SECONDS
        if completed_mono is not None:
            return time.monotonic() < completed_mono + window
        ref_ts = _ts(latest.get("updated_at")) or _ts(latest.get("created_at"))
        if ref_ts is None:
            return False
        return time.time() < ref_ts + window
