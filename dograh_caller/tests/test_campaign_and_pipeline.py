import asyncio
import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import config
from campaign_manager import CampaignJob, CampaignLead, _ts
from decision_maker import Decision, Service
from dograh_client import Agent, DograhClient
from pipeline import LeadPipeline


def test_ts_treats_naive_isoformat_as_utc():
    naive = _ts("2026-11-19T12:00:00")
    aware = _ts("2026-11-19T12:00:00Z")
    assert naive == aware == datetime(2026, 11, 19, 12, 0, 0, tzinfo=timezone.utc).timestamp()
    assert _ts(None) is None
    assert _ts("not-a-timestamp") is None


def test_retry_pending_uses_completed_mono_and_safe_fallback(monkeypatch):
    monkeypatch.setattr(config, "RETRY_GRACE_SECONDS", 10)
    cfg = {"enabled": True, "max_retries": 1, "retry_delay_seconds": 30, "retry_on_no_answer": True}
    latest = {"gathered_context": {"call_disposition": "no-answer"}, "is_completed": True}

    now_mono = time.monotonic()
    # Within retry window measured from completion monotonic time
    assert CampaignJob._retry_pending(latest, attempts=1, retry_cfg=cfg, completed_mono=now_mono - 5) is True
    # Past retry window
    assert CampaignJob._retry_pending(latest, attempts=1, retry_cfg=cfg, completed_mono=now_mono - 45) is False
    # Max retries exhausted
    assert CampaignJob._retry_pending(latest, attempts=2, retry_cfg=cfg, completed_mono=now_mono - 5) is False
    # Missing timestamps in standalone mode must not hang forever
    assert CampaignJob._retry_pending(latest, attempts=1, retry_cfg=cfg) is False


def test_monitor_shared_progress_clock_and_undialed_vs_dialed_timeout(monkeypatch):
    monkeypatch.setattr(config, "CAMPAIGN_POLL_SECONDS", 0.01)
    monkeypatch.setattr(config, "CAMPAIGN_IDLE_TIMEOUT_SECONDS", 0.08)
    # The never-ending call counts as stuck after this long (then the idle timeout applies)
    monkeypatch.setattr(config, "CAMPAIGN_MAX_CALL_SECONDS", 0.05)
    monkeypatch.setattr(config, "RESULT_WAIT_SECONDS", 0.0)

    svc = Service(id="svc", name="Svc", agent_id=1, description="d", good_fit="", not_a_fit="")
    agent = Agent(id=1, name="Agent", version_status="published")
    lead_dialed = CampaignLead(ref="L00001", phone="+15125550101", lead={"company": "Dialed Co"})
    lead_undialed = CampaignLead(ref="L00002", phone="+15125550102", lead={"company": "Undialed Co"})
    job = CampaignJob(svc, agent, [lead_dialed, lead_undialed])
    job.campaign_id = 10

    class FakeClient:
        def get_campaign(self, cid):
            return {"id": cid, "state": "running", "retry_config": {"enabled": False}}

        def get_campaign_runs(self, cid):
            # L00001 started a run that never completes; L00002 has never been dialed.
            return [
                {
                    "id": 99,
                    "is_completed": False,
                    "initial_context": {"lead_ref": "L00001", "phone_number": "+15125550101"},
                }
            ]

    shared_clock = [time.monotonic()]
    results = {}

    async def run():
        # Verify shared progress_clock keeps sibling monitor alive past single-campaign idle timeout
        async def bump_clock():
            for _ in range(4):
                await asyncio.sleep(0.03)
                shared_clock[0] = time.monotonic()

        bumper = asyncio.create_task(bump_clock())
        await job.monitor(
            FakeClient(),
            lambda cl, r, att: None,
            lambda cl, outcome: results.__setitem__(cl.ref, outcome),
            progress_clock=shared_clock,
        )
        await bumper

    asyncio.run(run())
    assert results["L00001"].status == "call_timeout"
    assert results["L00002"].status == "call_failed"
    assert "Not dialed" in (results["L00002"].error or "")


def test_monitor_exits_on_external_paused_or_cancelled_state(monkeypatch):
    monkeypatch.setattr(config, "CAMPAIGN_POLL_SECONDS", 0.01)
    svc = Service(id="svc", name="Svc", agent_id=1, description="d", good_fit="", not_a_fit="")
    agent = Agent(id=1, name="Agent", version_status="published")
    lead = CampaignLead(ref="L00001", phone="+15125550101", lead={"company": "Acme"})
    job = CampaignJob(svc, agent, [lead])
    job.campaign_id = 7

    class PausedClient:
        def get_campaign(self, cid):
            return {"id": cid, "state": "paused", "retry_config": {}}

        def get_campaign_runs(self, cid):
            return []

    results = {}
    asyncio.run(
        job.monitor(
            PausedClient(),
            lambda cl, r, att: None,
            lambda cl, outcome: results.__setitem__(cl.ref, outcome),
        )
    )
    assert results["L00001"].status == "call_failed"
    assert "paused" in (results["L00001"].error or "")


def test_dograh_client_uses_thread_local_sessions_and_closes_all():
    created = []

    class FakeSession:
        def __init__(self):
            self.headers = {}
            self.closed = False
            created.append(self)

        def request(self, *args, **kwargs):
            return SimpleNamespace(status_code=200, json=lambda: {"id": 1, "name": "A"})

        def close(self):
            self.closed = True

    import dograh_client

    orig_session_cls = dograh_client.requests.Session
    dograh_client.requests.Session = FakeSession
    try:
        client = DograhClient(base_url="https://example.test/api/v1", api_key="secret-key")
        sessions_seen = []

        def worker():
            sessions_seen.append(client._get_session())
            client.get_agent(1)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(created) == 4
        assert len({id(s) for s in sessions_seen}) == 4
        client.close()
        assert all(s.closed for s in created)
    finally:
        dograh_client.requests.Session = orig_session_cls


def test_pipeline_abort_drains_queued_results_and_stops_campaigns(tmp_path, monkeypatch):
    services_yaml = tmp_path / "services.yaml"
    services_yaml.write_text(
        "services:\n"
        "  - id: draftliner\n"
        "    name: Draftliner\n"
        "    agent_id: 2\n"
        "    description: AEC staffing\n",
        encoding="utf-8",
    )
    results_xlsx = tmp_path / "results.xlsx"
    stopped_campaigns = []

    class FakeDograh:
        def get_agent(self, agent_id):
            return Agent(id=agent_id, name="Danial", version_status="published")

        def stop_campaign(self, cid):
            stopped_campaigns.append(cid)
            return "paused"

        def close(self):
            pass

    class FakeDecider:
        def __init__(self, services, threshold, api_key, model):
            self.services = services

        async def decide(self, lead):
            return Decision(probabilities={"draftliner": 0.10}, service=None, probability=0.10)

        async def close(self):
            pass

    monkeypatch.setattr("pipeline.DograhClient", FakeDograh)
    monkeypatch.setattr("pipeline.DecisionMaker", FakeDecider)

    async def run():
        pipeline = LeadPipeline(
            results_file=results_xlsx,
            services_file=services_yaml,
            tracker_file=tmp_path / "tracker.xlsx",
        )
        await pipeline.start()
        pipeline._live_campaigns[42] = "Draftliner_2026-11-19"
        await pipeline.submit({"company": "Test Bistro", "category": "Restaurant", "phone": "(512) 555-0101"})
        # Wait until verifier + decider push the not_interested row into _results_q
        await pipeline._verify_q.join()
        await pipeline._decision_q.join()
        # Simulate Ctrl+C / abort right after scoring
        await pipeline.close(abort=True)

    asyncio.run(run())
    assert stopped_campaigns == [42]
    assert results_xlsx.exists()


def test_all_skipped_campaign_does_not_log_or_raise_failed_start(tmp_path, monkeypatch, capsys):
    services_yaml = tmp_path / "services.yaml"
    services_yaml.write_text(
        "services:\n"
        "  - id: draftliner\n"
        "    name: Draftliner\n"
        "    agent_id: 2\n"
        "    description: AEC staffing\n",
        encoding="utf-8",
    )
    results_xlsx = tmp_path / "results.xlsx"
    tracker_xlsx = tmp_path / "tracker.xlsx"
    monkeypatch.setattr(config, "TRANSFERS_FILE", tmp_path / "transfers.xlsx")

    class FakeDograh:
        def get_agent(self, agent_id):
            return Agent(id=agent_id, name="Danial", version_status="published")

        def get_required_variables(self, agent_id):
            return {"city"}  # lead lacks city -> every lead skipped

        def upload_csv(self, *args, **kwargs):
            raise AssertionError("upload_csv must not be called when all leads are skipped")

        def create_campaign(self, *args, **kwargs):
            raise AssertionError("create_campaign must not be called when all leads are skipped")

        def close(self):
            pass

    class FakeDecider:
        def __init__(self, services, threshold, api_key, model):
            self.services = services

        async def decide(self, lead):
            return Decision(
                probabilities={"draftliner": 0.95},
                service=self.services[0],
                probability=0.95,
                threshold=0.80,
                model="gemini-3.8-flash",
            )

        async def close(self):
            pass

    monkeypatch.setattr("pipeline.DograhClient", FakeDograh)
    monkeypatch.setattr("pipeline.DecisionMaker", FakeDecider)

    async def run():
        async with LeadPipeline(
            results_file=results_xlsx,
            services_file=services_yaml,
            tracker_file=tracker_xlsx,
        ) as p:
            await p.submit({"company": "No City Co", "phone": "(512) 555-0101"})
            return p.stats

    stats = asyncio.run(run())
    out = capsys.readouterr().out
    assert "FAILED to start" not in out
    assert stats["skipped_missing_data"] == 1
    assert stats["call_failed"] == 0


def test_stop_event_interrupts_pipeline_and_pauses_campaign(tmp_path, monkeypatch):
    services_yaml = tmp_path / "services.yaml"
    services_yaml.write_text(
        "services:\n"
        "  - id: draftliner\n"
        "    name: Draftliner\n"
        "    agent_id: 2\n"
        "    description: AEC staffing\n",
        encoding="utf-8",
    )
    results_xlsx = tmp_path / "results.xlsx"
    tracker_xlsx = tmp_path / "tracker.xlsx"
    monkeypatch.setattr(config, "TRANSFERS_FILE", tmp_path / "transfers.xlsx")
    monkeypatch.setattr(config, "CAMPAIGN_POLL_SECONDS", 0.02)

    stop_event = threading.Event()
    stopped_campaigns = []

    class FakeDograh:
        def get_agent(self, agent_id):
            return Agent(id=agent_id, name="Danial", version_status="published")

        def get_required_variables(self, agent_id):
            return set()

        def upload_csv(self, rows, file_name):
            return "file-key"

        def create_campaign(self, name, agent_id, file_key):
            return {"id": 88, "name": name}

        def start_campaign(self, cid):
            pass

        def get_campaign(self, cid):
            stop_event.set()
            return {"id": cid, "state": "running", "retry_config": {"enabled": False}}

        def get_campaign_runs(self, cid):
            return []

        def stop_campaign(self, cid):
            stopped_campaigns.append(cid)
            return "paused"

        def close(self):
            pass

    class FakeDecider:
        def __init__(self, services, threshold, api_key, model):
            self.services = services

        async def decide(self, lead):
            return Decision(
                probabilities={"draftliner": 0.91},
                service=self.services[0],
                probability=0.91,
                threshold=0.80,
                model="gemini-3.8-flash",
            )

        async def close(self):
            pass

    monkeypatch.setattr("pipeline.DograhClient", FakeDograh)
    monkeypatch.setattr("pipeline.DecisionMaker", FakeDecider)

    async def run():
        async with LeadPipeline(
            results_file=results_xlsx,
            services_file=services_yaml,
            tracker_file=tracker_xlsx,
            stop_event=stop_event,
        ) as p:
            await p.submit({"company": "Stop Me Co", "phone": "(512) 555-0102"})
            return p.stats

    stats = asyncio.run(run())
    assert 88 in stopped_campaigns
    assert stats["interrupted"] == 1

    from openpyxl import load_workbook
    wb = load_workbook(tracker_xlsx, read_only=True, data_only=True)
    try:
        rows = list(wb["leads"].iter_rows(values_only=True))
        header = rows[0]
        row = dict(zip(header, rows[1]))
        assert row["stage"] == "interrupted"
        assert row["status_detail"] == "Stopped from dashboard"
    finally:
        wb.close()


def test_close_drains_pending_results_when_join_interrupted(tmp_path, monkeypatch):
    services_yaml = tmp_path / "services.yaml"
    services_yaml.write_text(
        "services:\n"
        "  - id: draftliner\n"
        "    name: Draftliner\n"
        "    agent_id: 2\n"
        "    description: AEC staffing\n",
        encoding="utf-8",
    )
    results_xlsx = tmp_path / "results.xlsx"

    class FakeDecider:
        def __init__(self, services, threshold, api_key, model):
            self.services = services

        async def decide(self, lead):
            return Decision(probabilities={"draftliner": 0.1}, service=None, probability=0.1)

        async def close(self):
            pass

    monkeypatch.setattr("pipeline.DecisionMaker", FakeDecider)

    async def run():
        p = LeadPipeline(
            results_file=results_xlsx,
            services_file=services_yaml,
            score_only=True,
            tracker_file=tmp_path / "tracker.xlsx",
        )
        await p.start()
        await p._results_q.put((
            {"processed_at": "2026-11-23 12:00:00", "status": "not_interested", "phone_e164": "+15125550177"},
            {"company": "Drained Co"},
        ))

        async def interrupt_join():
            raise KeyboardInterrupt()

        monkeypatch.setattr(p._results_q, "join", interrupt_join)
        await p.close(abort=True)

    asyncio.run(run())
    from openpyxl import load_workbook
    wb = load_workbook(results_xlsx, read_only=True, data_only=True)
    try:
        rows = list(wb.active.iter_rows(values_only=True))
        assert any("Drained Co" in map(str, r) for r in rows)
    finally:
        wb.close()
