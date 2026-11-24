from pathlib import Path

from openpyxl import Workbook

import config
from results_store import SHEET_NAME
from tools import compare_scores


def _write_results(path: Path, headers: list[str], rows: list[list]):
    wb = Workbook()
    ws = wb.active
    ws.title = SHEET_NAME
    ws.append(headers)
    for r in rows:
        ws.append(r)
    wb.save(path)


def test_compare_scores_keeps_newest_day_row_per_phone(tmp_path, monkeypatch):
    services_yaml = tmp_path / "services.yaml"
    services_yaml.write_text(
        "services:\n"
        "  - id: draftliner\n"
        "    name: Draftliner\n"
        "    agent_id: 2\n"
        "    description: AEC\n"
        "  - id: paradise\n"
        "    name: Paradise Estimating\n"
        "    agent_id: 3\n"
        "    description: Est\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "SERVICES_FILE", services_yaml)

    call_results = tmp_path / "call_results.xlsx"
    score_only = tmp_path / "score_only.xlsx"

    # call_results has newest day on top, older day on bottom for same phone
    _write_results(
        call_results,
        ["processed_at", "phone_e164", "prob_draftliner", "prob_paradise"],
        [
            ["2026-11-23"],
            ["2026-11-23 09:00:00", "+15125550101", 0.91, 0.20],
            ["2026-11-20"],
            ["2026-11-20 09:00:00", "+15125550101", 0.15, 0.30],
        ],
    )
    _write_results(
        score_only,
        ["processed_at", "phone_e164", "prob_draftliner", "prob_paradise"],
        [
            ["2026-11-23 12:00:00", "+15125550101", 0.88, 0.10],
        ],
    )

    comps = compare_scores.compare_workbooks(score_only, call_results, threshold=0.80)
    assert len(comps) == 1
    assert comps[0]["old_probs"]["draftliner"] == 0.91
    assert comps[0]["old_best"] == "draftliner"
    assert comps[0]["old_qualified"] is True
    assert comps[0]["changed"] is False


def test_compare_scores_tie_breaks_by_services_yaml_order(tmp_path, monkeypatch):
    services_yaml = tmp_path / "services.yaml"
    # Declare zeta BEFORE alpha so alphabetical sorting would get the tie-break wrong
    services_yaml.write_text(
        "services:\n"
        "  - id: zeta\n"
        "    name: Zeta Service\n"
        "    agent_id: 2\n"
        "    description: First declared\n"
        "  - id: alpha\n"
        "    name: Alpha Service\n"
        "    agent_id: 3\n"
        "    description: Second declared\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "SERVICES_FILE", services_yaml)

    call_results = tmp_path / "call_results.xlsx"
    score_only = tmp_path / "score_only.xlsx"

    _write_results(
        call_results,
        ["processed_at", "phone_e164", "prob_alpha", "prob_zeta"],
        [["2026-11-23 09:00:00", "+15125550102", 0.85, 0.85]],
    )
    _write_results(
        score_only,
        ["processed_at", "phone_e164", "prob_alpha", "prob_zeta"],
        [["2026-11-23 10:00:00", "+15125550102", 0.85, 0.85]],
    )

    comps = compare_scores.compare_workbooks(score_only, call_results, threshold=0.80)
    assert len(comps) == 1
    assert comps[0]["old_best"] == "zeta"
    assert comps[0]["new_best"] == "zeta"
