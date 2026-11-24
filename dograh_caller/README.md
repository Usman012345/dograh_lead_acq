# dograh_caller

Give it a list of businesses—or pull them on demand from the reverse-proxied lead streamer. It finds the ones likely to buy **Draftliner** or **Paradise Estimating** and has the right AI voice agent call them.

> **Docs:** [Full reference](DOCUMENTATION.md) · [Going live](GO_LIVE.md) · [Service descriptions](SERVICES.md) · [Adding a service](ADDING_A_SERVICE.md) · [Lead streamer](lead_streamer/README.md)

## What it does

1. **Reads your list** of businesses (a spreadsheet, Python dicts, or on-demand batches pulled directly from the reverse proxy at `https://ai-sdr.grobots.ai` — nothing is pulled or called automatically on startup).
2. **Checks each phone number.** It keeps only real US numbers, and skips
   duplicates and businesses that were already called.
3. **Asks Gemini (`gemini-3.8-flash`)** to score each enabled service (0–100%)
   and write a short explanation.
4. **Calls the qualified businesses (≥ 80%).** For each service it creates a Dograh
   campaign, e.g. `Draftliner_2026-11-19`, and that service's agent makes the calls.
5. **Saves everything** to `output/call_results.xlsx` (one row per business) and
   `output/transferred_calls.xlsx` (transferred leads), plus per-lead live tracking
   in `output/pipeline_tracker.xlsx` for the web dashboard.

```
https://ai-sdr.grobots.ai (Start Batch / Enter) ─┐
or spreadsheet / Python list ────────────────────┴─► check numbers ─► Gemini ─► Dograh campaigns ─► Excel & Dashboard
```

| Service | Dograh agent that calls |
|---|---|
| Draftliner | 2 — *Testing Lead qualification - outbound* (Danial) |
| Paradise Estimating | 3 — *Paradise Agent - outbound* (Olivia) |

---

## 1. One-time setup

**a. Install the requirements** (Python 3.10 or newer). Open a terminal in this folder and run:

```bash
pip install -r requirements.txt
```

**b. Add your keys.** Copy `.env.example` to `.env` and fill in:

```ini
GEMINI_API_KEY=your-gemini-key
DOGRAH_API_KEY=your-dograh-key
DOGRAH_BASE_URL=https://dograh.grobots.ai/api/v1
```

A `.env` file in the parent folder is also checked if present.
Keys belong only in `.env`, never in code.

**c. Publish your agents in Dograh.** Calls always use an agent's *published*
version, so publish after every change you make to an agent.

**d. Check everything** (takes no leads, calls nobody):

```bash
python pull_leads.py --check
```

It confirms the Gemini key, `services.yaml`, each Dograh agent, that the lead
streamer answers at `LEAD_SOURCE_URL`, and that it accepts the token.

### Running it on a server (Linux)

Copy this folder to the server **with its `.env`**, then:

```bash
bash install.sh                      # .venv + requirements + the --check above; no root needed
tmux new -s caller                   # keeps it running if your SSH connection drops
.venv/bin/python dashboard_server.py # starts idle; click Start Batch in the UI to pull/call leads
```

Detach with **Ctrl+B, D**; come back with `tmux attach -t caller`. Nothing is
pulled or called on startup — leads are only pulled directly from the reverse
proxy and called when you click **Start Batch** in the dashboard (or press Enter
in `pull_leads.py`). If a running batch is stopped or interrupted, this run's
Dograh campaigns are paused and the unfinished batch is kept and continues on
the next start.

On Windows: `python -m venv .venv`, `.venv\Scripts\pip install -r requirements.txt`,
then `.venv\Scripts\python dashboard_server.py`. (`bash install.sh` also works in Git Bash.)

Tests: `pip install -r requirements-dev.txt`, then `python -m pytest`.

---

## 2. Calling a list of businesses

**Step 1 — Prepare the spreadsheet** (`.xlsx`, `.csv` or `.json`).
The first row holds the column names. You need:
- **a phone column**, for example `Phone`, `Phone(s)`, `Phone Number` or `Mobile`
  (several numbers in one cell are fine; **fax numbers are never called**);
- **columns describing the business**, for example company, industry,
  description and city. The more Gemini knows about a business, the better it decides.

Data from the scraper can be unstructured (any field names, nested data, lists,
JSON text): phone numbers are found wherever they are, and Gemini only gets the
fields that describe the business. Details: [DOCUMENTATION.md](DOCUMENTATION.md) §6–7.

| Company | Industry | Notes | City / State | Phone(s) |
|---|---|---|---|---|
| Smith Framing LLC | Framing subcontractor | Bids 10+ jobs a month | Austin, TX | (512) 555-0101 |

**Step 2 — Point the program at your file.** Open [main.py](main.py) and edit the top:

```python
INPUT_FILE_PATH = r"C:\path\to\your\leads.xlsx"   # None = no file
LIMIT = None                                     # or 5 for only the first 5 businesses
USE_STREAMER = False                             # False by default (never auto-pulls on startup)
STREAMER_CHECK_SECONDS = 60
```

**Step 3 — Run it:**

```bash
python main.py                           # live: calls only the configured file/list (does not auto-pull)
python main.py leads.xlsx --score-only   # test scoring only; never calls Dograh
```

By default `USE_STREAMER = False`, so starting `main.py` never pulls or calls
leads from the reverse proxy on its own. To pull and call leads from the
reverse proxy on demand, use `python dashboard_server.py` (click **Start Batch**)
or `python pull_leads.py` (press **Enter** to pull a batch).

> ⚠️ **Normal runs are live.** Businesses that pass the 80% threshold really get called.
> Pass `--score-only` to test verification and Gemini scoring safely without contacting Dograh or touching `output/call_results.xlsx`.

Leads are processed **300 at a time** (`LEAD_BATCH_SIZE`): the program checks and scores a batch,
calls everyone in it, then moves on to the next 300.

---

## 3. What you see while it runs

Example output (the real lines also start with the time):

```
[setup]    Services: Draftliner (agent 2), Paradise Estimating (agent 3)
[setup]    Gemini model gemini-3.8-flash (thinking MEDIUM), threshold 80%
[verify]   DROP +1 416 555 0107 (dropped_not_us (CA)) - Maple Leaf Builders   ← number not usable
[decide]   NON-QUALIFIED +16025550105 Taco Loco (draftliner 3%, paradise 5%)
[decide]   QUALIFIED +18285550102 Blue Ridge Architects -> Draftliner (draftliner 94%, paradise 8%)
[campaign] Started 'Draftliner_2026-11-19' (campaign 21) with 12 number(s)
[call]     -> Calling +18285550102 (Blue Ridge Architects) [Draftliner_2026-11-19, run 2210]
[call]     <- +18285550102 Result: user_qualified | 67s | tags: user_qualified
[campaign] 'Draftliner_2026-11-19' (campaign 21) finished - paused
Done. Leads received: 10 | dropped by verifier: 1 | not interested: 2 | called: 7 | ...
```

- **DROP**: the phone number can't be used (not US, invalid, a duplicate, or already called).
- **NON-QUALIFIED**: Gemini scored every service below the 80% threshold.
- **QUALIFIED**: scored ≥ 80%; the business is added to the highest-scoring service's campaign.
- **Calling / Result**: a call starts, then its outcome from Dograh.
- **finished - paused**: all calls in the campaign are done. Campaigns are
  never left running.

Calls take about a minute each and happen one at a time, so a big list takes hours.

---

## 4. Stopping it

Press **Ctrl+C**. The program pauses this run's campaigns in Dograh, so no new
calls are made, and saves the results so far.

Don't just close the window: then the campaigns stay running in Dograh and you
have to pause them there yourself.

---

## 5. Reading the results

Open **`output/call_results.xlsx`**. Close it in Excel while the program runs,
because Excel locks the file.

**Transferred calls** also go to a separate file, **`output/transferred_calls.xlsx`**:
one row per business whose call the agent transferred, grouped by day. It starts with
their details (business, contact name and title, phone called, email, website, address,
city, state, category), then the call (service, campaign, agent, duration, what the agent
collected on the call, transcript and recording links), then any other information the
lead had. The file appears with the first transfer. In `call_results.xlsx` those rows have
`call_transferred` = TRUE.

- One row per business, grouped by day, **newest day on top**.
- The most useful columns:

| Column | What it tells you |
|---|---|
| `status` | What happened (see below) |
| `interested_service` | Draftliner or Paradise Estimating |
| `prob_draftliner`, `prob_paradise` | How likely Gemini thinks each service is (0–1) |
| `decision_verdict`, `decision_summary` | Qualification verdict and two-line Gemini explanation |
| `campaign_name` | The Dograh campaign it was called in |
| `call_disposition` | How the call ended, e.g. `user_qualified`, `user_hangup`, `no-answer` |
| `transcript_url`, `recording_url` | The conversation |

| `status` | Meaning |
|---|---|
| `called` | The call happened; see `call_disposition` |
| `not_interested` | Gemini scored it below 80% for every service; not called |
| `scored_qualified` | Scored ≥ 80% in a `--score-only` run; not called |
| `decision_error` | Gemini request failed after retries; saved for retry |
| `dropped_...` | Phone problem: `not_us`, `invalid_number`, `special_number` (e.g. 900), `fax_only` (only fax numbers — fax numbers are never called), `duplicate`, `already_called`, `no_phone` |
| `call_failed` / `call_timeout` | Dograh couldn't place the call, or it never finished |
| `skipped_missing_data` | The agent's script needs information this business didn't have |
| `interrupted` | Run stopped (`Stop` or `Ctrl+C`) before the lead finished |

> **Keep `output/call_results.xlsx` and `output/pipeline_tracker.xlsx`.** Together they track who was already called so earlier businesses are not called again.

---

## 6. Getting leads from the scraper's lead streamer

Setting it up on the servers for the first time: [GO_LIVE.md](GO_LIVE.md).

The [lead streamer](lead_streamer/) runs behind the reverse proxy at
**`https://ai-sdr.grobots.ai`** (`LEAD_SOURCE_URL`), serving up to 300 leads per
request. Put its token in `.env`:

```ini
LEAD_SOURCE_TOKEN=the-streamer's-STREAM_TOKEN
# LEAD_SOURCE_URL=https://ai-sdr.grobots.ai   (default reverse proxy URL)
```

Then start the dashboard (recommended) or interactive CLI:

```bash
python dashboard_server.py   # starts idle; click Start Batch to pull from the reverse proxy and call
# or:
python pull_leads.py         # stays idle until you press Enter before each ping (including the first)
```

1. On startup, nothing is pulled or called automatically.
2. When you click **Start Batch** (or press **Enter** in `pull_leads.py`), it pings
   `https://ai-sdr.grobots.ai/leads/next` directly through the reverse proxy and
   receives up to **300** leads (or the requested batch size).
3. It saves the batch to `output/pulled_batches/`, then processes **all** of it
   (check numbers → Gemini → campaigns → every call finished → results sheet).
4. Once the requested batch(es) finish, the runner stops automatically and waits
   for your next explicit start (or next **Enter** in `pull_leads.py`).

If it is stopped mid-batch (Ctrl+C, Stop button, crash, error), the batch stays in
`output/pulled_batches/*.pending.json` and is finished first on the next start,
before any new leads are taken. That matters because the streamer marks leads as
sent the moment it hands them out. Numbers that were already called are skipped.

## 7. Sending leads from other code

```python
import sys; sys.path.insert(0, r"C:\path\to\dograh_caller")
from main import run

run({"company": "Summit Concrete Co", "category": "Concrete subcontractor",
     "location": "Denver, CO", "phone": "(303) 555-0142"})   # one business
run(list_of_business_dicts)                                    # many (processed 300 at a time)
```

See [examples/from_python.py](examples/from_python.py) for streaming leads one by one.

---

## 8. Client Dashboard

The web dashboard shows the live pipeline flow (`Incoming Leads → Number Verifier → Decision Maker → Qualified / Non-Qualified → Calling Agent → Call Results`, plus `Transferred Calls`) and lets you start or stop streamer batch runs from the top bar. Clients can see the conversion funnel and anything needing attention, open any stage, and read a lead's full story: AI reasoning, why it was or was not called, the recording and the conversation.

```bash
python dashboard_server.py   # optional: --host 127.0.0.1 --port 8090 --tracker PATH --demo
```

Then open <http://127.0.0.1:8090>.

- **What it reads:** `output/pipeline_tracker.xlsx` (read-only snapshots; never locks the workbook). Leads appear as they progress through each stage, and the server serves the pre-built `dashboard/static/` bundle without needing Node at runtime.
- **Start / Stop Batch:** starts **idle** on startup (`source_url` from `LEAD_SOURCE_URL` is shown in the **Start Lead Batch Run** dialog). Click **Start Batch** to pull 1–100 batches of 1–1,000 leads directly from the reverse proxy and run the pipeline in the background (or click **Stop** to pause Dograh campaigns and keep unfinished leads pending for resume). In `--demo` mode, **Start Batch** runs an offline simulation without touching the streamer or Dograh.
- **Recordings and transcripts:** the server fetches them from Dograh with the key in `.env`. The key and the private file links never reach the browser.
- **Security:** the dashboard has **no login**. By default it binds to `127.0.0.1:8090` (this computer only) and refuses requests for other host names. With a non-loopback `--host` it prints a warning, because anyone who can reach the address can see lead details, phone numbers, transcripts, recordings, and batch controls.

**Client demo:** show the dashboard with ~60 fictional leads, conversations and sample audio. It uses no real data and contacts nothing:

```bash
python dashboard_server.py --demo
```

**Changing the interface:** edit `dashboard/web/src`, then run `npm install` and `npm run build` in `dashboard/web`. The built files in `dashboard/static` are what the server uses.

---

## 9. Changing how it behaves

| I want to… | Do this |
|---|---|
| Add or change a service (who it targets, which agent calls) | Edit [services.yaml](services.yaml); no code. See [SERVICES.md](SERVICES.md) |
| Test scoring without placing any calls | `python main.py path\to\leads.xlsx --score-only` |
| Call fewer, surer leads (or more) | Set `INTEREST_THRESHOLD=0.9` (stricter) or `0.75` in `.env` |
| Change the batch size | Set `LEAD_BATCH_SIZE=500` in `.env` (and `BATCH_SIZE` in the streamer's `.env` to match) |
| Change retries for busy / no-answer calls | `CAMPAIGN_MAX_RETRIES`, `CAMPAIGN_RETRY_DELAY_SECONDS` in `.env` |
| Run a different file once without editing main.py | `python main.py other.xlsx --limit 5` |

All settings: [DOCUMENTATION.md](DOCUMENTATION.md), section 4.

---

## 10. Common problems

| Problem | Fix |
|---|---|
| `Gemini API key not found` / `Dograh API key not found` | Add the key to `.env` (`GEMINI_API_KEY=...` or `GEMINI_API=...`) |
| `Dograh rejected the API key (HTTP 401)` | The key comes from a different Dograh, or was archived. Create one under Settings → API Keys |
| Every row says `dropped_no_phone` | No column name looks like a phone column. Rename it to `Phone` |
| Almost everyone is `not_interested` | Your list lacks business details, or the services don't fit. Add industry/description columns, or check [services.yaml](services.yaml) |
| Warning `has unpublished changes` | Publish the agent in Dograh, or calls use the older version |
| `FAILED to start '…'` | Dograh refused the campaign; the message says why |
| `… is open in another program` | Close the results spreadsheet in Excel |
| `Tracker is locked` / `HTTP 409` | Another pipeline run is active (`.pipeline_tracker.lock`); wait for it to finish or click **Stop** |
| Intermittent TLS `Hostname mismatch` from `https://ai-sdr.grobots.ai` | Stale nginx workers on the scraper server; run `sudo nginx -t && sudo systemctl restart nginx` there (see [GO_LIVE.md](GO_LIVE.md#if-pull_leadspy---check-fails)) |

---

## Good to know

- **Calling laws.** AI-voice calls to US numbers are regulated (consent and
  calling hours). Make sure your calling is compliant.
- **How it works inside**, every setting, column and status:
  [DOCUMENTATION.md](DOCUMENTATION.md).
