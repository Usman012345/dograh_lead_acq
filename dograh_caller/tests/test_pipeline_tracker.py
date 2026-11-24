import asyncio
import json
import os
from pathlib import Path

from openpyxl import load_workbook
import pytest

import config
from decision_maker import Decision, Service, ServiceScore
from dograh_client import Agent, CallOutcome
from number_verifier import NumberVerifier
from pipeline import LeadPipeline
from pipeline_tracker import PipelineTracker, TRACKER_SHEET_NAME, TrackerLockedError


def _services():
    return [
        Service(id="draftliner", name="Draftliner", agent_id=2, description="AEC", good_fit="", not_a_fit=""),
        Service(id="paradise", name="Paradise Estimating", agent_id=3, description="Est", good_fit="", not_a_fit=""),
    ]


def _read_tracker(path: Path):
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb[TRACKER_SHEET_NAME]
        rows = list(ws.iter_rows(values_only=True))
        header = list(rows[0])
        out = []
        for r in rows[1:]:
            d = {}
            lead_chunks = []
            dograh_chunks = []
            for k, v in zip(header, r):
                if k == "lead_json" or (k.startswith("lead_json_") and k[10:].isdigit()):
                    idx = 1 if k == "lead_json" else int(k.split("_")[-1])
                    lead_chunks.append((idx, str(v or "")))
                elif k == "dograh_json" or (k.startswith("dograh_json_") and k[11:].isdigit()):
                    idx = 1 if k == "dograh_json" else int(k.split("_")[-1])
                    dograh_chunks.append((idx, str(v or "")))
                else:
                    d[k] = v
            d["lead_json"] = "".join(c for _, c in sorted(lead_chunks))
            d["dograh_json"] = "".join(c for _, c in sorted(dograh_chunks))
            out.append(d)
        return header, out
    finally:
        wb.close()


def _assert_invariants(rows):
    total = len(rows)
    v_passed = sum(1 for r in rows if r.get("verification_result") == "passed")
    v_rejected = sum(1 for r in rows if r.get("verification_result") == "rejected")
    v_errors = sum(1 for r in rows if r.get("verification_result") == "error")
    v_waiting = sum(1 for r in rows if not str(r.get("verification_result") or "").strip())
    assert total == v_passed + v_rejected + v_errors + v_waiting

    q_total = sum(1 for r in rows if r.get("decision_result") == "qualified")
    nq_total = sum(1 for r in rows if r.get("decision_result") == "non_qualified")
    d_errors = sum(1 for r in rows if r.get("decision_result") == "error")
    d_waiting = sum(
        1 for r in rows
        if r.get("verification_result") == "passed" and not str(r.get("decision_result") or "").strip()
    )
    assert v_passed == q_total + nq_total + d_errors + d_waiting

    ca_sub = sum(
        1 for r in rows
        if r.get("decision_result") == "qualified"
        and r.get("stage") in ("qualified_waiting", "calling", "not_sent", "scored_qualified", "interrupted")
    )
    cr_total = sum(1 for r in rows if r.get("stage") == "call_finished")
    assert q_total == ca_sub + cr_total

    called = sum(1 for r in rows if r.get("stage") == "call_finished" and r.get("call_status") == "called")
    failed = sum(1 for r in rows if r.get("stage") == "call_finished" and r.get("call_status") == "call_failed")
    timeout = sum(1 for r in rows if r.get("stage") == "call_finished" and r.get("call_status") == "call_timeout")
    assert cr_total == called + failed + timeout


def test_stage_transitions_and_invariants(tmp_path):
    tracker_path = tmp_path / "tracker.xlsx"
    svcs = _services()
    t = PipelineTracker(tracker_path)
    t.open()
    try:
        verifier = NumberVerifier()
        l1 = t.register({"company": "Fictional One", "phone": "(512) 555-0101"}, "python")
        l2 = t.register({"company": "Fictional Two"}, "python")
        l3 = t.register({"company": "Fictional Three", "phone": "(512) 555-0103"}, "python")
        _assert_invariants(t._rows)

        t.record_verification(l1, verifier.verify({"phone": "(512) 555-0101"}))
        t.record_verification(l2, verifier.verify({}))
        t.record_verification(l3, verifier.verify({"phone": "(512) 555-0103"}))
        _assert_invariants(t._rows)

        d1 = Decision(
            probabilities={"draftliner": 0.2, "paradise": 0.9},
            service=svcs[1],
            probability=0.9,
            scores=[
                ServiceScore("draftliner", "Draftliner", 0.2, "No CAD."),
                ServiceScore("paradise", "Paradise Estimating", 0.9, "Concrete bidder."),
            ],
            best_service=svcs[1],
            summary="Fictional One bids concrete. Clear estimating match.",
            verdict="Qualified for Paradise Estimating at 90% (the requirement is 80%).",
            threshold=0.8,
            model="gemini-3.8-flash",
        )
        t.record_decision(l1, d1)
        t.record_decision_error(l3, "timeout")
        _assert_invariants(t._rows)

        agent = Agent(id=3, name="Olivia", version_status="published")
        t.record_campaign(l1, agent, 55, "Paradise Estimating_2026-11-23")
        t.record_call_started(l1, 9001, 1)
        _assert_invariants(t._rows)

        outcome = CallOutcome(
            status="called",
            run_id=9001,
            disposition="transfer_call",
            duration_seconds=65.0,
            tags=["transfer_call"],
            transcript_url="https://example.com/t/9001",
            recording_url="https://example.com/r/9001",
            attempts=1,
            raw={
                "id": 9001,
                "public_access_token": "SECRET-TOKEN",
                "initial_context": {"secret": "hide"},
                "gathered_context": {"call_disposition": "transfer_call", "api_key": "NOPE"},
            },
        )
        t.record_finished(l1, "called", "", outcome)
        _assert_invariants(t._rows)
        assert t.save() is True
    finally:
        t.close()

    _, rows = _read_tracker(tracker_path)
    _assert_invariants(rows)
    by_id = {r["lead_id"]: r for r in rows}
    assert by_id[l1]["stage"] == "call_finished"
    assert by_id[l1]["call_transferred"] is True
    assert "SECRET-TOKEN" not in by_id[l1]["dograh_json"]
    assert "api_key" not in by_id[l1]["dograh_json"]
    assert by_id[l2]["stage"] == "number_rejected"
    assert by_id[l3]["stage"] == "decision_error"


def test_full_fake_run_produces_tracker_and_excludes_lead_id_from_csv(tmp_path, monkeypatch):
    services_yaml = tmp_path / "services.yaml"
    services_yaml.write_text(
        "services:\n"
        "  - id: paradise\n"
        "    name: Paradise Estimating\n"
        "    agent_id: 3\n"
        "    description: Construction estimating\n",
        encoding="utf-8",
    )
    results_xlsx = tmp_path / "results.xlsx"
    transfers_xlsx = tmp_path / "transfers.xlsx"
    tracker_xlsx = tmp_path / "tracker.xlsx"
    monkeypatch.setattr(config, "TRANSFERS_FILE", transfers_xlsx)
    monkeypatch.setattr(config, "CAMPAIGN_POLL_SECONDS", 0.01)
    monkeypatch.setattr(config, "RESULT_WAIT_SECONDS", 0.0)

    uploaded_csv_rows = []

    class FakeDograh:
        def get_agent(self, agent_id):
            return Agent(id=agent_id, name="Olivia", version_status="published")

        def get_required_variables(self, agent_id):
            return set()

        def upload_csv(self, rows, file_name):
            uploaded_csv_rows.extend(rows)
            return "file-key-1"

        def create_campaign(self, name, agent_id, file_key):
            return {"id": 77, "name": name}

        def start_campaign(self, cid):
            pass

        def get_campaign(self, cid):
            return {"id": cid, "state": "running", "retry_config": {"enabled": False}}

        def get_campaign_runs(self, cid):
            ref = uploaded_csv_rows[0]["lead_ref"]
            return [{
                "id": 501,
                "is_completed": True,
                "public_access_token": "DO-NOT-LEAK",
                "initial_context": {"lead_ref": ref, "phone_number": "+15125550101"},
                "gathered_context": {"call_disposition": "user_qualified", "call_tags": ["user_qualified"]},
                "cost_info": {"call_duration_seconds": 45},
                "transcript_public_url": "https://example.com/t/501",
                "recording_public_url": "https://example.com/r/501",
            }]

        def stop_campaign(self, cid):
            return "paused"

        def close(self):
            pass

    class FakeDecider:
        def __init__(self, services, threshold, api_key, model):
            self.services = services

        async def decide(self, lead):
            if "Concrete" in lead.get("company", ""):
                return Decision(
                    probabilities={"paradise": 0.92},
                    service=self.services[0],
                    probability=0.92,
                    scores=[ServiceScore("paradise", "Paradise Estimating", 0.92, "Concrete contractor.")],
                    best_service=self.services[0],
                    summary="Fictional Concrete does commercial flatwork. Strong estimating fit.",
                    verdict="Qualified for Paradise Estimating at 92% (the requirement is 80%).",
                    threshold=0.80,
                    model="gemini-3.8-flash",
                )
            return Decision(
                probabilities={"paradise": 0.10},
                service=None,
                probability=0.10,
                scores=[ServiceScore("paradise", "Paradise Estimating", 0.10, "Restaurant.")],
                best_service=self.services[0],
                summary="Fictional Cafe is a restaurant. Outside construction estimating.",
                verdict="Non-Qualified: the best match was Paradise Estimating at 10%, below the 80% requirement.",
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
            source_label="file:demo.json",
        ) as p:
            await p.submit({"company": "Fictional Concrete Co", "phone": "(512) 555-0101"})
            await p.submit({"company": "Fictional Cafe", "phone": "(512) 555-0102"})
            await p.submit({"company": "Fictional NoPhone"})

    asyncio.run(run())

    assert len(uploaded_csv_rows) == 1
    assert "lead_id" not in uploaded_csv_rows[0]
    assert uploaded_csv_rows[0]["lead_ref"] == "L000001"

    _, rows = _read_tracker(tracker_xlsx)
    _assert_invariants(rows)
    assert len(rows) == 3
    assert rows[0]["stage"] == "call_finished"
    assert rows[0]["source_label"] == "file:demo.json"
    assert rows[1]["stage"] == "non_qualified"
    assert rows[2]["stage"] == "number_rejected"
    assert "DO-NOT-LEAK" not in tracker_xlsx.read_bytes().decode("latin-1", errors="ignore")


def test_identity_rules_and_restart_interrupted(tmp_path):
    tracker_path = tmp_path / "tracker.xlsx"

    # Run 1: create one row with call activity, one unfinished row, one non-called row
    t1 = PipelineTracker(tracker_path)
    t1.open()
    called_id = t1.register({"lead_id": "src-1", "company": "Fictional Called", "phone": "(512) 555-0101"}, "streamer")
    waiting_id = t1.register({"lead_id": "src-2", "company": "Fictional Waiting", "phone": "(512) 555-0102"}, "streamer")
    nq_id = t1.register({"lead_id": "src-3", "company": "Fictional NQ", "phone": "(512) 555-0103"}, "streamer")

    # Rule 2: same source_key within same tracker instance -> new row with previous_lead_id
    dup_same_run = t1.register({"lead_id": "src-2", "company": "Fictional Waiting", "phone": "(512) 555-0102"}, "streamer")
    assert dup_same_run != waiting_id
    assert t1._by_lead_id[dup_same_run]["previous_lead_id"] == waiting_id

    verifier = NumberVerifier()
    t1.record_verification(called_id, verifier.verify({"phone": "(512) 555-0101"}))
    t1.record_campaign(called_id, Agent(3, "Olivia", "published"), 11, "Camp")
    t1.record_call_started(called_id, 999, 1)
    t1.record_finished(called_id, "called", "", CallOutcome(status="called", run_id=999, attempts=1))

    t1.record_verification(nq_id, verifier.verify({"phone": "(512) 555-0103"}))
    t1.record_finished(nq_id, "not_interested")

    # waiting_id left in awaiting_decision (unfinished)
    t1.record_verification(waiting_id, verifier.verify({"phone": "(512) 555-0102"}))
    assert t1.save() is True
    t1.close()

    # Run 2: reopening marks unfinished rows interrupted, protects called row, reuses non-called row
    t2 = PipelineTracker(tracker_path)
    t2.open()
    try:
        assert "+15125550101" in t2.called_numbers()
        assert t2._by_lead_id[waiting_id]["stage"] == "interrupted"
        assert t2._by_lead_id[dup_same_run]["stage"] == "interrupted"

        # Rule 3: earlier row has call data -> new row with previous_lead_id, never overwrite
        new_for_called = t2.register(
            {"lead_id": "src-1", "company": "Fictional Called Updated", "phone": "(512) 555-0101"},
            "streamer",
        )
        assert new_for_called != called_id
        assert t2._by_lead_id[new_for_called]["previous_lead_id"] == called_id
        assert t2._by_lead_id[called_id]["call_status"] == "called"

        # Rule 4: earlier row from earlier run with no call activity -> reuse row, increment times_processed
        reused_nq = t2.register(
            {"lead_id": "src-3", "company": "Fictional NQ Retried", "phone": "(512) 555-0103"},
            "streamer",
        )
        assert reused_nq == nq_id
        assert t2._by_lead_id[reused_nq]["times_processed"] == 2
        assert t2._by_lead_id[reused_nq]["stage"] == "incoming"
    finally:
        t2.close()


def test_long_description_control_chars_and_formula_round_trip(tmp_path):
    tracker_path = tmp_path / "tracker.xlsx"
    long_desc = "X" * 100_000
    t = PipelineTracker(tracker_path)
    t.open()
    try:
        lid = t.register({
            "company": "=SUM(1,2)",
            "notes": "line1\x0bline2",
            "description": long_desc,
            "phone": "(512) 555-0104",
        }, "python")
        assert t.save() is True
    finally:
        t.close()

    t2 = PipelineTracker(tracker_path)
    t2.open()
    try:
        row = t2._by_lead_id[lid]
        parsed = json.loads(row["lead_json"])
        assert parsed["description"] == long_desc
        assert row["business"] == "=SUM(1,2)"
        assert "\x0b" not in row["lead_json"]
    finally:
        t2.close()


def test_permission_error_keeps_data_and_second_tracker_locks(tmp_path, monkeypatch):
    tracker_path = tmp_path / "tracker.xlsx"
    t1 = PipelineTracker(tracker_path)
    t1.open()
    try:
        t2 = PipelineTracker(tracker_path)
        with pytest.raises(TrackerLockedError):
            t2.open()

        t1.register({"company": "Fictional Lock Co", "phone": "(512) 555-0105"}, "python")

        orig_replace = os.replace
        monkeypatch.setattr(os, "replace", lambda src, dst: (_ for _ in ()).throw(PermissionError("open in excel")))
        monkeypatch.setattr("pipeline_tracker.time.sleep", lambda _: None)
        assert t1.save() is False
        assert t1._dirty is True

        monkeypatch.setattr(os, "replace", orig_replace)
        assert t1.save() is True
        assert t1._dirty is False
        assert tracker_path.exists()
    finally:
        t1.close()


def test_start_failure_releases_lock_and_save_exception_does_not_stop_pipeline(tmp_path, monkeypatch):
    services_yaml = tmp_path / "services.yaml"
    services_yaml.write_text(
        "services:\n"
        "  - id: paradise\n"
        "    name: Paradise Estimating\n"
        "    agent_id: 3\n"
        "    description: Est\n",
        encoding="utf-8",
    )
    tracker_path = tmp_path / "tracker.xlsx"

    # Simulate start() failing midway (missing API key) -> lock must be released
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    p_fail = LeadPipeline(
        results_file=tmp_path / "r1.xlsx",
        services_file=services_yaml,
        score_only=True,
        tracker_file=tracker_path,
    )
    with pytest.raises(ValueError, match="Gemini API key"):
        asyncio.run(p_fail.start())

    # Second pipeline on the same tracker can acquire the lock and survive tracker.save failure
    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake-key")

    class FakeDecider:
        def __init__(self, services, threshold, api_key, model):
            self.services = services

        async def decide(self, lead):
            return Decision(
                probabilities={"paradise": 0.90},
                service=self.services[0],
                probability=0.90,
                scores=[ServiceScore("paradise", "Paradise Estimating", 0.90, "Good fit.")],
                best_service=self.services[0],
                summary="Fictional Co bids projects. Strong fit.",
                verdict="Qualified for Paradise Estimating at 90% (the requirement is 80%).",
                threshold=0.80,
                model="gemini-3.8-flash",
            )

        async def close(self):
            pass

    monkeypatch.setattr("pipeline.DecisionMaker", FakeDecider)

    async def run_with_save_error():
        p = LeadPipeline(
            results_file=tmp_path / "r2.xlsx",
            services_file=services_yaml,
            score_only=True,
            tracker_file=tracker_path,
        )
        p.tracker.save = lambda: (_ for _ in ()).throw(RuntimeError("disk full"))
        async with p:
            await p.submit({"company": "Fictional Resilient Co", "phone": "(512) 555-0106"})
            return p.stats

    stats = asyncio.run(run_with_save_error())
    assert stats["scored_qualified"] == 1
    assert (tmp_path / "r2.xlsx").exists()


def test_rule3_preserved_when_called_lead_followed_by_same_run_duplicate_then_rerun(tmp_path):
    tracker_path = tmp_path / "tracker.xlsx"
    verifier = NumberVerifier()

    # Run 1: lead src-dup is called, then a same-run duplicate arrives and is dropped_duplicate.
    t1 = PipelineTracker(tracker_path)
    t1.open()
    called_id = t1.register({"lead_id": "src-dup", "company": "Fictional Original", "phone": "(512) 555-0191"}, "streamer")
    t1.record_verification(called_id, verifier.verify({"phone": "(512) 555-0191"}))
    t1.record_campaign(called_id, Agent(3, "Olivia", "published"), 21, "Camp")
    t1.record_call_started(called_id, 888, 1)
    t1.record_finished(called_id, "called", "", CallOutcome(status="called", run_id=888, attempts=1))

    dup_id = t1.register({"lead_id": "src-dup", "company": "Fictional Dup", "phone": "(512) 555-0191"}, "streamer")
    assert dup_id != called_id
    t1.record_finished(dup_id, "dropped_duplicate", "duplicate phone")
    assert t1._by_lead_id[dup_id]["verified_at"]
    assert t1.save() is True
    t1.close()

    # Run 2: src-dup arrives again. Rule 3 must prevent reusing dup_id or overwriting called_id.
    t2 = PipelineTracker(tracker_path)
    t2.open()
    try:
        rerun_id = t2.register({"lead_id": "src-dup", "company": "Fictional Rerun", "phone": "(512) 555-0191"}, "streamer")
        assert rerun_id not in (called_id, dup_id)
        assert t2._by_lead_id[rerun_id]["previous_lead_id"] == dup_id
        assert t2._by_lead_id[rerun_id]["times_processed"] == 3
        assert t2._by_lead_id[called_id]["call_status"] == "called"
        assert t2._by_lead_id[dup_id]["status"] == "dropped_duplicate"
    finally:
        t2.close()


def test_verified_at_set_on_verifier_error(tmp_path):
    tracker_path = tmp_path / "tracker.xlsx"
    t = PipelineTracker(tracker_path)
    t.open()
    try:
        lid = t.register({"lead_id": "err-1", "company": "Fictional Err Co", "phone": "(512) 555-0192"}, "python")
        assert t._by_lead_id[lid]["verified_at"] == ""
        t.record_finished(lid, "error", "verifier: boom")
        assert t._by_lead_id[lid]["verified_at"] != ""
        _assert_invariants(t._rows)
    finally:
        t.close()


def test_float_string_times_processed_reloaded_cleanly(tmp_path):
    from openpyxl import load_workbook

    tracker_path = tmp_path / "tracker.xlsx"
    t1 = PipelineTracker(tracker_path)
    t1.open()
    lid = t1.register({"lead_id": "flt-1", "company": "Fictional Float Co", "phone": "(512) 555-0193"}, "streamer")
    t1.record_finished(lid, "not_interested")
    assert t1.save() is True
    t1.close()

    # Simulate Excel storing times_processed as float string "1.0"
    wb = load_workbook(tracker_path)
    ws = wb[TRACKER_SHEET_NAME]
    header = [c.value for c in ws[1]]
    tp_col = header.index("times_processed") + 1
    ws.cell(row=2, column=tp_col, value="1.0")
    wb.save(tracker_path)
    wb.close()

    t2 = PipelineTracker(tracker_path)
    t2.open()
    try:
        reused = t2.register({"lead_id": "flt-1", "company": "Fictional Float Co", "phone": "(512) 555-0193"}, "streamer")
        assert reused == lid
        assert t2._by_lead_id[reused]["times_processed"] == 2
    finally:
        t2.close()
