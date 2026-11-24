"""Offline tests for pull_leads.py: a local HTTP server stands in for the lead streamer,
and the pipeline is replaced by a fake, so nobody is called."""

import gzip
import http.server
import json
import threading
from collections import Counter

import pytest

import config
import pull_leads
from pull_leads import BATCH, EMPTY, FATAL, RETRY, BatchStore, PingResult, Puller, ping

TOKEN = "s" * 48


# ------------------------------------------------------------------ fake streamer

class FakeStreamer:
    """Answers each GET with the next queued (status, headers, body) and records the requests."""

    def __init__(self):
        self.answers = []
        self.requests = []
        streamer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                streamer.requests.append({"path": self.path, "auth": self.headers.get("Authorization"),
                                          "accept_encoding": self.headers.get("Accept-Encoding", "")})
                status, headers, body = streamer.answers.pop(0)
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def queue_json(self, status, data, gz=False, headers=None):
        body = json.dumps(data).encode()
        h = {"Content-Type": "application/json; charset=utf-8", **(headers or {})}
        if gz:
            body = gzip.compress(body)
            h["Content-Encoding"] = "gzip"
        self.answers.append((status, h, body))

    def queue_html(self, status, html="<html><title>404 Not Found</title></html>"):
        self.answers.append((status, {"Content-Type": "text/html"}, html.encode()))

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def streamer():
    s = FakeStreamer()
    yield s
    s.close()


def _leads(n, start=0):
    return [{"lead_id": f"L{i}", "content_version": 1, "event": "new", "company": f"Org {i}",
             "phone": "(512) 555-0101", "category": "General Contractors"} for i in range(start, start + n)]


def _ok(leads):
    return {"status": "ok", "count": len(leads), "new": len(leads), "updated": 0,
            "served_at": "2026-11-20T10:00:00Z", "leads": leads}


# ------------------------------------------------------------------ ping()

def test_ping_ok_gzip_batch_of_300(streamer):
    streamer.queue_json(200, _ok(_leads(300)), gz=True)
    r = ping(streamer.url, TOKEN, 120)
    assert r.kind == BATCH and len(r.leads) == 300
    assert r.meta["count"] == 300 and r.meta["served_at"] == "2026-11-20T10:00:00Z"
    req = streamer.requests[0]
    assert req["path"] == "/leads/next" and req["auth"] == f"Bearer {TOKEN}"
    assert "gzip" in req["accept_encoding"]


def test_ping_no_new_leads(streamer):
    streamer.queue_json(200, {"status": "no_new_leads", "message": "No new Leads available",
                              "count": 0, "leads": []})
    r = ping(streamer.url, TOKEN, 120)
    assert r.kind == EMPTY and "No new Leads" in r.message


@pytest.mark.parametrize("status,body,kind,text", [
    (401, {"status": "error", "error": "unauthorized"}, FATAL, "token"),
    (421, {"status": "error", "error": "wrong_host"}, FATAL, "PUBLIC_BASE_URL"),
    (503, {"status": "error", "error": "busy", "retry_after_seconds": 5}, RETRY, "Wait 5 seconds"),
    (503, {"status": "error", "error": "database_unavailable", "retry_after_seconds": 30}, RETRY, "30"),
    (500, {"status": "error", "error": "internal_error"}, RETRY, "internal_error"),
    (200, {"status": "ok", "leads": "nope"}, RETRY, "no 'leads' list"),
    (200, {"status": "weird"}, RETRY, "Unexpected status"),
])
def test_ping_errors(streamer, status, body, kind, text):
    streamer.queue_json(status, body)
    r = ping(streamer.url, TOKEN, 120)
    assert r.kind == kind and text in r.message and not r.leads


def test_ping_html_pages_are_not_mistaken_for_leads(streamer):
    streamer.queue_html(404)                                   # what ai-sdr.grobots.ai/leads/next gives today
    assert ping(streamer.url, TOKEN, 120).kind == RETRY
    streamer.queue_html(200, "<h1>CyberPanel Installed</h1>")  # a default site page
    r = ping(streamer.url, TOKEN, 120)
    assert r.kind == RETRY and "not JSON" in r.message


def test_ping_unreachable():
    r = ping("http://127.0.0.1:9", TOKEN, 120)
    assert r.kind == RETRY and "Could not reach" in r.message


def test_ping_skips_non_object_items(streamer):
    streamer.queue_json(200, _ok(_leads(2) + ["x", 5]))
    r = ping(streamer.url, TOKEN, 120)
    assert r.kind == BATCH and len(r.leads) == 2 and "2 item(s)" in r.message


# ------------------------------------------------------------------ the loop

class Script:
    """Scripted answers for the prompts; records every prompt and event in order."""

    def __init__(self, answers, events):
        self.answers = list(answers)
        self.events = events

    def __call__(self, prompt):
        self.events.append(("ask", prompt.strip().splitlines()[0][:40]))
        return self.answers.pop(0) if self.answers else "q"


def make_puller(tmp_path, pings, answers, process=None, events=None):
    events = [] if events is None else events
    pings = list(pings)

    def fake_ping(url, token, timeout):
        events.append(("ping",))
        return pings.pop(0)

    def fake_process(leads):
        events.append(("process", len(leads)))
        return Counter(received=len(leads))

    p = Puller(source_url="https://ai-sdr.grobots.ai", token=TOKEN, timeout=180,
               store=BatchStore(tmp_path / "pulled"), process=process or fake_process,
               ask_fn=Script(answers, events), ping_fn=fake_ping)
    return p, events


def kinds(events):
    return [e[0] for e in events]


def test_waits_for_a_person_after_every_batch(tmp_path):
    pings = [PingResult(BATCH, "Received 300", _leads(300)), PingResult(BATCH, "Received 120", _leads(120))]
    p, events = make_puller(tmp_path, pings, answers=["", "q"])
    assert p.run() == 0
    # ping -> process fully -> ask -> (Enter) ping -> process -> ask -> (q) stop
    assert kinds(events) == ["ping", "process", "ask", "ping", "process", "ask"]
    assert [e[1] for e in events if e[0] == "process"] == [300, 120]
    done = sorted((tmp_path / "pulled").glob("*.done.json"))
    assert len(done) == 2 and not list((tmp_path / "pulled").glob("*.pending.json"))
    assert len(json.loads(done[0].read_text(encoding="utf-8"))["leads"]) in (300, 120)


def test_quit_after_first_batch_means_exactly_one_ping(tmp_path):
    p, events = make_puller(tmp_path, [PingResult(BATCH, "x", _leads(300))], answers=["q"])
    assert p.run() == 0
    assert kinds(events).count("ping") == 1


def test_no_new_leads_and_errors_also_wait_for_a_person(tmp_path):
    pings = [PingResult(EMPTY, "No new Leads available"), PingResult(RETRY, "HTTP 503"),
             PingResult(BATCH, "x", _leads(3))]
    p, events = make_puller(tmp_path, pings, answers=["", "", "q"])
    assert p.run() == 0
    assert kinds(events) == ["ping", "ask", "ping", "ask", "ping", "process", "ask"]


def test_fatal_stops_without_asking(tmp_path):
    p, events = make_puller(tmp_path, [PingResult(FATAL, "HTTP 401")], answers=[])
    assert p.run() == 2
    assert kinds(events) == ["ping"]


def test_batch_is_on_disk_before_processing(tmp_path):
    seen = []

    def process(leads):
        seen.append(len(list((tmp_path / "pulled").glob("*.pending.json"))))
        return Counter()

    p, _ = make_puller(tmp_path, [PingResult(BATCH, "x", _leads(5))], answers=["q"], process=process)
    p.run()
    assert seen == [1]


def test_processing_error_keeps_batch_and_can_be_retried(tmp_path):
    calls = []

    def flaky(leads):
        calls.append(len(leads))
        if len(calls) == 1:
            raise RuntimeError("Dograh rejected the API key (HTTP 401)")
        return Counter()

    p, events = make_puller(tmp_path, [PingResult(BATCH, "x", _leads(7))], answers=["", "q"], process=flaky)
    assert p.run() == 0
    assert calls == [7, 7]                    # same batch retried, no new ping in between
    assert kinds(events).count("ping") == 1
    assert len(list((tmp_path / "pulled").glob("*.done.json"))) == 1


def test_ctrl_c_mid_batch_keeps_it_and_next_start_offers_to_resume(tmp_path):
    def interrupted(leads):
        raise KeyboardInterrupt

    p, events = make_puller(tmp_path, [PingResult(BATCH, "x", _leads(9))], answers=[], process=interrupted)
    assert p.run() == 130
    assert len(list((tmp_path / "pulled").glob("*.pending.json"))) == 1
    assert "ask" not in kinds(events)

    # Next start: asked about the unfinished batch first, processes it, then pings
    p2, events2 = make_puller(tmp_path, [PingResult(EMPTY, "No new Leads available")], answers=["", "q"])
    assert p2.run() == 0
    assert kinds(events2) == ["ask", "process", "ping", "ask"]
    assert events2[1] == ("process", 9)
    assert len(list((tmp_path / "pulled").glob("*.done.json"))) == 1


def test_unfinished_batch_can_be_left_for_later(tmp_path):
    store = BatchStore(tmp_path / "pulled")
    store.save(PingResult(BATCH, "x", _leads(4)), "test")
    p, events = make_puller(tmp_path, [PingResult(EMPTY, "none")], answers=["l", "q"])
    assert p.run() == 0
    assert "process" not in kinds(events)
    assert len(list((tmp_path / "pulled").glob("*.pending.json"))) == 1


def test_oversized_batch_is_still_processed_whole(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "LEAD_BATCH_SIZE", 300)
    p, events = make_puller(tmp_path, [PingResult(BATCH, "x", _leads(1000))], answers=["q"])
    p.run()
    assert ("process", 1000) in events
    assert "BATCH_SIZE=300" in capsys.readouterr().out


def test_default_batch_size_is_300():
    assert config.LEAD_BATCH_SIZE == 300


def test_end_to_end_with_real_http(tmp_path, streamer):
    """Real HTTP + gzip through Puller.run(): two batches, one Enter in between."""
    streamer.queue_json(200, _ok(_leads(300)), gz=True)
    streamer.queue_json(200, {"status": "no_new_leads", "message": "No new Leads available", "leads": []})
    processed = []
    p = Puller(source_url=streamer.url, token=TOKEN, timeout=120, store=BatchStore(tmp_path / "pulled"),
               process=lambda leads: processed.append([l["lead_id"] for l in leads]) or Counter(),
               ask_fn=Script(["", "q"], []))
    assert p.run() == 0
    assert len(streamer.requests) == 2
    assert processed == [[f"L{i}" for i in range(300)]]


def test_preflight_requires_token(monkeypatch):
    monkeypatch.setattr(config, "LEAD_SOURCE_TOKEN", "")
    assert "LEAD_SOURCE_TOKEN" in pull_leads.preflight()


# ------------------------------------------------------------------ automatic mode

class StopLoop:
    """Records every wait and stops the endless loop after the scripted number of waits."""

    def __init__(self, stop_after):
        self.waits = []
        self.stop_after = stop_after

    def __call__(self, seconds):
        self.waits.append(seconds)
        if len(self.waits) >= self.stop_after:
            raise KeyboardInterrupt


def auto_puller(tmp_path, pings, process=None, events=None):
    events = [] if events is None else events
    pings = list(pings)

    def must_not_ask(prompt):
        raise AssertionError(f"automatic mode must never prompt: {prompt}")

    def fake_ping(url, token, timeout):
        events.append(("ping",))
        return pings.pop(0)

    def fake_process(leads):
        events.append(("process", len(leads)))
        return Counter(received=len(leads))

    return Puller(source_url="https://ai-sdr.grobots.ai", token=TOKEN, timeout=180,
                  store=BatchStore(tmp_path / "pulled"), process=process or fake_process,
                  ask_fn=must_not_ask, ping_fn=fake_ping), events


def test_auto_waits_for_proxy_then_processes_batches_without_a_person(tmp_path):
    pings = [PingResult(BATCH, "Received 300", _leads(300)), PingResult(BATCH, "Received 20", _leads(20)),
             PingResult(EMPTY, "No new Leads available")]
    p, events = auto_puller(tmp_path, pings)
    ready = iter([False, False, True, True, True])
    sleep = StopLoop(3)
    assert p.run_auto(60, ready_fn=lambda url: next(ready), sleep_fn=sleep) == 130
    assert events == [("ping",), ("process", 300), ("ping",), ("process", 20), ("ping",)]
    assert sleep.waits == [60, 60, 60]
    assert p.batches_done == 2 and p.leads_done == 320
    assert not p.store.unfinished()


def test_auto_never_pings_until_the_real_streamer_answers(tmp_path):
    p, events = auto_puller(tmp_path, [])
    assert p.run_auto(60, ready_fn=lambda url: False, sleep_fn=StopLoop(4)) == 130
    assert events == []


def test_auto_finishes_saved_batches_before_taking_new_leads(tmp_path):
    p, events = auto_puller(tmp_path, [PingResult(EMPTY, "No new Leads available")])
    p.store.save(PingResult(BATCH, "x", _leads(7)), "earlier run")
    assert p.run_auto(60, ready_fn=lambda url: True, sleep_fn=StopLoop(1)) == 130
    assert events == [("process", 7), ("ping",)]


def test_auto_keeps_a_failed_batch_and_takes_nothing_new(tmp_path):
    calls = []

    def flaky(leads):
        calls.append(len(leads))
        if len(calls) < 3:
            raise RuntimeError("Dograh is down")
        return Counter(received=len(leads))

    p, events = auto_puller(tmp_path, [PingResult(EMPTY, "No new Leads available")], process=flaky)
    p.store.save(PingResult(BATCH, "x", _leads(4)), "earlier run")
    sleep = StopLoop(3)
    assert p.run_auto(60, ready_fn=lambda url: True, sleep_fn=sleep) == 130
    assert calls == [4, 4, 4]
    assert sleep.waits == [60, 120, 60], "failures back off, then normal checks resume"
    assert events == [("ping",)]


def test_auto_backs_off_when_gemini_scoring_fails(tmp_path):
    def failing_scores(leads):
        p.store.save(PingResult(BATCH, "retry", leads), "decision_retry")
        return Counter(decision_error=len(leads))

    p, _events = auto_puller(tmp_path, [], process=failing_scores)
    p.store.save(PingResult(BATCH, "x", _leads(2)), "earlier run")
    sleep = StopLoop(3)
    assert p.run_auto(60, ready_fn=lambda url: True, sleep_fn=sleep) == 130
    assert sleep.waits == [60, 120, 240]
    assert p.store.unfinished(), "failed leads stay saved"


def test_auto_stops_on_a_token_or_host_problem(tmp_path, capsys):
    p, events = auto_puller(tmp_path, [PingResult(FATAL, "The streamer rejected the token (HTTP 401).")])
    assert p.run_auto(60, ready_fn=lambda url: True, sleep_fn=StopLoop(5)) == 2
    assert events == [("ping",)]
    assert "STOP" in capsys.readouterr().out


def test_auto_respects_the_streamers_retry_after(tmp_path):
    p, _events = auto_puller(tmp_path, [PingResult(RETRY, "busy", meta={"retry_after": 90})])
    sleep = StopLoop(1)
    assert p.run_auto(60, ready_fn=lambda url: True, sleep_fn=sleep) == 130
    assert sleep.waits == [90]


def test_auto_does_not_repeat_the_same_waiting_message(tmp_path, capsys):
    p, _events = auto_puller(tmp_path, [])
    p.run_auto(60, ready_fn=lambda url: False, sleep_fn=StopLoop(5))
    assert capsys.readouterr().out.count("Waiting for the lead streamer") == 1


@pytest.mark.parametrize("status,body,expected", [
    (200, {"status": "alive", "database": "ok"}, True),
    (200, {"status": "alive", "database": "busy"}, False),
    (200, {"status": "ok"}, False),                       # the old receiver on this subdomain
    (503, {"status": "alive", "database": "ok"}, False),
])
def test_streamer_ready_only_for_the_real_streamer(streamer, status, body, expected):
    streamer.queue_json(status, body)
    assert pull_leads.streamer_is_ready(streamer.url) is expected
    assert streamer.requests[0]["path"] == "/health"
    assert streamer.requests[0]["auth"] is None, "the token is never sent to an unconfirmed server"


def test_streamer_ready_handles_web_pages_and_no_connection(streamer):
    streamer.queue_html(200, "<h1>CyberPanel Installed</h1>")
    assert pull_leads.streamer_is_ready(streamer.url) is False
    assert pull_leads.streamer_is_ready("http://127.0.0.1:9") is False


def test_auto_end_to_end_with_real_http(tmp_path, streamer):
    streamer.queue_json(200, {"status": "alive", "database": "ok"})
    streamer.queue_json(200, _ok(_leads(3)), gz=True)
    streamer.queue_json(200, {"status": "alive", "database": "ok"})
    streamer.queue_json(200, {"status": "no_new_leads", "message": "No new Leads available", "leads": []})
    processed = []
    p = Puller(source_url=streamer.url, token=TOKEN, timeout=120, store=BatchStore(tmp_path / "pulled"),
               process=lambda leads: processed.append(len(leads)) or Counter())
    assert p.run_auto(60, sleep_fn=StopLoop(1)) == 130
    assert processed == [3]
    assert [r["path"] for r in streamer.requests] == ["/health", "/leads/next", "/health", "/leads/next"]


# ------------------------------------------------------------------ deployment helpers

def test_stop_signals_become_ctrl_c():
    import signal
    saved = {s: signal.getsignal(s) for s in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGHUP", None)) if s}
    try:
        handled = pull_leads.stop_signals_act_like_ctrl_c()
        assert signal.SIGTERM in handled
        with pytest.raises(KeyboardInterrupt):     # same path as Ctrl+C: campaigns get paused
            signal.raise_signal(signal.SIGTERM)
    finally:
        for s, h in saved.items():
            signal.signal(s, h)


def _stats():
    return {"status": "ok", "eligible": 650, "sent_current": 300, "updated_since_sent": 0, "never_sent": 350}


def test_check_passes_and_takes_no_leads(streamer, monkeypatch, capsys):
    monkeypatch.setattr(pull_leads, "preflight", lambda: None)
    monkeypatch.setattr(config, "LEAD_SOURCE_TOKEN", TOKEN)
    streamer.queue_json(200, {"status": "alive", "database": "ok"})
    streamer.queue_json(200, _stats())
    assert pull_leads.check_setup(streamer.url) == 0
    assert [r["path"] for r in streamer.requests] == ["/health", "/stats"]   # never /leads/next
    assert "350 lead(s) never sent" in capsys.readouterr().out


def test_check_spots_a_server_that_is_not_the_streamer(streamer, monkeypatch, capsys):
    monkeypatch.setattr(pull_leads, "preflight", lambda: None)
    streamer.queue_json(200, {"status": "ok"})          # e.g. the old receiver on ai-sdr.grobots.ai
    assert pull_leads.check_setup(streamer.url) == 1
    assert "not the lead streamer" in capsys.readouterr().out


def test_check_reports_wrong_token(streamer, monkeypatch, capsys):
    monkeypatch.setattr(pull_leads, "preflight", lambda: None)
    monkeypatch.setattr(config, "LEAD_SOURCE_TOKEN", "wrong")
    streamer.queue_json(200, {"status": "alive", "database": "ok"})
    streamer.queue_json(401, {"status": "error", "error": "unauthorized"})
    assert pull_leads.check_setup(streamer.url) == 1
    assert "rejected" in capsys.readouterr().out


def test_after_hangup_output_is_silenced_so_cleanup_can_finish(monkeypatch):
    import sys

    class DeadTerminal:
        def write(self, *_):
            raise OSError(5, "Input/output error")   # what a vanished SSH terminal does

        def flush(self):
            raise OSError(5, "Input/output error")

    monkeypatch.setattr(sys, "stdout", DeadTerminal())
    monkeypatch.setattr(sys, "stderr", DeadTerminal())
    with pytest.raises(OSError):
        pull_leads.say("campaign paused")            # without the fix, cleanup logging would crash
    pull_leads._silence_output()
    pull_leads.say("campaign paused")                # now harmless
    print("summary line")


def test_ping_passes_limit_query_param_when_provided(streamer):
    streamer.queue_json(200, _ok(_leads(25)))
    r = ping(streamer.url, TOKEN, 120, limit=25)
    assert r.kind == BATCH and len(r.leads) == 25
    assert streamer.requests[0]["path"] == "/leads/next?limit=25"

    streamer.queue_json(200, _ok(_leads(5)))
    r2 = ping(streamer.url, TOKEN, 120)
    assert r2.kind == BATCH and len(r2.leads) == 5
    assert streamer.requests[1]["path"] == "/leads/next"


def test_default_process_forwards_batch_size_and_stop_event(monkeypatch):
    import pipeline
    captured = {}

    def fake_run_leads_sync(leads, **kwargs):
        captured["leads"] = leads
        captured["kwargs"] = kwargs
        return Counter(received=len(leads))

    monkeypatch.setattr(pipeline, "run_leads_sync", fake_run_leads_sync)
    evt = threading.Event()
    pull_leads.default_process([{"company": "X"}], threshold=0.85, output=None, batch_size=75, stop_event=evt)
    assert captured["kwargs"]["batch_size"] == 75
    assert captured["kwargs"]["stop_event"] is evt


def test_ping_and_check_setup_do_not_follow_redirects(streamer, monkeypatch, capsys):
    # A misconfigured proxy returning 302 must not forward Authorization to the redirect target
    redirect_headers = {"Location": f"{streamer.url}/other"}
    streamer.queue_json(302, {"status": "redirect"}, headers=redirect_headers)
    r = ping(streamer.url, TOKEN, 120)
    assert r.kind == RETRY and "HTTP 302" in r.message
    assert len(streamer.requests) == 1

    monkeypatch.setattr(pull_leads, "preflight", lambda: None)
    monkeypatch.setattr(config, "LEAD_SOURCE_TOKEN", TOKEN)
    streamer.queue_json(200, {"status": "alive", "database": "ok"})
    streamer.queue_json(302, {"status": "redirect"}, headers=redirect_headers)
    rc = pull_leads.check_setup(streamer.url)
    assert rc == 1
    assert "HTTP 302" in capsys.readouterr().out
    assert len(streamer.requests) == 3


def test_preflight_validates_threshold_and_thinking_level(monkeypatch):
    import decision_maker
    monkeypatch.setattr(config, "LEAD_SOURCE_TOKEN", TOKEN)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr(decision_maker, "check_gemini_access",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not call Gemini")))

    monkeypatch.setattr(config, "INTEREST_THRESHOLD", 80.0)
    err_t = pull_leads.preflight()
    assert err_t is not None and "INTEREST_THRESHOLD" in err_t

    monkeypatch.setattr(config, "INTEREST_THRESHOLD", 0.80)
    monkeypatch.setattr(config, "GEMINI_THINKING_LEVEL", "MINIMAL")
    err_l = pull_leads.preflight()
    assert err_l is not None and "GEMINI_THINKING_LEVEL" in err_l


def test_pull_leads_main_fails_before_ping_when_tracker_is_locked(tmp_path, monkeypatch, capsys):
    from pipeline_tracker import PipelineTracker

    tracker_path = tmp_path / "pipeline_tracker.xlsx"
    monkeypatch.setattr(config, "PIPELINE_TRACKER_FILE", tracker_path)
    monkeypatch.setattr(pull_leads, "preflight", lambda: None)
    monkeypatch.setattr(
        pull_leads.Puller,
        "run",
        lambda self, *a, **kw: (_ for _ in ()).throw(AssertionError("Puller.run must not start while tracker is locked")),
    )

    held = PipelineTracker(tracker_path)
    held.open()
    try:
        rc = pull_leads.main([])
        assert rc == 1
        assert "locked" in capsys.readouterr().out.lower()
    finally:
        held.close()


def test_preflight_requires_source_url(monkeypatch):
    monkeypatch.setattr(config, "LEAD_SOURCE_URL", "")
    monkeypatch.setattr(config, "LEAD_SOURCE_TOKEN", TOKEN)
    err = pull_leads.preflight()
    assert err is not None and "LEAD_SOURCE_URL" in err


def test_pull_leads_main_waits_for_user_before_first_ping_and_quit_pings_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PIPELINE_TRACKER_FILE", tmp_path / "pipeline_tracker.xlsx")
    monkeypatch.setattr(config, "PULLED_BATCHES_DIR", tmp_path / "pulled")
    monkeypatch.setattr(config, "LEAD_SOURCE_URL", "http://fake-streamer.invalid")
    monkeypatch.setattr(config, "LEAD_SOURCE_TOKEN", "fake-token")
    monkeypatch.setattr(pull_leads, "preflight", lambda: None)
    monkeypatch.setattr(pull_leads, "stop_signals_act_like_ctrl_c", lambda: [])

    ping_calls = []
    monkeypatch.setattr(pull_leads, "ping", lambda *a, **kw: ping_calls.append(True) or PingResult(EMPTY, "empty"))
    monkeypatch.setattr(
        pull_leads,
        "default_process",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not process leads")),
    )
    prompts = []
    answers = iter(["q"])
    monkeypatch.setattr("builtins.input", lambda prompt="": prompts.append(prompt) or next(answers))

    rc = pull_leads.main([])
    assert rc == 0
    assert len(prompts) == 1
    assert "Press Enter to ping" in prompts[0]
    assert "http://fake-streamer.invalid" in prompts[0]
    assert ping_calls == []


def test_pull_leads_main_only_pings_after_enter_and_prompts_even_after_leaving_pending(tmp_path, monkeypatch):
    batches_dir = tmp_path / "pulled"
    BatchStore(batches_dir).save(PingResult(BATCH, "saved", _leads(2)), "earlier")

    monkeypatch.setattr(config, "PIPELINE_TRACKER_FILE", tmp_path / "pipeline_tracker.xlsx")
    monkeypatch.setattr(config, "PULLED_BATCHES_DIR", batches_dir)
    monkeypatch.setattr(config, "LEAD_SOURCE_URL", "http://fake-streamer.invalid")
    monkeypatch.setattr(config, "LEAD_SOURCE_TOKEN", "fake-token")
    monkeypatch.setattr(pull_leads, "preflight", lambda: None)
    monkeypatch.setattr(pull_leads, "stop_signals_act_like_ctrl_c", lambda: [])

    events = []
    answers = iter(["l", "", "q"])

    def fake_input(prompt=""):
        ans = next(answers)
        events.append(("ask", ans))
        return ans

    def fake_ping(url, token, timeout):
        events.append(("ping", url))
        return PingResult(BATCH, "Received 2", _leads(2))

    def fake_process(leads, threshold, output):
        events.append(("process", len(leads)))
        return Counter(received=len(leads))

    monkeypatch.setattr("builtins.input", fake_input)
    monkeypatch.setattr(pull_leads, "ping", fake_ping)
    monkeypatch.setattr(pull_leads, "default_process", fake_process)

    rc = pull_leads.main([])
    assert rc == 0
    assert events == [
        ("ask", "l"),
        ("ask", ""),
        ("ping", "http://fake-streamer.invalid"),
        ("process", 2),
        ("ask", "q"),
    ]


def test_pull_leads_main_eof_quits_without_ping(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PIPELINE_TRACKER_FILE", tmp_path / "pipeline_tracker.xlsx")
    monkeypatch.setattr(config, "PULLED_BATCHES_DIR", tmp_path / "pulled")
    monkeypatch.setattr(config, "LEAD_SOURCE_URL", "http://fake-streamer.invalid")
    monkeypatch.setattr(config, "LEAD_SOURCE_TOKEN", "fake-token")
    monkeypatch.setattr(pull_leads, "preflight", lambda: None)
    monkeypatch.setattr(pull_leads, "stop_signals_act_like_ctrl_c", lambda: [])
    monkeypatch.setattr(
        pull_leads,
        "ping",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not ping on EOF")),
    )
    monkeypatch.setattr(
        pull_leads,
        "default_process",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not process leads on EOF")),
    )
    monkeypatch.setattr("builtins.input", lambda prompt="": (_ for _ in ()).throw(EOFError()))

    assert pull_leads.main([]) == 0


def test_pull_leads_main_reprompts_typos_and_quits_on_no_without_ping(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PIPELINE_TRACKER_FILE", tmp_path / "pipeline_tracker.xlsx")
    monkeypatch.setattr(config, "PULLED_BATCHES_DIR", tmp_path / "pulled")
    monkeypatch.setattr(config, "LEAD_SOURCE_URL", "http://fake-streamer.invalid")
    monkeypatch.setattr(config, "LEAD_SOURCE_TOKEN", "fake-token")
    monkeypatch.setattr(pull_leads, "preflight", lambda: None)
    monkeypatch.setattr(pull_leads, "stop_signals_act_like_ctrl_c", lambda: [])
    monkeypatch.setattr(
        pull_leads,
        "ping",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not ping on typo or 'no'")),
    )
    monkeypatch.setattr(
        pull_leads,
        "default_process",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not process leads")),
    )

    prompts = []
    answers = iter(["maybe", "?", "no"])
    monkeypatch.setattr("builtins.input", lambda prompt="": prompts.append(prompt) or next(answers))

    assert pull_leads.main([]) == 0
    assert len(prompts) == 3
