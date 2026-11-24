# Describing our services for Gemini

Gemini decides which service a lead might buy **only from the text in
[`services.yaml`](services.yaml)**. Better descriptions mean better decisions.
Adding, editing, or disabling a service never needs a code change.

## How a decision is made

For every lead, Gemini scores every **enabled** service independently (0–100%):

> *"How likely is this business to be a genuine potential customer for `<name>`?"*

and returns a probability (0–100%) and one-sentence reason for each, plus a two-line summary. Code then applies the threshold:

| Result | What happens |
|---|---|
| Best probability **≥ 80%** | Added to that service's campaign (`<name>_<date>`), called by its Dograh agent (`agent_id`) |
| Two services ≥ 80% | Goes to the service with the **higher** probability (each lead is in one campaign only; ties use `services.yaml` order) |
| All below 80% | Not called; saved as `not_interested` (shown as Non-Qualified in the dashboard) |

All probabilities are saved in the results sheet as `prob_<id>` columns, so you
can see how close each lead came. The 80% threshold is `INTEREST_THRESHOLD` in
`.env` / `config.py`, or `--threshold` on the command line.

## Fields

```yaml
  - id: draftliner          # unique, lowercase letters/digits/_ ; becomes the prob_<id> column
    name: Draftliner        # human-readable name, also sent to the agent as {{interested_service}}
    enabled: true           # false = ignored by Gemini, never called
    agent_id: 2             # Dograh agent ID (Voice Agents page, "ID" column)
    description: |          # REQUIRED - what the company sells
      ...
    good_fit: |             # who SHOULD be called (strongly recommended)
      ...
    not_a_fit: |            # who should NOT be called (strongly recommended)
      ...
```

## Adding a service (e.g. Paradise)

1. Create and **publish** its calling agent in Dograh and note its ID.
2. In `services.yaml`, copy an existing block, set a new `id`, `name` and `agent_id`.
3. Write `description`, `good_fit` and `not_a_fit` (see tips below).
4. Set `enabled: true`.
5. Test scoring first without placing any calls (`python main.py path\to\leads.xlsx --score-only`), then run a small live test (`python main.py path\to\leads.xlsx --limit 3`) and check the `prob_<id>` column and call outcomes in the results.

## Writing good descriptions

- **Describe the buyer, not just the product.** `good_fit` should name the kinds
  of businesses (categories, trades, sizes) that buy, plus signals in a listing
  that suggest a need ("bids on projects", "growing", "uses Revit").
- **Say who is NOT a fit.** Gemini uses `not_a_fit` to push clear mismatches toward
  0%. List the near-misses that waste calls (e.g. homeowners, real estate agents).
- **Use the words scrapers produce.** If listings say "GC", "remodeler",
  "MEP", include those terms.
- **Keep services distinct.** If two services overlap (e.g. both do estimating),
  state in each `good_fit` what makes a lead lean to one rather than the other.
- **Keep it factual and reasonably short.** A few paragraphs is plenty.

## Current services and how they are separated

| Service | Agent | Who it targets |
|---|---|---|
| Draftliner | 2 — *Testing Lead qualification - outbound* (Danial) | Firms with recurring **design, drafting, BIM, engineering or project-office work**: architects, engineers, design-build, GCs running preconstruction/PM, homebuilders needing plans/permits, developers, shop-drawing producers |
| Paradise Estimating | 3 — *Paradise Agent - outbound* (Olivia) | Contractors that **bid jobs from drawings** and need per-project estimates/takeoffs: trade subcontractors of any trade and size, GCs, builders, remodelers, restoration (Xactimate) |

Both companies offer estimating, so each `not_a_fit` names the leads that belong
to the other one (small trade crews → Paradise; architects/engineers → Draftliner).
Descriptions were written from `Draftliner_knowledgebase.docx` and
`Paradise_Estimating_AI_Knowledge_Base.pdf`.

Example score ranges by lead type:

| Lead type | Draftliner | Paradise |
|---|---|---|
| Architecture / MEP engineering firm | 0.94 | 0.08–0.13 |
| Steel fabricator (shop drawings), custom-home builder, developer | 0.88–0.92 | 0.80–0.84 |
| Drywall / roofing / concrete / masonry / HVAC / electrical contractor | 0.14–0.52 | 0.86–0.95 |
| Restaurant, handyman | 0.03–0.04 | 0.06–0.09 |
