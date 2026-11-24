from openpyxl import Workbook

from number_verifier import NumberVerifier, extract_labelled_numbers, load_called_numbers
from results_store import SHEET_NAME


def test_root_dict_category_or_type_does_not_hijack_phone():
    verifier = NumberVerifier()
    # Root dict has category="Home" and type="Business" plus a 10-digit ID before the real phone.
    lead = {
        "category": "Home",
        "type": "Business",
        "external_id": "5125550199",
        "source_url": "https://example.com/companies/5125550188",
        "phone": "(303) 555-0142",
    }
    res = verifier.verify(lead)
    assert res.ok is True
    assert res.e164 == "+13035550142"


def test_root_dict_without_phone_is_not_matched_by_category_home():
    verifier = NumberVerifier()
    lead = {
        "category": "Home",
        "type": "Business",
        "external_id": "5125550199",
        "notes": "Call 512-555-0177",
    }
    res = verifier.verify(lead)
    assert res.ok is False
    assert res.reason == "dropped_no_phone"


def test_root_dict_category_fax_does_not_drop_real_phone():
    verifier = NumberVerifier()
    lead = {
        "category": "Fax",
        "company": "Acme Printing",
        "phone": "(512) 555-0101",
    }
    res = verifier.verify(lead)
    assert res.ok is True
    assert res.e164 == "+15125550101"


def test_nested_typed_entries_extract_phone_and_exclude_fax():
    verifier = NumberVerifier()
    lead = {
        "contacts": [
            {"type": "fax", "number": "(512) 555-0102", "id": "5125550190"},
            {"label": "Main", "value": "(512) 555-0101", "id": "5125550191"},
        ],
        "phones": ["(512) 555-0102", "(512) 555-0103"],
    }
    res = verifier.verify(lead)
    assert res.ok is True
    # 0101 comes from contacts[1], 0102 was excluded globally because it is marked as fax
    assert res.e164 == "+15125550101"

    # Even if 0102 were the only number in phones[], it must still be dropped as fax_only
    lead2 = {
        "contacts": [{"type": "fax", "value": "(512) 555-0102"}],
        "phone": "(512) 555-0102",
    }
    res2 = verifier.verify(lead2)
    assert res2.ok is False
    assert res2.reason == "dropped_fax_only"


def test_nested_label_entry_skips_non_phone_metadata_keys():
    verifier = NumberVerifier()
    lead = {
        "contacts": [
            {
                "label": "Office",
                "id": "5125550188",
                "zip": "5125550177",
                "value": "(512) 555-0104",
            }
        ]
    }
    res = verifier.verify(lead)
    assert res.ok is True
    assert res.e164 == "+15125550104"


def test_fax_label_regex_handles_initial_names_vs_f_prefix():
    labelled_name, err = extract_labelled_numbers("F. Smith: (512) 555-0101")
    assert err is None
    assert len(labelled_name) == 1
    assert labelled_name[0][1] is False

    for text in [
        "F: (512) 555-0102",
        "F# (512) 555-0102",
        "F. (512) 555-0102",
        "(512) 555-0102 (F.)",
        "512-555-0102 (fax)",
        "Tel/Fax: (512) 555-0102",
    ]:
        labelled, err = extract_labelled_numbers(text)
        assert err is None
        assert len(labelled) == 1
        assert labelled[0][1] is True, f"Expected fax=True for {text!r}"


def test_region_special_vanity_and_float_values():
    verifier = NumberVerifier()
    assert verifier.verify({"phone": "+1 416 555 0107"}).reason == "dropped_not_us (CA)"
    assert verifier.verify({"phone": "1-900-555-0199"}).reason.startswith("dropped_special_number")

    vanity = verifier.verify({"phone": "1-800-FLOWERS"})
    assert vanity.ok is True
    assert vanity.e164 == "+18003569377"

    # Excel numeric float + duplicate protection in same run
    first = verifier.verify({"phone": 15125550125.0})
    assert first.ok is True
    assert first.e164 == "+15125550125"
    assert first.location == "Texas"
    assert first.time_zone == "America/Chicago"

    dup = verifier.verify({"phone": "(512) 555-0125"})
    assert dup.ok is False
    assert dup.reason == "dropped_duplicate"


def test_load_called_numbers_reads_results_sheet_and_fallbacks(tmp_path):
    main_xlsx = tmp_path / "call_results.xlsx"
    wb = Workbook()
    ws_summary = wb.active
    ws_summary.title = "Summary"
    ws_summary.append(["note"])
    ws_summary.append(["User opened workbook and saved on Summary tab"])

    ws_results = wb.create_sheet(SHEET_NAME)
    ws_results.append(["processed_at", "status", "phone_e164"])
    ws_results.append(["2026-11-19 10:00:00", "called", "+15125550101"])
    ws_results.append(["2026-11-19 10:01:00", "call_timeout", "+15125550102"])
    ws_results.append(["2026-11-19 10:02:00", "not_interested", "+15125550103"])
    ws_results.append(["2026-11-19 10:03:00", "call_failed", "+15125550104"])
    wb.save(main_xlsx)

    fallback_xlsx = tmp_path / "call_results_20261119_120000.xlsx"
    wb2 = Workbook()
    ws2 = wb2.active
    ws2.title = SHEET_NAME
    ws2.append(["processed_at", "status", "phone_e164"])
    ws2.append(["2026-11-19 12:00:00", "called", "+15125550105"])
    wb2.save(fallback_xlsx)

    called = load_called_numbers(main_xlsx)
    assert called == {"+15125550101", "+15125550102", "+15125550105"}

    verifier = NumberVerifier(already_called=called)
    assert verifier.verify({"phone": "(512) 555-0105"}).reason == "dropped_already_called"
    assert verifier.verify({"phone": "(512) 555-0103"}).ok is True
    assert verifier.verify({"phone": "(512) 555-0104"}).ok is True

    verifier.mark_called("+15125550103")
    assert verifier.verify({"phone": "(512) 555-0103"}).reason == "dropped_already_called"


def test_fax_entries_with_many_fields_are_never_called():
    # Unstructured phone entries often carry ids, sources, dates and flags.
    # Fax typing must work whatever the entry size (regression: <= 6 fields only).
    fax_entry = {
        "type": "fax", "number": "512-555-0102", "id": "p1", "source": "bbb",
        "is_primary": False, "created_at": "2026-11-01", "verified": False,
    }
    office = {"type": "office", "number": "512-555-0101"}

    res = NumberVerifier().verify({"phones": [fax_entry, office]})
    assert res.ok is True and res.e164 == "+15125550101"

    res = NumberVerifier().verify({"phones": [fax_entry]})
    assert res.ok is False and res.reason == "dropped_fax_only"

    res = NumberVerifier().verify({"contact_numbers": [fax_entry, {"label": "Main", "value": "512-555-0101"}]})
    assert res.ok is True and res.e164 == "+15125550101"


def test_nested_category_fax_entry_is_never_called():
    res = NumberVerifier().verify({"phones": [{"category": "fax", "number": "512-555-0103"}]})
    assert res.ok is False and res.reason == "dropped_fax_only"

    res = NumberVerifier().verify({"phones": [{"category": "fax", "number": "512-555-0103"},
                                              {"category": "main", "number": "512-555-0104"}]})
    assert res.ok is True and res.e164 == "+15125550104"
