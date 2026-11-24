"""Transferred calls: detection from Dograh's run data and the separate transfers spreadsheet."""

import pytest
from openpyxl import load_workbook

from dograh_client import is_transferred
from results_store import (TRANSFER_COLUMNS, TRANSFERS_SHEET_NAME, ResultsStore, transfer_row)


def run(disposition=None, tags=None, **extra):
    g = {"call_disposition": disposition, "mapped_call_disposition": disposition, "call_tags": tags or []}
    g.update(extra)
    return {"id": 7, "gathered_context": g}


@pytest.mark.parametrize("r", [
    run("transfer_call"),                                   # phone transfer (Twilio etc.)
    run("call_transferred"),                                # external PBX transfer
    run("user_hangup", external_pbx_transferred=True),      # PBX flag wins over a late hangup
    run("transferred_to_sales"),                            # disposition set on the transfer tool
    run("user_qualified", tags=["user_speech", "transfer_call"]),
    run(None, transfer_state="complete"),
])
def test_transfers_are_detected(r):
    assert is_transferred(r)


@pytest.mark.parametrize("r", [
    run("user_qualified"), run("user_hangup"), run("no-answer"), run("busy"),
    run("transfer_failed"), run("user_hangup", tags=["transfer_failed"]),
    {"id": 1}, {},
])
def test_other_calls_are_not_transfers(r):
    assert not is_transferred(r)


def test_custom_disposition_from_settings(monkeypatch):
    import config
    assert not is_transferred(run("sales_handoff"))
    monkeypatch.setattr(config, "TRANSFER_DISPOSITIONS", frozenset({"transfer_call", "sales_handoff"}))
    assert is_transferred(run("sales_handoff"))


def _row():
    return {"processed_at": "2026-11-20 10:00:00", "status": "called", "phone_e164": "+15125550101",
            "interested_service": "Draftliner", "campaign_name": "Draftliner_2026-11-20", "agent_name": "Danial",
            "call_disposition": "transfer_call", "call_duration_s": 93.0, "transcript_url": "https://t/1",
            "recording_url": "https://r/1", "dograh_run_id": 7, "call_transferred": True,
            "dograh_response": run("transfer_call", extracted_variables={"Name": "Pat Doe", "Email": "pat@x.test"},
                                   provider="twilio", call_id="abc", budget="50k")}


def test_transfer_row_puts_contact_details_first_without_duplicates():
    lead = {"lead_id": "L1", "Company Name": "Summit Concrete", "contact_name": "Pat Doe",
            "Email": "info@summit.test", "website": "https://summit.test", "city": "Austin", "state": "TX",
            "category": "Concrete", "phone": "(512) 555-0101", "rating": "A+"}
    out, rest = transfer_row(_row(), lead)
    assert out["business"] == "Summit Concrete" and out["contact_name"] == "Pat Doe"
    assert out["email"] == "info@summit.test" and out["phone_called"] == "+15125550101"
    assert out["lead_id"] == "L1" and out["transferred_at"] == "2026-11-20 10:00:00"
    assert out["call_duration_s"] == 93.0 and out["transcript_url"] == "https://t/1"
    # what the agent captured, without Dograh bookkeeping
    assert "Name: Pat Doe" in out["collected_by_agent"] and "budget: 50k" in out["collected_by_agent"]
    assert "provider" not in out["collected_by_agent"] and "call_id" not in out["collected_by_agent"]
    # fields used above are not repeated; the rest still come along
    assert "Company Name" not in rest and "Email" not in rest
    assert rest == {"phone": "(512) 555-0101", "rating": "A+"}


def test_transfers_file_layout(tmp_path):
    path = tmp_path / "transferred_calls.xlsx"
    store = ResultsStore(path, fixed_columns=TRANSFER_COLUMNS, sheet_name=TRANSFERS_SHEET_NAME,
                         time_column="transferred_at")
    assert store.save() and not path.exists()          # nothing transferred yet -> no file
    out, rest = transfer_row(_row(), {"company": "Summit Concrete", "rating": "A+"})
    store.append(out, rest)
    assert store.save()
    wb = load_workbook(path)
    ws = wb[TRANSFERS_SHEET_NAME]
    rows = list(ws.iter_rows(values_only=True))
    header = list(rows[0])
    assert header[:4] == ["transferred_at", "phone_called", "business", "contact_name"]
    assert "rating" in header                            # remaining lead info as extra columns
    assert rows[1][0] == "2026-11-20"                    # grouped by day like the results file
    data = dict(zip(header, rows[2]))
    assert data["business"] == "Summit Concrete" and data["rating"] == "A+"

    # reopened later: appends, keeps the earlier row
    again = ResultsStore(path, fixed_columns=TRANSFER_COLUMNS, sheet_name=TRANSFERS_SHEET_NAME,
                         time_column="transferred_at")
    out2, rest2 = transfer_row({**_row(), "processed_at": "2026-11-20 11:00:00"}, {"company": "Beta Roofing"})
    again.append(out2, rest2)
    assert again.save()
    names = [r[header.index("business")] for r in load_workbook(path)[TRANSFERS_SHEET_NAME].iter_rows(
        min_row=3, values_only=True)]
    assert names == ["Summit Concrete", "Beta Roofing"]
