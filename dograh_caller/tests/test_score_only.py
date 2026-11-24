import asyncio
from openpyxl import load_workbook
import pytest

import config
import main as caller_main
from decision_maker import Decision
from pipeline import LeadPipeline


def test_score_only_never_constructs_dograh_client_and_marks_scored_qualified(tmp_path, monkeypatch):
    services_yaml = tmp_path / "services.yaml"
    services_yaml.write_text(
        "services:\n"
        "  - id: paradise\n"
        "    name: Paradise Estimating\n"
        "    agent_id: 3\n"
        "    description: Construction estimating\n",
        encoding="utf-8",
    )
    out_xlsx = tmp_path / "score_only.xlsx"

    class ExplodingDograh:
        def __init__(self, *args, **kwargs):
            raise AssertionError("DograhClient must not be constructed in score-only mode")

    class FakeDecider:
        def __init__(self, services, threshold, api_key, model):
            self.services = services

        async def decide(self, lead):
            return Decision(
                probabilities={"paradise": 0.91},
                service=self.services[0],
                probability=0.91,
            )

        async def close(self):
            pass

    monkeypatch.setattr("pipeline.DograhClient", ExplodingDograh)
    monkeypatch.setattr("pipeline.DecisionMaker", FakeDecider)

    async def run():
        async with LeadPipeline(results_file=out_xlsx, services_file=services_yaml, score_only=True) as p:
            await p.submit({"company": "Fictional Concrete Co", "phone": "(512) 555-0101"})
            return p.stats

    stats = asyncio.run(run())
    assert stats["scored_qualified"] == 1
    assert out_xlsx.exists()

    wb = load_workbook(out_xlsx, read_only=True, data_only=True)
    try:
        rows = list(wb["results"].iter_rows(values_only=True))
        header = rows[0]
        assert rows[2][header.index("status")] == "scored_qualified"
        assert rows[2][header.index("interested_service")] == "Paradise Estimating"
    finally:
        wb.close()


def test_score_only_run_campaigns_raises(tmp_path):
    p = LeadPipeline(results_file=tmp_path / "score.xlsx", score_only=True)
    with pytest.raises(RuntimeError, match="disabled in score-only mode"):
        asyncio.run(p._run_campaigns({}))


def test_score_only_does_not_create_or_modify_call_results_xlsx(tmp_path, monkeypatch, capsys):
    fake_results = tmp_path / "call_results.xlsx"
    fake_transfers = tmp_path / "transferred_calls.xlsx"
    monkeypatch.setattr(config, "RESULTS_FILE", fake_results)
    monkeypatch.setattr(config, "TRANSFERS_FILE", fake_transfers)
    monkeypatch.setattr(config, "PROJECT_DIR", tmp_path)

    services_yaml = tmp_path / "services.yaml"
    services_yaml.write_text(
        "services:\n"
        "  - id: paradise\n"
        "    name: Paradise Estimating\n"
        "    agent_id: 3\n"
        "    description: Construction estimating\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "SERVICES_FILE", services_yaml)

    class ExplodingDograh:
        def __init__(self, *args, **kwargs):
            raise AssertionError("DograhClient must not be constructed in score-only mode")

    class FakeDecider:
        def __init__(self, services, threshold, api_key, model):
            self.services = services

        async def decide(self, lead):
            return Decision(probabilities={"paradise": 0.85}, service=self.services[0], probability=0.85)

        async def close(self):
            pass

    monkeypatch.setattr("pipeline.DograhClient", ExplodingDograh)
    monkeypatch.setattr("pipeline.DecisionMaker", FakeDecider)

    with pytest.raises(ValueError, match="must not write to call_results.xlsx"):
        LeadPipeline(results_file=fake_results, score_only=True)

    rc = caller_main.run(
        [{"company": "Fictional Builders", "phone": "(512) 555-0102"}],
        score_only=True,
    )
    assert rc == 0
    assert "SCORE ONLY: no calls will be placed" in capsys.readouterr().out
    assert not fake_results.exists()
    assert not fake_transfers.exists()
    all_score_files = sorted((tmp_path / "output").glob("score_only_*.xlsx"))
    tracker_files = [p for p in all_score_files if p.stem.endswith("_tracker")]
    result_files = [p for p in all_score_files if not p.stem.endswith("_tracker")]
    assert len(result_files) == 1
    assert len(tracker_files) == 1
    assert tracker_files[0].stem == f"{result_files[0].stem}_tracker"
