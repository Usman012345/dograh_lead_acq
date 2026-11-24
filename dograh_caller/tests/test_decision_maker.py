import asyncio
from types import SimpleNamespace

import config
from decision_maker import DecisionMaker, Service, lead_state


def test_booleans_preserved_when_descriptive_and_filtered_when_noise():
    lead = {
        "company": "Summit Concrete Co",
        "Accredited": True,
        "is_accredited": True,
        "licensed": False,
        "email_verified": True,
        "is_primary": True,
        "is_headquarters": True,
        "auto_enriched": False,
    }
    state = lead_state(lead)
    assert state["company"] == "Summit Concrete Co"
    assert state["Accredited"] == "Yes"
    assert state["is_accredited"] == "Yes"
    assert state["licensed"] == "No"
    assert "email_verified" not in state
    assert "is_primary" not in state
    assert "is_headquarters" not in state
    assert "auto_enriched" not in state


def test_refined_noise_words_keep_descriptive_compounds_and_drop_bookkeeping():
    lead = {
        "company": "Apex Builders",
        "primary_category": "General Contractors",
        "primary_trade": "Concrete",
        "site_work": "Excavation and grading",
        "job_site": "Commercial",
        "headquarters_city": "Austin",
        "business_activities": "Commercial tenant build-outs",
        # Noise fields that should still be removed
        "is_primary": "true",
        "is_headquarters": "true",
        "last_activity": "emailed yesterday",
        "Activity Timeline": "Called 2x",
        "Recent Action Logged": "Imported",
        "site": "https://apex.example",
        "web_site": "apex.example",
        "Email Verified": "Yes",
        "Lead Status": "New",
        "Source URL": "https://bbb.org/apex",
    }
    state = lead_state(lead)
    for kept in (
        "company",
        "primary_category",
        "primary_trade",
        "site_work",
        "job_site",
        "headquarters_city",
        "business_activities",
    ):
        assert kept in state, f"Expected {kept!r} to be kept in lead_state"

    for dropped in (
        "is_primary",
        "is_headquarters",
        "last_activity",
        "Activity Timeline",
        "Recent Action Logged",
        "site",
        "web_site",
        "Email Verified",
        "Lead Status",
        "Source URL",
    ):
        assert dropped not in state, f"Expected {dropped!r} to be filtered from lead_state"


def test_lead_state_caps_merged_list_fields_and_respects_state_budget(monkeypatch):
    monkeypatch.setattr(config, "LEAD_MAX_FIELD_CHARS", 50)
    monkeypatch.setattr(config, "LEAD_MAX_STATE_CHARS", 400)

    lead = {
        "company": "Bridgeworks Engineering",
        "services": [{"name": "A" * 35}, {"name": "B" * 35}, {"name": "C" * 35}],
        "description": "D" * 300,
        "extra_note": "E" * 300,
    }
    state = lead_state(lead)
    assert state["company"] == "Bridgeworks Engineering"
    assert len(state["services > name"]) <= 54  # 50 + " ..."
    assert state["services > name"].endswith(" ...")
    total_cost = sum(len(k) + len(str(v)) + 6 for k, v in state.items())
    assert total_cost <= 400


def test_decide_short_circuits_empty_state(monkeypatch):
    called = False

    class FakeModels:
        async def generate_content(self, **kwargs):
            nonlocal called
            called = True
            return SimpleNamespace(text="{}")

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.aio = SimpleNamespace(models=FakeModels(), aclose=self._aclose)

        async def _aclose(self):
            pass

    monkeypatch.setattr("decision_maker.genai.Client", FakeClient)
    svc = Service(
        id="draftliner",
        name="Draftliner",
        agent_id=2,
        description="AEC back office",
        good_fit="Architects",
        not_a_fit="Restaurants",
    )
    dm = DecisionMaker([svc], 0.80, "fake-key", "gemini-3.8-flash")

    async def run():
        try:
            # Lead containing only noise/contact fields
            d = await dm.decide({"phone": "512-555-0101", "email": "a@example.com", "id": 123})
            assert d.qualified is False
            assert d.probabilities == {}
            assert "no business details" in d.summary.lower()
            assert "Non-Qualified" in d.verdict
            assert called is False
        finally:
            await dm.close()

    asyncio.run(run())
