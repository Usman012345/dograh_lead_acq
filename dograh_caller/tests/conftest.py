import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _isolate_real_output(tmp_path_factory, monkeypatch):
    """
    Tests never read or write the real output/ folder: the pipeline also reads the
    main call_results.xlsx (already-called numbers) when a run writes elsewhere.
    A test that needs other paths still sets them itself.
    """
    import config

    out = tmp_path_factory.mktemp("isolated_output")
    monkeypatch.setattr(config, "RESULTS_FILE", out / "call_results.xlsx")
    monkeypatch.setattr(config, "TRANSFERS_FILE", out / "transferred_calls.xlsx")
    monkeypatch.setattr(config, "STOP_SYNC_SECONDS", 0.0)
