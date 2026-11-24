"""Tests for the review fixes (numbers refer to the review's findings)."""

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from openpyxl import load_workbook

import config
import dograh_client
import pull_leads
from campaign_manager import CampaignJob, CampaignLead
from decision_maker import Decision, Service, lead_state
from dograh_client import (
    Agent, CallOutcome, DograhError, call_context, outcome_from_run, used_template_variables,
    variables_in_campaign_error,
)
from number_verifier import NumberVerifier
from pipeline import LEAD_META_KEY, LeadPipeline, _Item, run_leads
from pipeline_tracker import PipelineTracker
from pull_leads import BATCH, EMPTY, BatchStore, PingResult

ROOT = Path(__file__).resolve().parent.parent
SVC = Service(id="draftliner", name="Draftliner", agent_id=2, description="d", good_fit="", not_a_fit="")
AGENT = Agent(id=2, name="Danial", version_status="published")


def _services_yaml(tmp_path):
    p = tmp_path / "services.yaml"
    p.write_text("services:\n  - id: draftliner\n    name: Draftliner\n    agent_id: 2\n    description: AEC\n",
                 encoding="utf-8")
    return p


def _wait_until(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


# ------------------------------------------------------------------ 22 / 8: configuration

@pytest.mark.parametrize("raw,expected", [
    ("off", False), ("OFF", False), ("n", False), ("No", False), ("0", False), ("f", False),
    ("on", True), ("Yes", True), ("y", True), ("1", True), ("TRUE", True),
])
def test_env_bool_understands_common_words(monkeypatch, raw, expected):
    monkeypatch.setenv("X_TEST_BOOL", raw)
    assert config._env_bool("X_TEST_BOOL", not expected) is expected


def test_env_bool_unknown_word_keeps_default_with_warning(monkeypatch, capsys):
    monkeypatch.setenv("X_TEST_BOOL", "maybe")
    assert config._env_bool("X_TEST_BOOL", True) is True
    assert "not true/false" in capsys.readouterr().err


def test_relative_paths_are_relative_to_the_project_not_the_cwd():
    assert config.project_path("output/x.xlsx") == config.PROJECT_DIR / "output" / "x.xlsx"
    absolute = Path(os.path.abspath(os.sep)) / "abs" / "y.xlsx"
    assert config.project_path(absolute) == absolute


def _run_config_copy(tmp_path, *, local_env, parent_env, cwd):
    proj = tmp_path / "parent" / "proj"
    proj.mkdir(parents=True)
    (proj / "config.py").write_text((ROOT / "config.py").read_text(encoding="utf-8"), encoding="utf-8")
    if local_env is not None:
        (proj / ".env").write_text(local_env, encoding="utf-8")
    if parent_env is not None:
        (proj.parent / ".env").write_text(parent_env, encoding="utf-8")
    env = {k: v for k, v in os.environ.items()
           if k not in ("DOGRAH_API_KEY", "DOGRAH_BASE_URL", "RESULTS_FILE", "GEMINI_API_KEY", "GEMINI_API")}
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import config, json; "
            "print(json.dumps([config.DOGRAH_API_KEY, config.DOGRAH_BASE_URL, str(config.RESULTS_FILE)]))")
    out = subprocess.run([sys.executable, "-c", code, str(proj)], cwd=cwd, env=env,
                         capture_output=True, text=True, check=True)
    return proj, json.loads(out.stdout.strip().splitlines()[-1])


def test_parent_env_is_never_mixed_with_the_projects_env(tmp_path):
    proj, (key, url, results) = _run_config_copy(
        tmp_path,
        local_env="DOGRAH_API_KEY=local-key\nRESULTS_FILE=output/r.xlsx\n",
        parent_env="DOGRAH_API_KEY=parent-key\nDOGRAH_BASE_URL=https://other.example/api/v1\n",
        cwd=tmp_path,                       # started from another folder
    )
    assert key == "local-key"
    assert url == "https://dograh.grobots.ai/api/v1", "the parent's URL must not be mixed in"
    assert Path(results) == proj / "output" / "r.xlsx", "relative path resolved against the project"


def test_parent_env_is_used_when_the_project_has_none(tmp_path):
    _proj, (key, url, _results) = _run_config_copy(
        tmp_path, local_env=None,
        parent_env="DOGRAH_API_KEY=parent-key\nDOGRAH_BASE_URL=https://other.example/api/v1\n", cwd=tmp_path)
    assert (key, url) == ("parent-key", "https://other.example/api/v1")


# ------------------------------------------------------------------ 4: not-reached calls

@pytest.mark.parametrize("disposition", ["busy", "no-answer", "failed", "voicemail"])
def test_unanswered_calls_are_call_failed_not_called(disposition):
    run = {"id": 5, "is_completed": True, "gathered_context": {"call_disposition": disposition}}
    o = outcome_from_run(run, attempts=2)
    assert o.status == "call_failed"
    assert o.run_id == 5 and o.disposition == disposition and o.attempts == 2
    assert disposition in (o.error or "")


def test_answered_and_transferred_calls_stay_called():
    assert outcome_from_run({"id": 1, "gathered_context": {"call_disposition": "user_qualified"}}).status == "called"
    transferred = {"id": 2, "gathered_context": {"call_disposition": "failed", "external_pbx_transferred": True}}
    assert outcome_from_run(transferred).status == "called"


def test_not_reached_numbers_are_not_blocked_but_unknown_ones_are(tmp_path):
    t = PipelineTracker(tmp_path / "t.xlsx")
    t.open()
    try:
        v = NumberVerifier()
        cases = {
            "+15125550101": ("call_failed", "no-answer", 11),    # nobody reached -> may call again
            "+15125550102": ("called", "user_hangup", 12),       # answered -> blocked
            "+15125550103": ("call_failed", None, 13),           # dialed, outcome unknown -> blocked
            "+15125550104": ("call_failed", None, None),         # never dialed -> not blocked
        }
        for phone, (status, disp, run_id) in cases.items():
            lid = t.register({"company": phone, "phone": phone}, "test")
            t.record_verification(lid, v.verify({"phone": phone}))
            if run_id:
                t.record_call_started(lid, run_id, 1)
            outcome = CallOutcome(status=status, run_id=run_id, disposition=disp) if run_id else None
            t.record_finished(lid, status, "", outcome)
        assert t.called_numbers() == {"+15125550102", "+15125550103"}
    finally:
        t.close()


def _item(status_outcome=None, dialed=False, retries=None):
    it = _Item({"company": "Retry Co", "phone": "(512) 555-0109"}, position=0, retries=retries)
    it.outcome = status_outcome
    it.dialed = dialed
    return it


def test_retry_rules_for_failed_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RETRY_MAX_ATTEMPTS", 2)
    p = LeadPipeline(results_file=tmp_path / "r.xlsx", tracker_file=tmp_path / "t.xlsx")

    # Never dialed: always saved
    assert p._save_for_retry(_item(None), "call_failed") is True
    # Dialed, nobody reached: only with the option
    not_reached = CallOutcome(status="call_failed", run_id=9, disposition="busy")
    assert p._save_for_retry(_item(not_reached, dialed=True), "call_failed") is False
    p.retry_failed_calls = True
    assert p._save_for_retry(_item(not_reached, dialed=True), "call_failed") is True
    # Dialed, outcome unknown (lost contact): never - it may have been answered
    lost = CallOutcome(status="call_failed", error="Lost contact with Dograh: x")
    assert p._save_for_retry(_item(lost, dialed=True), "call_failed") is False
    # Answered calls are never retried
    assert p._save_for_retry(_item(CallOutcome(status="called", run_id=3)), "called") is False

    assert len(p.retry_call_leads) == 2
    assert p.retry_call_leads[0][LEAD_META_KEY] == {"retries": {"call": 1}}
    # 11: capped
    assert p._save_for_retry(_item(None, retries={"call": 2}), "call_failed") is False
    assert p.stats["retry_given_up_call"] == 1


# ------------------------------------------------------------------ 5 / 17: CSV for the agent

def test_current_datetime_is_in_the_leads_time_zone():
    from zoneinfo import ZoneInfo
    ctx = call_context({"company": "A"}, "+15125550101", "Draftliner", time_zone="America/Los_Angeles")
    assert ctx["lead_timezone"] == "America/Los_Angeles"
    expected = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%A, %B %d, %Y")
    assert ctx["current_date"] == expected
    assert ctx["current_datetime"].startswith(expected)
    assert ctx["current_datetime"].endswith(("PST", "PDT"))
    multi = call_context({}, "+1", "S", time_zone="America/Chicago, America/Denver")
    assert multi["lead_timezone"] == "America/Chicago"
    assert "lead_timezone" not in call_context({}, "+1", "S", time_zone="")


def test_only_used_fields_are_sent_and_collisions_are_reported():
    lead = {"Company Name": "First", "company_name": "Second", "description": "x" * 5000,
            "email": "a@b.c", "city": "Austin", "phone_number": "spoofed"}
    notes = []
    ctx = call_context(lead, "+15125550101", "Draftliner", fields={"company_name", "city"}, collisions=notes)
    assert ctx["company_name"] == "First"
    assert ctx["city"] == "Austin"
    assert "description" not in ctx and "email" not in ctx
    assert ctx["phone_number"] == "+15125550101"
    assert len(notes) == 1 and "Company Name" in notes[0] and "company_name" in notes[0]
    # fields=None keeps the old send-everything behaviour
    assert "description" in call_context(lead, "+1", "S")


def test_used_template_variables_scans_the_whole_workflow():
    wf = {
        "nodes": [{"data": {"prompt": "Hi {{company}} in {{city | your city}} {{current_time_America/Chicago}}",
                            "tools": [{"url": "https://x/{{initial_context.lead_id}}"}]}}],
        "edges": [{"data": {"transition_speech": "{{gathered_context.name}} {{campaign_id}}"}}],
    }
    assert used_template_variables(wf) == {"company", "city", "lead_id", "gathered_context"}


def test_launch_sends_only_agent_variables_plus_extras(monkeypatch):
    monkeypatch.setattr(config, "DOGRAH_SEND_ALL_FIELDS", False)
    monkeypatch.setattr(config, "DOGRAH_EXTRA_FIELDS", ["company"])
    uploaded = {}

    class Client:
        def get_agent_variables(self, agent_id):
            return {"city"}, {"city", "owner"}

        def upload_csv(self, rows, name):
            uploaded["rows"] = rows
            return "k"

        def create_campaign(self, name, agent_id, key):
            return {"id": 4}

        def start_campaign(self, cid):
            pass

    lead = {"company": "Co", "city": "Austin", "owner": "Pat", "notes": "long", "email": "e@x.y"}
    job = CampaignJob(SVC, AGENT, [CampaignLead("L1", "+15125550101", lead, time_zone="America/Chicago")])
    job.launch(Client())
    row = uploaded["rows"][0]
    assert {"company", "city", "owner", "lead_ref", "phone_number"} <= set(row)
    assert "notes" not in row and "email" not in row
    assert row["lead_timezone"] == "America/Chicago"


# ------------------------------------------------------------------ 18: Dograh's own variable check

def test_campaign_error_variables_are_parsed():
    assert variables_in_campaign_error(
        "Workflow uses template variables that are missing from the source data: 'city', 'Zip'. "
        "Add the missing columns or remove them from the workflow.") == {"city", "zip"}
    assert variables_in_campaign_error(
        "Template variable 'owner' is empty in rows: 2, 3. All template variables ...") == {"owner"}
    assert variables_in_campaign_error("Telephony is not configured") == set()


def test_launch_retries_without_the_leads_dograh_names():
    created = []

    class Client:
        def get_required_variables(self, agent_id):
            return set()

        def upload_csv(self, rows, name):
            created.append([r["lead_ref"] for r in rows])
            return "k"

        def create_campaign(self, name, agent_id, key):
            if len(created) == 1:
                raise DograhError("Creating campaign failed (HTTP 400): Template variable 'owner' is empty in rows: 3.")
            return {"id": 77}

        def start_campaign(self, cid):
            pass

    leads = [CampaignLead("L1", "+15125550101", {"owner": "Pat"}),
             CampaignLead("L2", "+15125550102", {})]
    job = CampaignJob(SVC, AGENT, leads)
    job.launch(Client())
    assert created == [["L1", "L2"], ["L1"]]
    assert job.campaign_id == 77
    assert [cl.ref for cl in job.leads] == ["L1"]
    assert [cl.ref for cl, _ in job.skipped] == ["L2"] and "owner" in job.skipped[0][1]
    assert any("owner" in w for w in job.warnings)


def test_launch_does_not_retry_other_dograh_errors():
    class Client:
        def get_required_variables(self, agent_id):
            return set()

        def upload_csv(self, rows, name):
            return "k"

        def create_campaign(self, *a):
            raise DograhError("Creating campaign failed (HTTP 400): telephony_configuration_not_found")

    with pytest.raises(DograhError, match="telephony"):
        CampaignJob(SVC, AGENT, [CampaignLead("L1", "+15125550101", {})]).launch(Client())


# ------------------------------------------------------------------ 3: long calls

def test_long_call_in_progress_is_not_timed_out(monkeypatch):
    monkeypatch.setattr(config, "CAMPAIGN_POLL_SECONDS", 0.01)
    monkeypatch.setattr(config, "CAMPAIGN_IDLE_TIMEOUT_SECONDS", 0.08)
    monkeypatch.setattr(config, "CAMPAIGN_MAX_CALL_SECONDS", 60)
    monkeypatch.setattr(config, "RESULT_WAIT_SECONDS", 0.0)
    started = time.monotonic()

    class Client:
        def get_campaign(self, cid):
            return {"state": "running", "retry_config": {"enabled": False}}

        def get_campaign_runs(self, cid):
            done = time.monotonic() - started > 0.4          # a "long" (5x the idle timeout) call
            run = {"id": 1, "is_completed": done, "initial_context": {"lead_ref": "L1"}}
            if done:
                run["gathered_context"] = {"call_disposition": "transfer_call"}
            return [run]

    job = CampaignJob(SVC, AGENT, [CampaignLead("L1", "+15125550101", {})])
    job.campaign_id = 1
    results = {}
    asyncio.run(job.monitor(Client(), lambda *a: None, lambda cl, o: results.__setitem__(cl.ref, o)))
    assert results["L1"].status == "called"
    assert results["L1"].disposition == "transfer_call"


# ------------------------------------------------------------------ 19: after a Stop

def test_final_sync_records_late_calls_and_finished_results():
    class Client:
        def get_campaign_runs(self, cid):
            return [
                {"id": 1, "is_completed": True, "initial_context": {"lead_ref": "L1"},
                 "gathered_context": {"call_disposition": "user_hangup"}},
                {"id": 2, "is_completed": False, "initial_context": {"lead_ref": "L2"}},
            ]

    job = CampaignJob(SVC, AGENT, [CampaignLead("L1", "+1", {}), CampaignLead("L2", "+2", {}),
                                   CampaignLead("L3", "+3", {})])
    job.campaign_id = 9
    started, finished = [], {}
    done = asyncio.run(job.final_sync(Client(), lambda cl, run, n: started.append(cl.ref),
                                      lambda cl, o: finished.__setitem__(cl.ref, o)))
    assert done == {"L1"}
    assert finished["L1"].status == "called"
    assert started == ["L1", "L2"], "the call Dograh started at the last moment is recorded"


def _pipeline_fakes(monkeypatch, runs_by_campaign=None):
    class FakeDograh:
        def get_agent(self, agent_id):
            return AGENT

        def get_campaign_runs(self, cid):
            return (runs_by_campaign or {}).get(cid, [])

        def stop_campaign(self, cid):
            return "paused"

        def close(self):
            pass

    class FakeDecider:
        def __init__(self, services, *a):
            self.services = services

        async def decide(self, lead):
            return Decision(probabilities={"draftliner": 0.1}, service=None, probability=0.1)

        async def close(self):
            pass

    monkeypatch.setattr("pipeline.DograhClient", FakeDograh)
    monkeypatch.setattr("pipeline.DecisionMaker", FakeDecider)


def test_results_of_interrupted_calls_are_recovered_at_the_next_start(tmp_path, monkeypatch):
    tracker_path = tmp_path / "tracker.xlsx"
    t = PipelineTracker(tracker_path)
    t.open()
    v = NumberVerifier()
    lid = t.register({"company": "Recovered Co", "phone": "(512) 555-0111"}, "streamer", batch_id="job_a")
    t.record_verification(lid, v.verify({"phone": "(512) 555-0111"}))
    t.record_campaign(lid, AGENT, 31, "Draftliner_2026-11-24")
    t.record_finished(lid, "interrupted", "Stopped from dashboard")
    lid2 = t.register({"company": "Still Calling Co", "phone": "(512) 555-0112"}, "streamer", batch_id="job_a")
    t.record_verification(lid2, v.verify({"phone": "(512) 555-0112"}))
    t.record_campaign(lid2, AGENT, 31, "Draftliner_2026-11-24")
    t.record_finished(lid2, "interrupted", "Stopped from dashboard")
    t.save()
    t.close()

    runs = {31: [
        {"id": 500, "is_completed": True, "initial_context": {"lead_ref": lid},
         "gathered_context": {"call_disposition": "user_qualified"}},
        {"id": 501, "is_completed": False, "initial_context": {"lead_ref": lid2}},
    ]}
    _pipeline_fakes(monkeypatch, runs)
    results = tmp_path / "results.xlsx"

    async def go():
        p = LeadPipeline(results_file=results, services_file=_services_yaml(tmp_path), tracker_file=tracker_path)
        await p.start()
        blocked = set(p.verifier.already_called)
        await p.close()
        return blocked

    blocked = asyncio.run(go())
    assert {"+15125550111", "+15125550112"} <= blocked, "both numbers are known to have been dialed"
    wb = load_workbook(results, read_only=True)
    try:
        rows = list(wb.active.iter_rows(values_only=True))
        header = rows[0]
        data = [dict(zip(header, r)) for r in rows[1:] if r[header.index("status")]]
        assert [(r["status"], r["dograh_run_id"]) for r in data] == [("called", 500)]
    finally:
        wb.close()
    t2 = PipelineTracker(tracker_path)
    t2.open()
    try:
        row = next(r for r in t2._rows if r["lead_id"] == lid)
        assert row["stage"] == "call_finished" and row["call_status"] == "called"
        row2 = next(r for r in t2._rows if r["lead_id"] == lid2)
        assert str(row2["dograh_run_id"]) == "501"
    finally:
        t2.close()


# ------------------------------------------------------------------ 7 / 20: crashes

def test_failed_leads_are_handed_back_even_when_the_run_raises(tmp_path, monkeypatch):
    _pipeline_fakes(monkeypatch)

    class Boom:
        async def decide(self, lead):
            raise RuntimeError("Gemini down")

        async def close(self):
            pass

    monkeypatch.setattr("pipeline.DecisionMaker", lambda *a, **k: Boom())

    async def leads():
        yield {"company": "Fail Co", "phone": "(512) 555-0121"}
        await asyncio.sleep(0.3)        # let the decision fail before the error below
        raise ValueError("scraper crashed")

    failed = []
    with pytest.raises(ValueError):
        asyncio.run(run_leads(leads(), failed_leads=failed, results_file=tmp_path / "r.xlsx",
                              services_file=_services_yaml(tmp_path), tracker_file=tmp_path / "t.xlsx"))
    assert [f["company"] for f in failed] == ["Fail Co"]
    assert failed[0][LEAD_META_KEY] == {"retries": {"decision": 1}}


def test_main_run_writes_retry_files_after_an_error(tmp_path, monkeypatch):
    import main as caller_main
    monkeypatch.setattr(config, "PROJECT_DIR", tmp_path)

    def fake_run(leads, *, failed_leads, retry_leads, **kw):
        failed_leads.append({"company": "Decision Co"})
        retry_leads.append({"company": "Call Co"})
        raise RuntimeError("Dograh unreachable")

    monkeypatch.setattr(caller_main, "run_leads_sync", fake_run)
    assert caller_main.run([{"company": "X", "phone": "1"}]) == 1
    assert len(list((tmp_path / "output").glob("decision_retry_*.json"))) == 1
    assert len(list((tmp_path / "output").glob("call_retry_*.json"))) == 1


def test_abort_marks_queued_leads_interrupted(tmp_path, monkeypatch):
    _pipeline_fakes(monkeypatch)
    gate = asyncio.Event()

    class SlowDecider:
        async def decide(self, lead):
            await gate.wait()

        async def close(self):
            pass

    monkeypatch.setattr("pipeline.DecisionMaker", lambda *a, **k: SlowDecider())
    monkeypatch.setattr(config, "DECISION_CONCURRENCY", 1)

    async def go():
        p = LeadPipeline(results_file=tmp_path / "r.xlsx", services_file=_services_yaml(tmp_path),
                         tracker_file=tmp_path / "t.xlsx")
        await p.start()
        for i in range(3):
            await p.submit({"company": f"Q{i}", "phone": f"(512) 555-013{i}"})
        await asyncio.sleep(0.2)          # one lead in decide(), two queued
        await p.close(abort=True)          # e.g. an error - not a dashboard Stop
        return p.stats

    stats = asyncio.run(go())
    assert stats["interrupted"] == 3


def test_a_failing_campaign_monitor_does_not_take_down_the_others(tmp_path, monkeypatch):
    p = LeadPipeline(results_file=tmp_path / "r.xlsx", tracker_file=tmp_path / "t.xlsx")
    stopped = []

    class FakeDograh:
        def stop_campaign(self, cid):
            stopped.append(cid)
            return "paused"

    async def go():
        await p.start() if False else None
        p.dograh = FakeDograh()
        p.services = [SVC, Service(id="paradise", name="Paradise", agent_id=3, description="d",
                                   good_fit="", not_a_fit="")]
        p.agents = {"draftliner": AGENT, "paradise": Agent(id=3, name="Olivia", version_status="published")}
        p.verifier = NumberVerifier()
        p.run_started = datetime.now()
        items = {}
        for sid, phone in (("draftliner", "+15125550141"), ("paradise", "+15125550142")):
            it = _Item({"company": sid})
            it.verification = SimpleNamespace(e164=phone, time_zone="", raw=phone, location="")
            items[sid] = [it]

        async def bad_monitor(self, *a, **k):
            if self.service.id == "draftliner":
                raise RuntimeError("unexpected answer")
            for cl in self.leads:
                a[2](cl, CallOutcome(status="called", run_id=1, disposition="user_hangup"))

        def launch(self, client):
            self.campaign_id = 100 if self.service.id == "draftliner" else 200

        monkeypatch.setattr(CampaignJob, "monitor", bad_monitor)
        monkeypatch.setattr(CampaignJob, "launch", launch)
        await p._run_campaigns(items)
        return items

    items = asyncio.run(go())
    assert items["draftliner"][0].status == "interrupted"
    assert "Monitoring the campaign failed" in items["draftliner"][0].detail
    assert items["paradise"][0].status == "called"
    assert set(stopped) == {100, 200}


# ------------------------------------------------------------------ 16: --output

def test_main_results_file_counts_when_writing_elsewhere(tmp_path, monkeypatch):
    from results_store import ResultsStore
    main_results = tmp_path / "call_results.xlsx"
    monkeypatch.setattr(config, "RESULTS_FILE", main_results)
    s = ResultsStore(main_results)
    s.append({"processed_at": "2026-11-20 10:00:00", "status": "called", "phone_e164": "+15125550151"})
    s.save()
    _pipeline_fakes(monkeypatch)

    async def go():
        p = LeadPipeline(results_file=tmp_path / "other.xlsx", services_file=_services_yaml(tmp_path),
                         tracker_file=tmp_path / "t.xlsx")
        await p.start()
        blocked = set(p.verifier.already_called)
        await p.close()
        return blocked

    assert "+15125550151" in asyncio.run(go())


# ------------------------------------------------------------------ 15: saving less often

def test_results_are_saved_throttled_and_always_at_close(tmp_path, monkeypatch):
    from results_store import ResultsStore
    monkeypatch.setattr(config, "RESULTS_SAVE_SECONDS", 3600)
    saves = []
    orig = ResultsStore.save

    def counting_save(self):
        saves.append(self.path.name)
        return orig(self)

    monkeypatch.setattr(ResultsStore, "save", counting_save)
    _pipeline_fakes(monkeypatch)

    async def go():
        p = LeadPipeline(results_file=tmp_path / "r.xlsx", services_file=_services_yaml(tmp_path),
                         tracker_file=tmp_path / "t.xlsx")
        await p.start()
        for i in range(5):
            await p.submit({"company": f"S{i}", "phone": f"(512) 555-016{i}"})
        await p.flush()
        during = list(saves)
        await p.close()
        return during

    during = asyncio.run(go())
    assert during == [], "no rewrite of the workbook per result"
    assert saves.count("r.xlsx") == 1
    wb = load_workbook(tmp_path / "r.xlsx", read_only=True)
    try:
        assert sum(1 for r in wb.active.iter_rows(values_only=True) if r[2] == "not_interested") == 5
    finally:
        wb.close()


def test_old_tracker_rows_are_archived_and_stay_blocked(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TRACKER_ARCHIVE_DAYS", 30)
    path = tmp_path / "tracker.xlsx"
    t = PipelineTracker(path)
    t.open()
    v = NumberVerifier()
    old_id = t.register({"company": "Old Co", "phone": "(512) 555-0171"}, "test")
    t.record_verification(old_id, v.verify({"phone": "(512) 555-0171"}))
    t.record_call_started(old_id, 42, 1)
    t.record_finished(old_id, "called", "", CallOutcome(status="called", run_id=42, disposition="user_hangup"))
    new_id = t.register({"company": "New Co", "phone": "(512) 555-0172"}, "test")
    t.record_finished(new_id, "not_interested")
    t._by_lead_id[old_id]["updated_at"] = (datetime.now() - timedelta(days=45)).strftime("%Y-%m-%d %H:%M:%S")
    t.save()
    t.close()

    t2 = PipelineTracker(path)
    t2.open()
    try:
        assert [r["lead_id"] for r in t2._rows] == [new_id]
        assert "+15125550171" in t2.called_numbers()
        assert t2.register({"company": "Next", "phone": "1"}, "test") == "L000003", "lead ids are not reused"
    finally:
        t2.close()
    archived = load_workbook(t2.archive_path, read_only=True)
    try:
        ids = [r[0] for r in list(archived.active.iter_rows(values_only=True))[1:]]
        assert ids == [old_id]
    finally:
        archived.close()


# ------------------------------------------------------------------ 9 / 10 / 11: saved batches

def test_runner_resumes_saved_batch_in_chunks_and_skips_finished_leads(tmp_path):
    from dashboard.data import TrackerDataStore
    from dashboard.runner import BatchRunner

    batches = tmp_path / "pulled"
    store = BatchStore(batches)
    saved = store.save(PingResult(BATCH, "x", [{"lead_id": f"P{i}"} for i in range(7)]), "earlier")
    store.add_progress(saved, [0, 1])          # finished before the earlier run was stopped
    seen = []

    def fake_process(leads, *, progress_path=None, lead_indexes=None, **kw):
        seen.append(([l["lead_id"] for l in leads], list(lead_indexes)))
        # This job's failures are saved for later - they must not be picked up by this job
        pull_leads.save_failed_for_retry([{"lead_id": "R"}], batches_dir=batches)
        return Counter(received=len(leads), retry_saved_decision=1)

    ds = TrackerDataStore(tmp_path / "missing.xlsx", start_watcher=False)
    runner = BatchRunner(store=ds, source_url="http://fake", token="t", batches_dir=batches,
                         ping_fn=lambda *a, **k: PingResult(EMPTY, "No new Leads available"),
                         process_fn=fake_process, preflight_fn=lambda: None)
    assert runner.get_status()["pending_leads"] == 5
    runner.start(batch_size=2, num_batches=10)
    assert _wait_until(lambda: not runner.get_status()["active"])
    st = runner.get_status()
    assert st["state"] == "completed", st
    assert seen == [(["P2", "P3"], [2, 3]), (["P4", "P5"], [4, 5]), (["P6"], [6])]
    assert not saved.exists() and saved.with_name(saved.name.replace(".pending", ".done")).exists()
    assert not BatchStore.progress_path(saved).exists()
    assert len(store.unfinished()) == 3, "this job's retry batches wait for the next job"
    assert st["pending_leads"] == 3
    ds.close()


def test_progress_is_recorded_per_lead_so_a_stop_resumes_where_it_stopped(tmp_path, monkeypatch):
    batches = tmp_path / "pulled"
    saved = BatchStore(batches).save(PingResult(BATCH, "x", [{"a": 1}, {"a": 2}, {"a": 3}]), "earlier")

    def fake_run_leads_sync(leads, *, on_lead_final=None, failed_leads=None, retry_leads=None, **kw):
        on_lead_final(0, "called")                      # lead index 1 in the file
        on_lead_final(1, "not_interested")              # lead index 2
        return Counter()

    import pipeline
    monkeypatch.setattr(pipeline, "run_leads_sync", fake_run_leads_sync)
    stop = threading.Event()
    stop.set()
    pull_leads.default_process([{"a": 2}, {"a": 3}], None, None, stop_event=stop,
                               progress_path=saved, lead_indexes=[1, 2], batches_dir=batches)
    assert BatchStore.load_progress(saved) == {1, 2}
    assert BatchStore.remaining(saved)[1] == [0]


def test_pipeline_reports_finished_leads_but_not_interrupted_or_retried(tmp_path, monkeypatch):
    _pipeline_fakes(monkeypatch)
    finals = []

    async def go():
        await run_leads(
            [{"company": "A", "phone": "(512) 555-0181"},
             {"company": "B"},                                   # no phone -> dropped (final)
             {"company": "C", "phone": "(512) 555-0182", LEAD_META_KEY: {"retries": {"decision": 1}}}],
            results_file=tmp_path / "r.xlsx", services_file=_services_yaml(tmp_path),
            tracker_file=tmp_path / "t.xlsx", on_lead_final=lambda pos, st: finals.append((pos, st)))

    asyncio.run(go())
    assert sorted(finals) == [(0, "not_interested"), (1, "dropped_no_phone"), (2, "not_interested")]
    # the retry bookkeeping never reaches the results sheet
    wb = load_workbook(tmp_path / "r.xlsx", read_only=True)
    try:
        header = next(wb.active.iter_rows(values_only=True))
        assert not any(LEAD_META_KEY in str(h) for h in header)
    finally:
        wb.close()


def test_start_api_passes_retry_failed_calls(tmp_path):
    from fastapi.testclient import TestClient
    from dashboard.app import create_app
    from dashboard.data import TrackerDataStore
    from dashboard.runner import BatchRunner

    got = []
    ds = TrackerDataStore(tmp_path / "missing.xlsx", start_watcher=False)
    runner = BatchRunner(store=ds, source_url="http://fake", token="t", batches_dir=tmp_path / "pulled",
                         ping_fn=lambda *a, **k: PingResult(BATCH, "x", [{"lead_id": "Z"}]),
                         process_fn=lambda leads, *, retry_failed_calls=None, **kw: got.append(retry_failed_calls)
                         or Counter(), preflight_fn=lambda: None)
    app = create_app(tracker_path=tmp_path / "missing.xlsx", start_watcher=False, runner=runner)
    with TestClient(app) as client:
        r = client.post("/api/runner/start", json={"batch_size": 5, "num_batches": 1, "retry_failed_calls": True})
        assert r.status_code == 200 and r.json()["retry_failed_calls"] is True
        assert _wait_until(lambda: not client.get("/api/runner").json()["active"])
    assert got == [True]


# ------------------------------------------------------------------ 13: kill signals in main.py

def test_main_with_a_file_turns_kill_signals_into_ctrl_c(tmp_path, monkeypatch):
    import main as caller_main
    leads = tmp_path / "leads.json"
    leads.write_text(json.dumps([{"company": "Co", "phone": "(512) 555-0191"}]), encoding="utf-8")
    calls = []
    monkeypatch.setattr(pull_leads, "stop_signals_act_like_ctrl_c", lambda: calls.append("signals") or [])
    monkeypatch.setattr(caller_main, "run_leads_sync", lambda leads, **kw: calls.append("run") or Counter())
    monkeypatch.setattr(sys, "argv", ["main.py", str(leads)])
    assert caller_main.main() == 0
    assert calls == ["signals", "run"]


# ------------------------------------------------------------------ 21: figures are not phone numbers

def test_revenue_and_size_figures_reach_gemini():
    state = lead_state({
        "company": "Big Builder",
        "annual_revenue": "$1,250,000,000",
        "employees": 1500000000,
        "revenue_range": "10,000,000 - 25,000,000",
        "notes": "512-555-0101",
    })
    assert state["annual_revenue"] == "$1,250,000,000"
    assert state["employees"] == "1500000000"
    assert state["revenue_range"] == "10,000,000 - 25,000,000"
    assert "notes" not in state, "a phone number in a text field is still left out"
