"""
Example: feeding leads (dicts) into the pipeline from your own Python code.
This is how the AI-powered scraper will plug in later.

Runs LIVE: qualified leads are called. The numbers below are fictional
(555-01xx) - replace them with real leads before running.

Run from the dograh_caller folder:
    python examples/from_python.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import LeadPipeline, run_leads_sync  # noqa: E402

LEADS = [
    {"company": "Summit Concrete Co", "category": "Concrete subcontractor",
     "description": "Commercial flatwork and foundations, bids public work", "location": "Denver, CO",
     "phone": "(303) 555-0142"},
    {"company": "Sunny Side Cafe", "category": "Restaurant",
     "description": "Breakfast and brunch spot", "location": "Tampa, FL", "phone": "813-555-0177"},
]


# Option 1: a list (or any iterable) of dicts, blocking call
def batch_example() -> None:
    run_leads_sync(LEADS)


# Option 2: stream leads one by one, e.g. as a scraper produces them
async def stream_example() -> None:
    async with LeadPipeline() as pipeline:
        for lead in LEADS:              # replace with: async for lead in scraper()
            await pipeline.submit(lead)  # verifier -> Gemini -> Dograh call -> results.xlsx


if __name__ == "__main__":
    asyncio.run(stream_example())
