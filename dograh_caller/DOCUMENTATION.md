# dograh_caller — Project Documentation

> Quick start: [README.md](README.md) · Describing services for Gemini: [SERVICES.md](SERVICES.md)

## Contents

1. [Purpose](#1-purpose)
2. [System overview](#2-system-overview)
3. [Project structure](#3-project-structure)
4. [Installation and configuration](#4-installation-and-configuration)
5. [Running the pipeline](#5-running-the-pipeline)
6. [Stage 1 — Number verifier](#6-stage-1--number-verifier)
7. [Stage 2 — Decision maker (Gemini)](#7-stage-2--decision-maker-gemini)
8. [Stage 3 — Campaigns](#8-stage-3--campaigns)
9. [Stage 4 — Results, transfers, and pipeline tracker](#9-stage-4--results-transfers-and-pipeline-tracker)
10. [Client Dashboard & Batch Runner](#10-client-dashboard--batch-runner)
11. [Concurrency model](#11-concurrency-model)
12. [Python API reference](#12-python-api-reference)
13. [Extending the system](#13-extending-the-system)
14. [Helper tools & testing](#14-helper-tools--testing)
15. [Troubleshooting](#15-troubleshooting)
16. [Limitations and known behaviour](#16-limitations-and-known-behaviour)
17. [Compliance](#17-compliance)

---

## 1. Purpose

`dograh_caller` takes business leads (scraped from the web or listed in a
spreadsheet), decides with **Gemini** (`gemini-3.8-flash`) which of our services each lead is
likely to buy, and has the matching **Dograh** voice agent call the qualified
ones. Every lead and its outcome is recorded in Excel (`call_results.xlsx` and `pipeline_tracker.xlsx`).

| Service | Dograh agent | Status |
|---|---|---|
| Draftliner | 2 — *Testing Lead qualification - outbound* | enabled |
| Paradise Estimating | 3 — *Paradise Agent - outbound* | enabled |

---

## 2. System overview

```
                                   ┌───────────────────────────────────────────────────────────────────────┐
  Reverse proxy                    │                             dograh_caller                             │
  https://ai-sdr.grobots.ai        │                                                                       │
    (on-demand via Start Batch ───►│  Number verifier ──► Decision maker ──► per-service lists ──► Dograh  │
     or Enter in pull_leads.py)    │   US numbers only     Gemini: P(fit)       Draftliner: [...]   calls  │
  .xlsx / .csv / .json / Python ──►│   dedupe, E.164       per service ≥ 80%    Paradise:   [...]   (& retries)
                                   │        │                    │                                    │    │
                                   │        └── dropped ─────────┴── not_interested ──────── outcome ─┘    │
                                   └───────────────────────────────────────────────────────────────────────┘
                                                                        │
                                    ┌───────────────────────────────────┼──────────────────────────────────┐
                                    ▼                                   ▼                                  ▼
                       output/call_results.xlsx        output/transferred_calls.xlsx      output/pipeline_tracker.xlsx
                                                                                                           │
                                                                                                           ▼
                                                                                          Dashboard (http://127.0.0.1:8090)
                                                                                          Start/Stop Batch · Funnel · Media
```

**Idle-on-startup design:** starting the dashboard (`dashboard_server.py`), the interactive CLI (`pull_leads.py`), or `main.py` with defaults **never** pulls or calls leads automatically. Leads are only pulled directly from the configured reverse proxy (`LEAD_SOURCE_URL`, default `https://ai-sdr.grobots.ai`) when the user clicks **Start Batch** in the dashboard or presses **Enter** in `pull_leads.py`.

**Lifecycle of a run**

1. **Pull or load leads:** on-demand jobs pull a batch (saved first to `output/pulled_batches/*.pending.json`) directly from the reverse proxy, or a local file/Python iterable submits lead `dict`s.
2. **Number verifier:** finds the phone field(s), excludes fax numbers, validates format/region/type, blocks numbers already called in earlier runs, and keeps valid US numbers. Otherwise → `dropped_*`.
3. **Decision maker:** asks Gemini (`gemini-3.8-flash`) for a probability (0–100%) and one-sentence reason per enabled service plus a two-sentence summary. Code applies `INTEREST_THRESHOLD` (80%): if the highest probability ≥ 80%, the lead is queued for that service's campaign; otherwise → `not_interested` (labelled **Non-Qualified** in the dashboard).
4. **Campaigns:** once a batch's leads are scored, **one Dograh campaign per service** with qualified leads is created automatically, named `<service name>_<date>` (e.g. `Draftliner_2026-11-19`), using that service's published Dograh agent. Campaigns run in parallel and are paused automatically when finished or stopped.
5. **Call tracking:** each call attempt (and Dograh retry) is followed until a final outcome (`called`, `call_failed`, `call_timeout`, or `interrupted`).
6. **Persistence & live UI:** `output/call_results.xlsx` records one row per lead grouped by day; `output/transferred_calls.xlsx` collects transferred calls; `output/pipeline_tracker.xlsx` tracks every lead across stages in near real time for the FastAPI + React dashboard.

---

## 3. Project structure

```
dograh_caller/
├── main.py              Command-line entry point (file / Python runs, supports --score-only; USE_STREAMER=False by default)
├── pull_leads.py        Interactive CLI puller & --check tool (waits for Enter before every ping, including the first)
├── dashboard_server.py  FastAPI + React dashboard server (stays idle until Start Batch; --demo mode supported)
├── install.sh           Server setup: .venv, requirements, `pull_leads.py --check`
├── GO_LIVE.md           Steps to put https://ai-sdr.grobots.ai live (streamer, nginx, DNS, certbot)
├── requirements-dev.txt Test dependencies (pytest, httpx, psycopg for the streamer tests)
├── pipeline.py          LeadPipeline: asyncio queues, stage workers, campaign flush, stop_event, summary
├── pipeline_tracker.py  Per-lead Excel tracker (output/pipeline_tracker.xlsx) with .pipeline_tracker.lock
├── lead_fields.py       Helpers for unstructured leads (field-name words, nesting, JSON text)
├── number_verifier.py   Stage 1 – phone validation (US only, never fax)
├── decision_maker.py    Stage 2 – Gemini structured scoring, service loading, check_gemini_access()
├── campaign_manager.py  Stage 3 – one campaign per service: create, start, follow calls & retries
├── dograh_client.py     Dograh HTTP client (agents, CSV upload, campaigns, runs)
├── results_store.py     Stage 4 – call_results.xlsx and transferred_calls.xlsx writer
├── config.py            All settings (read from environment and .env)
├── services.yaml        Our services, as described to Gemini  ← edit this to add services
├── dashboard/           Dashboard backend (app.py, data.py, media.py, demo_media.py, runner.py) & React UI (web/ -> static/)
├── tools/               Helper scripts (make_demo_tracker.py, compare_scores.py)
├── requirements.txt
├── README.md            Quick start
├── SERVICES.md          How to write service descriptions
├── ADDING_A_SERVICE.md  Step-by-step guide to adding a service
├── DOCUMENTATION.md     This file
├── lead_streamer/       Standalone PostgreSQL HTTP lead streamer service
├── tests/               Offline pytest test suite
├── examples/
│   ├── from_python.py   Feeding dicts from Python code (scraper integration)
│   └── sample_leads.xlsx  Test leads with fictional 555-01xx numbers
└── output/              Created on first run
    ├── call_results.xlsx
    ├── transferred_calls.xlsx
    ├── pipeline_tracker.xlsx
    └── pulled_batches/  Saved *.pending.json / *.done.json streamer batches
```

---

## 4. Installation and configuration

### Requirements

- Python 3.10+
- `pip install -r requirements.txt`
  (`google-genai`, `phonenumbers`, `requests`, `openpyxl`, `PyYAML`, `python-dotenv`, `fastapi`, `uvicorn`)

### `.env`

Settings are read from, in priority order: real environment variables →
`dograh_caller/.env` → `../.env` (parent folder).

```ini
# Required
GEMINI_API_KEY=...                # Gemini API key (GEMINI_API also accepted)
DOGRAH_API_KEY=...                # Dograh: Settings -> API Keys (same deployment as the URL)
DOGRAH_BASE_URL=https://dograh.grobots.ai/api/v1
```

### All settings

| Variable | Default | Meaning |
|---|---|---|
| `GEMINI_API_KEY` / `GEMINI_API` | — | Gemini API key (required) |
| `GEMINI_MODEL` | `gemini-3.8-flash` | Gemini model for lead qualification |
| `GEMINI_THINKING_LEVEL` | `MEDIUM` | Reasoning level (`LOW`, `MEDIUM`, or `HIGH`) |
| `GEMINI_TIMEOUT_SECONDS` | `60` | HTTP timeout for Gemini requests |
| `GEMINI_MAX_ATTEMPTS` | `4` | Retry attempts for transient Gemini HTTP errors |
| `LEAD_MAX_FIELD_CHARS` | `2000` | Longest value Gemini gets from one field (longer text is cut) |
| `LEAD_MAX_STATE_CHARS` | `12000` | Most characters Gemini gets per lead in total (most useful fields first) |
| `INTEREST_THRESHOLD` | `0.80` | Minimum probability for a lead to be qualified/called |
| `ALLOWED_PHONE_REGIONS` | `US` | Phone regions accepted, comma-separated ISO codes. `US,PR,VI,GU,MP,AS` adds the US territories |
| `SKIP_ALREADY_CALLED` | `true` | Block numbers already called in earlier runs (read from `call_results.xlsx` — including timestamped fallback copies — and `pipeline_tracker.xlsx`) |
| `SERVICES_FILE` | `services.yaml` | Service definitions |
| `DOGRAH_API_KEY` | — | Dograh API key (required to place calls) |
| `DOGRAH_BASE_URL` | `https://dograh.grobots.ai/api/v1` | Dograh API base URL, must end in `/api/v1` |
| `DOGRAH_VERIFY_SSL` | `true` | Set `false` only for self-signed certificates |
| `DOGRAH_REQUEST_TIMEOUT` | `30` | Seconds per HTTP request |
| `CAMPAIGN_NAME_DATE_FORMAT` | `%Y-%m-%d` | Date part of campaign names (`Draftliner_2026-11-19`) |
| `CAMPAIGN_RETRY_ENABLED` | `true` | Let Dograh retry unanswered calls |
| `CAMPAIGN_MAX_RETRIES` | `1` | Extra attempts per number |
| `CAMPAIGN_RETRY_DELAY_SECONDS` | `120` | Wait before a retry (Dograh minimum 30) |
| `CAMPAIGN_RETRY_ON_BUSY` / `_NO_ANSWER` / `_VOICEMAIL` | `true` / `true` / `false` | Which outcomes are retried |
| `CAMPAIGN_POLL_SECONDS` | `5` | How often running campaigns are checked |
| `CAMPAIGN_IDLE_TIMEOUT_SECONDS` | `900` | If a campaign makes no progress for this long, its unfinished leads become `call_timeout` |
| `RESULT_WAIT_SECONDS` | `20` | After a call ends, wait this long for Dograh to fill in the outcome |
| `RETRY_GRACE_SECONDS` | `120` | Extra wait beyond the retry delay for a retry call to appear |
| `DECISION_CONCURRENCY` | `8` | Parallel Gemini requests |
| `LEAD_BATCH_SIZE` | `300` | Leads per batch; each batch is scored, put into campaigns and fully called before the next. Keep equal to the streamer's `BATCH_SIZE` |
| `QUEUE_MAXSIZE` | `100` | Max leads waiting between two stages |
| `RESULTS_FILE` | `output/call_results.xlsx` | Results spreadsheet |
| `PIPELINE_TRACKER_FILE` | `output/pipeline_tracker.xlsx` | Per-lead Excel pipeline tracker |
| `TRACKER_SAVE_SECONDS` | `3` | Minimum interval between periodic tracker saves |
| `TRACKER_WARN_ROWS` | `20000` | Row count at which tracker logs a size warning |
| `DASHBOARD_HOST` | `127.0.0.1` | Default host for `dashboard_server.py` |
| `DASHBOARD_PORT` | `8090` | Default port for `dashboard_server.py` |
| `DASHBOARD_REFRESH_SECONDS` | `5` | Dashboard polling interval |
| `TRANSFERS_FILE` | `output/transferred_calls.xlsx` | Only businesses whose call was transferred, with their details |
| `TRANSFER_DISPOSITIONS` | `transfer_call,call_transferred` | Dispositions/tags that mean "transferred" (Dograh's defaults). Add the disposition you set on a transfer tool if it doesn't contain "transfer". Anything containing "transfer" but not "fail" also counts |
| `LEAD_SOURCE_URL` | `https://ai-sdr.grobots.ai` | Lead streamer reverse proxy URL (alias `STREAMER_URL`), used by `dashboard_server.py` (`BatchRunner`) and `pull_leads.py` (`<url>/leads/next`) |
| `LEAD_SOURCE_TOKEN` | — | The streamer's `STREAM_TOKEN` (alias `STREAM_TOKEN`); required by `BatchRunner` and `pull_leads.py` |
| `LEAD_SOURCE_TIMEOUT` | `180` | Seconds to wait for a ping's answer (minimum 120) |
| `PULLED_BATCHES_DIR` | `output/pulled_batches` | Received batches (`.pending.json` until fully processed, then `.done.json`) |
| `RESULTS_MAX_LEAD_COLUMNS` | `80` | Most lead fields with their own column; further fields go (as JSON) into `other_lead_fields` |

---

## 5. Running the pipeline

### Recommended: Dashboard server (on-demand reverse proxy)

```bash
python dashboard_server.py
```

- Starts **idle** at `http://127.0.0.1:8090` (`Lead source (reverse proxy): https://ai-sdr.grobots.ai`). Nothing is pulled or called until you click **Start Batch** (or `POST /api/runner/start`).
- Choose `Leads per batch` (`1`–`1,000`, default `300`) and `Number of batches` (`1`–`100`, default `1`), or click **Stop** at any time to pause active Dograh campaigns and keep unfinished batches pending for resume.
- Pass `--demo` to preview the UI with ~60 fictional leads and offline simulated batch runs. See [§10](#10-client-dashboard--batch-runner) for all options and API details.

### Interactive terminal puller (`pull_leads.py`)

```bash
python pull_leads.py --check   # validate keys, Dograh agents, reverse proxy /health and /stats (takes no leads)
python pull_leads.py           # stays idle until you press Enter before every ping (including the first)
```

Options: `--url <URL>`, `--threshold <0-1>`, `--output <path.xlsx>`, `--check`.

### File / Python / `--score-only` runs (`main.py`)

Edit the **INPUT** section at the top of [main.py](main.py), then run `python main.py`.
Every run without `--score-only` is **live**: leads that pass the threshold are called.

| Variable | Default | Meaning |
|---|---|---|
| `INPUT_FILE_PATH` | `None` | Leads file (.xlsx / .csv / .json) called when `python main.py` starts. Relative paths are relative to `dograh_caller/`. Don't point it at `examples/sample_leads.xlsx` (fictional numbers). |
| `INPUT_SHEET` | `None` | Worksheet to read (`None` = first sheet) |
| `INPUT_LEADS` | `[]` | Lead dicts, e.g. from the scraper. Processed together with the file if both are set. |
| `LIMIT` | `None` | Process only the first N leads of the file |
| `USE_STREAMER` | `False` | `False` by default so starting `main.py` never auto-pulls or auto-calls leads. Set `True` only if you explicitly want unattended polling after the file |
| `STREAMER_CHECK_SECONDS` | `60` | Wait between streamer checks when `USE_STREAMER = True` |

### From other code: `run()`

```python
from main import run      # add dograh_caller to sys.path first if calling from another folder

run(source, sheet=None, limit=None, threshold=None, output=None) -> int
```

`source` may be a **file path**, **one lead dict**, or a **list of lead dicts**.
Returns `0` on success, `1` on error, `130` if interrupted.

### Command-line overrides (optional)

```bash
python main.py [file] [--threshold 0.9] [--output path.xlsx] [--sheet NAME] [--limit N] [--score-only]
```

| Option | Effect |
|---|---|
| `file` | Use this file once instead of `INPUT_FILE_PATH` / `INPUT_LEADS`, without using the streamer |
| `--threshold` | Override `INTEREST_THRESHOLD` for this run (0–1) |
| `--output` | Write results to another .xlsx |
| `--sheet` | Worksheet to read (default: `INPUT_SHEET` or first sheet) |
| `--limit` | Process only the first N leads (overrides `LIMIT`) |
| `--score-only` | Verify and score with Gemini without creating Dograh campaigns or placing calls (outputs `output/score_only_<timestamp>.xlsx`) |

**Input files**

- `.xlsx` — first row = column names; empty rows skipped.
- `.csv` — first row = column names (UTF-8).
- `.json` — one object or a list of objects.
- `.xls` (old Excel format) is not supported: save it as `.xlsx`.

Any columns are allowed; one must contain the phone number (see §6).

### Terminal output

```
[23:47:01] setup    Services: Draftliner (agent 2)
[23:47:01] setup    Gemini model gemini-3.8-flash (thinking MEDIUM), threshold 80%
[23:47:02] setup    Draftliner -> agent 2 'Testing Lead qualification - outbound'
[23:47:02] verify   DROP +1 416 555 0107 (dropped_not_us (CA)) - Maple Leaf Builders
[23:47:03] decide   NON-QUALIFIED +16025550105 Taco Loco (draftliner 2%)
[23:47:03] decide   QUALIFIED +12155550103 Keystone MEP Engineers -> Draftliner (draftliner 94%)
[23:47:29] call     -> Calling +12155550103 (Keystone MEP Engineers) via 'Testing Lead ...' [run 2201]
[23:48:41] call     <- +12155550103 Result: user_qualified | 67s | tags: user_qualified, user_speech
Done. Leads received: 10 | dropped by verifier: 4 | not interested: 2 | called: 4 | ...
```

**Batches.** Leads are processed **300 at a time** (`LEAD_BATCH_SIZE`): the
batch is verified and scored, one campaign per service is created for it, and
every call finishes before the next 300 are taken:

```
[batch]    Batch 1: leads 1-300 of 700
...        (verify, score, campaigns, calls)
[campaign] 'Draftliner_2026-11-19' (campaign 21) finished - paused
[batch]    Batch 2: leads 301-600 of 700
```

`Ctrl+C` stops immediately: this run's campaigns are **paused** in Dograh,
rows already written are kept, and leads in progress are lost.

---

## 6. Stage 1 — Number verifier

File: [number_verifier.py](number_verifier.py) · Class: `NumberVerifier` ·
helpers for unstructured data: [lead_fields.py](lead_fields.py)

**Unstructured leads.** Leads may have any field names, any nesting (dicts in
dicts, lists of dicts) and JSON stored as text. Nothing depends on fixed names:
field names are split into words (`faxNumber` → *fax, number*, `Phone(s)` →
*phone*) and nested values are walked to any depth.

**Finding the phone fields.** A value is a phone value when its field name, or
the name of any field it is nested under, is phone-like: `Phone(s)`,
`Phone Number`, `phoneNumber`, `PHONE-NO`, `Mobile`, `mobileNo`, `Cell`,
`Telephone`, `Tel`, `Contact No`, `contact_numbers`, `Number`, any name with the
word "phone"; also `contact > phone`, `phones[] > number`, and entries labelled
as a phone line (`{"label": "Main", "value": "512-555-0101"}`). Fields like
`Phone Verified: Yes` simply contain no number; if the `Phone` field is junk but
`Mobile` holds a good number, `Mobile` is used. Numbers that only appear in
free text (e.g. a description) are not used.

**Fax numbers are never called.** A number is a fax number when:

| How it's marked | Examples |
|---|---|
| Its field name says fax | `fax`, `Fax No.`, `fax_phone`, `faxNumber`, `telefax`, `contact > fax` |
| Its entry is typed as fax | `{"type": "fax", "number": "..."}`, `{"label": "Fax", "value": "..."}`, `{"category": "fax", ...}` — any nested entry, whatever its size (entries with ids, sources, dates, flags included). The lead's own top-level `type` / `category` never counts. |
| Its label in the text says fax | `Fax: 512…`, `F: 512…`, `512… (fax)`, `512… fax, 713…`, `Tel/Fax: 512…` (shared line → not called) |

Every fax number found anywhere in the lead is excluded, **even if the same
number is also listed in a phone field** (e.g. a fax number that sits in the
`phones` list). "Fairfax" or "fax machines" in other text are not fax labels.
A lead whose only numbers are fax numbers → `dropped_fax_only`.

**Reading numbers.** Numbers are found inside each value with libphonenumber's
number finder, so all of these work: `+19292659900, +19174102250`,
`512-555-0102 / 512-555-0103`, `512-555-0106 and 512-555-0107`,
`Office: (512) 555-0104 Cell: 512-555-0105`, `Tel #: 512 555 0124`,
`(512) 555-0108 ext. 23` (extension dropped), `512.555.0111`, Excel values like
`15125550125.0` or `'+15125550110`, and vanity numbers like `1-800-FLOWERS`.
Numbers without a `+country` prefix are read as US numbers.

**Checks** (Google's libphonenumber):

| Check | Status if no number of the lead passes |
|---|---|
| A phone field exists | `dropped_no_phone` |
| A number can be read | `dropped_unreadable_number` |
| Not a fax number (see above) | `dropped_fax_only` |
| Valid for the numbering plan (real area code, length, ...) | `dropped_invalid_number` |
| Region allowed — default **US only** (Canada, Puerto Rico, Guam, Caribbean... excluded; see `ALLOWED_PHONE_REGIONS`) | `dropped_not_us (<REGION>)` |
| Callable type: landline, mobile or toll-free. Premium-rate (900), shared-cost, personal (500-type), pager and other special numbers are dropped | `dropped_special_number (<TYPE>)` |
| None of the lead's good numbers was **already called in an earlier run** (status `called` / `call_timeout` in the results sheet) | `dropped_already_called` |
| None was already seen earlier **in this run** | `dropped_duplicate` |

**Already called.** At startup the results sheet is read and every number with
status `called` or `call_timeout` is blocked, so restarts and later batches
never call the same business twice. Leads that were only `not_interested`,
`call_failed` or skipped are evaluated again. Turn off with `SKIP_ALREADY_CALLED=false`.

**State and time zone.** The chosen number's US state and time zone (from the
area code) are saved as `phone_location` / `phone_timezone`, e.g. `Texas` /
`America/Chicago` — for calling-hours rules and reporting. Toll-free numbers
have no location and are left blank.

- The **first** number that passes all checks is called (normalized to E.164,
  `+1XXXXXXXXXX`).
- If several numbers fail for different reasons, the most informative reason
  is reported (not-US > fax-only > special > invalid > unreadable).
- **Duplicates:** if *any* good number of a lead was seen before in this run,
  the lead is the same business again and is dropped — a business is never
  called twice, even when it is listed with a different number first.
  Detection is per process run (in memory).
- **Limitation:** "valid" means correctly formed for the US numbering plan.
  Whether a number is currently in service needs a carrier lookup (e.g.
  Twilio Lookup), which is not done.

---

## 7. Stage 2 — Decision maker (Gemini)

File: [decision_maker.py](decision_maker.py) · Class: `DecisionMaker`

### What Gemini sees

**State:** only the information that describes the business, built by
`lead_state()` from the unstructured lead:

| Kept (most useful first) | Removed |
|---|---|
| 1. Business name, opportunity title | Phone, fax and e-mail fields |
| 2. Category, industry, trade, services, type | Links: website, URLs, domain, social profiles |
| 3. Descriptions, notes, about, summary | IDs, versions, `event`, created / updated / seen dates, timestamps |
| 4. Size, employees, years, licenses, rating | Status, activity, follow-up, source, retry and other bookkeeping |
| 5. City, state, country, markets served | People's names (contact / owner names); job titles are kept |
| 6. Anything else descriptive | Placeholders (`n/a`, `Not specified`, `Unassigned`, `-`), values that look like phone numbers, URLs, e-mails or dates |

- Nested fields are flattened to `parent > child` (e.g. `business > about`);
  repeated fields from lists are joined with `;`.
- Each value is cut to `LEAD_MAX_FIELD_CHARS` (2,000) and the whole state to
  `LEAD_MAX_STATE_CHARS` (12,000), filling in the order above — so a 40,000-
  character tender description can't crowd out the rest.
- Example: a 41,000-character streamer lead becomes ~2,500 characters of
  company, category, services, description (capped), size and location.

**Structured scoring:** one Gemini request per lead (`GEMINI_MODEL`, default `gemini-3.8-flash`, reasoning level `GEMINI_THINKING_LEVEL`) evaluates every enabled service from `services.yaml` using `HttpOptions` with `GEMINI_TIMEOUT_SECONDS` (60s) and `GEMINI_MAX_ATTEMPTS` (4 attempts with exponential backoff on HTTP 408/429/500/502/503/504), and returns a structured JSON object with:
- `services`: for each service, an integer `probability_percent` (0–100) and a
  one-sentence `reason` citing evidence from the lead;
- `summary`: a two-short-sentence explanation of what the business does and why
  it does or does not fit its best-matching service.

Before taking leads from the streamer, `preflight()` runs `check_gemini_access()` (`client.models.get(model=...)`) to verify the API key and model access without consuming leads or generating content.

### Routing rule

```
best = service with the highest probability (ties broken by services.yaml order)
if probability(best) >= INTEREST_THRESHOLD: call via best.agent_id
else: not_interested
```

Code applies the threshold (Gemini is not told the cutoff) and generates the
`verdict` text. One lead is called at most once, by one agent. Every service's
probability is saved as `prob_<id>`, its reason as `decision_reason_<id>`, and
full per-lead reasoning is persisted in `output/pipeline_tracker.xlsx`.

**Failed Gemini scoring (`decision_error`):** if a lead fails Gemini scoring after retries, it is never treated as non-qualified. Instead it is recorded as `decision_error` and saved for retry:
- For **streamer / dashboard runs**, failed leads are saved as a new `batch_*.pending.json` in `output/pulled_batches/` (`source="decision_retry"`) so the next batch run retries them automatically.
- For **file / Python runs** (`main.run()`), failed leads are written to `output/decision_retry_<timestamp>.json` with a ready-to-run `python main.py ...` command.

---

## 8. Stage 3 — Campaigns

Files: [campaign_manager.py](campaign_manager.py) (`CampaignJob`) ·
[dograh_client.py](dograh_client.py) (`DograhClient`)

### One campaign per service per run

When all leads of the run have been scored, for **each service with at least one
interested lead**:

1. A contacts CSV is built with that service's numbers only.
2. A campaign named **`<service name>_<YYYY-MM-DD>`** — the date the run
   **started**, so a run that passes midnight keeps its name (e.g.
   `Draftliner_2026-11-19`, `Paradise Estimating_2026-11-19`) — is created with **that service's agent**
   (`agent_id` in services.yaml) and the retry settings from config.
3. The campaign is started. All campaigns of the run run in parallel; they share
   the Dograh organization's concurrent-call limit.

Services with no interested leads get no campaign (Dograh can't create a campaign
without contacts). Running twice on the same day creates a second campaign with
the same name (Dograh shows them with different IDs).

**Why campaigns are created after scoring, not at the start:** a Dograh
campaign's contact list is fixed when the campaign is created — the API has no
way to add numbers to an existing campaign. For a scraper that runs for a long
time, call `pipeline.flush()` periodically: each flush creates a new set of
campaigns for the leads collected since the previous flush.

### Dograh endpoints used

All requests send header `X-API-Key: <DOGRAH_API_KEY>`.

| When | Request | Purpose |
|---|---|---|
| Startup | `GET /workflow/fetch/{agent_id}` | Check each service's agent exists; warn about unpublished changes |
| Per campaign | `GET /workflow/{agent_id}/versions` | Find the `{{variables}}` the published agent needs |
| Per campaign | `POST /s3/presigned-upload-url` + `PUT` | Upload the contacts CSV |
| Per campaign | `POST /campaign/create` | Create `<service>_<date>` with the agent, CSV and retry config |
| Per campaign | `POST /campaign/{id}/start` | Start dialing |
| Every `CAMPAIGN_POLL_SECONDS` | `GET /campaign/{id}` and `GET /campaign/{id}/runs` | State, retry config, and one run per call attempt |
| Campaign done, or program exits | `POST /campaign/{id}/pause` | Make sure the campaign isn't left running |

### Following the calls

- Every call attempt is a Dograh **run**. Runs are matched to leads by the
  `lead_ref` column in the CSV (falls back to the phone number).
- `-> Calling …` is printed when a run appears; `<- … Result: …` when the lead
  is final.
- A lead is **final** when its latest attempt is completed, its outcome is known
  (or `RESULT_WAIT_SECONDS` passed), and **no retry is expected** — i.e. the
  outcome isn't a retryable one (busy / no-answer by default) with retries left
  and the retry window (`retry_delay` + `RETRY_GRACE_SECONDS`) still open.
- The result row records the **last** attempt (`dograh_run_id`) and the number
  of attempts (`call_attempts`).
- If the campaign reaches a final state (`completed`/`failed`/`paused`/`cancelled`/`stopped`),
  leads never dialed become `call_failed`. If all active campaigns make no progress
  for `CAMPAIGN_IDLE_TIMEOUT_SECONDS`, dialed unfinished leads become `call_timeout`
  while never-dialed leads become `call_failed` (so later runs can still dial them).
- Before creating a campaign, the agent's **published** prompts are scanned for
  `{{variables}}` (same rules as Dograh). Dograh refuses a whole campaign if any
  row lacks one of them, so leads without such a value are left out and recorded
  as `skipped_missing_data`; the rest of the campaign runs normally.
- If a campaign can't be created for another reason, its leads become
  `call_failed` with Dograh's message.

Campaign progress counters on the Dograh dashboard can lag; this project
tracks the actual runs instead.

### Campaigns are never left running

Dograh doesn't always mark a finished campaign `completed` by itself (it can
stay `running` at e.g. 9/9), and it has no "complete" or "cancel" API. So:

- as soon as every lead of a campaign has its final result, the campaign is
  **paused** (`'Draftliner_2026-11-19' (campaign 21) finished - paused`);
- when the program exits for any reason — normal end, `Ctrl+C`, or an error —
  every campaign this run started that is still `running`/`syncing` is paused.
  Calls already in progress finish; no new calls are placed.

Finished campaigns therefore show as **paused** on the dashboard. If the
process is killed hard (e.g. the window is closed or the machine powers off),
the exit step can't run — pause any leftover campaign in Dograh.

### Variables sent to the agent (CSV columns)

Every lead field, with its name lower-cased and non-alphanumeric characters
replaced by `_` (`"Company Name"` → `company_name`), plus:

| Variable | Value |
|---|---|
| `phone_number` | E.164 number being called |
| `interested_service` | Service name Gemini qualified, e.g. `Draftliner` |
| `current_datetime` | Local time, e.g. `Wednesday, November 18, 2026, 11:47 PM` |
| `lead_ref` | Internal ID used to match calls to leads |

Use them in the agent's prompts as `{{company}}`, `{{location}}`,
`{{interested_service}}`, etc.

### Call outcome fields

`disposition` (e.g. `user_qualified`, `user_hangup`, `user_idle_max_duration_exceeded`,
`end_call_tool`, `no-answer`, `busy`, `failed`), `duration_seconds`, `tags`,
`transcript_url`, `recording_url`, and the full run (`raw`, minus logs).

---

## 9. Stage 4 — Results, transfers, and pipeline tracker

### `output/call_results.xlsx` (`ResultsStore`)

File: [results_store.py](results_store.py) · Class: `ResultsStore` · Sheet: `results`

- Created on first use and reused on later runs. Header row is frozen and styled.
- One row per submitted lead, whatever happened to it.
- **Grouped by day, newest day on top:**

  ```
  row 1   header
  row 2   2026-11-19            <- date row (grey, only the date)
  row 3   today 08:00 result
  row 4   today 12:00 result    <- later results of the same day go BELOW
  row 5   2026-11-18            <- earlier days move down when a new day starts
  row 6   yesterday 09:00 result
  ```

  The first result of a new day inserts a new date row directly under the
  header — also in the middle of a long run: results after midnight start the
  new day's block on top, while the run keeps using the same campaigns.
  The day comes from `processed_at` (local time). Files written in an older
  layout are regrouped into this layout when opened.
- New services add `prob_<id>` and `decision_reason_<id>` columns automatically. Lead fields get their
  own columns too, up to `RESULTS_MAX_LEAD_COLUMNS` (80) — leads are
  unstructured, so without a limit every new field name would widen the sheet.
  Fields beyond the limit are kept, as JSON, in the `other_lead_fields` column.
- Nested lead values (dicts, lists) are stored as JSON in one cell.
- **Excel safety:** `clean_excel_text()` strips XML-disallowed control characters (`ILLEGAL_CHARACTERS_RE`), strings longer than 32,000 characters are truncated, and any string starting with `=` is stored with `cell.data_type = "s"` so scraped text cannot become an Excel formula.
- **A lead field never overwrites anything.** If its name is one of this
  program's columns (e.g. the scraper's own `status`, `detail`), it is stored
  as `lead.<name>` — always the same column, so the scraper's `status` is
  always in `lead.status` and our `status` stays ours. The lead's other fields
  (e.g. its `lead_status`) are untouched.
- Saving is batched and atomic (written to a temporary file then swapped in via `os.replace`) whenever the queue of pending rows is empty.
- If the file is open in Excel (locked), the pipeline warns and keeps retrying;
  at shutdown it retries for 60 s and then saves a timestamped copy
  (`call_results_YYYYMMDD_HHMMSS.xlsx`). `load_called_numbers()` reads both `call_results.xlsx` and its timestamped fallback copies.

#### Columns (`call_results.xlsx`)

| Column | Description |
|---|---|
| `processed_at` | When the row was written |
| `pipeline_lead_id` | Sequential tracker ID (`L000001`, ...) matching `pipeline_tracker.xlsx` |
| `status` | See table below |
| `detail` | Error text or extra info |
| `phone_raw` | Phone as received |
| `phone_e164` | Normalized phone |
| `phone_location`, `phone_timezone` | US state and time zone of the number's area code |
| `interested_service` | Qualified service chosen by code (empty if none passed) |
| `interest_probability` | Highest probability across services |
| `decision_verdict`, `decision_summary` | Code-generated verdict and Gemini two-line summary |
| `decision_reason_<service_id>` | One-sentence reason per service |
| `agent_id`, `agent_name` | Dograh agent used for the call |
| `campaign_id`, `campaign_name` | Dograh campaign the number was dialed in (e.g. `Draftliner_2026-11-19`) |
| `dograh_run_id` | Run ID of the last call attempt (look it up under *Agent Runs*) |
| `call_attempts` | Number of call attempts including Dograh retries |
| `call_disposition`, `call_duration_s`, `call_tags` | Call outcome |
| `call_transferred` | TRUE when the agent transferred the call; the business is also in `TRANSFERS_FILE` |
| `transcript_url`, `recording_url` | Call artifacts |
| `dograh_response` | Full Dograh run as JSON (cut at Excel's 32k-character cell limit) |
| `prob_<service_id>` | Gemini probability for each service |
| `other_lead_fields` | Lead fields beyond the column limit (JSON) |
| *lead fields* | Original fields; stored as `lead.<name>` if the name is one of the columns above |

#### Status values

| Status | Meaning |
|---|---|
| `dropped_no_phone` / `dropped_unreadable_number` / `dropped_invalid_number` / `dropped_not_us (XX)` / `dropped_special_number (TYPE)` / `dropped_fax_only` / `dropped_already_called` / `dropped_duplicate` | Rejected by the verifier (§6) |
| `not_interested` | No service reached the threshold (shown as Non-Qualified in the dashboard) |
| `scored_qualified` | Qualified in a `--score-only` run; no call placed |
| `called` | Call completed; see disposition |
| `skipped_missing_data` | Left out of its campaign because it has no value for a `{{variable}}` the agent's prompt uses (Dograh would otherwise refuse the whole campaign) |
| `call_failed` | Campaign couldn't be created/started, the number was never dialed (including before an idle timeout), or Dograh couldn't be reached |
| `call_timeout` | A dialed call never finished before `CAMPAIGN_IDLE_TIMEOUT_SECONDS` without campaign progress |
| `decision_error` | Gemini request or validation failed after retries; lead is saved for retry |
| `interrupted` | Run stopped (`Stop` button or `Ctrl+C`) before the lead reached a final outcome |
| `error` | Unexpected error in the verifier |

### `output/transferred_calls.xlsx` (`TRANSFERS_FILE`)

- Sheet: `transferred` (`TRANSFERS_SHEET_NAME`), created on the first transferred call (`is_transferred()` matches `TRANSFER_DISPOSITIONS` — default `transfer_call`, `call_transferred` — or any disposition/tag containing `"transfer"` without `"fail"`).
- Fixed leading columns (`TRANSFER_COLUMNS`): `transferred_at`, `phone_called`, contact details (`business`, `contact_name`, `contact_title`, `email`, `website`, `address`, `city`, `state`, `postal_code`, `category`, `lead_id`), call summary (`interested_service`, `campaign_name`, `agent_name`, `call_disposition`, `call_duration_s`, `collected_by_agent`, `transcript_url`, `recording_url`, `dograh_run_id`), and `other_lead_fields`.

### `output/pipeline_tracker.xlsx` (`PipelineTracker`)

File: [pipeline_tracker.py](pipeline_tracker.py) · Class: `PipelineTracker` · Sheet: `leads`

- **Purpose:** per-lead state machine backing the live dashboard. Unlike `call_results.xlsx` (written when a lead finishes), `pipeline_tracker.xlsx` registers every lead on arrival (`L000001`, `L000002`, ...) and updates its row at each stage transition (`incoming` → `awaiting_decision` → `qualified_waiting` → `calling` → final stages `number_rejected`, `non_qualified`, `decision_error`, `not_sent`, `scored_qualified`, `call_finished`, `interrupted`).
- **Single-writer lock:** opening `PipelineTracker` acquires `output/.pipeline_tracker.lock` (using exclusive file creation + PID/host liveness detection). A second live pipeline run refuses to start with `TrackerLockedError` while another run holds the lock; the dashboard server only reads snapshots and never locks it.
- **Atomic periodic saves & chunked JSON:** a background `_tracker_worker` flushes dirty state atomically (`os.replace`) at least every `TRACKER_SAVE_SECONDS` (3s). Large `lead_json` / `dograh_json` payloads are split across `lead_json_2`, `dograh_json_2`, ... in 16,000-character chunks to stay under Excel's cell limit, sensitive keys (`token`, `secret`, `password`, `api_key`, `authorization`, `credential`, e.g. `public_access_token`) are stripped by `sanitize_dograh_data()`, and `called_numbers()` feeds `SKIP_ALREADY_CALLED`.

---

## 10. Client Dashboard & Batch Runner

Files: [dashboard_server.py](dashboard_server.py) · [dashboard/app.py](dashboard/app.py) · [dashboard/data.py](dashboard/data.py) · [dashboard/runner.py](dashboard/runner.py) · [dashboard/media.py](dashboard/media.py) · [dashboard/demo_media.py](dashboard/demo_media.py)

### Starting the server

```bash
python dashboard_server.py [--host 127.0.0.1] [--port 8090] [--tracker PATH] [--demo]
```

| Flag | Default | Meaning |
|---|---|---|
| `--host` | `DASHBOARD_HOST` (`127.0.0.1`) | Bind address. Loopback binds (`127.0.0.1`, `localhost`, `::1`) restrict `Host` headers to loopback names to block DNS rebinding; non-loopback binds print a prominent no-login security warning. |
| `--port` | `DASHBOARD_PORT` (`8090`) | HTTP listen port |
| `--tracker` | `PIPELINE_TRACKER_FILE` | Workbook to read (or `output/demo_tracker.xlsx` under `--demo`) |
| `--demo` | off | Generates ~60 fictional leads + `DemoMedia` transcripts/audio and simulates **Start Batch** offline without touching Dograh or the reverse proxy |

Security headers applied to all responses: `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, strict `Content-Security-Policy` (`default-src 'self'`), and `Cache-Control: no-store` on all `/api/*` endpoints.

### `BatchRunner` lifecycle (`dashboard/runner.py`)

- **Idle at startup:** `create_app()` initializes `BatchRunner` in `"idle"` (`active=false`) and never pings `LEAD_SOURCE_URL` or launches pipelines automatically.
- **Starting a run (`POST /api/runner/start`):** validates `batch_size` (`1`–`1,000`, default `300`) and `num_batches` (`1`–`100`, default `1`); rejects overlapping runs with `HTTP 409`. In live mode, runs `preflight()` (checking `LEAD_SOURCE_URL`, `LEAD_SOURCE_TOKEN`, `GEMINI_API_KEY`, `INTEREST_THRESHOLD`, `GEMINI_THINKING_LEVEL`, `services.yaml`, and Dograh agents) and verifies `PipelineTracker.assert_unlocked()`.
- **Direct reverse-proxy pull & resume:** for each requested batch, finishes any saved `output/pulled_batches/*.pending.json` first; otherwise pings `GET <LEAD_SOURCE_URL>/leads/next?limit=<batch_size>`, saves the batch atomically to `output/pulled_batches/`, processes it end-to-end through `default_process()`, refreshes the dashboard cache, marks the batch `.done.json`, and stops automatically (`state="completed"`) when finished or when the streamer returns `no_new_leads`.
- **Stopping (`POST /api/runner/stop`):** sets `stop_event`, triggering cooperative cancellation across pipeline queues and `CampaignJob.monitor()`, pausing active Dograh campaigns, marking unfinished leads `interrupted`, and leaving the `.pending.json` batch on disk for resume.

### HTTP API endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/health` | `GET` | Server status, `tracker_found`, and `stale` flag |
| `/api/summary` | `GET` | Funnel node counts (`incoming`, `number_verifier`, `decision_maker`, `non_qualified`, `qualified`, `calling_agent`, `call_results`, `transferred`), `activity`, `services`, `threshold`, and embedded `runner` status (including `source_url`) |
| `/api/runner` | `GET` | Current `BatchRunner` state (`idle`, `running`, `stopping`, `completed`, `stopped`, `error`) |
| `/api/runner/start` | `POST` | Start a batch run (`{"batch_size": 1..1000, "num_batches": 1..100}`, defaults `300` and `1`) |
| `/api/runner/stop` | `POST` | Request cooperative stop of the active batch run |
| `/api/leads` | `GET` | Paginated, searchable, sortable rows for `view` ∈ `incoming`, `number_verifier`, `decision_maker`, `non_qualified`, `qualified`, `calling_agent`, `call_results`, `transferred`, with optional `service`, `filter`, `q`, `sort`, `order`, `page`, `page_size` (1–200) |
| `/api/leads/{lead_id}` | `GET` | Full per-lead detail: lifecycle `journey`, `number_check`, `decision` scores & reasons, `call` outcome & `audio_tracks`, and sanitized `lead_fields` |
| `/api/leads/{lead_id}/transcript` | `GET` | Parsed conversation `turns` (`agent` / `customer` / `other`) fetched via `DograhMedia` |
| `/api/leads/{lead_id}/audio/{track}` | `GET` | Range-capable audio stream for `track` ∈ `recording`, `agent`, `customer` proxied through `DograhMedia` |

`DograhMedia` ([dashboard/media.py](dashboard/media.py)) validates storage keys (`recordings/...` or `transcripts/...` recorded on that lead), requests short-lived signed URLs from Dograh using `DOGRAH_API_KEY`, and streams content through the server so neither `DOGRAH_API_KEY` nor Dograh's signed URLs / `public_access_token` ever reach the browser.

### Frontend build (`dashboard/web/` → `dashboard/static/`)

At runtime `dashboard_server.py` serves pre-built static files from `dashboard/static/` without needing Node. To modify the React UI in `dashboard/web/src/`:

```bash
cd dashboard/web
npm install
npm run build   # runs tsc && vite build into dashboard/static/
```

---

## 11. Concurrency model

The pipeline is built on `asyncio` (see [pipeline.py](pipeline.py)):

| Stage | Workers | Input |
|---|---|---|
| Verifier | 1 | `verify` queue (max `QUEUE_MAXSIZE`) |
| Decision maker | `DECISION_CONCURRENCY` (8) | `decision` queue (max `QUEUE_MAXSIZE`) |
| Campaigns | one monitor task per campaign, all in parallel | per-service lists, launched by `flush()` / `close()` |
| Results writer | 1 | `results` queue (unbounded) |
| Tracker saver | 1 (`_tracker_worker`) | Periodic atomic save every `TRACKER_SAVE_SECONDS` |

- **Concurrent scoring:** leads are verified and scored by Gemini while more are
  still being submitted.
- **Back-pressure:** bounded queues make `submit()` wait when scoring falls
  behind, so memory use stays bounded.
- **Calls** are placed by Dograh's campaign dispatcher. Campaigns of the same run
  share the organization's concurrent-call limit (currently 1, roughly one call
  per minute).
- **Blocking HTTP** (Dograh via `requests`) runs in worker threads
  (`asyncio.to_thread`); Gemini uses its async `client.aio` API.
- **Cooperative stop (`stop_event`):** checked across `submit()`, `flush()`, `run_leads()`, and `CampaignJob.monitor()`. When set, pending queues and uncompleted calls are marked `interrupted`, live campaigns are paused, and stores are saved cleanly.
- **Shutdown:** `close()` runs `flush()` (score everything, run the campaigns,
  wait for all calls), then stops the workers, pauses any campaign of this run
  still running, saves `call_results.xlsx`, `transferred_calls.xlsx`, and `pipeline_tracker.xlsx`, and prints the summary. On `Ctrl+C`, `stop_event`, or an error,
  it skips the flush but still pauses the campaigns and saves.
- **Batches:** `run_leads()` calls `flush()` after every `batch_size` (`LEAD_BATCH_SIZE` = 300 by default)
  leads, so each batch is completely called before the next is taken.
- **Persistence:** in-flight queues live in memory only, while pulled streamer batches remain on disk as `*.pending.json` until completed and `pipeline_tracker.xlsx` saves periodically every `TRACKER_SAVE_SECONDS`.

---

## 12. Python API reference

### `LeadPipeline` ([pipeline.py](pipeline.py))

```python
LeadPipeline(
    results_file: str | Path = None,   # default RESULTS_FILE (or score_only_<ts>.xlsx if score_only=True)
    threshold: float = None,           # default INTEREST_THRESHOLD
    services_file: str | Path = None,  # default services.yaml
    score_only: bool = False,          # True = skip Dograh entirely
    tracker_file: str | Path = None,   # default PIPELINE_TRACKER_FILE (or <results_stem>_tracker.xlsx if score_only)
    source_label: str = "python",
    stop_event: Any = None,            # optional threading.Event for cooperative cancellation
)
await pipeline.start()                 # acquire tracker lock, resolve agents, start workers
await pipeline.submit(lead: dict)      # add one lead (waits if queues are full)
await pipeline.flush()                 # score what's submitted, run one campaign per service, wait for calls
await pipeline.close(abort=False)      # flush (unless abort), pause campaigns, save stores & tracker
pipeline.stats                         # Counter of statuses
pipeline.failed_decision_leads         # list of leads that hit decision_error
```

Use it as an async context manager (`start` / `close` are called for you):

```python
async with LeadPipeline() as pipeline:
    async for lead in scraper():
        await pipeline.submit(lead)
```

### Helpers

```python
await run_leads(leads, batch_size=None, failed_leads=None, stop_event=None, **pipeline_kwargs)
run_leads_sync(leads, batch_size=None, failed_leads=None, stop_event=None, **pipeline_kwargs)

from main import run
run(source, *, sheet=None, limit=None, threshold=None, output=None,
    score_only=False, source_label=None, on_failed=None) -> int
```

### Scraper & Reverse Proxy integration

**Lead streamer (live reverse-proxy setup).** Nothing is pulled or called on startup.
Use `python dashboard_server.py` (click **Start Batch**) or `python pull_leads.py`
(press **Enter** before each ping, including the first) to pull batches directly from
the reverse proxy `GET https://ai-sdr.grobots.ai/leads/next` (optionally with `?limit=<batch_size>`, `Authorization: Bearer <LEAD_SOURCE_TOKEN>`),
save each batch to `PULLED_BATCHES_DIR`, and run it through `run_leads()` until every
call is finished. `main.py` has `USE_STREAMER = False` by default; `Puller.run_auto()`
is only used if `USE_STREAMER` is explicitly set to `True` in `main.py`.

| Streamer answer | Dashboard (`BatchRunner`) | Interactive CLI (`pull_leads.py`) | Opt-in Auto (`USE_STREAMER=True`) |
|---|---|---|---|
| `/health` is not the live streamer | not polled on startup; errors on `/leads/next` failure | not checked before a ping | waits, checks again every `STREAMER_CHECK_SECONDS` |
| `200 "ok"` + leads | save → process `N` batches → stop idle | save → process fully → ask | save → process fully → next ping |
| `200 "no_new_leads"` | complete early (`state="completed"`) | say so → ask | check again every `STREAMER_CHECK_SECONDS` |
| `503` busy / database down | finish with `state="error"` (safe to retry) | show it → ask | wait `retry_after_seconds` or check interval |
| `404`, HTML page, TLS/network error, timeout | finish with `state="error"` (no leads consumed if pre-HTTP) | explain → ask | explain once → check again |
| `401` token / `421` host / `503 schema_mismatch` | finish with `state="error"` (fix `.env` / proxy) | stop (exit code 2) | stop (exit code 2) |

Unfinished batches (`*.pending.json`) are always finished before new leads are taken (on Dashboard **Start Batch**, CLI resume prompt, or auto mode). In all modes `preflight()` validates Gemini (`check_gemini_access`), `services.yaml`, each Dograh agent, and reverse-proxy settings before pulling leads. `python pull_leads.py --check` runs the same checks plus the streamer's `/health` and `/stats` (with token) and exits without taking any leads. SIGTERM and SIGHUP are turned into `SIGINT` (`stop_signals_act_like_ctrl_c()`) so campaigns pause cleanly and pending batches are preserved. See [lead_streamer/LEAD_STREAMER_SPEC.md](lead_streamer/LEAD_STREAMER_SPEC.md) §7, §12, §16 for the streamer contract.

**In-process.**

The scraper only needs to produce dicts with a phone field and descriptive
fields (company, category, description, location, …) and call
`pipeline.submit(lead)`. Example: [examples/from_python.py](examples/from_python.py).

Campaigns are created when the input ends (`close()`), because Dograh campaigns
can't take new numbers after creation. If the scraper runs continuously, call
`await pipeline.flush()` at a sensible interval (e.g. every 100 leads or every
hour) to dial what has been collected so far.

### Using stages on their own

```python
from number_verifier import NumberVerifier
NumberVerifier().verify({"phone": "(512) 555-0101"})   # Verification(ok=True, e164='+15125550101', ...)

from decision_maker import DecisionMaker, load_services
dm = DecisionMaker(load_services(path), 0.80, api_key, "gemini-3.8-flash")
decision = await dm.decide(lead)                       # decision.probabilities, decision.service, decision.summary

from dograh_client import DograhClient
from campaign_manager import CampaignJob, CampaignLead
client = DograhClient()
job = CampaignJob(service, client.get_agent(service.agent_id),
                  [CampaignLead(ref="L1", phone="+15125550101", lead=lead)])
job.launch(client)                                     # creates + starts "<service>_<date>"
await job.monitor(client, on_call_started, on_result)  # callbacks per call / final outcome
```

---

## 13. Extending the system

| Task | How |
|---|---|
| Add a service | Add a block to [services.yaml](services.yaml) with its own `agent_id`; no code change. See [SERVICES.md](SERVICES.md) and [ADDING_A_SERVICE.md](ADDING_A_SERVICE.md). |
| Change which agent calls a service | Change its `agent_id` in services.yaml. |
| Pause a service | `enabled: false`. |
| Be stricter / looser | `INTEREST_THRESHOLD` in `.env` or `--threshold`. |
| Allow more simultaneous calls | Raise the Dograh organization's concurrent-call limit. |
| Change retries | `CAMPAIGN_MAX_RETRIES`, `CAMPAIGN_RETRY_*` in `.env`. |
| Change campaign naming | `CAMPAIGN_NAME_DATE_FORMAT`, or `campaign_name()` in [campaign_manager.py](campaign_manager.py). |
| Accept other countries | Set `ALLOWED_PHONE_REGIONS` in `.env` (comma-separated ISO codes, e.g. `US,CA`). |
| Add another input format | Add a branch to `read_leads()` in [main.py](main.py). |
| Store results elsewhere | Replace `ResultsStore` (it only needs `append(row, lead)`, `save()`, and `.path`). |

---

## 14. Helper tools & testing

### `tools/compare_scores.py`

Compares a `--score-only` workbook against historical `call_results.xlsx` scores (matched by `phone_e164`) with phone numbers masked:

```bash
python tools/compare_scores.py output/score_only_20261124_120000.xlsx output/call_results.xlsx
```

### `tools/make_demo_tracker.py`

Generates ~60 fictional leads (names containing `"Fictional"`, `555-01xx` phone numbers) across every pipeline stage and refuses to overwrite the live `PIPELINE_TRACKER_FILE`:

```bash
python tools/make_demo_tracker.py --out output/demo_tracker.xlsx
```

### Automated test suite

All tests run 100% offline using local HTTP servers, local TLS reverse proxies, and fake Gemini/Dograh clients:

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

---

## 15. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Gemini API key not found` | Add `GEMINI_API_KEY=...` (or `GEMINI_API=...`) to `.env`. |
| `Dograh API key not found` | Add `DOGRAH_API_KEY=...` to `.env`. |
| `Dograh rejected the API key (HTTP 401)` | Key was created in a different Dograh deployment than `DOGRAH_BASE_URL`, or it was archived. |
| `Dograh agent N does not exist` | Wrong `agent_id` in services.yaml (see the ID column on Dograh's *Voice Agents* page). |
| Startup warning `has unpublished changes` | The agent has edits saved as a draft. Calls use the last **published** version — publish the agent in Dograh. |
| Intermittent `SSL: CERTIFICATE_VERIFY_FAILED` / `Hostname mismatch` from `https://ai-sdr.grobots.ai` | Stale nginx workers on the scraper server still serving another site's certificate. Fix on the scraper server with `sudo nginx -t && sudo systemctl restart nginx` (see [GO_LIVE.md](GO_LIVE.md#if-pull_leadspy---check-fails)). |
| `Tracker is locked` / `TrackerLockedError` | Another live pipeline run or `BatchRunner` job is already using `output/pipeline_tracker.xlsx` (`.pipeline_tracker.lock`). Wait for it to finish or stop it cleanly. |
| `HTTP 409` on `POST /api/runner/start` | A batch run is already active in the dashboard; monitor it or click **Stop** first. |
| Dashboard returns `400 This address is not allowed.` | Loopback-bound server (`DASHBOARD_HOST=127.0.0.1`) received a non-loopback `Host` header (DNS rebinding protection). Access via `http://127.0.0.1:8090` or `http://localhost:8090`, or bind explicitly with `--host`. |
| Every lead is `not_interested` | Service descriptions too narrow, threshold too high, or leads lack descriptive fields. Check the `prob_*` columns in the results or **Decision Maker** view in the dashboard. |
| Many `decision_error` rows | Gemini rate limit or network issue; lower `DECISION_CONCURRENCY`. Failed leads are saved for retry (`*.pending.json` or `decision_retry_*.json`). |
| `FAILED to start '<service>_<date>'` | Dograh refused the campaign; the message says why (telephony not configured, quota, invalid CSV, ...). |
| Leads marked `skipped_missing_data` | The agent's prompt uses a `{{variable}}` those leads have no value for. Add the field to the leads, give the variable a default in the prompt (`{{company | there}}`), or remove it. |
| Calls stay on "Calling…" for a long time | Normal while the conversation lasts. If calls never finish, Dograh is not receiving the telephony provider's status callbacks — check the deployment's public backend URL. Unfinished dialed leads end as `call_timeout` (and undialed leads as `call_failed`) after `CAMPAIGN_IDLE_TIMEOUT_SECONDS` without progress. |
| Long pauses between calls | One concurrent-call slot shared by all campaigns, plus retry delays. |
| A lead waits ~4 min after "no-answer" | It is waiting for Dograh's retry (`CAMPAIGN_RETRY_DELAY_SECONDS` + `RETRY_GRACE_SECONDS`). |
| `results ... is open in another program` | Close the spreadsheet in Excel. |
| Agent says something unexpected | Check which version is published in Dograh and what the agent's prompt does with the variables in §8. |

---

## 16. Limitations and known behaviour

- **In-memory queues.** In-flight queues are not persisted across crashes, though
  already-written results are saved atomically to the spreadsheets and cross-run
  repeat calls are blocked when `SKIP_ALREADY_CALLED=true` (reading `call_results.xlsx`,
  timestamped fallback copies, and `pipeline_tracker.xlsx`). Within a single run, `self._seen` also prevents
  calling or re-scoring the same number twice.
- **Campaigns are created after scoring.** Dograh can't add numbers to an
  existing campaign, so campaigns are created when the input ends (or on
  `flush()`), not at the start of the run. Services with no interested leads
  get no campaign.
- **One campaign per service per run/flush.** Two runs on the same day create
  two campaigns with the same name.
- **No voicemail detection** in this project; it must be handled by the Dograh
  agent or telephony provider.
- **US only.** By design, all non-US numbers are dropped.
- **Gemini model pinning.** The model defaults to `GEMINI_MODEL=gemini-3.8-flash`
  with `GEMINI_THINKING_LEVEL=MEDIUM`. Failed scoring requests are saved to
  `decision_retry` files/batches rather than treated as non-qualified.

---

## 17. Compliance

AI-generated-voice calls to US numbers are governed by the TCPA: calls to mobile
numbers generally require prior express consent (written consent for
telemarketing), numbers must be checked against Do-Not-Call lists, and calls
must respect permitted hours in the recipient's time zone. Leads gathered by
scraping usually come with no consent. Obtain legal guidance before calling
scraped leads at scale.
