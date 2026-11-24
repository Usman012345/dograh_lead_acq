# How to add a new service

A **service** is something we sell (today: Draftliner and Paradise Estimating).
For every lead, Gemini scores how likely it is to buy each service, and leads that
score high enough are called by that service's Dograh agent.

Adding a service is **configuration only**: you edit [`services.yaml`](services.yaml)
and set up an agent in Dograh. No Python changes are needed. The pipeline picks
up the new service, asks Gemini about it, creates its campaigns, and adds its
`prob_<id>` column to the results sheet automatically.

The example below adds a made-up service called **Acme Permits**.

---

## Step 1: Build and publish the calling agent in Dograh

1. In Dograh, open **Voice Agents** and create the agent (or copy an existing
   outbound agent and change its script).
2. Write its prompts. The agent can use any lead column as a variable:
   - Column names are lowercased, and spaces/symbols become `_`.
     `Company Name` becomes `{{company_name}}`, and `City / State` becomes `{{city___state}}`.
   - Always available: `{{phone_number}}`, `{{interested_service}}` (the
     service's `name`), and `{{current_datetime}}`.
   - Give optional variables a fallback, e.g. `{{company_name | fallback:there}}`
     (or the short form `{{company_name | there}}`).
     Without a default, a variable is **required**: any lead missing it is left out
     of the campaign and saved as `skipped_missing_data`.
3. **Publish** the agent. Calls always use the *published* version.
4. Write down the agent's **ID** (the "ID" column on the Voice Agents page).

## Step 2: Add the service block to `services.yaml`

Copy an existing block to the end of the `services:` list and change it.
Keep the same indentation (two spaces, then `- id:`).

```yaml
  # ----------------------------------------------------------------------------
  - id: acme_permits
    name: Acme Permits
    enabled: false        # keep false until Step 4 is done
    # Dograh agent (workflow) ID - 4 = "Acme Permits - outbound"
    agent_id: 4

    description: |
      What the company sells, who it is, how it works...

    good_fit: |
      - the kinds of businesses that buy it...
      Strong signals: words that appear in listings...

    not_a_fit: |
      - near-misses that waste calls...
```

Field rules (they are checked at startup, and the run stops with a clear error if one fails):

| Field | Required | Rule |
|---|---|---|
| `id` | yes | Unique. Only lowercase letters, digits and `_`. Becomes the `prob_<id>` results column, so don't rename it later. |
| `name` | yes | Shown in logs, used in the campaign name (`Acme Permits_2026-11-19`) and sent to the agent as `{{interested_service}}`. |
| `agent_id` | yes | A number: the Dograh agent ID from Step 1. |
| `description` | yes | What the company sells. |
| `good_fit` | recommended | Who **should** be called. |
| `not_a_fit` | recommended | Who should **not** be called. |
| `enabled` | no (default `true`) | `false` = ignored completely. |

## Step 3: Write the three descriptions well

Gemini decides **only** from this text, so this step matters most.

- **Describe the buyer, not just the product.** In `good_fit`, name the business
  types, trades and sizes that buy, plus signals from a listing ("bids on projects", "uses Revit").
- **List the near-misses in `not_a_fit`** (e.g. homeowners, real estate agents,
  suppliers). This pushes clear mismatches toward 0%.
- **Use the words scrapers produce**, like "GC", "remodeler" and "MEP".
- **Separate overlapping services.** A lead is called for only one service (the
  highest score). If the new service overlaps an existing one, say in each
  `not_a_fit` which leads belong to the *other* service, and update the existing
  services' `not_a_fit` too.
- Keep it factual. A few paragraphs per field is plenty.

See [SERVICES.md](SERVICES.md) for more tips and the current services.

## Step 4: Test it without calling anyone

Running `main.py` is always **live**, so test the agent and the scoring first.

Before running these checks, temporarily set `enabled: true` for the new service.
If you don't, `load_services` skips it.

**a. Check that the agent is found, is published, and which variables it needs.**
Open a terminal in the `dograh_caller` folder and run:

```powershell
python -c "from dograh_client import DograhClient; c=DograhClient(); a=c.get_agent(4); print(a.name, a.version_status); print(sorted(c.get_required_variables(4)))"
```

Replace `4` with your agent ID. Every variable printed must be a column in your
leads, or those leads will be skipped.

**b. Score some sample leads with Gemini.** You can either run `python main.py your_fictional_leads.json --score-only` or save the following as a scratch file in the `dograh_caller` folder (e.g. `try_scores.py`)
and run `python try_scores.py`:

```python
import asyncio
import config
from decision_maker import DecisionMaker, load_services

LEADS = [
    {"company": "Should Fit Co", "category": "...", "description": "..."},
    {"company": "Should NOT Fit Co", "category": "...", "description": "..."},
    {"company": "Overlaps Other Service Co", "category": "...", "description": "..."},
]

async def main():
    dm = DecisionMaker(load_services(config.SERVICES_FILE), config.INTEREST_THRESHOLD,
                       config.GEMINI_API_KEY, config.GEMINI_MODEL)
    for lead in LEADS:
        d = await dm.decide(lead)
        scores = ", ".join(f"{k} {v:.0%}" for k, v in d.probabilities.items())
        print(f"{lead['company']}: {scores} -> {d.service.name if d.service else 'not called'}")
    await dm.close()

asyncio.run(main())
```

Good leads should score **≥ 80%** for the new service, and bad ones well below.
Leads that belong to an existing service should still go to that service. If the
scores are wrong, edit the descriptions (Step 3) and run again. Delete the scratch file when you're done.

## Step 5: Enable it and run a small live test

1. Make sure `enabled: true` is set in `services.yaml`.
2. Run a few real leads:

   ```powershell
   python main.py path\to\leads.xlsx --limit 3
   ```

3. At startup, check that the new service is listed with **no** "unpublished changes" warning:

   ```
   [setup] Services: Draftliner (agent 2), Paradise Estimating (agent 3), Acme Permits (agent 4)
   [setup] Acme Permits -> agent 4 'Acme Permits - outbound'
   ```

4. In `output/call_results.xlsx`, check the new `prob_acme_permits` column,
   `interested_service`, `campaign_name` (`Acme Permits_<date>`) and `call_disposition`.
   Listen to a recording or two.

## Step 6: Update the docs

- Add the service to the "Current services" table in [SERVICES.md](SERVICES.md)
  and the agent table in [README.md](README.md).
- Note the agent ID in the comment above `agent_id` in `services.yaml`.

---

## Turning a service off or changing it

- **Pause a service:** set `enabled: false`. Leads are no longer scored or called for it.
- **Change the agent:** update `agent_id`, or edit and **re-publish** the Dograh agent.
- **Rename the service:** change `name` only. Changing `id` starts a new `prob_` column
  and breaks comparisons with older results.

## Troubleshooting

| Problem | Cause / fix |
|---|---|
| `missing required field(s): ...` | Add `id`, `name`, `agent_id` or `description` to the block. |
| `id '...' must use only lowercase letters, digits and _` | Fix the `id` (no spaces, capitals or `-`). |
| `duplicate id` | Two blocks share an `id`. |
| `agent_id must be a number` | Use the numeric ID, not the agent's name. |
| `Dograh agent N does not exist` | Wrong ID, or the agent belongs to a different Dograh account than `DOGRAH_API_KEY`. |
| YAML error at startup | Usually indentation. Each block starts with `  - id:` and the text under `description: |` is indented further. |
| New service never gets leads | Scores are below the threshold, or another service always scores higher. Strengthen `good_fit`, and check the other services' descriptions for overlap. |
| Many `skipped_missing_data` | The agent's prompt uses a `{{variable}}` your leads don't have. Add a fallback (`{{var | fallback:...}}`) or the column. |
