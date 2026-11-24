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
5. **Call tracking:** each call attempt (and Dograh retry) is followed until a final outcome (`called` = someone reached, `call_failed` = nobody reached or never dialed, `call_timeout`, or `interrupted`). Leads that were never dialed — and, with **Retry failed calls**, leads nobody answered — are saved to be called again in a later batch.
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
├── pipeline_tracker.py  Per-lead Excel tracker (output/pipeline_tracker.xlsx) with .pipeline_tracker.lock and archive
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
    ├── pipeline_tracker_archive.xlsx        Tracker rows older than TRACKER_ARCHIVE_DAYS
    ├── .pipeline_tracker_archive_index.json Their blocked numbers + next lead number (keep it)
    ├── decision_retry_*.json / call_retry_*.json   Leads to retry from file / Python runs
    └── pulled_batches/  Saved streamer batches: *.pending.json (+ *.progress.json) until finished, then *.done.json
```

---

## 4. Installation and configuration

### Requirements

- Python 3.10+
- `pip install -r requirements.txt`
  (`google-genai`, `phonenumbers`, `requests`, `openpyxl`, `PyYAML`, `python-dotenv`, `fastapi`, `uvicorn`,
  `tzdata` — time zone data, which Windows lacks)

### `.env`

Settings are read from real environment variables first, then from **one**
`.env` file: `dograh_caller/.env`, or — only if that file does not exist —
`../.env` (parent folder). The two files are never mixed, so keys from two
different Dograh deployments can't end up combined.

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
| `SKIP_ALREADY_CALLED` | `true` | Block numbers already called in earlier runs (read from `call_results.xlsx` — including timestamped fallback copies, and also when a run writes elsewhere with `--output` — plus `pipeline_tracker.xlsx` and its archive). Numbers whose call reached nobody are not blocked; see §6 |
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
| `CAMPAIGN_IDLE_TIMEOUT_SECONDS` | `900` | If no call is in progress and nothing happens for this long, dialed unfinished leads become `call_timeout` and never-dialed ones `call_failed` |
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
| `PULLED_BATCHES_DIR` | `output/pulled_batches` | Received batches (`.pending.json` until fully processed, then `.done.json`; `.progress.json` lists the leads already finished) |
| `RESULTS_MAX_LEAD_COLUMNS` | `80` | Most lead fields with their own column; further fields go (as JSON) into `other_lead_fields` |
| `RESULTS_SAVE_SECONDS` | `15` | Results files are rewritten at most this often during a run (always at the end) |
| `TRACKER_ARCHIVE_DAYS` | `30` | Finished tracker rows older than this move to `pipeline_tracker_archive.xlsx` at run start; their numbers stay blocked (0 = never) |
| `CAMPAIGN_MAX_CALL_SECONDS` | `3600` | A call still in progress counts as progress (no idle timeout) until it is this old |
| `STOP_SYNC_SECONDS` | `2` | After Stop pauses a campaign, wait this long, then record calls Dograh started at that moment |
| `NOT_REACHED_DISPOSITIONS` | `busy,no-answer,no_answer,failed,error,canceled,cancelled,voicemail,machine` | Outcomes meaning nobody was reached: recorded as `call_failed`, number not blocked |
| `RETRY_FAILED_CALLS` | `false` | Save leads whose call reached nobody to call again in a later batch (dashboard checkbox / `--retry-failed-calls` override it). Never-dialed leads are always saved |
| `RETRY_MAX_ATTEMPTS` | `3` | Most times one lead is saved for a retry (Gemini failure or failed call) |
| `RECOVER_CALLS_DAYS` | `7` | At run start, fetch the final result of calls an earlier run stopped during (0 = off) |
| `DOGRAH_SEND_ALL_FIELDS` | `false` | `false`: the contacts CSV holds only the `{{variables}}` the published agent uses plus `DOGRAH_EXTRA_FIELDS`; `true`: every lead field |
| `DOGRAH_EXTRA_FIELDS` | `company,company_name,business_name,contact_name,contact_title,category,industry,city,state,location,lead_id` | Lead fields always sent to the agent |

Relative paths (e.g. `RESULTS_FILE=output/x.xlsx`) are relative to
`dograh_caller/`, whatever folder the program is started from. true/false
settings accept `true/false`, `yes/no`, `on/off`, `y/n`, `1/0` (any case);
anything else keeps the default and prints a warning.

---

## 5. Running the pipeline

### Recommended: Dashboard server (on-demand reverse proxy)

```bash
python dashboard_server.py
```

- Starts **idle** at `http://127.0.0.1:8090` (`Lead source (reverse proxy): https://ai-sdr.grobots.ai`). Nothing is pulled or called until you click **Start Batch** (or `POST /api/runner/start`).
- Choose `Leads per batch` (`1`–`1,000`, default `300`) and `Number of batches` (`1`–`100`, default `1`), tick **Retry failed calls** if leads nobody answered should be called again in a later batch, or click **Stop** at any time to pause active Dograh campaigns and keep unfinished leads pending for resume.
- Saved leads from an earlier run (stopped batches, retry batches) are processed first, in pieces of the chosen batch size; the Start dialog says how many there are.
- Pass `--demo` to preview the UI with ~60 fictional leads and offline simulated batch runs. See [§10](#10-client-dashboard--batch-runner) for all options and API details.

### Interactive terminal puller (`pull_leads.py`)

```bash
python pull_leads.py --check   # validate keys, Dograh agents, reverse proxy /health and /stats (takes no leads)
python pull_leads.py           # stays idle until you press Enter before every ping (including the first)
```

Options: `--url <URL>`, `--threshold <0-1>`, `--output <path.xlsx>`, `--check`,
`--retry-failed-calls` / `--no-retry-failed-calls` (default `RETRY_FAILED_CALLS`).
A saved batch from an earlier run is offered first and processed whole (only its
unfinished leads, see §10).

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

run(
    source,
    *,
    sheet=None,
    limit=None,
    threshold=None,
    output=None,
    score_only=False,
    source_label=None,
    on_failed=None,
    retry_failed_calls=None,
    on_call_retry=None,
) -> int
```

`source` may be a **file path**, **one lead dict**, or a **list of lead dicts**.
Returns `0` on success, `1` on error, `130` if interrupted.

Leads to retry are handed to `on_failed` (Gemini failures) and `on_call_retry`
(calls to try again); without them they are written to
`output/decision_retry_<time>.json` / `output/call_retry_<time>.json`, each with a
ready-to-run `python main.py <file>` command. This also happens when the run ends
with an error or `Ctrl+C`. `retry_failed_calls` = also keep leads whose call
reached nobody (default `RETRY_FAILED_CALLS`).

### Command-line overrides (optional)

```bash
python main.py [file] [--threshold 0.9] [--output path.xlsx] [--sheet NAME] [--limit N] [--score-only]
               [--retry-failed-calls | --no-retry-failed-calls]
```

| Option | Effect |
|---|---|
| `file` | Use this file once instead of `INPUT_FILE_PATH` / `INPUT_LEADS`, without using the streamer |
| `--threshold` | Override `INTEREST_THRESHOLD` for this run (0–1) |
| `--output` | Write results to another .xlsx (numbers in the main `call_results.xlsx` stay blocked) |
| `--sheet` | Worksheet to read (default: `INPUT_SHEET` or first sheet) |
| `--limit` | Process only the first N leads (overrides `LIMIT`) |
| `--score-only` | Verify and score with Gemini without creating Dograh campaigns or placing calls (outputs `output/score_only_<timestamp>.xlsx`) |
| `--retry-failed-calls` | Also save leads whose call reached nobody to `output/call_retry_<time>.json` (overrides `RETRY_FAILED_CALLS`; `--no-retry-failed-calls` turns it off) |

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
results so far are saved, and leads still in progress are recorded as
`interrupted`. In a file / Python run, leads waiting for a retry (Gemini
failures, calls to try again) are still written to their retry file; in a
streamer run they stay in the saved batch, which is resumed later. `kill`, `systemctl stop` and a dropped
SSH session (SIGTERM / SIGHUP) act exactly like `Ctrl+C` — for `main.py`,
`pull_leads.py` and file runs alike. Calls that were still going are looked up
the next time a run starts (§8).

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
| None of the lead's good numbers was **already called in an earlier run** (see below) | `dropped_already_called` |
| None was already seen earlier **in this run** | `dropped_duplicate` |

**Already called.** At startup these numbers are blocked, so restarts and later
batches never call the same business twice:

- every number with status `called` or `call_timeout` in the results sheet
  (`RESULTS_FILE`, its timestamped fallback copies, and — when a run writes
  elsewhere with `--output` — that file too);
- every number in `pipeline_tracker.xlsx` (and its archive) that was answered,
  timed out, or that Dograh dialed with an **unknown** outcome (e.g. the run
  stopped mid-call, or contact with Dograh was lost).

Not blocked, so evaluated again: leads that were `not_interested`, skipped,
never dialed, and leads whose call is known to have **reached nobody**
(`call_failed` with a disposition in `NOT_REACHED_DISPOSITIONS` — busy, no
answer, failed, voicemail). Turn the check off with `SKIP_ALREADY_CALLED=false`.

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

A value counts as a phone number only when it is mostly digits **and** written
like one (digits, spaces, `+ ( ) - . /`, `ext`). Money amounts and grouped
figures (`$1,250,000,000`, `10,000,000 - 25,000,000`, `USD 12,500,000.50`) and
long plain numbers in revenue / size / count fields (`annual_revenue`,
`employees`, …) are business information and are kept.

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
- For **streamer / dashboard runs**, failed leads are saved as a new `batch_*.pending.json` in `output/pulled_batches/` (`source="decision_retry"`). The **next** Start (not the job that created it) retries them automatically. After a **Stop** nothing is saved separately: the stopped batch is resumed later with these leads in it.
- For **file / Python runs** (`main.run()`), failed leads are written to `output/decision_retry_<timestamp>.json` with a ready-to-run `python main.py ...` command — also when the run ends with an error or `Ctrl+C`.
- Each saved lead carries a small retry counter (key `_dograh_caller`, removed again before the lead is processed, so Gemini, Dograh and the sheets never see it). After `RETRY_MAX_ATTEMPTS` (3) retries a lead is no longer saved; the run's summary line counts these.

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
| Startup | `GET /campaign/{id}/runs` | For calls an earlier run stopped during (`RECOVER_CALLS_DAYS`): fetch their final result |
| Per campaign | `GET /workflow/{agent_id}/versions` | Find the `{{variables}}` the published agent uses / needs |
| Per campaign | `POST /s3/presigned-upload-url` + `PUT` | Upload the contacts CSV (again if a retry without some leads is needed) |
| Per campaign | `POST /campaign/create` | Create `<service>_<date>` with the agent, CSV and retry config |
| Per campaign | `POST /campaign/{id}/start` | Start dialing |
| Every `CAMPAIGN_POLL_SECONDS` | `GET /campaign/{id}` and `GET /campaign/{id}/runs` | State, retry config, and one run per call attempt |
| Campaign done, Stop, or program exits | `POST /campaign/{id}/pause` | Make sure the campaign isn't left running |
| After Stop | `GET /campaign/{id}/runs` | Last look: record calls that just started, take results of calls that ended |

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
- **Outcome → status.** A call whose disposition is in `NOT_REACHED_DISPOSITIONS`
  (busy, no-answer, failed, error, canceled, voicemail, machine) reached nobody:
  it is recorded as `call_failed` ("Call did not reach anyone (busy)"), never as
  `called`, and its number is not blocked for later runs. Any other completed
  call — and every transferred call — is `called`.
- **Long calls.** A call still in progress counts as progress, so a long call (a
  transfer to a person can last a long time) is never cut off by the idle
  timeout. Only once it has been going for `CAMPAIGN_MAX_CALL_SECONDS` (1 hour)
  is it treated as stuck; then the idle timeout applies.
- If the campaign reaches a final state (`completed`/`failed`/`paused`/`cancelled`/`stopped`),
  leads never dialed become `call_failed`. If no call is in progress and all
  active campaigns make no progress for `CAMPAIGN_IDLE_TIMEOUT_SECONDS`, dialed
  unfinished leads become `call_timeout` while never-dialed leads become
  `call_failed`.
- **Calls to try again.** Never-dialed `call_failed` leads (campaign not
  created/started, "Not dialed: …") are always saved for a later batch. Leads
  whose call reached nobody are saved too when **Retry failed calls** is on
  (`RETRY_FAILED_CALLS`, the dashboard checkbox, `--retry-failed-calls`). Leads
  whose outcome is unknown ("Lost contact with Dograh") are not retried — they
  may have been answered. Streamer/dashboard runs save them as a pending batch
  with `source="call_retry"`; file runs write `output/call_retry_<time>.json`.
  At most `RETRY_MAX_ATTEMPTS` (3) times per lead.
- Before creating a campaign, the agent's **published** prompts are scanned for
  `{{variables}}` (same rules as Dograh). Dograh refuses a whole campaign if any
  row lacks one of them, so leads without such a value are left out and recorded
  as `skipped_missing_data`; the rest of the campaign runs normally.
- If Dograh still refuses the campaign because of its own contacts check
  ("…missing from the source data: 'x'" / "Template variable 'x' is empty…"), the
  named variables are added to the required ones, the leads without them are
  left out (`skipped_missing_data`) and the campaign is created again (up to 5
  times). The log shows a `NOTE` when this happens.
- If a campaign can't be created for another reason, its leads become
  `call_failed` with Dograh's message (and are saved to be tried again).
- If following one campaign fails unexpectedly, that campaign is paused and its
  unfinished leads become `interrupted`; the other campaigns carry on.

### After a Stop, and at the next start

- **Stop:** each campaign is paused, then after `STOP_SYNC_SECONDS` (2 s) the
  program looks at its calls once more: a call Dograh started at that moment is
  recorded (its number is never dialed again) and calls that already ended get
  their real result. Leads still on the phone become `interrupted`.
- **Next start** (any live run): tracker rows from the last `RECOVER_CALLS_DAYS`
  (7) days that were put in a campaign but stopped or timed out without an
  outcome are looked up in their Dograh campaign. A finished call gets its real
  result in the tracker and a new row in `call_results.xlsx` (detail "Result
  recovered after the run had stopped"); a call still going gets its run ID
  recorded, so the number stays blocked. This runs before the already-called
  list is built.

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

Lead field names are lower-cased with non-alphanumeric characters replaced by
`_` (`"Company Name"` → `company_name`). By default the CSV holds **only**:

- the lead fields the agent's published version refers to anywhere — prompts,
  greetings, transition speeches, tool settings — with or without a default
  (`{{company}}`, `{{city | there}}`, `{{initial_context.city}}`);
- the fields in `DOGRAH_EXTRA_FIELDS` (company and business names, contact name
  and title, category, industry, city, state, location, lead_id);
- the variables below.

Large text fields, e-mails, raw data and the like are no longer sent. To send
another field, use it as a `{{variable}}` in the agent or add it to
`DOGRAH_EXTRA_FIELDS`; `DOGRAH_SEND_ALL_FIELDS=true` restores sending every
field. If two lead fields map to the same variable (`Company Name` and
`company_name`), the first non-empty one is kept and the log shows a `NOTE`.

| Variable | Value |
|---|---|
| `phone_number` | E.164 number being called |
| `interested_service` | Service name Gemini qualified, e.g. `Draftliner` |
| `current_datetime` | Date and time **in the lead's time zone** (from its area code), e.g. `Wednesday, November 18, 2026, 09:47 AM CST` |
| `current_date` | Same, date only, e.g. `Wednesday, November 18, 2026` |
| `lead_timezone` | The lead's IANA time zone, e.g. `America/Chicago` (absent for toll-free numbers) |
| `lead_ref` | Internal ID used to match calls to leads |

`current_datetime` / `current_date` are fixed when the campaign is created (a
campaign's contacts can't change later), so with many calls they can be hours
old by the time a lead is dialed. For the time **at the moment of the call**,
use Dograh's built-in `{{current_time_<Area/City>}}` (e.g.
`{{current_time_America/Chicago}}`) in the prompt. Toll-free numbers have no
time zone; they get the server's local time.

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
- Saving is atomic (written to a temporary file then swapped in via `os.replace`). Each save rewrites the whole workbook, so during a run the results files are saved at most every `RESULTS_SAVE_SECONDS` (15 s) rather than after every result; the final save when the run ends always happens. A file open in Excel can therefore lag the run by a few seconds.
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
| `called` | Someone was reached (or the call was transferred); see disposition |
| `skipped_missing_data` | Left out of its campaign because it has no value for a `{{variable}}` the agent's prompt uses (Dograh would otherwise refuse the whole campaign) |
| `call_failed` | The call reached nobody (`NOT_REACHED_DISPOSITIONS`: busy, no-answer, failed, voicemail — saved for retry only with `RETRY_FAILED_CALLS`), or it was never dialed (campaign couldn't be created/started, idle timeout — always saved for retry, at most `RETRY_MAX_ATTEMPTS` times), or Dograh couldn't be reached (not retried). Not-reached numbers are not blocked as "already called" |
| `call_timeout` | A dialed call's result never arrived: no progress for `CAMPAIGN_IDLE_TIMEOUT_SECONDS`, or the call was still going after `CAMPAIGN_MAX_CALL_SECONDS` |
| `decision_error` | Gemini request or validation failed after retries; lead is saved for retry |
| `interrupted` | Run stopped (`Stop` button, `Ctrl+C`, kill, or an error) before the lead reached a final outcome. A call that was still going gets its real result at the next start (§8) |
| `error` | Unexpected error in the verifier |

A lead can therefore have two rows: `interrupted`, then later the recovered
call result, or a retried lead's new outcome. The newest row is the current one.

### `output/transferred_calls.xlsx` (`TRANSFERS_FILE`)

- Sheet: `transferred` (`TRANSFERS_SHEET_NAME`), created on the first transferred call (`is_transferred()` matches `TRANSFER_DISPOSITIONS` — default `transfer_call`, `call_transferred` — or any disposition/tag containing `"transfer"` without `"fail"`).
- Fixed leading columns (`TRANSFER_COLUMNS`): `transferred_at`, `phone_called`, contact details (`business`, `contact_name`, `contact_title`, `email`, `website`, `address`, `city`, `state`, `postal_code`, `category`, `lead_id`), call summary (`interested_service`, `campaign_name`, `agent_name`, `call_disposition`, `call_duration_s`, `collected_by_agent`, `transcript_url`, `recording_url`, `dograh_run_id`), and `other_lead_fields`.

### `output/pipeline_tracker.xlsx` (`PipelineTracker`)

File: [pipeline_tracker.py](pipeline_tracker.py) · Class: `PipelineTracker` · Sheet: `leads`

- **Purpose:** per-lead state machine backing the live dashboard. Unlike `call_results.xlsx` (written when a lead finishes), `pipeline_tracker.xlsx` registers every lead on arrival (`L000001`, `L000002`, ...) with an optional `batch_id` (set per `BatchRunner` job so each job is isolated in the UI while cross-batch `called_numbers()` still protects `SKIP_ALREADY_CALLED`; re-processing a lead under a new `batch_id` creates a new row linked via `previous_lead_id`), and updates its row at each stage transition (`incoming` → `awaiting_decision` → `qualified_waiting` → `calling` → final stages `number_rejected`, `non_qualified`, `decision_error`, `not_sent`, `scored_qualified`, `call_finished`, `interrupted`).
- **Single-writer lock:** opening `PipelineTracker` acquires an OS-level non-blocking lock on `output/.pipeline_tracker.lock` (`msvcrt.locking` on Windows, `fcntl.flock` on Unix/Linux), automatically released by the OS on exit or crash. A second live pipeline run refuses to start with `TrackerLockedError` while another run holds the lock; the dashboard server only reads snapshots and never locks it.
- **Atomic periodic saves & chunked JSON:** a background `_tracker_worker` flushes dirty state atomically (`os.replace`) at least every `TRACKER_SAVE_SECONDS` (3s). Large `lead_json` / `dograh_json` payloads are split across `lead_json_2`, `dograh_json_2`, ... in 16,000-character chunks to stay under Excel's cell limit, sensitive keys (`token`, `secret`, `password`, `api_key`, `authorization`, `credential`, e.g. `public_access_token`) are stripped by `sanitize_dograh_data()`, and `called_numbers()` feeds `SKIP_ALREADY_CALLED`.
- **Archive:** the tracker is rewritten every few seconds and read in full by the dashboard, so it is kept small. When a run opens it, finished rows not updated for `TRACKER_ARCHIVE_DAYS` (30) days move to `output/pipeline_tracker_archive.xlsx` (same layout). Their blocked numbers and the next free lead number are kept in `output/.pipeline_tracker_archive_index.json`, so archived numbers are never called again and lead IDs are never reused. Archived batches no longer appear in the dashboard's Batches list or lead details. `TRACKER_ARCHIVE_DAYS=0` turns archiving off.

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
- **Starting a run (`POST /api/runner/start`):** validates `batch_size` (`1`–`1,000`, default `300`) and `num_batches` (`1`–`100`, default `1`) and takes the optional `retry_failed_calls` (default `RETRY_FAILED_CALLS`); rejects overlapping runs with `HTTP 409`. Generates a unique `batch_id` (`job_<timestamp>_<hex>`) so the dashboard immediately clears previous history and shows only the active job's leads (while previous batches remain accessible from the top-bar **Batches** dropdown). In live mode, runs `preflight()` (checking `LEAD_SOURCE_URL`, `LEAD_SOURCE_TOKEN`, `GEMINI_API_KEY`, `INTEREST_THRESHOLD`, `GEMINI_THINKING_LEVEL`, `services.yaml`, and Dograh agents) and verifies `PipelineTracker.assert_unlocked()`.
- **Direct reverse-proxy pull & resume:** for each requested batch, first takes the next `batch_size` **unfinished** leads of the oldest saved `output/pulled_batches/*.pending.json`; only when none is left does it ping `GET <LEAD_SOURCE_URL>/leads/next?limit=<batch_size>` and save the new batch atomically to `output/pulled_batches/`. Each piece is processed end-to-end through `default_process()`, the dashboard cache is refreshed, and the batch file becomes `.done.json` once all its leads are finished. The runner stops automatically (`state="completed"`) when the requested batches are done or the streamer returns `no_new_leads`.
- **Progress per lead:** every lead that reaches a final result (anything except `interrupted`, and except leads saved for a retry) is recorded in `<batch>.progress.json` as it finishes. A stopped or crashed batch therefore resumes with only the unfinished leads — none is scored or called twice. A saved batch of 1,000 leads with **Leads per batch** = 100 is worked through in pieces of 100, one per requested batch.
- **Retry batches:** batches the job itself saves (`decision_retry`, `call_retry`) are left for the **next** Start; a job only resumes batches that existed when it started, so it never spends its batches re-running its own failures. The completion message counts leads saved for retry.
- **Saved leads in the Start dialog:** `GET /api/runner` reports `pending_batches` / `pending_leads` (unfinished saved leads); the Start dialog warns that they will be processed first.
- **Stopping (`POST /api/runner/stop`):** sets `stop_event`, triggering cooperative cancellation across pipeline queues and `CampaignJob.monitor()`, pausing active Dograh campaigns, taking the last look at their calls (§8), marking unfinished leads `interrupted`, and leaving the `.pending.json` batch (with its progress file) on disk for resume.

### Batch scoping & history dropdown (`BatchHistoryDropdown.tsx`)

- **Current-job view:** each live `BatchRunner` job creates a unique `batch_id` (`job_<timestamp>_<hex>`, shared across all `num_batches` in that job), immediately clearing the live dashboard to `0` and displaying only that job's leads across the funnel, stage modals (`ViewModal`), and `TransferredPanel`. (`--demo` simulation keeps `batch_id=None` so the fictional demo tracker rows remain visible.)
- **Top-bar Batches dropdown:** lists all recorded batches newest-first (`Batch #N`, ..., `"Earlier runs"` for legacy pre-`batch_id` rows, plus `"All Batches"` combined history when multiple batches exist). Selecting an older batch displays a banner with a one-click button back to the latest/live batch. Cross-batch `SKIP_ALREADY_CALLED` deduplication (`called_numbers()`) still scans all batches in `pipeline_tracker.xlsx`, its archive and `call_results.xlsx`. Batches older than `TRACKER_ARCHIVE_DAYS` are archived (§9) and no longer listed.

### Dashboard UI overview (`dashboard/web/`)

- **Pipeline flow & funnel:** visual stage nodes (`Incoming Leads` → `Number Verifier` → `Decision Maker` → `Qualified` / `Non-Qualified` → `Calling Agent` → `Call Results`), live activity indicators, conversion funnel card (`FunnelCard`), attention summary (`AttentionCard`), and slide-out **Transferred Calls** side panel (`TransferredPanel`).
- **Stage tables (`ViewModal`):** clicking any node opens a paginated (`50`/page), searchable, sortable table with stage status filter chips and service filter.
- **Lead detail modal (`LeadDetailModal`):** clicking any lead opens its 6-step lifecycle timeline (`Received` → `Number Check` → `Decision` → `Campaign` → `Call` → `Result`), per-service Gemini probabilities and reasons, raw lead fields, Dograh call details, parsed conversation transcript, and audio player supporting up to 3 tracks (`recording` = Full call, `agent` = AI agent, `customer` = Customer).

### HTTP API endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/health` | `GET` | Server status, `tracker_found`, and `stale` flag |
| `/api/batches` | `GET` | Recorded batches newest-first (`batch_id`, `label`, counts, `is_active`, `is_legacy`), plus `active_batch_id` and `latest_batch_id` |
| `/api/summary` | `GET` | Funnel node counts (`incoming`, `number_verifier`, `decision_maker`, `non_qualified`, `qualified`, `calling_agent`, `call_results`, `transferred`), `activity`, `services`, `threshold`, `selected_batch_id`, `batches`, and embedded `runner` status (`batch_id`, `source_url`). Accepts optional `?batch_id=<id|all>` (defaults to active or latest batch) |
| `/api/runner` | `GET` | Current `BatchRunner` state (`idle`, `running`, `stopping`, `completed`, `stopped`, `error`), plus `retry_failed_calls`, `pending_batches` and `pending_leads` (unfinished saved leads processed before new ones) |
| `/api/runner/start` | `POST` | Start a batch run (`{"batch_size": 1..1000, "num_batches": 1..100, "retry_failed_calls": true/false}`, defaults `300`, `1` and `RETRY_FAILED_CALLS`) |
| `/api/runner/stop` | `POST` | Request cooperative stop of the active batch run |
| `/api/leads` | `GET` | Paginated, searchable, sortable rows for `view` ∈ `incoming`, `number_verifier`, `decision_maker`, `non_qualified`, `qualified`, `calling_agent`, `call_results`, `transferred`, with optional `batch_id` (defaults to active/latest; `all` for combined history), `service`, `filter`, `q`, `sort`, `order`, `page`, `page_size` (1–200) |
| `/api/leads/{lead_id}` | `GET` | Full per-lead detail across all batches: lifecycle `journey`, `number_check`, `decision` scores & reasons, `call` outcome & `audio_tracks`, and sanitized `lead_fields` |
| `/api/leads/{lead_id}/transcript` | `GET` | Parsed conversation `turns` (`agent` / `customer` / `other`) fetched via `DograhMedia` (resolves across all batches) |
| `/api/leads/{lead_id}/audio/{track}` | `GET` | Range-capable audio stream for `track` ∈ `recording` (Full call), `agent` (AI agent), `customer` (Customer) proxied through `DograhMedia` (resolves across all batches) |

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
| Results writer | 1 | `results` queue (unbounded); saves at most every `RESULTS_SAVE_SECONDS` |
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
- **Cooperative stop (`stop_event`):** checked across `submit()`, `flush()`, `run_leads()`, and `CampaignJob.monitor()`. When set, campaigns are paused and checked one last time (§8), pending queues and uncompleted calls are marked `interrupted`, and stores are saved cleanly.
- **Shutdown:** `close()` runs `flush()` (score everything, run the campaigns,
  wait for all calls), then stops the workers, pauses any campaign of this run
  still running, saves `call_results.xlsx`, `transferred_calls.xlsx`, and `pipeline_tracker.xlsx`, and prints the summary. On `Ctrl+C`, `stop_event`, or an error,
  it skips the flush, marks every lead still queued, being scored or waiting
  for a campaign `interrupted`, and still pauses the campaigns and saves.
- **Campaign monitors are independent:** if following one campaign fails, it is
  paused and its unfinished leads are marked `interrupted`; the others go on.
- **Batches:** `run_leads()` calls `flush()` after every `batch_size` (`LEAD_BATCH_SIZE` = 300 by default)
  leads, so each batch is completely called before the next is taken.
- **Persistence:** in-flight queues live in memory only, while pulled streamer batches remain on disk as `*.pending.json` (with a `*.progress.json` of finished leads) until completed, and `pipeline_tracker.xlsx` saves periodically every `TRACKER_SAVE_SECONDS`.

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
    batch_id: str = None,              # optional job batch ID for dashboard scoping
    stop_event: Any = None,            # optional threading.Event for cooperative cancellation
    retry_failed_calls: bool = None,   # also re-queue calls that reached nobody (default RETRY_FAILED_CALLS)
    on_lead_final: Callable[[int, str], None] = None,  # (position, status) when a lead is finished for good
)
await pipeline.start()                 # acquire tracker lock, recover earlier calls, resolve agents, start workers
await pipeline.submit(lead: dict, position: int = None)  # add one lead (waits if queues are full)
await pipeline.flush()                 # score what's submitted, run one campaign per service, wait for calls
await pipeline.close(abort=False)      # flush (unless abort), pause campaigns, save stores & tracker
pipeline.stats                         # Counter of statuses (+ retry_saved_decision / retry_saved_call / retry_given_up_*)
pipeline.failed_decision_leads         # leads that hit decision_error, saved for a retry
pipeline.retry_call_leads              # leads whose call should be tried again
```

`on_lead_final` is not called for `interrupted` leads or leads saved for a
retry. Leads saved for a retry carry a `_dograh_caller` key (their retry
count); `submit()` removes it again.

Use it as an async context manager (`start` / `close` are called for you):

```python
async with LeadPipeline() as pipeline:
    async for lead in scraper():
        await pipeline.submit(lead)
```

### Helpers

```python
await run_leads(leads, batch_size=None, failed_leads=None, stop_event=None, retry_leads=None, **pipeline_kwargs)
run_leads_sync(leads, batch_size=None, failed_leads=None, stop_event=None, retry_leads=None, **pipeline_kwargs)

from main import run
run(source, *, sheet=None, limit=None, threshold=None, output=None,
    score_only=False, source_label=None, on_failed=None,
    retry_failed_calls=None, on_call_retry=None) -> int

from pull_leads import default_process, BatchStore
default_process(leads, threshold, output, *, batch_size=None, stop_event=None, batches_dir=None,
                tracker_file=None, batch_id=None, progress_path=None, lead_indexes=None,
                retry_failed_calls=None) -> Counter
BatchStore.remaining(path) -> (doc, unfinished_indexes)   # progress of a saved batch
```

`failed_leads` / `retry_leads` are filled also when the run ends with an
exception or `Ctrl+C` (the exception is re-raised afterwards).

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

Unfinished batches (`*.pending.json`) are always finished before new leads are taken (on Dashboard **Start Batch**, CLI resume prompt, or auto mode) — only their unfinished leads (`*.progress.json`). The dashboard works through them in pieces of the chosen batch size and leaves retry batches it creates for the next Start; the CLI processes a saved batch whole; auto mode also retries saved batches but backs off (doubling the wait) after a batch that produced new Gemini or call retries. In all modes `preflight()` validates Gemini (`check_gemini_access`), `services.yaml`, each Dograh agent, and reverse-proxy settings before pulling leads. `python pull_leads.py --check` runs the same checks plus the streamer's `/health` and `/stats` (with token) and exits without taking any leads. SIGTERM and SIGHUP are turned into `SIGINT` (`stop_signals_act_like_ctrl_c()`) so campaigns pause cleanly and pending batches are preserved. See [lead_streamer/LEAD_STREAMER_SPEC.md](lead_streamer/LEAD_STREAMER_SPEC.md) §7, §12, §16 for the streamer contract.

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
                  [CampaignLead(ref="L1", phone="+15125550101", lead=lead,
                                time_zone="America/Chicago")])       # time_zone: for current_datetime
job.launch(client)                                     # creates + starts "<service>_<date>"; job.skipped, job.warnings
await job.monitor(client, on_call_started, on_result)  # callbacks per call / final outcome
await job.final_sync(client, on_call_started, on_result)  # after pausing: one last look at the calls
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
| Change retries | `CAMPAIGN_MAX_RETRIES`, `CAMPAIGN_RETRY_*` in `.env` (Dograh's retries inside a campaign); `RETRY_FAILED_CALLS`, `RETRY_MAX_ATTEMPTS` (calling again in a later batch). |
| Change which outcomes mean "nobody reached" | `NOT_REACHED_DISPOSITIONS` in `.env`. |
| Send more lead fields to the agent | Use them as `{{variables}}` in the agent, or add them to `DOGRAH_EXTRA_FIELDS`. |
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

Coverage across `tests/` includes:
- Phone parsing, fax exclusion, region/type checks, and duplicate detection (`test_number_verifier.py`)
- Gemini structured scoring, prompt safety, `--score-only`, and `tools/compare_scores.py` (`test_decision_maker.py`, `test_gemini_decision.py`, `test_score_only.py`, `test_compare_scores.py`)
- Dograh campaign creation, retry/timeout handling, results grouping, Excel sanitization, and transferred calls (`test_campaign_and_pipeline.py`, `test_results_store.py`, `test_transfers.py`)
- `PipelineTracker` locking, round-trip persistence, and `batch_id` isolation (`test_pipeline_tracker.py`)
- `pull_leads.py` confirmation prompts, `main.py` idle defaults, and `lead_streamer` SQL/HTTP limits (`test_pull_leads.py`, `test_main_and_lead_streamer.py`, `test_lead_streamer_setup.py`)
- Dashboard API views, `DograhMedia` signed-URL proxying, `BatchRunner` start/stop/batch scoping, and full TLS reverse-proxy end-to-end tests (`test_dashboard_api.py`, `test_dashboard_media.py`, `test_runner.py`, `test_reverse_proxy_and_streamer_e2e.py`)
- Retries of failed calls and Gemini failures with their limit, not-reached outcomes, long calls, the last look after Stop and recovery at the next start, resuming saved batches lead by lead and in pieces, the CSV fields and time zone sent to the agent, Dograh's variable check, settings parsing, results throttling and tracker archiving (`test_fixes.py`)

`tests/conftest.py` points `RESULTS_FILE` / `TRANSFERS_FILE` at a temporary folder for every test, so the suite never reads or writes the real `output/` folder.

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
| Many `decision_error` rows | Gemini rate limit or network issue; lower `DECISION_CONCURRENCY`. Failed leads are saved for retry (`*.pending.json` or `decision_retry_*.json`), up to `RETRY_MAX_ATTEMPTS` times. |
| `NOT saved again: … (RETRY_MAX_ATTEMPTS=3)` | That lead failed (Gemini or call) on every retry; it is left as is. Look at its `detail`, then re-run it by hand or raise `RETRY_MAX_ATTEMPTS`. |
| `FAILED to start '<service>_<date>'` | Dograh refused the campaign; the message says why (telephony not configured, quota, invalid CSV, ...). Its leads are saved to be tried again. |
| `NOTE '<campaign>': Dograh also requires …` | Dograh's contacts check wanted variables our scan of the agent missed; leads without them became `skipped_missing_data` and the campaign was created without them. |
| `NOTE '<campaign>': lead fields 'X' and 'Y' are both sent as {{x}}` | Two lead fields map to the same variable; the first non-empty value is used. Rename one field at the source if the other was meant. |
| Leads marked `skipped_missing_data` | The agent's prompt uses a `{{variable}}` those leads have no value for. Add the field to the leads, give the variable a default in the prompt (`{{company | there}}`), or remove it. |
| The agent says a variable is empty that the lead has | Since only used fields are sent, check the spelling in the prompt matches the CSV name (`company_name`, not `Company Name`), or add the field to `DOGRAH_EXTRA_FIELDS`. |
| The agent gets the time of day wrong | `current_datetime` is fixed when the campaign is created (in the lead's time zone). Use `{{current_time_<Area/City>}}` for the time of the call. |
| Calls stay on "Calling…" for a long time | Normal while the conversation lasts (transferred calls can last long; they are never cut off while going). If calls never finish, Dograh is not receiving the telephony provider's status callbacks — check the deployment's public backend URL. Such a call is treated as stuck after `CAMPAIGN_MAX_CALL_SECONDS` and ends as `call_timeout` after a further `CAMPAIGN_IDLE_TIMEOUT_SECONDS` without progress (undialed leads as `call_failed`). |
| `Recovered the final result of N call(s) from earlier runs` | Calls still going when an earlier run stopped have finished; their results were added (tracker + a new results row). Normal. |
| A setting seems ignored / `WARNING: X=... is not true/false` | Use true/false, yes/no, on/off or 1/0. Settings come from `dograh_caller/.env` only (the parent folder's `.env` is read only when this one is missing). |
| Results file lags behind the dashboard | It is saved at most every `RESULTS_SAVE_SECONDS` during a run, and always at the end. |
| An old batch is missing from the Batches list | Batches older than `TRACKER_ARCHIVE_DAYS` are in `output/pipeline_tracker_archive.xlsx`. |
| Long pauses between calls | One concurrent-call slot shared by all campaigns, plus retry delays. |
| A lead waits ~4 min after "no-answer" | It is waiting for Dograh's retry (`CAMPAIGN_RETRY_DELAY_SECONDS` + `RETRY_GRACE_SECONDS`). |
| `results ... is open in another program` | Close the spreadsheet in Excel. |
| Agent says something unexpected | Check which version is published in Dograh and what the agent's prompt does with the variables in §8. |

---

## 16. Limitations and known behaviour

- **In-memory queues.** In-flight queues are not persisted across crashes, though
  already-written results are saved atomically to the spreadsheets, streamer
  batches resume from their `*.progress.json`, and cross-run repeat calls are
  blocked when `SKIP_ALREADY_CALLED=true` (reading `call_results.xlsx`,
  timestamped fallback copies, `pipeline_tracker.xlsx` and its archive). Within a
  single run, `self._seen` also prevents calling or re-scoring the same number twice.
- **Resumed leads may be dialed again if nobody was reached.** A lead whose call
  reached nobody is not blocked, so if its batch is resumed (or the streamer
  sends it again as "updated") it can be called again, even without
  **Retry failed calls**.
- **`current_datetime` is not the time of the call** — it is fixed when the
  campaign is created; see §8.
- **Results files are rewritten whole.** Excel files can't be appended to, so
  each save rewrites the workbook; saves are spaced by `RESULTS_SAVE_SECONDS` and
  the tracker is kept small by archiving, but a very large `call_results.xlsx`
  still makes each save slower.
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
