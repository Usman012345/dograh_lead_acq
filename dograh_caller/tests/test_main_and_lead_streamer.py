import http
import importlib.util
import sys
from pathlib import Path

import main as caller_main


def _load_lead_streamer():
    import pytest
    pytest.importorskip("psycopg", reason="lead streamer tests need psycopg (pip install -r requirements-dev.txt)")
    name = "lead_streamer_mod"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name,
        Path(__file__).resolve().parent.parent / "lead_streamer" / "lead_streamer.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_main_read_leads_filters_blank_csv_rows_and_validates_limit(tmp_path):
    csv_path = tmp_path / "leads.csv"
    csv_path.write_text(
        "company,phone\n"
        "Acme Builders,(512) 555-0101\n"
        " , \n"
        ",\n"
        "Beta Roofing,(512) 555-0102\n",
        encoding="utf-8",
    )
    leads = caller_main.read_leads(csv_path)
    assert len(leads) == 2
    assert [l["company"] for l in leads] == ["Acme Builders", "Beta Roofing"]

    assert caller_main.run(leads, limit=0) == 1
    assert caller_main.run(leads, limit=-2) == 1


def test_lead_streamer_explicit_http_and_eligible_sql_placeholders():
    ls = _load_lead_streamer()
    assert ls.http is http
    assert "NOT IN ('none', 'null', 'n/a')" in ls._ELIGIBLE_SQL


def test_lead_streamer_flatten_extra_filters_camelcase_and_spaced_keys():
    ls = _load_lead_streamer()
    merged = ls._merge_extra_sources(
        {"Extra": {"companyName": "Dup Co", "retryCount": 3, "Years In Business": "12"}},
        {"Email Status": "VALID", "servicesOffered": "Concrete"},
    )
    flattened = dict(ls._flatten_extra(merged))
    assert "company_name" not in flattened
    assert "retry_count" not in flattened
    assert "email_status" not in flattened
    assert flattened["years_in_business"] == "12"
    assert flattened["services_offered"] == "Concrete"


def test_lead_streamer_prevents_cross_contact_phone_and_email_bleed():
    ls = _load_lead_streamer()

    def make_row(contact_id):
        # 37-element row matching _build_lead_payload unpacking
        return (
            f"lead-{contact_id or 'org'}",  # lead_id
            "org-1",                        # org_id
            contact_id,                     # contact_id
            "company",                      # record_kind
            None,                           # lead_title
            "Concrete",                     # lead_category
            "Austin",                       # lead_city
            "TX",                           # lead_us_state
            "bbb",                          # source_code
            None,                           # source_url
            None,                           # due_at
            {},                             # raw_meta
            1,                              # content_version
            None,                           # created_at
            None,                           # updated_at
            None,                           # lead_notes
            None,                           # lead_status
            None,                           # lead_last_activity
            None,                           # lead_next_follow_up
            None,                           # lead_external_id
            None,                           # lead_first_seen_at
            None,                           # lead_last_seen_at
            "Acme Concrete",                # org_name
            None,                           # org_website
            None,                           # org_industry
            None,                           # org_domain
            None,                           # org_company_size
            None,                           # org_linkedin_url
            None,                           # org_raw_data
            f"Person {contact_id}" if contact_id else None,
            None,                           # contact_title
            None,                           # contact_first_name
            None,                           # contact_last_name
            None,                           # contact_department
            None,                           # contact_linkedin_url
            None,                           # contact_raw_data
            False,                          # was_sent
        )

    phones_by_contact = {"c1": ["+15125550101"], "c2": ["+15125550202"]}
    phones_by_org_direct = {"org-1": ["+15125550100"]}
    phones_by_org_all = {"org-1": ["+15125550101", "+15125550202", "+15125550100"]}

    emails_by_contact = {"c1": ["c1@acme.example"], "c2": ["c2@acme.example"]}
    emails_by_org_direct = {"org-1": ["info@acme.example"]}
    emails_by_org_all = {"org-1": ["c1@acme.example", "c2@acme.example", "info@acme.example"]}

    # Contact c1 gets its own + direct org phone/email, NOT c2's phone/email
    p_c1 = ls._build_lead_payload(
        make_row("c1"),
        phones_by_contact,
        phones_by_org_all,
        emails_by_contact,
        emails_by_org_all,
        {},
        {},
        {},
        phones_by_org_direct=phones_by_org_direct,
        emails_by_org_direct=emails_by_org_direct,
        invalid_by_org_direct={},
    )
    assert p_c1["phones"] == ["+15125550101", "+15125550100"]
    assert p_c1["emails"] == ["c1@acme.example", "info@acme.example"]

    # Contact c3 with no own/direct phone falls back to org-wide pool
    p_c3 = ls._build_lead_payload(
        make_row("c3"),
        phones_by_contact,
        phones_by_org_all,
        emails_by_contact,
        emails_by_org_all,
        {},
        {},
        {},
        phones_by_org_direct={},
        emails_by_org_direct={},
        invalid_by_org_direct={},
    )
    assert p_c3["phones"] == ["+15125550101", "+15125550202", "+15125550100"]

    # Org-only lead (contact_id=None) uses all org phones/emails
    p_org = ls._build_lead_payload(
        make_row(None),
        phones_by_contact,
        phones_by_org_all,
        emails_by_contact,
        emails_by_org_all,
        {},
        {},
        {},
        phones_by_org_direct=phones_by_org_direct,
        emails_by_org_direct=emails_by_org_direct,
        invalid_by_org_direct={},
    )
    assert p_org["phones"] == ["+15125550101", "+15125550202", "+15125550100"]


def test_main_without_input_stops_before_contacting_anything(monkeypatch, capsys):
    # Every run is live, so with no input configured main() must stop at once.
    monkeypatch.setattr(caller_main, "INPUT_FILE_PATH", None)
    monkeypatch.setattr(caller_main, "INPUT_LEADS", [])
    monkeypatch.setattr(caller_main, "USE_STREAMER", False)
    monkeypatch.setattr(sys, "argv", ["main.py"])

    def must_not_run(*args, **kwargs):
        raise AssertionError("pipeline must not start without input")

    monkeypatch.setattr(caller_main, "run_leads_sync", must_not_run)
    assert caller_main.main() == 1
    assert "No input set" in capsys.readouterr().out


def test_main_default_input_is_not_the_fictional_sample():
    assert caller_main.INPUT_FILE_PATH is None
    assert caller_main.USE_STREAMER is False
    assert "sample_leads" not in str(caller_main.INPUT_FILE_PATH or "")


def test_main_default_startup_does_not_auto_pull_or_call(monkeypatch, capsys):
    import pull_leads

    monkeypatch.setattr(sys, "argv", ["main.py"])
    monkeypatch.setattr(pull_leads, "preflight", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not preflight")))
    monkeypatch.setattr(pull_leads.Puller, "run_auto", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not run_auto")))
    monkeypatch.setattr(pull_leads.Puller, "run", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not run puller")))
    monkeypatch.setattr(caller_main, "run_leads_sync", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not run pipeline")))

    assert caller_main.main() == 1
    out = capsys.readouterr().out
    assert "No input set" in out
    assert "dashboard_server.py" in out


class _AutoSetup:
    """Replaces everything external for `python main.py`; records what ran, in order."""

    def __init__(self, monkeypatch, tmp_path, *, use_streamer=True):
        import json

        import pull_leads

        self.events = []
        leads_file = tmp_path / "configured.json"
        leads_file.write_text(json.dumps([{"company": "Fictional Co", "phone": "(512) 555-0101"}]), encoding="utf-8")
        monkeypatch.setattr(caller_main, "INPUT_FILE_PATH", str(leads_file))
        monkeypatch.setattr(caller_main, "INPUT_LEADS", [])
        monkeypatch.setattr(caller_main, "LIMIT", None)
        monkeypatch.setattr(caller_main, "USE_STREAMER", use_streamer)
        monkeypatch.setattr(caller_main, "STREAMER_CHECK_SECONDS", 17)
        monkeypatch.setattr(pull_leads, "stop_signals_act_like_ctrl_c", lambda: [])

        def preflight(need_streamer=True):
            self.events.append(("preflight", need_streamer))

        def run_leads(leads, **kwargs):
            self.events.append(("file", len(leads), kwargs["source_label"], kwargs["score_only"]))

        def run_auto(puller, seconds, **kwargs):
            self.events.append(("streamer", seconds))
            return 130

        monkeypatch.setattr(pull_leads, "preflight", preflight)
        monkeypatch.setattr(caller_main, "run_leads_sync", run_leads)
        monkeypatch.setattr(pull_leads.Puller, "run_auto", run_auto)


def test_plain_run_calls_the_configured_file_then_starts_the_streamer(monkeypatch, tmp_path):
    setup = _AutoSetup(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", ["main.py"])
    assert caller_main.main() == 130
    assert setup.events == [("preflight", True), ("file", 1, "file:configured.json", False), ("streamer", 17)]


def test_streamer_can_be_turned_off_in_code(monkeypatch, tmp_path):
    setup = _AutoSetup(monkeypatch, tmp_path, use_streamer=False)
    monkeypatch.setattr(sys, "argv", ["main.py"])
    assert caller_main.main() == 0
    assert setup.events == [("preflight", False), ("file", 1, "file:configured.json", False)]


def test_streamer_only_when_no_file_is_set(monkeypatch, tmp_path):
    setup = _AutoSetup(monkeypatch, tmp_path)
    monkeypatch.setattr(caller_main, "INPUT_FILE_PATH", None)
    monkeypatch.setattr(sys, "argv", ["main.py"])
    assert caller_main.main() == 130
    assert setup.events == [("preflight", True), ("streamer", 17)]


def test_wrong_file_path_stops_before_contacting_anything(monkeypatch, tmp_path, capsys):
    setup = _AutoSetup(monkeypatch, tmp_path)
    monkeypatch.setattr(caller_main, "INPUT_FILE_PATH", str(tmp_path / "missing.xlsx"))
    monkeypatch.setattr(sys, "argv", ["main.py"])
    assert caller_main.main() == 1
    assert setup.events == []
    assert "INPUT_FILE_PATH" in capsys.readouterr().out


def test_score_only_and_one_off_files_never_use_the_streamer(monkeypatch, tmp_path):
    setup = _AutoSetup(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", ["main.py", "--score-only", "--output", str(tmp_path / "s.xlsx")])
    assert caller_main.main() == 0
    assert setup.events == [("file", 1, "file:configured.json", True)]
    setup.events.clear()
    monkeypatch.setattr(sys, "argv", ["main.py", caller_main.INPUT_FILE_PATH])
    assert caller_main.main() == 0
    assert setup.events == [("file", 1, "file:configured.json", False)]


def test_lead_streamer_limit_query_validation_and_clamping():
    import threading
    from types import SimpleNamespace
    import requests as req

    ls = _load_lead_streamer()
    recorded_limits = []

    class FakeStore:
        batch_lock = threading.Lock()

        def check_health(self):
            return "ok"

        def fetch_next_batch(self, limit=None):
            recorded_limits.append(limit)
            return {"status": "no_new_leads", "message": "No new Leads available", "count": 0, "leads": []}

    token = "test-stream-token-123456"
    cfg = SimpleNamespace(
        expected_host="",
        max_concurrent_requests=4,
        request_timeout_seconds=5.0,
        stream_token_bytes=token.encode("utf-8"),
        batch_size=300,
    )
    logger = ls.StructuredLogger(50, (token,))
    server = ls.LeadStreamerServer(("127.0.0.1", 0), ls.LeadStreamerHandler, cfg, logger, FakeStore())
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        headers = {"Authorization": f"Bearer {token}"}

        r_default = req.get(f"{base}/leads/next", headers=headers, timeout=5)
        assert r_default.status_code == 200
        assert recorded_limits[-1] is None

        r_50 = req.get(f"{base}/leads/next?limit=50", headers=headers, timeout=5)
        assert r_50.status_code == 200
        assert recorded_limits[-1] == 50

        r_over = req.get(f"{base}/leads/next?limit=5000", headers=headers, timeout=5)
        assert r_over.status_code == 200
        assert recorded_limits[-1] == 1000

        for bad in ("0", "-3", "abc", ""):
            r_bad = req.get(f"{base}/leads/next?limit={bad}", headers=headers, timeout=5)
            assert r_bad.status_code == 400
            assert r_bad.json()["error"] == "bad_request"
    finally:
        server.shutdown()
        server.server_close()


def test_pg_store_fetch_next_batch_clamps_limit():
    import threading
    from types import SimpleNamespace

    ls = _load_lead_streamer()
    seen_params = []

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=None):
            seen_params.append(params)

        def fetchall(self):
            return []

    class FakeConn:
        def cursor(self):
            return FakeCursor()

        def rollback(self):
            pass

    store = ls.PgStore.__new__(ls.PgStore)
    store._config = SimpleNamespace(batch_size=300)
    store._conn_lock = threading.Lock()
    store._begin_batch_tx_locked = lambda: FakeConn()

    store.fetch_next_batch()
    store.fetch_next_batch(limit=45)
    store.fetch_next_batch(limit=0)
    store.fetch_next_batch(limit=2500)
    assert [p["batch_size"] for p in seen_params] == [300, 45, 1, 1000]
