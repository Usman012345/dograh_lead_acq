"""Streamer setup fixes: schema check, PUBLIC_BASE_URL override, log redaction, caller reaction."""

import json

import pytest

pytest.importorskip("psycopg", reason="lead streamer tests need psycopg (pip install -r requirements-dev.txt)")

from tests.test_main_and_lead_streamer import _load_lead_streamer  # noqa: E402

ls = _load_lead_streamer()


def _all_columns():
    return [(t, c) for t, cols in ls.REQUIRED_COLUMNS.items() for c in cols]


def test_schema_complete_has_nothing_missing():
    assert ls.missing_columns(_all_columns()) == []


def test_schema_reports_missing_column_and_missing_table():
    present = [(t, c) for t, c in _all_columns() if (t, c) != ("leads", "content_version") and t != "locations"]
    assert ls.missing_columns(present) == ["leads.content_version", "locations (table)"]


def test_schema_mismatch_maps_to_its_own_error_code():
    assert ls._unavailable(ls.SchemaMismatchError("x")).code == "schema_mismatch"
    assert ls._unavailable(OSError("down")).code == "database_unavailable"


@pytest.fixture
def base_env(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres:4321@127.0.0.1:5432/dataops")
    monkeypatch.setenv("STREAM_TOKEN", "k" * 40)
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("BATCH_SIZE", raising=False)
    # read no .env from the real folder
    monkeypatch.setattr(ls, "_read_dotenv", lambda path: {})
    return monkeypatch


def test_defaults_point_at_subdomain_with_300_per_batch(base_env):
    cfg = ls.load_config()
    assert cfg.ping_url == "https://ai-sdr.grobots.ai/leads/next"
    assert cfg.batch_size == 300


def test_empty_public_base_url_turns_host_check_off(base_env):
    base_env.setenv("PUBLIC_BASE_URL", "")
    cfg = ls.load_config()
    assert cfg.public_base_url == ""


def test_empty_public_base_url_in_dotenv_turns_host_check_off(base_env):
    base_env.setattr(ls, "_read_dotenv", lambda path: {"PUBLIC_BASE_URL": ""})
    assert ls.load_config().public_base_url == ""


def test_short_password_does_not_garble_logs_but_url_is_hidden():
    logger = ls.StructuredLogger(20, ("4321", "k" * 40))
    text = logger.sanitize_error(Exception(
        'connection to postgresql://postgres:4321@127.0.0.1:5432/dataops failed after 4321 ms, token ' + "k" * 40))
    assert "postgres:4321@" not in text and "***" in text
    assert "after 4321 ms" in text          # short password no longer replaced everywhere
    assert "k" * 40 not in text             # long secrets still are


def test_caller_stops_on_schema_mismatch():
    import http.server
    import threading

    import pull_leads

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"status": "error", "error": "schema_mismatch", "retry_after_seconds": 30}).encode()
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        r = pull_leads.ping(f"http://127.0.0.1:{srv.server_address[1]}", "t" * 40, 120)
    finally:
        srv.shutdown()
        srv.server_close()
    assert r.kind == pull_leads.FATAL and "--check" in r.message
