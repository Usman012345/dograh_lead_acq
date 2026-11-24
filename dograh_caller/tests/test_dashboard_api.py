import json
import re

from fastapi.testclient import TestClient
import pytest

import config
import dashboard_server
from dashboard.app import create_app
from tools.make_demo_tracker import generate_demo_tracker


@pytest.fixture
def demo_tracker(tmp_path):
    path = tmp_path / "demo_tracker.xlsx"
    generate_demo_tracker(path)
    return path


@pytest.fixture
def client(demo_tracker):
    app = create_app(tracker_path=demo_tracker, start_watcher=False)
    with TestClient(app) as c:
        yield c


def test_make_demo_tracker_refuses_live_tracker_path(tmp_path, monkeypatch):
    live = tmp_path / "pipeline_tracker.xlsx"
    monkeypatch.setattr(config, "PIPELINE_TRACKER_FILE", live)
    with pytest.raises(ValueError, match="Refusing to overwrite live tracker"):
        generate_demo_tracker(live)


def test_exact_columns_for_every_view(client):
    expected_prefix = {
        "incoming": ["Lead ID", "Name", "Phone", "Source", "Received At"],
        "number_verifier": ["Lead ID", "Name", "Phone", "Result", "Reason"],
        "decision_maker": ["Lead ID", "Name", "Best Match", "Probability", "Result"],
        "non_qualified": ["Lead ID", "Why It Was Not Qualified"],
        "qualified": ["Lead ID", "Qualified Service", "Why It Qualified"],
        "calling_agent": ["Lead ID", "Name", "Service", "Agent", "Campaign", "Call Progress"],
        "call_results": [
            "Lead ID", "Name", "Service", "Call Status", "Outcome", "Duration",
            "Attempts", "Transferred", "Tags", "Transcript", "Recording",
        ],
        "transferred": ["Lead ID", "Name", "Service", "Transferred At", "Duration", "Outcome"],
    }

    for view, labels in expected_prefix.items():
        r = client.get(f"/api/leads?view={view}")
        assert r.status_code == 200
        assert r.headers.get("cache-control") == "no-store"
        body = r.json()
        got_labels = [c["label"] for c in body["columns"]]
        if view == "call_results":
            assert got_labels[:len(labels)] == labels
            assert any(" > " in lbl for lbl in got_labels[len(labels):])
        else:
            assert got_labels == labels


def test_79_and_80_leads_land_in_correct_views(client):
    nq_search = client.get("/api/leads?view=non_qualified&q=Borderline%20Roofing%2079").json()
    assert nq_search["total"] == 1
    lid_79 = nq_search["rows"][0]["lead_id"]

    q_search = client.get("/api/leads?view=qualified&q=Horizon%20Concrete%2080").json()
    assert q_search["total"] == 1
    lid_80 = q_search["rows"][0]["lead_id"]

    assert client.get(f"/api/leads/{lid_79}").json()["decision"]["result"] == "non_qualified"
    assert client.get(f"/api/leads/{lid_80}").json()["decision"]["result"] == "qualified"
    assert client.get("/api/leads?view=qualified&q=Borderline%20Roofing%2079").json()["total"] == 0
    assert client.get("/api/leads?view=non_qualified&q=Horizon%20Concrete%2080").json()["total"] == 0


def test_summary_invariants_hold_on_demo_data(client):
    r = client.get("/api/summary")
    assert r.status_code == 200
    data = r.json()
    assert data["tracker_found"] is True
    assert data["stale"] is False
    nodes = data["nodes"]

    v = nodes["number_verifier"]
    d = nodes["decision_maker"]
    nq = nodes["non_qualified"]
    q = nodes["qualified"]
    ca = nodes["calling_agent"]
    cr = nodes["call_results"]
    tr = nodes["transferred"]

    assert nodes["incoming"]["total"] == v["passed"] + v["rejected"] + v["errors"] + v["waiting"]
    assert v["passed"] == q["total"] + nq["total"] + d["errors"] + d["waiting"]
    assert d["reviewed"] == q["total"] + nq["total"]
    assert q["total"] == (ca["waiting"] + ca["calling"] + ca["not_sent"] + ca["not_called"] + ca["interrupted"] + cr["total"])
    assert cr["total"] == cr["called"] + cr["failed"] + cr["timeout"]
    assert tr["total"] <= cr["total"]
    assert sum(q["by_service"].values()) == q["total"]


def test_search_sort_filters_paging_and_404(client):
    # Numeric sort on lead_id
    asc = client.get("/api/leads?view=incoming&sort=lead_id&order=asc&page_size=200").json()
    ids_asc = [r["lead_id"] for r in asc["rows"]]
    desc = client.get("/api/leads?view=incoming&sort=lead_id&order=desc&page_size=200").json()
    ids_desc = [r["lead_id"] for r in desc["rows"]]
    assert ids_asc == list(reversed(ids_desc))
    assert ids_asc[0] == "L000001"

    # Paging
    p1 = client.get("/api/leads?view=incoming&page=1&page_size=10").json()
    p2 = client.get("/api/leads?view=incoming&page=2&page_size=10").json()
    assert len(p1["rows"]) == 10
    assert p1["rows"][0]["lead_id"] != p2["rows"][0]["lead_id"]

    # Service filter on qualified
    q_draft = client.get("/api/leads?view=qualified&service=draftliner").json()
    assert q_draft["total"] > 0
    assert all(r["assigned_service"] == "Draftliner" for r in q_draft["rows"])

    # View filters
    assert client.get("/api/leads?view=number_verifier&filter=rejected").json()["total"] == 8
    assert client.get("/api/leads?view=number_verifier&filter=errors").json()["total"] == 1
    assert client.get("/api/leads?view=decision_maker&filter=errors").json()["total"] == 2
    assert client.get("/api/leads?view=calling_agent&filter=not_sent").json()["total"] == 2
    assert client.get("/api/leads?view=call_results&filter=timeout").json()["total"] == 1

    # Search
    s = client.get("/api/leads?view=incoming&q=Fictional%20Pinnacle").json()
    assert s["total"] == 1

    # 404
    assert client.get("/api/leads/L999999").status_code == 404


def test_public_access_token_absent_and_javascript_links_removed(client):
    cr = client.get("/api/leads?view=call_results&page_size=200").json()
    assert "public_access_token" not in json.dumps(cr)
    for row in cr["rows"]:
        assert not str(row.get("transcript_url") or "").lower().startswith("javascript:")
        assert not str(row.get("recording_url") or "").lower().startswith("javascript:")

    # Check every lead detail
    inc = client.get("/api/leads?view=incoming&page_size=200").json()
    for row in inc["rows"]:
        detail = client.get(f"/api/leads/{row['lead_id']}").json()
        dumped = json.dumps(detail)
        assert "public_access_token" not in dumped
        assert "SECRET-DOGRAH-TOKEN" not in dumped
        assert "javascript:" not in dumped.lower()


def test_write_methods_return_405(client):
    for method in ("post", "put", "patch", "delete"):
        for path in ("/api/summary", "/api/leads?view=incoming", "/"):
            resp = getattr(client, method)(path)
            assert resp.status_code == 405


def test_missing_tracker_returns_empty_valid_response(tmp_path):
    app = create_app(tracker_path=tmp_path / "does_not_exist.xlsx", start_watcher=False)
    with TestClient(app) as c:
        h = c.get("/api/health").json()
        assert h == {"status": "ok", "tracker_found": False, "stale": False}
        s = c.get("/api/summary").json()
        assert s["tracker_found"] is False
        assert s["nodes"]["incoming"]["total"] == 0
        leads = c.get("/api/leads?view=incoming").json()
        assert leads["rows"] == []
        assert leads["total"] == 0


def test_non_loopback_warning_prints(capsys):
    assert dashboard_server.warn_if_non_loopback("127.0.0.1") is False
    assert dashboard_server.warn_if_non_loopback("localhost") is False
    assert dashboard_server.warn_if_non_loopback("::1") is False
    assert capsys.readouterr().out == ""

    assert dashboard_server.warn_if_non_loopback("0.0.0.0") is True
    out = capsys.readouterr().out
    assert "WARNING: this dashboard has no login" in out


def test_alphanumeric_query_does_not_match_random_phone_numbers(tmp_path):
    from number_verifier import Verification
    from pipeline_tracker import PipelineTracker

    path = tmp_path / "tracker.xlsx"
    t = PipelineTracker(path)
    t.open()
    try:
        l_phone = t.register({"company": "Austin Concrete Co", "phone": "(512) 555-0101"}, "python")
        t.record_verification(l_phone, Verification(True, "(512) 555-0101", "+15125550101", "ok"))
        l_suite = t.register({"company": "Suite 512 Builders", "phone": "(713) 555-0202"}, "python")
        t.record_verification(l_suite, Verification(True, "(713) 555-0202", "+17135550202", "ok"))
        t.save()
    finally:
        t.close()

    with TestClient(create_app(tracker_path=path, start_watcher=False)) as c:
        res = c.get("/api/leads", params={"view": "incoming", "q": "Suite 512"}).json()
        assert res["total"] == 1
        assert res["rows"][0]["lead_id"] == l_suite

        # Pure digits / phone query still matches the (512) phone number
        res_digits = c.get("/api/leads", params={"view": "incoming", "q": "512"}).json()
        assert {r["lead_id"] for r in res_digits["rows"]} == {l_phone, l_suite}


def test_filter_counts_updates_with_service_filter(client):
    all_ca = client.get("/api/leads?view=calling_agent").json()
    draft_ca = client.get("/api/leads?view=calling_agent&service=draftliner").json()
    paradise_ca = client.get("/api/leads?view=calling_agent&service=paradise").json()

    assert "filter_counts" in all_ca
    assert draft_ca["filter_counts"][""] + paradise_ca["filter_counts"][""] == all_ca["filter_counts"][""]
    assert draft_ca["filter_counts"]["not_sent"] + paradise_ca["filter_counts"]["not_sent"] == all_ca["filter_counts"]["not_sent"]

    # Filtering by status still preserves service-scoped filter_counts across chips
    draft_not_sent = client.get("/api/leads?view=calling_agent&service=draftliner&filter=not_sent").json()
    assert draft_not_sent["filter_counts"] == draft_ca["filter_counts"]
    assert draft_not_sent["total"] == draft_ca["filter_counts"]["not_sent"]


def test_static_bundle_serves_index_assets_and_blocks_traversal(client):
    index = client.get("/")
    assert index.status_code == 200
    assert "text/html" in index.headers.get("content-type", "")
    assets = re.findall(r'(?:src|href)="(/assets/[^"]+)"', index.text)
    assert assets, "built index.html must reference /assets/* files"
    for asset_path in assets:
        resp = client.get(asset_path)
        assert resp.status_code == 200, f"missing static asset: {asset_path}"
        assert len(resp.content) > 0

    # Path traversal out of dashboard/static must never succeed
    assert client.get("/..%2F.env").status_code == 404
    assert client.get("/assets/../../.env").status_code == 404
