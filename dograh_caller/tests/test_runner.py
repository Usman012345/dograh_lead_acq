"""Tests for dashboard BatchRunner and /api/runner endpoints."""

from __future__ import annotations

import threading
import time
from collections import Counter

import pytest
from fastapi.testclient import TestClient

from dashboard.app import create_app
from dashboard.data import TrackerDataStore
from dashboard.runner import BatchRunner, RunnerBusyError
from pull_leads import BATCH, EMPTY, PingResult
from tools.make_demo_tracker import generate_demo_tracker


@pytest.fixture
def demo_tracker(tmp_path):
    path = tmp_path / "demo_tracker.xlsx"
    generate_demo_tracker(path)
    return path


def _wait_until(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def test_runner_api_defaults_custom_validation_and_409_busy(demo_tracker, tmp_path):
    store = TrackerDataStore(demo_tracker, start_watcher=False, demo=True)
    started = threading.Event()
    release = threading.Event()
    ping_calls = []
    process_calls = []

    def fake_ping(url, token, timeout, limit=None):
        ping_calls.append({"url": url, "limit": limit})
        started.set()
        release.wait(timeout=5.0)
        return PingResult(BATCH, "Received 2", [{"lead_id": "L1", "company": "A"}, {"lead_id": "L2", "company": "B"}])

    def fake_process(leads, *, batch_size=None, stop_event=None, **kwargs):
        process_calls.append({"count": len(leads), "batch_size": batch_size})
        return Counter(received=len(leads))

    runner = BatchRunner(
        store=store,
        demo=False,
        source_url="http://fake-streamer.local",
        token="tok",
        timeout=5.0,
        batches_dir=tmp_path / "pulled",
        ping_fn=fake_ping,
        process_fn=fake_process,
        preflight_fn=lambda: None,
    )
    app = create_app(tracker_path=demo_tracker, start_watcher=False, runner=runner)

    with TestClient(app) as client:
        # Initial idle status
        s0 = client.get("/api/runner").json()
        assert s0["state"] == "idle"
        assert s0["active"] is False
        assert s0["batch_size"] == 300
        assert s0["total_batches"] == 1

        # Validation errors (Pydantic -> 422)
        assert client.post("/api/runner/start", json={"batch_size": 0, "num_batches": 1}).status_code == 422
        assert client.post("/api/runner/start", json={"batch_size": 1001, "num_batches": 1}).status_code == 422
        assert client.post("/api/runner/start", json={"batch_size": 10, "num_batches": 0}).status_code == 422
        assert client.post("/api/runner/start", json={"batch_size": 10, "num_batches": 101}).status_code == 422

        # Start with defaults (empty body) -> 300 & 1
        r_start = client.post("/api/runner/start", json={})
        assert r_start.status_code == 200
        body = r_start.json()
        assert body["active"] is True
        assert body["state"] == "running"
        assert body["batch_size"] == 300
        assert body["total_batches"] == 1

        assert started.wait(timeout=3.0)

        # /api/summary embeds live runner status
        summary = client.get("/api/summary").json()
        assert summary["runner"]["active"] is True
        assert summary["runner"]["batch_size"] == 300

        # Starting while active -> 409 Conflict
        r_busy = client.post("/api/runner/start", json={"batch_size": 50, "num_batches": 2})
        assert r_busy.status_code == 409

        # Let batch finish and verify auto-stop (completed)
        release.set()
        assert _wait_until(lambda: not client.get("/api/runner").json()["active"])
        done = client.get("/api/runner").json()
        assert done["state"] == "completed"
        assert done["batches_completed"] == 1
        assert done["leads_fetched"] == 2
        assert done["leads_processed"] == 2
        assert ping_calls == [{"url": "http://fake-streamer.local", "limit": 300}]
        assert process_calls == [{"count": 2, "batch_size": 300}]


def test_runner_auto_completes_n_batches_with_custom_values(demo_tracker, tmp_path):
    store = TrackerDataStore(demo_tracker, start_watcher=False, demo=False)
    ping_limits = []
    batch_idx = [0]

    def fake_ping(url, token, timeout, limit=None):
        ping_limits.append(limit)
        batch_idx[0] += 1
        return PingResult(
            BATCH,
            f"Received {limit}",
            [{"lead_id": f"B{batch_idx[0]}-{i}", "company": f"Co {i}"} for i in range(3)],
        )

    def fake_process(leads, *, batch_size=None, stop_event=None, **kwargs):
        return Counter(received=len(leads))

    runner = BatchRunner(
        store=store,
        source_url="http://fake.local",
        token="tok",
        batches_dir=tmp_path / "pulled",
        ping_fn=fake_ping,
        process_fn=fake_process,
        preflight_fn=lambda: None,
    )
    app = create_app(tracker_path=demo_tracker, start_watcher=False, runner=runner)

    with TestClient(app) as client:
        r = client.post("/api/runner/start", json={"batch_size": 125, "num_batches": 3})
        assert r.status_code == 200
        assert _wait_until(lambda: not client.get("/api/runner").json()["active"])
        status = client.get("/api/runner").json()
        assert status["state"] == "completed"
        assert status["batches_completed"] == 3
        assert status["total_batches"] == 3
        assert status["leads_fetched"] == 9
        assert status["leads_processed"] == 9
        assert ping_limits == [125, 125, 125]
        assert len(list((tmp_path / "pulled").glob("*.done.json"))) == 3


def test_runner_manual_stop_mid_run_sets_stop_event_and_keeps_unfinished_batch(demo_tracker, tmp_path):
    store = TrackerDataStore(demo_tracker, start_watcher=False, demo=False)
    in_process = threading.Event()
    saw_stop_event = threading.Event()

    def fake_ping(url, token, timeout, limit=None):
        return PingResult(BATCH, "Received 2", [{"lead_id": "S1", "company": "Stop Co"}])

    def fake_process(leads, *, batch_size=None, stop_event=None, **kwargs):
        in_process.set()
        if stop_event is not None and stop_event.wait(timeout=3.0):
            saw_stop_event.set()
        return Counter(interrupted=len(leads))

    runner = BatchRunner(
        store=store,
        source_url="http://fake.local",
        token="tok",
        batches_dir=tmp_path / "pulled",
        ping_fn=fake_ping,
        process_fn=fake_process,
        preflight_fn=lambda: None,
    )
    app = create_app(tracker_path=demo_tracker, start_watcher=False, runner=runner)

    with TestClient(app) as client:
        assert client.post("/api/runner/start", json={"batch_size": 50, "num_batches": 4}).status_code == 200
        assert in_process.wait(timeout=3.0)

        r_stop = client.post("/api/runner/stop")
        assert r_stop.status_code == 200
        assert r_stop.json()["state"] in ("stopping", "stopped")
        assert saw_stop_event.wait(timeout=3.0)
        assert _wait_until(lambda: not client.get("/api/runner").json()["active"])

        final = client.get("/api/runner").json()
        assert final["state"] == "stopped"
        assert final["batches_completed"] == 0
        # Unfinished batch remains pending on disk for resume
        assert len(list((tmp_path / "pulled").glob("*.pending.json"))) == 1


def test_runner_stops_early_when_streamer_returns_empty(demo_tracker, tmp_path):
    store = TrackerDataStore(demo_tracker, start_watcher=False, demo=False)
    runner = BatchRunner(
        store=store,
        source_url="http://fake.local",
        token="tok",
        batches_dir=tmp_path / "pulled",
        ping_fn=lambda *a, **kw: PingResult(EMPTY, "No new Leads available"),
        process_fn=lambda *a, **kw: Counter(),
        preflight_fn=lambda: None,
    )
    runner.start(batch_size=100, num_batches=5)
    assert _wait_until(lambda: not runner.get_status()["active"])
    st = runner.get_status()
    assert st["state"] == "completed"
    assert "No new Leads" in st["message"]
    assert st["batches_completed"] == 0
    store.close()


def test_runner_demo_mode_auto_completes_and_can_be_stopped(demo_tracker):
    app = create_app(tracker_path=demo_tracker, start_watcher=False, demo=True)
    with TestClient(app) as client:
        r = client.post("/api/runner/start", json={"batch_size": 50, "num_batches": 2})
        assert r.status_code == 200
        assert r.json()["active"] is True
        # Stop mid-demo run
        time.sleep(0.2)
        s = client.post("/api/runner/stop")
        assert s.status_code == 200
        assert _wait_until(lambda: not client.get("/api/runner").json()["active"])
        assert client.get("/api/runner").json()["state"] == "stopped"


def test_runner_direct_validation(tmp_path):
    store = TrackerDataStore(tmp_path / "does_not_exist.xlsx", start_watcher=False)
    runner = BatchRunner(store=store, demo=True)
    with pytest.raises(ValueError, match="batch_size"):
        runner.start(batch_size=True, num_batches=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="num_batches"):
        runner.start(batch_size=10, num_batches=0)
    runner.start(batch_size=10, num_batches=1)
    try:
        with pytest.raises(RunnerBusyError):
            runner.start(batch_size=10, num_batches=1)
    finally:
        runner.close()
        store.close()


def test_runner_forwards_custom_tracker_file_to_process(tmp_path):
    custom_tracker = tmp_path / "custom_tracker.xlsx"
    store = TrackerDataStore(custom_tracker, start_watcher=False)
    seen_trackers = []

    def fake_process(leads, *, tracker_file=None, **kwargs):
        seen_trackers.append(tracker_file)
        return Counter(received=len(leads))

    runner = BatchRunner(
        store=store,
        source_url="http://fake.local",
        token="tok",
        batches_dir=tmp_path / "pulled",
        ping_fn=lambda *a, **kw: PingResult(BATCH, "Received 1", [{"lead_id": "T1", "company": "Co"}]),
        process_fn=fake_process,
        preflight_fn=lambda: None,
    )
    runner.start(batch_size=10, num_batches=1)
    assert _wait_until(lambda: not runner.get_status()["active"])
    assert runner.get_status()["state"] == "completed"
    assert seen_trackers == [custom_tracker]
    store.close()


def test_runner_refuses_to_ping_streamer_when_tracker_is_locked(tmp_path):
    from pipeline_tracker import PipelineTracker

    tracker_path = tmp_path / "pipeline_tracker.xlsx"
    held = PipelineTracker(tracker_path)
    held.open()
    store = TrackerDataStore(tracker_path, start_watcher=False)
    ping_called = []
    try:
        runner = BatchRunner(
            store=store,
            source_url="http://fake.local",
            token="tok",
            batches_dir=tmp_path / "pulled",
            ping_fn=lambda *a, **kw: ping_called.append(True) or PingResult(EMPTY, "empty"),
            process_fn=None,
            preflight_fn=lambda: None,
        )
        runner.start(batch_size=10, num_batches=1)
        assert _wait_until(lambda: not runner.get_status()["active"])
        st = runner.get_status()
        assert st["state"] == "error"
        assert "locked" in (st["error"] or "").lower()
        assert ping_called == []
    finally:
        store.close()
        held.close()


def test_runner_stop_during_preflight_finishes_as_stopped(tmp_path):
    store = TrackerDataStore(tmp_path / "missing.xlsx", start_watcher=False)
    in_preflight = threading.Event()
    allow_preflight = threading.Event()
    ping_called = []

    def slow_preflight():
        in_preflight.set()
        allow_preflight.wait(timeout=3.0)
        return None

    runner = BatchRunner(
        store=store,
        source_url="http://fake.local",
        token="tok",
        batches_dir=tmp_path / "pulled",
        ping_fn=lambda *a, **kw: ping_called.append(True) or PingResult(EMPTY, "empty"),
        process_fn=lambda *a, **kw: Counter(),
        preflight_fn=slow_preflight,
    )
    runner.start(batch_size=10, num_batches=1)
    assert in_preflight.wait(timeout=3.0)
    runner.stop()
    allow_preflight.set()
    assert _wait_until(lambda: not runner.get_status()["active"])
    assert runner.get_status()["state"] == "stopped"
    assert ping_called == []
    store.close()


def test_runner_start_accepts_no_request_body(demo_tracker):
    app = create_app(tracker_path=demo_tracker, start_watcher=False, demo=True)
    with TestClient(app) as client:
        r = client.post("/api/runner/start")
        assert r.status_code == 200
        body = r.json()
        assert body["active"] is True
        assert body["batch_size"] == 300
        assert body["total_batches"] == 1
        client.post("/api/runner/stop")


def test_dashboard_startup_does_not_pull_or_call_leads_until_started(demo_tracker, tmp_path):
    store = TrackerDataStore(demo_tracker, start_watcher=False, demo=False)
    calls = []

    runner = BatchRunner(
        store=store,
        source_url="http://fake-proxy.local",
        token="tok",
        batches_dir=tmp_path / "pulled",
        preflight_fn=lambda: calls.append(("preflight",)) or None,
        ping_fn=lambda url, token, timeout, limit=None: calls.append(("ping", url)) or PingResult(EMPTY, "empty"),
        process_fn=lambda *a, **kw: calls.append(("process",)) or Counter(),
    )
    app = create_app(tracker_path=demo_tracker, start_watcher=False, runner=runner)

    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        summary = client.get("/api/summary").json()
        assert summary["runner"]["state"] == "idle"
        assert summary["runner"]["active"] is False
        assert summary["runner"]["source_url"] == "http://fake-proxy.local"
        assert client.get("/api/leads?view=incoming").status_code == 200
        time.sleep(0.1)
        assert calls == []

        # Only after explicit Start does it preflight and ping the reverse proxy directly
        r = client.post("/api/runner/start", json={"batch_size": 50, "num_batches": 1})
        assert r.status_code == 200
        assert _wait_until(lambda: not client.get("/api/runner").json()["active"])
        assert calls == [("preflight",), ("ping", "http://fake-proxy.local")]


def test_create_app_default_runner_stays_idle_on_startup(tmp_path, monkeypatch):
    import config
    import pipeline
    import pull_leads

    monkeypatch.setattr(
        pull_leads,
        "preflight",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not preflight on startup")),
    )
    monkeypatch.setattr(
        pull_leads,
        "ping",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not ping streamer on startup")),
    )
    monkeypatch.setattr(
        pull_leads,
        "default_process",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not process leads on startup")),
    )
    monkeypatch.setattr(
        pipeline,
        "run_leads_sync",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not run pipeline on startup")),
    )

    app = create_app(tracker_path=tmp_path / "missing_tracker.xlsx", start_watcher=True, demo=False)
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/health").status_code == 200
        summary = client.get("/api/summary").json()
        assert summary["runner"]["state"] == "idle"
        assert summary["runner"]["active"] is False
        assert summary["runner"]["source_url"] == config.LEAD_SOURCE_URL
        runner_state = client.get("/api/runner").json()
        assert runner_state["state"] == "idle"
        assert runner_state["active"] is False
        assert runner_state["source_url"] == config.LEAD_SOURCE_URL
        assert client.get("/api/leads?view=incoming").status_code == 200
        time.sleep(0.15)
        assert client.get("/api/runner").json()["state"] == "idle"

    demo_app = create_app(tracker_path=tmp_path / "missing_demo.xlsx", start_watcher=False, demo=True)
    with TestClient(demo_app) as demo_client:
        demo_runner = demo_client.get("/api/runner").json()
        assert demo_runner["state"] == "idle"
        assert demo_runner["source_url"] is None
