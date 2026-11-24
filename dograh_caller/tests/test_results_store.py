import os

from openpyxl import Workbook, load_workbook

import config
from results_store import FIXED_COLUMNS, OTHER_LEAD_FIELDS, ResultsStore, SHEET_NAME


def test_newest_day_first_same_day_order_and_extra_tabs_preserved(tmp_path):
    path = tmp_path / "call_results.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = SHEET_NAME
    ws.append(["processed_at", "status", "phone_e164", "company"])
    # Older legacy flat layout (ascending dates, missing fixed columns)
    ws.append(["2026-11-17 09:00:00", "called", "+15125550101", "Old Co"])
    ws.append(["2026-11-18 11:00:00", "called", "+15125550102", "Mid Co 2"])
    ws.append(["2026-11-18 08:00:00", "called", "+15125550103", "Mid Co 1"])
    notes_ws = wb.create_sheet("Notes")
    notes_ws.append(["user_note"])
    notes_ws.append(["keep me"])
    wb.save(path)

    store = ResultsStore(path)
    assert store.save() is True

    store.append(
        {"processed_at": "2026-11-19 10:00:00", "status": "called", "phone_e164": "+15125550104"},
        lead={"company": "New Co 1"},
    )
    store.append(
        {"processed_at": "2026-11-19 10:05:00", "status": "not_interested", "phone_e164": "+15125550105"},
        lead={"company": "New Co 2"},
    )
    assert store.save() is True

    reloaded = load_workbook(path, read_only=True, data_only=True)
    try:
        assert reloaded.sheetnames == [SHEET_NAME, "Notes"]
        assert list(reloaded["Notes"].iter_rows(values_only=True)) == [("user_note",), ("keep me",)]

        rows = list(reloaded[SHEET_NAME].iter_rows(values_only=True))
        header = rows[0]
        for col in FIXED_COLUMNS:
            assert col in header
        company_idx = header.index("company")
        # Row order: 2026-11-19 -> New Co 1 -> New Co 2 -> 2026-11-18 -> Mid Co 1 -> Mid Co 2 -> 2026-11-17 -> Old Co
        col0 = [r[0] for r in rows[1:]]
        assert col0 == [
            "2026-11-19",
            "2026-11-19 10:00:00",
            "2026-11-19 10:05:00",
            "2026-11-18",
            "2026-11-18 08:00:00",
            "2026-11-18 11:00:00",
            "2026-11-17",
            "2026-11-17 09:00:00",
        ]
        assert rows[2][company_idx] == "New Co 1"
        assert rows[3][company_idx] == "New Co 2"
        assert rows[5][company_idx] == "Mid Co 1"
        assert rows[6][company_idx] == "Mid Co 2"
    finally:
        reloaded.close()


def test_unknown_date_does_not_multiply_across_reloads(tmp_path):
    path = tmp_path / "call_results.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = SHEET_NAME
    ws.append(FIXED_COLUMNS + ["company"])
    # Simulate previously stacked unknown date rows + one row with unreadable date
    ws.append(["unknown date"])
    ws.append(["unknown date"])
    ws.append(["not-a-date", "called", None, None, "+15125550101"])
    wb.save(path)

    for _ in range(3):
        store = ResultsStore(path)
        store.save()

    reloaded = load_workbook(path, read_only=True, data_only=True)
    try:
        rows = list(reloaded[SHEET_NAME].iter_rows(values_only=True))
        unknown_banners = [r for r in rows[1:] if r[0] == "unknown date"]
        assert len(unknown_banners) == 1
        assert len(rows) == 3  # header + 1 banner + 1 data row
    finally:
        reloaded.close()


def test_blank_header_cells_do_not_shift_data_and_banner_spans_new_columns(tmp_path):
    path = tmp_path / "call_results.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = SHEET_NAME
    ws.append(["processed_at", None, "status", "phone_e164"])
    ws.append(["2026-11-19 09:00:00", "ignored_orphan", "called", "+15125550101"])
    wb.save(path)

    store = ResultsStore(path)
    store.append(
        {"processed_at": "2026-11-19 10:00:00", "status": "called", "phone_e164": "+15125550102"},
        lead={"new_custom_col": "hello"},
    )
    assert store.save() is True

    reloaded = load_workbook(path)
    try:
        ws2 = reloaded[SHEET_NAME]
        header = [c.value for c in ws2[1]]
        assert "new_custom_col" in header
        si = header.index("status") + 1
        pi = header.index("phone_e164") + 1
        assert ws2.cell(row=3, column=si).value == "called"
        assert ws2.cell(row=3, column=pi).value == "+15125550101"
        # Date banner row 2 has grey fill across ALL columns including new_custom_col
        last_col_cell = ws2.cell(row=2, column=len(header))
        assert last_col_cell.fill.fill_type == "solid"
    finally:
        reloaded.close()


def test_lead_column_collision_and_max_columns_overflow(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RESULTS_MAX_LEAD_COLUMNS", 2)
    path = tmp_path / "call_results.xlsx"
    store = ResultsStore(path)

    store.append(
        {"processed_at": "2026-11-19 10:00:00", "status": "called", "phone_e164": "+15125550101"},
        lead={"status": "raw_status", "company": "Acme", "extra_a": "A", "extra_b": "B"},
    )
    assert store.save() is True

    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        rows = list(wb[SHEET_NAME].iter_rows(values_only=True))
        header = rows[0]
        assert "lead.status" in header
        assert "company" in header
        assert "extra_a" not in header
        assert "extra_b" not in header
        data_row = rows[2]
        assert data_row[header.index("status")] == "called"
        assert data_row[header.index("lead.status")] == "raw_status"
        assert '"extra_a": "A"' in data_row[header.index(OTHER_LEAD_FIELDS)]
        assert '"extra_b": "B"' in data_row[header.index(OTHER_LEAD_FIELDS)]
    finally:
        wb.close()


def test_atomic_save_cleans_temp_file_on_success_and_permission_error(tmp_path, monkeypatch):
    path = tmp_path / "call_results.xlsx"
    store = ResultsStore(path)
    store.append(
        {"processed_at": "2026-11-19 10:00:00", "status": "called", "phone_e164": "+15125550101"},
        lead={"company": "Original Co"},
    )
    assert store.save() is True
    assert list(tmp_path.glob("*.tmp*")) == []
    original_bytes = path.read_bytes()

    store.append(
        {"processed_at": "2026-11-19 10:05:00", "status": "called", "phone_e164": "+15125550102"},
        lead={"company": "Locked Co"},
    )

    def fail_replace(src, dst):
        raise PermissionError("locked by Excel")

    monkeypatch.setattr(os, "replace", fail_replace)
    assert store.save() is False
    assert list(tmp_path.glob("*.tmp*")) == []
    assert path.read_bytes() == original_bytes


def test_illegal_control_chars_cleaned_and_later_rows_save(tmp_path):
    path = tmp_path / "call_results.xlsx"
    store = ResultsStore(path)
    store.append(
        {"processed_at": "2026-11-23 10:00:00", "status": "called", "phone_e164": "+15125550101"},
        lead={"company\x0bname": "Fictional\x0bConcrete Co"},
    )
    assert store.save() is True

    store.append(
        {"processed_at": "2026-11-23 10:05:00", "status": "called", "phone_e164": "+15125550102"},
        lead={"company name": "Fictional Second Co"},
    )
    assert store.save() is True

    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        rows = list(wb[SHEET_NAME].iter_rows(values_only=True))
        header = rows[0]
        assert "company name" in header
        idx = header.index("company name")
        assert rows[2][idx] == "Fictional Concrete Co"
        assert rows[3][idx] == "Fictional Second Co"
    finally:
        wb.close()


def test_formula_like_strings_saved_as_plain_text(tmp_path):
    path = tmp_path / "call_results.xlsx"
    store = ResultsStore(path)
    store.append(
        {"processed_at": "2026-11-23 10:00:00", "status": "called", "phone_e164": "+15125550101"},
        lead={"company": "=SUM(1,2)", "notes": "=HYPERLINK(\"http://example.com\",\"click\")"},
    )
    assert store.save() is True

    wb = load_workbook(path)
    try:
        ws = wb[SHEET_NAME]
        header = [c.value for c in ws[1]]
        comp_cell = ws.cell(row=3, column=header.index("company") + 1)
        notes_cell = ws.cell(row=3, column=header.index("notes") + 1)
        assert comp_cell.data_type == "s"
        assert comp_cell.value == "=SUM(1,2)"
        assert notes_cell.data_type == "s"
        assert notes_cell.value == '=HYPERLINK("http://example.com","click")'
    finally:
        wb.close()


def test_default_interest_threshold_is_0_80():
    assert config.INTEREST_THRESHOLD == 0.80


def test_malformed_timestamp_under_date_banner_stays_under_banner_on_reload(tmp_path):
    path = tmp_path / "call_results.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = SHEET_NAME
    ws.append(FIXED_COLUMNS + ["company"])
    ws.append(["2026-11-22"])
    ws.append(["not-a-timestamp", "called", None, None, "+15125550155", None, None, None, None, None, None, None, "Banner Co"])
    wb.save(path)

    for _ in range(2):
        store = ResultsStore(path)
        assert store.save() is True

    reloaded = load_workbook(path, read_only=True, data_only=True)
    try:
        rows = list(reloaded[SHEET_NAME].iter_rows(values_only=True))
        col0 = [r[0] for r in rows[1:]]
        assert "unknown date" not in col0
        assert col0 == ["2026-11-22", "not-a-timestamp"]
    finally:
        reloaded.close()


def test_two_distinct_keys_cleaning_to_same_name_do_not_get_lead_prefix(tmp_path):
    path = tmp_path / "call_results.xlsx"
    store = ResultsStore(path)
    store.append(
        {"processed_at": "2026-11-23 10:00:00", "status": "called", "phone_e164": "+15125550166"},
        lead={"company\x0bname": "Clean One", "company name": "Clean Two"},
    )
    assert store.save() is True
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        header = list(next(wb[SHEET_NAME].iter_rows(values_only=True)))
        assert "lead.company name" not in header
        assert "company name" in header
        assert "company name_2" in header
    finally:
        wb.close()
