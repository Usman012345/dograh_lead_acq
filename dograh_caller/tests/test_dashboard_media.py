"""Real Dograh file keys, secure media access, client wording and API hardening."""

import json
import threading

import pytest
from fastapi.testclient import TestClient

import dashboard_server
from dashboard import media as media_module
from dashboard.app import create_app
from dashboard.demo_media import DemoMedia
from dashboard.media import AudioStream, DograhMedia, MediaUnavailable, is_storage_key, parse_transcript
from decision_maker import Decision, DecisionMaker, Service, ServiceScore
from dograh_client import Agent, CallOutcome
from number_verifier import Verification
from pipeline_tracker import PipelineTracker

SERVICE = Service(id="paradise", name="Paradise Estimating", agent_id=3, description="d", good_fit="", not_a_fit="")
AGENT = Agent(id=3, name="Olivia", version_status="published")


def decision(pct=92, threshold=0.8):
    p = pct / 100
    qualified = p >= threshold
    return Decision(
        probabilities={"paradise": p},
        service=SERVICE if qualified else None,
        probability=p,
        scores=[ServiceScore("paradise", SERVICE.name, p, "Bids from drawings.")],
        best_service=SERVICE,
        summary="Fictional contractor. Good estimating fit.",
        verdict="verdict",
        threshold=threshold,
        model="gemini-3.8-flash",
    )


def add_called_lead(tracker, company="Fictional Real Shape Co", phone="+15125550101", pct=92, disposition="user_qualified"):
    lead_id = tracker.register({"company": company, "phone": phone, "website": "www.fictional.example"}, "streamer")
    tracker.record_verification(lead_id, Verification(True, phone, phone, "ok", "Texas", "America/Chicago"))
    tracker.record_decision(lead_id, decision(pct))
    tracker.record_campaign(lead_id, AGENT, 11, "Paradise_2026-11-23")
    tracker.record_call_started(lead_id, 501, 1)
    raw = {
        "id": 501,
        "public_access_token": "SECRET-TOKEN",
        "recording_url": "recordings/501.wav",
        "transcript_url": "transcripts/501.txt",
        "bot_recording_url": "recordings/501/bot.wav",
        "user_recording_url": "recordings/501/user.wav",
        "recording_public_url": None,
        "transcript_public_url": None,
        "gathered_context": {"call_disposition": disposition, "call_tags": [disposition, "user_speech"],
                             "extracted_variables": {"next_step": "Send a sample"}},
    }
    tracker.record_finished(lead_id, "called", "", CallOutcome(
        status="called", run_id=501, disposition=disposition, duration_seconds=61.0, tags=[disposition, "user_speech"],
        transcript_url="transcripts/501.txt", recording_url="recordings/501.wav", attempts=1, raw=raw,
    ))
    return lead_id


class FakeMedia:
    def __init__(self):
        self.audio_requests = []

    def fetch_text(self, key):
        if key == "transcripts/501.txt":
            return ("[2026-11-23T10:00:00.000+00:00] assistant: Hello there.\n"
                    "[2026-11-23T10:00:05.000+00:00] user: Hi.\nSecond line.")
        raise MediaUnavailable("The transcript could not be loaded.")

    def open_audio(self, key, range_header=None):
        self.audio_requests.append((key, range_header))
        return AudioStream(206, {"Content-Type": "audio/wav", "Content-Range": "bytes 0-3/10", "Content-Length": "4",
                                 "Accept-Ranges": "bytes"}, iter([b"RIFF"]), lambda: None)


@pytest.fixture
def tracker_path(tmp_path):
    path = tmp_path / "tracker.xlsx"
    tracker = PipelineTracker(path)
    tracker.open()
    add_called_lead(tracker)
    tracker.save()
    tracker.close()
    return path


@pytest.fixture
def fake_media():
    return FakeMedia()


@pytest.fixture
def client(tracker_path, fake_media):
    with TestClient(create_app(tracker_path, start_watcher=False, media=fake_media)) as c:
        yield c


def test_real_dograh_file_keys_become_private_dashboard_links(client):
    detail = client.get("/api/leads/L000001").json()
    assert detail["call"]["transcript_available"] is True
    assert [t["id"] for t in detail["call"]["audio_tracks"]] == ["recording", "agent", "customer"]
    assert detail["call"]["audio_tracks"][0]["url"] == "/api/leads/L000001/audio/recording"
    assert "recordings/501" not in json.dumps(detail)
    assert "SECRET-TOKEN" not in json.dumps(detail)

    row = client.get("/api/leads?view=call_results").json()["rows"][0]
    assert row["recording_url"] == "/api/leads/L000001/audio/recording"
    assert row["transcript_url"] == "/api/leads/L000001/transcript"


def test_transcript_is_returned_as_conversation_turns(client):
    turns = client.get("/api/leads/L000001/transcript").json()["turns"]
    assert [t["speaker"] for t in turns] == ["agent", "customer"]
    assert turns[1]["text"] == "Hi.\nSecond line."


def test_audio_streams_with_range_and_only_known_tracks(client, fake_media):
    r = client.get("/api/leads/L000001/audio/agent", headers={"Range": "bytes=0-3"})
    assert r.status_code == 206 and r.content == b"RIFF"
    assert r.headers["content-type"] == "audio/wav" and r.headers["content-range"] == "bytes 0-3/10"
    assert fake_media.audio_requests == [("recordings/501/bot.wav", "bytes=0-3")]
    assert client.get("/api/leads/L000001/audio/secret").status_code == 404
    assert client.get("/api/leads/L999999/audio/recording").status_code == 404


def test_media_failures_are_clean(tracker_path):
    class Down(FakeMedia):
        def fetch_text(self, key):
            raise MediaUnavailable("The transcript could not be loaded.")

        def open_audio(self, key, range_header=None):
            raise MediaUnavailable("The recording could not be loaded.")

    with TestClient(create_app(tracker_path, start_watcher=False, media=Down())) as c:
        assert c.get("/api/leads/L000001/transcript").status_code == 502
        assert c.get("/api/leads/L000001/audio/recording").status_code == 502


@pytest.mark.parametrize("key,ok", [
    ("recordings/501.wav", True),
    ("recordings/501/bot.wav", True),
    ("transcripts/501.txt", True),
    ("recordings/../secrets.txt", False),
    ("campaigns/1/contacts.csv", False),
    ("https://example.test/a.wav", False),
    ("", False),
])
def test_only_call_files_are_accepted(key, ok):
    assert is_storage_key(key) is ok


def test_dograh_media_keeps_key_server_side_and_refuses_other_files(monkeypatch):
    calls = []

    class Resp:
        status_code = 200
        headers = {}

        def json(self):
            return {"url": "https://files.example.test/signed"}

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return Resp()

    monkeypatch.setattr(media_module.requests, "get", fake_get)
    media = DograhMedia("https://dograh.example.test/api/v1", "api-key")
    assert media._signed_url("recordings/1.wav") == "https://files.example.test/signed"
    assert media._signed_url("recordings/1.wav") == "https://files.example.test/signed"
    assert len(calls) == 1, "short-lived link is reused"
    assert calls[0][1]["headers"]["X-API-Key"] == "api-key"
    with pytest.raises(MediaUnavailable):
        media._signed_url("campaigns/1/contacts.csv")


def test_parse_transcript_handles_unlabelled_text():
    assert parse_transcript("plain text")[0] == {"speaker": "other", "at": None, "text": "plain text"}
    assert parse_transcript("") == []


def test_demo_media_supports_seeking():
    demo = DemoMedia()
    assert "assistant:" in demo.fetch_text("transcripts/demo/transfer-paradise-1.txt")
    audio = demo.open_audio("recordings/demo/1.wav", "bytes=0-3")
    assert audio.status_code == 206 and b"".join(audio.chunks) == b"RIFF"
    with pytest.raises(MediaUnavailable):
        demo.fetch_text("transcripts/501.txt")


@pytest.mark.parametrize("bad_range", ["bytes=100-50", "bytes=-0", "bytes=99999999-"])
def test_demo_media_invalid_ranges_do_not_return_invalid_206(bad_range):
    demo = DemoMedia()
    audio = demo.open_audio("recordings/demo/1.wav", bad_range)
    assert audio.status_code == 200
    assert "Content-Range" not in audio.headers
    payload = b"".join(audio.chunks)
    assert payload.startswith(b"RIFF")
    assert audio.headers["Content-Length"] == str(len(payload))


def test_security_headers_and_rebinding_protection(tracker_path, fake_media):
    app = create_app(tracker_path, start_watcher=False, media=fake_media,
                     allowed_hosts=dashboard_server.allowed_hosts_for("127.0.0.1"))
    with TestClient(app, base_url="http://localhost:8090") as c:
        ok = c.get("/api/summary")
        assert ok.status_code == 200
        assert ok.headers["x-frame-options"] == "DENY"
        assert ok.headers["referrer-policy"] == "no-referrer"
        assert "script-src 'self'" in ok.headers["content-security-policy"]
        assert c.get("/api/summary", headers={"Host": "127.0.0.1:8090"}).status_code == 200
        assert c.get("/api/summary", headers={"Host": "[::1]:8090"}).status_code == 200
        assert c.get("/api/summary", headers={"Host": "evil.example:8090"}).status_code == 400
    assert dashboard_server.allowed_hosts_for("0.0.0.0") is None


def test_call_reason_never_claims_a_call_that_did_not_happen(tmp_path):
    tracker = PipelineTracker(tmp_path / "t.xlsx")
    tracker.open()
    try:
        lead = tracker.register({"company": "Fictional Queue Co", "phone": "+15125550102"}, "python")
        tracker.record_verification(lead, Verification(True, "+15125550102", "+15125550102", "ok"))
        tracker.record_decision(lead, decision())
        tracker.record_campaign(lead, AGENT, 11, "Paradise_2026-11-23")
        assert tracker._by_lead_id[lead]["call_reason"].startswith("In the calling queue")

        tracker.record_finished(lead, "call_failed", "Not dialed: campaign ended in state 'paused'",
                                CallOutcome(status="call_failed", error="Not dialed"))
        assert tracker._by_lead_id[lead]["call_reason"] == "Not called: the call could not be started."

        dialed = tracker.register({"company": "Fictional Dialed Co", "phone": "+15125550103"}, "python")
        tracker.record_verification(dialed, Verification(True, "+15125550103", "+15125550103", "ok"))
        tracker.record_decision(dialed, decision())
        tracker.record_campaign(dialed, AGENT, 11, "Paradise_2026-11-23")
        tracker.record_call_started(dialed, 77, 1)
        assert tracker._by_lead_id[dialed]["call_reason"].startswith("Called because")
        tracker.record_finished(dialed, "call_failed", "lost", CallOutcome(status="call_failed", run_id=77))
        assert "did not complete" in tracker._by_lead_id[dialed]["call_reason"]
    finally:
        tracker.close()


def test_failed_call_journey_and_detail_hide_internal_errors(tmp_path):
    path = tmp_path / "t.xlsx"
    tracker = PipelineTracker(path)
    tracker.open()
    lead = tracker.register({"company": "Fictional Failed Co", "phone": "+15125550104"}, "python")
    tracker.record_verification(lead, Verification(True, "+15125550104", "+15125550104", "ok"))
    tracker.record_decision(lead, decision())
    tracker.record_finished(lead, "call_failed",
                            "Campaign could not be started: Could not reach Dograh at https://internal.example/api")
    tracker.save()
    tracker.close()

    with TestClient(create_app(path, start_watcher=False, media=FakeMedia())) as c:
        detail = c.get(f"/api/leads/{lead}").json()
    states = {step["stage"]: step["state"] for step in detail["journey"]}
    assert states["Campaign"] == "error" and states["Call"] == "error" and states["Result"] == "error"
    assert detail["status_detail"] == "The call could not be started."
    assert "internal.example" not in json.dumps(detail)


def test_ordinary_lead_text_is_kept_but_script_links_are_removed(tmp_path):
    path = tmp_path / "t.xlsx"
    tracker = PipelineTracker(path)
    tracker.open()
    lead = tracker.register({
        "company": "Fictional Text Co",
        "website": "www.fictional.example",
        "notes": "Data: 5 projects this year",
        "bad": "javascript:alert(1)",
        "bad2": "data:text/html,<script>alert(1)</script>",
    }, "python")
    tracker.save()
    tracker.close()
    with TestClient(create_app(path, start_watcher=False, media=FakeMedia())) as c:
        fields = c.get(f"/api/leads/{lead}").json()["lead_fields"]
    assert fields["website"] == "www.fictional.example"
    assert fields["notes"] == "Data: 5 projects this year"
    assert fields["bad"] == "" and fields["bad2"] == ""


def test_sort_keeps_blanks_last_and_zero_is_a_real_score(tmp_path):
    path = tmp_path / "t.xlsx"
    tracker = PipelineTracker(path)
    tracker.open()
    for index, pct in enumerate((0, 90, None)):
        phone = f"+1512555011{index}"
        lead = tracker.register({"company": f"Fictional Sort {index}", "phone": phone}, "python")
        tracker.record_verification(lead, Verification(True, phone, phone, "ok"))
        if pct is not None:
            tracker.record_decision(lead, decision(pct))
    tracker.save()
    tracker.close()
    with TestClient(create_app(path, start_watcher=False, media=FakeMedia())) as c:
        desc = c.get("/api/leads?view=decision_maker&sort=best_probability_pct&order=desc").json()["rows"]
        asc = c.get("/api/leads?view=decision_maker&sort=best_probability_pct&order=asc").json()["rows"]
    assert [r["best_probability_pct"] for r in desc] == ["90%", "0%", ""]
    assert [r["best_probability_pct"] for r in asc] == ["0%", "90%", ""]


def test_phone_search_accepts_any_format(client):
    for query in ("(512) 555-0101", "512-555-0101", "5125550101", "+15125550101"):
        assert client.get("/api/leads", params={"view": "incoming", "q": query}).json()["total"] == 1
    assert client.get("/api/leads", params={"view": "incoming", "q": "999-999-9999"}).json()["total"] == 0


def test_outcomes_use_client_words(client):
    row = client.get("/api/leads?view=call_results").json()["rows"][0]
    assert row["call_disposition"] == "Qualified on the call"
    assert row["call_tags"] == "Qualified on the call, Customer spoke"
    assert "user_qualified" not in json.dumps(client.get("/api/leads/L000001").json()["call"]["fields"])


def test_summary_uses_the_most_recent_threshold(tmp_path):
    path = tmp_path / "t.xlsx"
    tracker = PipelineTracker(path)
    tracker.open()
    old = tracker.register({"company": "Fictional Old", "phone": "+15125550120"}, "python")
    tracker.record_verification(old, Verification(True, "+15125550120", "+15125550120", "ok"))
    tracker.record_decision(old, decision(90, threshold=0.85))
    tracker._by_lead_id[old]["decided_at"] = "2026-11-20 10:00:00"
    new = tracker.register({"company": "Fictional New", "phone": "+15125550121"}, "python")
    tracker.record_verification(new, Verification(True, "+15125550121", "+15125550121", "ok"))
    tracker.record_decision(new, decision(90, threshold=0.80))
    tracker.save()
    tracker.close()
    with TestClient(create_app(path, start_watcher=False, media=FakeMedia())) as c:
        assert c.get("/api/summary").json()["threshold"] == 0.8


@pytest.mark.parametrize("bad", [0, -0.1, 1.01, 80])
def test_unsafe_threshold_is_refused(bad):
    with pytest.raises(ValueError, match="threshold"):
        DecisionMaker([SERVICE], bad, "fake-key", "gemini-3.8-flash")


def test_overlapping_saves_never_corrupt_the_tracker(tmp_path):
    path = tmp_path / "t.xlsx"
    tracker = PipelineTracker(path)
    tracker.open()
    try:
        for index in range(30):
            tracker.register({"company": f"Fictional {index}", "phone": f"+1512555{index:04d}"}, "python")
        errors = []

        def save():
            try:
                tracker._mark_dirty()
                assert tracker.save() is True
            except Exception as e:  # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=save) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []
    finally:
        tracker.close()

    reopened = PipelineTracker(path)
    reopened.open()
    try:
        assert len(reopened._rows) == 30
    finally:
        reopened.close()


def test_long_emoji_text_fits_excel_cell_limits(tmp_path):
    from openpyxl import load_workbook

    path = tmp_path / "t.xlsx"
    tracker = PipelineTracker(path)
    tracker.open()
    tracker.register({"company": "Fictional Emoji", "description": "🏗" * 50_000}, "python")
    tracker.save()
    tracker.close()
    ws = load_workbook(path, read_only=True)["leads"]
    longest = max(len(str(v).encode("utf-16-le")) // 2 for row in ws.iter_rows(values_only=True) for v in row if v)
    assert longest <= 32_767
