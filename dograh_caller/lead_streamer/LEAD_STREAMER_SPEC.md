# Lead Streamer — Specification

**Status:** implemented (`lead_streamer.py` v1.0).
**Readers:** (1) the coding agent that will write the script, (2) the owner, who will check the build against it and test it live.
**Words used in this document:** **MUST** / **MUST NOT** are hard rules. **SHOULD** means "do this unless there is a written reason not to".

---

## Contents

1. [What this is](#1-what-this-is)
2. [How it works](#2-how-it-works)
3. [Hard rules](#3-hard-rules)
4. [Deliverable files](#4-deliverable-files)
5. [Configuration](#5-configuration)
6. [Database](#6-database)
7. [HTTP API (the ping)](#7-http-api-the-ping)
8. [Lead payload](#8-lead-payload)
9. [Batch algorithm](#9-batch-algorithm)
10. [Concurrency](#10-concurrency)
11. [Memory and long-running behaviour](#11-memory-and-long-running-behaviour)
12. [Errors](#12-errors)
13. [Logging](#13-logging)
14. [Security](#14-security)
15. [Deployment](#15-deployment)
16. [What the dograh caller must do](#16-what-the-dograh-caller-must-do)
17. [Live verification checklist](#17-live-verification-checklist)
18. [Operations](#18-operations)
19. [Acceptance criteria](#19-acceptance-criteria)

---

## 1. What this is

`lead_streamer` is a **standalone Python script** that runs on the scraper's deployment server, next to the scraper's PostgreSQL database. It hands leads from that database to the **dograh caller**, a separate system on another server, **300 at a time (`BATCH_SIZE`, at most 1,000), as one batch, on request**.

- It is **entirely separate** from the scraper project. It is copied to the server on its own, has its own dependencies, and **imports nothing** from `Backend/`, `Database/`, `RAG/` or `AI Powered/`.
- Its only connection to the scraper project is the **database**. It reads the scraper's tables and keeps its own record of which leads were sent.
- It runs **constantly** as a service. It never stops just because there are no leads left.

> **Not a WebSocket.** The link you paste in (your company subdomain) is a normal **HTTPS** address. Each ping is one ordinary HTTPS request with one response. No WebSocket connection is used or needed.

---

## 2. How it works

```
 dograh caller (other server)                         scraper server
 ────────────────────────────                         ──────────────────────────────────────────────
                                                       nginx (HTTPS, your company subdomain)
   "give me the next leads"                                 │
   GET https://<PUBLIC_BASE_URL>/leads/next  ─────────────► │ proxy to 127.0.0.1:<BIND_PORT>
   Authorization: Bearer <STREAM_TOKEN>                     ▼
                                                       lead_streamer.py (always running)
                                                            │ 1. pick next ≤300 leads that are NOT SENT 
                                                            │    or UPDATED SINCE SENT (must have a phone)
                                                            │ 2. mark them as sent  ─┐ one database
                                                            │ 3. commit             ─┘ transaction
   ◄──────────────── 200 {"status":"ok","leads":[...]} ─────┘ 4. reply with those leads
   ... processes the 300 leads (calls them) ...
   a person presses Enter -> pings again for the next 300
   ...
   ◄── 200 {"status":"no_new_leads","message":"No new Leads available"}   (when all are sent)
   ... waits a while, pings again (new or updated leads may have arrived) ...
```

- **Direction:** the dograh caller **pulls** from the streamer. The streamer never connects out to the dograh caller.
- **One ping = one batch** of at most `BATCH_SIZE` (300) leads, all in a single response.
- A lead is marked as sent **when it is handed out in a response**. No confirmation comes back from the caller (see §9.4 for what that means).
- **Updated leads are sent again.** The scraper increases `leads.content_version` whenever a lead's content changes. If that number is now higher than the version that was sent, the lead becomes due again and is sent with `"event": "updated"`.

---

## 3. Hard rules

| # | Rule |
|---|---|
| R1 | MUST be a separate script that imports **nothing** from the scraper project. |
| R2 | MUST use only the Python standard library plus `psycopg` (v3). No web framework, no ORM. |
| R3 | MUST NOT write to any table in the `public` schema. MUST NOT alter, add columns to, or create indexes on scraper tables. |
| R4 | All of the streamer's own state MUST be in the schema `lead_stream`, in the one table `lead_stream.sent_leads` (§6.2). |
| R5 | The only state stored per lead is: **sent or not**, and **the version that was sent** (which tells whether it was updated since). No accepted / rejected / failed / attempt status of any kind. |
| R6 | A batch MUST contain at most `BATCH_SIZE` leads, and `BATCH_SIZE` MUST be capped at **1000**. All leads of a batch go out in **one** response; never split a batch across several responses. |
| R7 | Only leads that have **at least one phone number** are eligible (§9.1). |
| R8 | Selecting a batch and marking it sent MUST happen in **one database transaction**. If anything fails before commit, nothing is marked and the ping gets an error. |
| R9 | Two pings MUST never receive the same lead at the same version (§10). |
| R10 | When nothing is due, MUST reply `200` with `"status": "no_new_leads"` and the message `"No new Leads available"`, then **keep running**. |
| R11 | MUST run indefinitely and MUST NOT grow in memory over time (§11). |
| R12 | MUST NOT include any dry-run, preview or "don't mark" mode in the final deliverable. Temporary test helpers used during development MUST be removed before delivery. |
| R13 | MUST require the bearer token on `/leads/next` and `/stats`, compared in constant time. |
| R14 | MUST listen on `127.0.0.1` by default and be exposed to the internet **only** through nginx with HTTPS. |
| R15 | MUST NOT write lead contents (names, phones, emails, addresses) to logs. |
| R16 | MUST be a single, readable script file (`lead_streamer.py`), with `PUBLIC_BASE_URL` as an obvious variable near the top. |

---

## 4. Deliverable files

```
lead_streamer/
├── lead_streamer.py          the whole program (one file)
├── requirements.txt          psycopg[binary]>=3.1
├── .env.example              every setting from §5 with comments
├── lead-streamer.service     systemd unit (§15.2)
├── nginx-lead-streamer.conf  nginx snippet (§15.3)
├── README.md                 short: install, configure, run, ping example, verify
└── LEAD_STREAMER_SPEC.md     this document
```

Supported Python is 3.10 or newer. The folder MUST work when copied anywhere on the server; nothing in it may depend on the scraper project's folder layout.

---

## 5. Configuration

### 5.1 The variable you paste in

At the top of `lead_streamer.py`, right after the imports, MUST be this block:

```python
# ─────────────────────────────────────────────────────────────────────────────
# PASTE YOUR COMPANY SUBDOMAIN HERE once it is ready (HTTPS address, no path),
# e.g. "https://leads.yourcompany.com". Leave "" to skip the host check.
# An environment variable PUBLIC_BASE_URL (or the .env file) overrides this.
PUBLIC_BASE_URL = "https://ai-sdr.grobots.ai"
# ─────────────────────────────────────────────────────────────────────────────
```

What `PUBLIC_BASE_URL` does:
- At startup the full ping address is logged, for example `Ping URL: https://leads.yourcompany.com/leads/next`, so it can be copied to the dograh caller.
- **Host check:** when set, requests whose `Host` header doesn't match its host name are refused with `421`. `/health` is exempt.
- It MUST start with `https://` when set. Otherwise the script exits at startup with a clear error.
- A trailing `/` is removed. A path (e.g. `https://x.com/streamer`) is allowed; in that case nginx must strip the path prefix before forwarding (§15.3).

### 5.2 All settings

Settings are read in this order, first match wins: **environment variable → `.env` file in the script's folder → default**. The `.env` reader is a few lines of standard-library code (simple `KEY=VALUE`, `#` comments, optional quotes); it MUST NOT require `python-dotenv`.

| Setting | Default | Rules |
|---|---|---|
| `PUBLIC_BASE_URL` | `https://ai-sdr.grobots.ai` (the value in the script) | See §5.1. |
| `DATABASE_URL` | — **required** | PostgreSQL URL. MUST accept the scraper's form `postgresql+psycopg://user:pass@host:5432/dataops` by turning `postgresql+psycopg://` into `postgresql://`. |
| `STREAM_TOKEN` | — **required** | Shared secret for the dograh caller. MUST be at least 32 characters, or startup fails. |
| `BIND_HOST` | `127.0.0.1` | Interface to listen on. |
| `BIND_PORT` | `8090` | Port to listen on. |
| `BATCH_SIZE` | `300` | Leads per ping; keep equal to the caller's `LEAD_BATCH_SIZE`. Values above 1000 are clamped to 1000 (warning logged); values below 1 fail startup. |
| `MAX_CONCURRENT_REQUESTS` | `8` | Requests handled at once. Extra requests get `503` straight away (§10). |
| `DB_STATEMENT_TIMEOUT_MS` | `15000` | Applied to every database statement. |
| `REQUEST_TIMEOUT_SECONDS` | `30` | Socket timeout per client connection. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR`. |

The script MUST check every setting at startup and exit with a non-zero code and one clear message per problem. The token and the database password MUST NOT be printed.

---

## 6. Database

### 6.1 Tables it reads (owned by the scraper; read only)

Checked against the live schema (PostgreSQL 18).

| Table | Columns used |
|---|---|
| `public.leads` | `id`, `organization_id`, `contact_id`, `record_kind`, `title`, `category`, `city`, `us_state`, `source_code`, `source_url`, `due_at`, `lead_metadata` (jsonb), `content_version` (int, not null), `created_at`, `updated_at`, `notes`, `status`, `last_activity`, `next_follow_up`, `external_id`, `first_seen_at`, `last_seen_at` |
| `public.organizations` | `id`, `name`, `website`, `industry`, `domain`, `company_size`, `linkedin_url`, `raw_data` |
| `public.contacts` | `id`, `full_name`, `title`, `first_name`, `last_name`, `department`, `linkedin_url`, `raw_data` |
| `public.phones` | `organization_id`, `contact_id`, `phone_raw`, `normalized_phone`, `is_primary`, `created_at` |
| `public.emails` | `organization_id`, `contact_id`, `email`, `email_status` (`VALID`, `RISKY`, `UNVERIFIED`, `INVALID` or null), `is_primary`, `created_at` |
| `public.locations` | `organization_id`, `address_line1`, `address_line2`, `city`, `state`, `postal_code`, `country`, `normalized_location`, `raw_location`, `is_headquarters`, `created_at` |

If a later scraper change renames any of these columns, the streamer needs a matching update. That is the only coupling.

### 6.2 Table it owns

Created automatically at startup if missing:

```sql
CREATE SCHEMA IF NOT EXISTS lead_stream;

CREATE TABLE IF NOT EXISTS lead_stream.sent_leads (
    lead_id      varchar(100) PRIMARY KEY,   -- = public.leads.id (no foreign key, on purpose)
    sent_version integer      NOT NULL,      -- public.leads.content_version at the moment it was sent
    sent_at      timestamptz  NOT NULL DEFAULT now()
);
```

- **Why a separate schema:** the scraper's Alembic migrations only look at the `public` schema (`include_schemas=False` in `Backend/migrations/env.py`). A table placed in `public` would be flagged for deletion in a future autogenerated migration.
- **No foreign key:** the streamer must never block or slow down the scraper. A lead deleted later leaves a harmless orphan row.
- **Lead states, all derived from this one table:**

| State | Condition |
|---|---|
| **Not sent** | no row for the lead |
| **Sent** | row exists and `sent_version = leads.content_version` |
| **Updated since sent** (due again) | row exists and `sent_version < leads.content_version` |

- **Permissions:** the database user needs `CREATE` on the database for the first start. Alternatively, a DBA can run the SQL above once and grant `USAGE` on `lead_stream`, `SELECT/INSERT/UPDATE` on `lead_stream.sent_leads`, and `SELECT` on the six tables in §6.1.

### 6.3 Performance and database health

All queries in this spec were run against the development database on 2026-11-19, inside a transaction that was rolled back:

| Query | Time |
|---|---|
| Select the next 1,000 due leads (§9.2 step 2) | ~0.1 s (up to ~1.5 s near the end of the data) |
| Load one batch of 1,000 (leads, phones, emails, locations) | ~1 s |
| Response size for 1,000 leads | **2.3 MB** typical, **3.3 MB** worst case (the 1,000 leads with the most data); about 0.3–0.5 MB with gzip |
| Peak memory while building one batch | ~20–30 MB |
| Any count over all leads (eligible, remaining, `/stats`) | ~2.5–3.5 s |

- **Normal pings** stop after 1,000 rows, so a whole ping takes about 1–2 s in total.
- **Near the end of the data** the "due" query must look past already-sent leads to find the last few, which costs about the same as a full count (~3 s). The same is true of every `no_new_leads` ping. At one ping every few minutes that is acceptable.
- **Table bloat:** the slow full scans come from bloat in the scraper's tables, not from the streamer. In the development database, `public.leads` takes **986 MB on disk for 9,843 rows**, about 10 MB of real data, and `public.organizations` takes 852 MB. These tables had never been vacuumed. Check the production database the same way and run `VACUUM (ANALYZE)` (or `VACUUM FULL` in a maintenance window) if it shows the same. This is the scraper's maintenance, not the streamer's; the streamer MUST NOT run it.
  ```sql
  SELECT relname, pg_size_pretty(pg_relation_size(relid)) AS table_size, n_live_tup, n_dead_tup, last_autovacuum
  FROM pg_stat_user_tables WHERE relname IN ('leads', 'organizations', 'phones', 'emails');
  ```

### 6.4 Connection

- One `psycopg` connection, opened at startup, `autocommit = False`, `application_name = 'lead_streamer'`, `statement_timeout = DB_STATEMENT_TIMEOUT_MS`.
- If PostgreSQL isn't reachable at startup, retry every 5 s, logging the reason, without exiting. The HTTP server still starts and answers pings with `503` (§12).
- If the connection breaks (`psycopg.OperationalError` / `InterfaceError`), close it and reconnect once before the next query. If the reconnect fails, reply `503`; the next ping tries again.
- There is no connection pool, and there MUST never be more than one connection.

---

## 7. HTTP API (the ping)

All responses are JSON, `Content-Type: application/json; charset=utf-8`, with header `Cache-Control: no-store`. Non-ASCII text is sent as-is (`ensure_ascii=False`).

### 7.1 `GET /leads/next` — the ping

Headers: `Authorization: Bearer <STREAM_TOKEN>`

**Leads available — `200`:**
```json
{
  "status": "ok",
  "count": 300,
  "new": 262,
  "updated": 38,
  "served_at": "2026-11-19T14:02:11Z",
  "leads": [ { ...lead... }, ... ]
}
```
- `leads` is ordered as described in §9.1.
- There is deliberately **no "remaining" count** here. Counting every due lead means scanning the whole `leads` table on every ping. Progress counts are in `/stats` (§7.3) instead.

**Nothing due — `200`:**
```json
{
  "status": "no_new_leads",
  "message": "No new Leads available",
  "count": 0,
  "served_at": "2026-11-19T14:02:11Z",
  "leads": []
}
```

### 7.2 `GET /health` — liveness (no token)

```json
{ "status": "alive", "database": "ok" }
```
- Returns `200` with `"database": "ok"` or `"database": "unavailable"`; the process is alive either way.
- MUST NOT reveal counts, versions or any lead data.

### 7.3 `GET /stats` — progress (token required)

```json
{
  "status": "ok",
  "eligible": 9046,
  "sent_current": 4836,
  "updated_since_sent": 12,
  "never_sent": 4198,
  "last_sent_at": "2026-11-19T14:02:11Z"
}
```
- `eligible` counts leads with a phone.
- `eligible = sent_current + updated_since_sent + never_sent`.
- These counts scan the whole `leads` table, which takes a few seconds on a large or bloated table (§6.3). That's fine here because `/stats` is for people checking progress, not for the caller's ping loop. It holds the connection lock while it runs, so a ping arriving at that moment waits.

### 7.4 Anything else

| Case | Response |
|---|---|
| Unknown path | `404 {"status":"error","error":"not_found"}` |
| Method other than `GET` on a known path | `405 {"status":"error","error":"method_not_allowed"}` with an `Allow: GET` header |
| A request body | Ignored; never read beyond 0 bytes. |
| Query strings | Ignored on `/health` and `/stats`. On `/leads/next`, optional `?limit=` (or `?batch_size=`) `1..1000` overrides `BATCH_SIZE` for that ping (values `> 1000` clamp to `1000`); invalid values (`< 1` or non-integer) return `400 {"status":"error","error":"bad_request"}`. |

---

## 8. Lead payload

Each item in `leads` is one flat JSON object. The **dograh caller uses it as-is**:
- its number verifier scans every field whose name contains "phone",
- Gemini reads the descriptive fields,
- every field becomes a variable available to the voice agent.

So field names MUST be stable, lowercase `snake_case`.

Empty values are **omitted**: `null`, empty strings and whitespace, `"none"`, `"null"`, `"n/a"`, empty lists and empty objects.

| Field | Type | Source, in order of preference (first non-empty wins) |
|---|---|---|
| `lead_id` | string | `leads.id` — **always present** |
| `content_version` | int | `leads.content_version` — **always present**; it is the value stored as `sent_version` |
| `event` | string | `"new"` if never sent, `"updated"` if sent before at a lower version — **always present** |
| `record_kind` | string | `leads.record_kind` (`company` or `opportunity`) |
| `company` | string | `organizations.name` → `lead_metadata.organization_name` → `lead_metadata.company_name` |
| `contact_name` | string | `contacts.full_name` → `lead_metadata.contact_name`. Omitted if equal (case-insensitive) to `company`. |
| `contact_title` | string | `contacts.title` → `lead_metadata.contact_title` |
| `title` | string | `leads.title` (an RFP's title for opportunities). Omitted if equal to `company`. |
| `phone` | string | The best number (§8.1) |
| `phones` | string[] | Every distinct number for the lead, `phone` first (§8.1) |
| `email` | string | The best email (§8.2) |
| `emails` | string[] | Every distinct email that isn't `INVALID`, `email` first |
| `website` | string | `lead_metadata.website` → `organizations.website` |
| `category` | string | `leads.category` → `organizations.industry` → `lead_metadata.category` → `lead_metadata.professional_category` |
| `address` | string | `lead_metadata.address` → first location's `address_line1` + `address_line2` → `normalized_location` → `raw_location` |
| `city` | string | `leads.city` → first location's `city` → `lead_metadata.city` |
| `state` | string | `leads.us_state` → first location's `state` → `lead_metadata.state` |
| `postal_code` | string | first location's `postal_code` → `lead_metadata.postal_code` / `zip` |
| `country` | string | first location's `country` → `lead_metadata.country` / `raw_country` |
| `location` | string | `"<city>, <state>"` when both exist, otherwise whichever exists |
| `source` | string | `leads.source_code` (e.g. `bbb`, `googlemaps`) |
| `source_url` | string | `leads.source_url` → `lead_metadata.source_url` |
| `due_at` | string | `leads.due_at` as ISO-8601 (opportunities only) |
| `created_at` | string | `leads.created_at`, ISO-8601 UTC |
| `updated_at` | string | `leads.updated_at`, ISO-8601 UTC |
| `notes`, `lead_status`, `external_id`, `last_activity`, `next_follow_up`, `first_seen_at`, `last_seen_at` | string | Direct columns from `public.leads` (timestamps ISO-8601 UTC) |
| `domain`, `industry`, `company_size`, `organization_linkedin_url` | string | Direct columns from `public.organizations` (`industry` omitted if identical to `category`) |
| `contact_first_name`, `contact_last_name`, `contact_department`, `contact_linkedin_url` | string | Direct columns from `public.contacts` |
| `other_locations` | object[] | Remaining `public.locations` rows after the primary location (`address`, `city`, `state`, `postal_code`, `country`, `is_headquarters`) |
| `other_website`, `alternate_*`, `professional_category` | string | Secondary distinct values from fallback sources (`alternate_company_name`, `alternate_contact_name`, `alternate_contact_title`, `alternate_category`, `professional_category`, `alternate_address`, `alternate_city`, `alternate_state`, `alternate_postal_code`, `alternate_country`, `alternate_source_url`) |
| *flattened extra keys* | scalar / array | Remaining non-empty keys merged from `organizations.raw_data`, `contacts.raw_data`, and `lead_metadata` promoted directly as top-level `snake_case` keys (§8.3) |

"First location" means `locations` rows of the lead's organization ordered by `is_headquarters DESC, created_at ASC`, first row.

### 8.1 Phones
1. Candidates, in this order:
   1. `lead_metadata.phone`, split on `,` and `;`
   2. the contact's `phones` (`is_primary DESC, created_at ASC`)
   3. the organization's direct `phones` (`contact_id IS NULL`); if a contact-linked lead has no phones from `lead_metadata`, the contact, or direct org rows, falls back to all `phones` on the organization (for org-only leads with no `contact_id`, all organization `phones` are used directly)
2. For each phone row use `phone_raw`, or `normalized_phone` if `phone_raw` is empty. Trim whitespace.
3. Remove duplicates by comparing digits only, keeping the first occurrence and its original text.
4. `phone` is the first item and `phones` is the whole list.
5. No reformatting or validation; the dograh caller does that.

### 8.2 Emails
1. Candidates: `lead_metadata.email` (split on `,` and `;`), then the contact's `emails`, then the organization's direct `emails` (`contact_id IS NULL`, falling back to all org `emails` when a contact-linked lead would otherwise have none). The rows are ordered by status `VALID`, `RISKY`, `UNVERIFIED`, null, then `is_primary DESC`, `created_at ASC`.
2. Drop any address whose lowercased value matches an `emails` row with `email_status = 'INVALID'` (matching the same contact/org scope). This applies to addresses from `lead_metadata` too.
3. Remove duplicates case-insensitively. `email` is the first item and `emails` is the whole list.

### 8.3 Flattened extra fields
- Merge `organizations.raw_data`, `contacts.raw_data`, and `lead_metadata` (later non-empty sources override earlier ones). Any wrapper objects named `extra`, `raw_data`, `scraped_data`, `metadata`, `details`, `custom_fields`, or `properties` are unpacked first.
- Remove every depth-0 key already used for a top-level field (`organization_name`, `company_name`, `contact_name`, `contact_title`, `phone`, `email`, `website`, `category`, `professional_category`, `address`, `city`, `state`, `postal_code`, `zip`, `country`, `source_url`) and internal bookkeeping keys (`retry_count`, `email_status`, `email_score`, `email_verified_at`, `email_retry_count`, `email_source`, `auto_enriched`, `profile_enriched`, `_is_enriched_update`, `scrapper_code`), matching both raw lowercased names and normalized `snake_case` names.
- Remove empty values (same rule as above), recursively inside nested objects and lists.
- Flatten nested dicts up to depth 8 with `_`-joined `snake_case` keys and write each key at the top level of the lead payload. If a flattened key collides with an already-populated top-level key with a different scalar value, store it under `extra_<key>` (skipping exact case-insensitive duplicates).
- Strings are sent **in full, never truncated**. Some RFP descriptions exceed 30,000 characters.

### 8.4 Example (shortened)

```json
{
  "lead_id": "ff9839d3-…",
  "content_version": 2,
  "event": "updated",
  "record_kind": "company",
  "company": "Advir Construction",
  "phone": "(215) 555-0103",
  "phones": ["(215) 555-0103", "+12155550104"],
  "email": "info@advir.example",
  "emails": ["info@advir.example"],
  "website": "https://advir.example",
  "category": "General Contractors",
  "address": "123 Market St",
  "city": "Philadelphia",
  "state": "PA",
  "location": "Philadelphia, PA",
  "source": "bbb",
  "source_url": "https://www.bbb.org/…",
  "created_at": "2026-10-12T09:14:03Z",
  "updated_at": "2026-11-18T22:40:51Z",
  "rating": "A+",
  "is_accredited": true,
  "years_in_business": "12",
  "services_offered": "…"
}
```

---

## 9. Batch algorithm

### 9.1 Eligible and due

A lead is **eligible** when it has at least one non-empty, non-placeholder phone (`'none'`, `'null'`, `'n/a'`, and blank/punctuation-only values are ignored):

```sql
(
    NULLIF(lower(btrim(COALESCE(l.lead_metadata->>'phone', ''), E' \t\r\n,;')), '') NOT IN ('none', 'null', 'n/a')
    OR (l.organization_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM public.phones p
        WHERE p.organization_id = l.organization_id
          AND (
              NULLIF(lower(btrim(COALESCE(p.phone_raw, ''), E' \t\r\n,;')), '') NOT IN ('none', 'null', 'n/a')
              OR NULLIF(lower(btrim(COALESCE(p.normalized_phone, ''), E' \t\r\n,;')), '') NOT IN ('none', 'null', 'n/a')
          )
    ))
    OR (l.contact_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM public.phones p
        WHERE p.contact_id = l.contact_id
          AND (
              NULLIF(lower(btrim(COALESCE(p.phone_raw, ''), E' \t\r\n,;')), '') NOT IN ('none', 'null', 'n/a')
              OR NULLIF(lower(btrim(COALESCE(p.normalized_phone, ''), E' \t\r\n,;')), '') NOT IN ('none', 'null', 'n/a')
          )
    ))
)
```

A lead is **due** when it is eligible **and** (`no row in sent_leads` **or** `sent_version < content_version`).

**Order:** `l.created_at ASC, l.id ASC` (oldest first). New and updated leads are mixed in that one order, so an updated old lead is reached before newer never-sent ones.

### 9.2 Steps for one `GET /leads/next`

All of the following happens inside one transaction (`BEGIN … COMMIT`) while holding the in-process batch lock (§10):

1. `SELECT pg_advisory_xact_lock(hashtext('lead_stream:next_batch'));` — this protects against a second copy of the script on the same database.
2. Select the due lead ids, oldest first, `LIMIT BATCH_SIZE`:
   ```sql
   SELECT l.id
   FROM public.leads l
   LEFT JOIN lead_stream.sent_leads s ON s.lead_id = l.id
   WHERE (s.lead_id IS NULL OR s.sent_version < l.content_version)
     AND <eligible condition from 9.1>
   ORDER BY l.created_at, l.id
   LIMIT %(batch_size)s;
   ```
3. If there are no ids, roll back and return the `no_new_leads` response.
4. Load the full data for exactly those ids, using **set-based queries** (one query per table, `WHERE … = ANY(%(ids)s)`), never one query per lead:
   - leads joined with organizations and contacts, **including `l.content_version`** and whether a `sent_leads` row exists (that decides `event`)
   - phones for those organizations and contacts
   - emails for those organizations and contacts
   - locations for those organizations
5. Build the payload objects (§8). Each lead's `content_version` comes from the query in step 4, so the version recorded is **exactly** the version of the data sent, even if the scraper updates the lead a moment later.
6. Mark them sent:
   ```sql
   INSERT INTO lead_stream.sent_leads (lead_id, sent_version, sent_at)
   SELECT * FROM unnest(%(ids)s::varchar[], %(versions)s::int[], %(now)s::timestamptz[])
   ON CONFLICT (lead_id) DO UPDATE
     SET sent_version = EXCLUDED.sent_version, sent_at = EXCLUDED.sent_at;
   ```
   Equivalent forms are fine, e.g. `executemany` or a `VALUES` list.
7. `COMMIT`.
8. Serialize to JSON and send the response. Then release the batch, rows and JSON (§11).

If any step from 1 to 7 raises an error: `ROLLBACK`, respond `503` (database problem) or `500` (anything else), and mark nothing.

### 9.3 Updates to a lead after it was sent
No action is needed when it happens. The scraper bumps `content_version`, the lead becomes due again, and the next ping sends it with `"event": "updated"` and the new version.

### 9.4 Known trade-off: marked when handed out
The owner chose "mark as sent when handed out, no confirmation". Leads are committed as sent **before** the response bytes go out. If the network drops that response (caller crash, timeout, proxy error), those leads count as sent but never arrived. They won't be sent again unless they change or are reset by hand (§18.2). This is accepted behaviour. **Do not** add acknowledgement or extra states without the owner's approval.

---

## 10. Concurrency

- **Server:** a `ThreadingHTTPServer` subclass with `daemon_threads = True`. At most `MAX_CONCURRENT_REQUESTS` requests are handled at once, using a non-blocking semaphore; extra requests get `503 {"status":"error","error":"busy"}` straight away.
- **Batch lock:** a process-wide `threading.Lock`. `/leads/next` waits for it, which means pings are handled **one at a time, in order**.
- **Database access:** the single connection is shared, so **every** use of it (`/leads/next`, `/stats`, `/health`) happens while holding one connection lock. `/health` uses a 2-second timeout to acquire it; on timeout it reports `"database": "busy"` and still returns 200.
- **Several copies of the script:** the advisory lock in step 9.2-1 keeps batches correct, but only one copy is supported, and the systemd unit runs exactly one.

---

## 11. Memory and long-running behaviour

The process runs for months. These rules keep its memory flat:

| Rule | Detail |
|---|---|
| No caches | Never keep leads, ids, counts or responses between requests. |
| Bounded work per request | At most `BATCH_SIZE` (≤1000) leads are loaded per ping. Never load "all leads" or all ids. |
| One batch in memory at a time | Pings are handled one at a time (§10), so at most one 1,000-lead batch (~30 MB peak) exists at once. Build the JSON once as `bytes`, send it, then drop every reference. |
| Release after each request | Local variables only; nothing appended to module-level lists or dicts. |
| Bounded threads | At most `MAX_CONCURRENT_REQUESTS` handler threads; daemon threads; client sockets time out after `REQUEST_TIMEOUT_SECONDS`. |
| Bounded input | Request bodies are never read. Header size is capped by the standard library's limits. |
| One DB connection | No pool, so connections can't leak. Use server-side `fetchall` only on the bounded queries above. |
| Logs | Go to stdout/stderr (collected by journald). No log files, no buffers held in memory. |
| Safety net | The systemd unit sets `MemoryMax=300M` and `Restart=always` (§15.2). Expected resident memory is **under 100 MB** (base process plus one 1,000-lead batch). |

The coding agent MUST include a soak test in its own verification (§19): 500 consecutive 1,000-lead pings against a test database, with resident memory checked before and after (growth under 15 MB once the first few pings have warmed up).

---

## 12. Errors

| Situation | HTTP | Body | Marked as sent? |
|---|---|---|---|
| Missing or wrong token | 401 | `{"status":"error","error":"unauthorized"}` | No |
| `Host` doesn't match `PUBLIC_BASE_URL` | 421 | `{"status":"error","error":"wrong_host"}` | No |
| Database unreachable or timed out | 503 | `{"status":"error","error":"database_unavailable","retry_after_seconds":30}` + `Retry-After: 30` | No |
| Too many requests at once | 503 | `{"status":"error","error":"busy","retry_after_seconds":5}` + `Retry-After: 5` | No |
| Unexpected error | 500 | `{"status":"error","error":"internal_error"}` (no stack trace in the response) | No |
| Nothing due | 200 | `no_new_leads` (§7.1) | — |

The process MUST NOT exit because of any per-request error. Startup configuration errors (§5.2) are the only reason to exit on its own.

**Shutdown:** on `SIGTERM` or `SIGINT`, stop accepting connections, let the request in progress finish (up to 20 s), close the database connection, and exit 0.

---

## 13. Logging

One line per event to stdout, as `key=value` pairs:

```
2026-11-19T14:02:11Z INFO  startup version=1.0 bind=127.0.0.1:8090 batch_size=300 ping_url=https://ai-sdr.grobots.ai/leads/next
2026-11-19T14:02:11Z INFO  db_connected application_name=lead_streamer
2026-11-19T14:05:40Z INFO  ping client=203.0.113.7 status=ok count=1000 new=874 updated=126 bytes=2341877 ms=1420
2026-11-19T15:10:02Z INFO  ping client=203.0.113.7 status=no_new_leads count=0 ms=21
2026-11-19T15:12:00Z WARN  unauthorized client=198.51.100.9 path=/leads/next
2026-11-19T15:20:13Z ERROR db_unavailable error="connection refused" action=reconnect_next_request
```

- `client` is the first address in `X-Forwarded-For` when the request comes from `127.0.0.1` (nginx), otherwise the socket address.
- MUST NOT log the token, the database password, or any lead field (R15).
- MUST NOT use the standard library's default request logging; replace `log_message` with the format above.

---

## 14. Security

- **Token:**
  - `Authorization: Bearer <STREAM_TOKEN>`, compared with `hmac.compare_digest`.
  - Generate it with `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
  - Rotate it by changing `.env` on both servers and restarting the streamer.
- **Network:**
  - The script listens on `127.0.0.1` only.
  - The public entry point is nginx with a **valid** HTTPS certificate on your company subdomain.
  - Plain HTTP on the subdomain MUST redirect to HTTPS (§15.3).
- **Personal data:** responses contain phone numbers, names and emails. Never expose the endpoint without the token and HTTPS. Never log lead contents.
- **Database user:** a dedicated, least-privilege user is recommended (§6.2 permissions).
- **Calling law:** the receiving side places AI voice calls. Scraped leads usually come without consent (see the TCPA note in the dograh caller's documentation). The streamer only hands out data; whether and how to call is the caller's responsibility.

---

## 15. Deployment

### 15.1 Install

```bash
sudo mkdir -p /opt/lead_streamer && sudo cp -r lead_streamer/* /opt/lead_streamer/
cd /opt/lead_streamer
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env     # then edit .env
```

### 15.2 systemd (`lead-streamer.service`)

```ini
[Unit]
Description=Lead streamer (hands scraper leads to the dograh caller)
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=simple
User=lead-streamer
Group=lead-streamer
WorkingDirectory=/opt/lead_streamer
ExecStart=/opt/lead_streamer/.venv/bin/python lead_streamer.py
Restart=always
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=25
MemoryMax=300M
NoNewPrivileges=true
ProtectSystem=strict
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

Enable it and follow its logs:
```bash
sudo cp lead-streamer.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now lead-streamer
journalctl -u lead-streamer -f
```

### 15.3 nginx (`nginx-lead-streamer.conf`)

For a dedicated subdomain (recommended): `nginx-lead-streamer.conf` in this folder. It is a
plain-HTTP `server` block for `ai-sdr.grobots.ai` that proxies to `127.0.0.1:8090` with
`proxy_set_header Host $host` (required by the host check), `X-Forwarded-For $remote_addr`,
`proxy_read_timeout 120s` and gzip for JSON. It deliberately has no `listen 443 ssl`: a TLS block
without a certificate makes `nginx -t` fail. `certbot --nginx -d ai-sdr.grobots.ai --redirect`
adds TLS and the http->https redirect to the same block. Step-by-step: `GO_LIVE.md` in the
dograh_caller folder.

If the streamer lives under a path on an existing domain instead (e.g. `https://example.com/streamer/`), use `location /streamer/ { proxy_pass http://127.0.0.1:8090/; ... }`. The trailing `/` on `proxy_pass` strips the prefix. Then set `PUBLIC_BASE_URL=https://example.com/streamer`.

**Before going live, the certificate MUST be valid.** Check it with `curl -I https://ai-sdr.grobots.ai/health`; that should return `200` without any `-k` flag.

State on 2026-11-20: `ai-sdr.grobots.ai` has only an A record, **167.88.38.179** (the
CyberPanel/OpenLiteSpeed mail server, where an older push receiver answers `/health` with
`{"status":"ok"}` and `/leads/next` with 404). The scraper server (79.143.180.241) already runs
nginx 1.24 for `scrapper.grobots.ai` with ports 80 and 443 open, and serves unknown names with
that site, so a request for `ai-sdr.grobots.ai` there currently reaches the scraper backend
(`{"status":"healthy","service":"DataOps AI Backend"}`). Going live = add the site above, move
the A record to 79.143.180.241, run certbot (`GO_LIVE.md`).

### 15.4 Once the subdomain is ready

1. Paste it into `PUBLIC_BASE_URL` (script top, or `.env`).
2. `sudo systemctl restart lead-streamer`.
3. Copy the `ping_url=` value from the startup log to the dograh caller, together with the token.

---

## 16. What the dograh caller must do

The dograh caller is a separate project; this section is the agreement it must follow.

1. Call `GET <PUBLIC_BASE_URL>/leads/next` with `Authorization: Bearer <STREAM_TOKEN>`.
2. On `"status": "ok"`: submit every item of `leads` to its pipeline. Each item is a ready-made lead dict with `phone` / `phones`.
3. Finish processing that batch before pinging again. In automatic mode (`python main.py`) the caller then pings on its own, after confirming `GET /health` reports `alive`. In manual mode (`python pull_leads.py`) it waits for a person (Enter = next ping, `q` = quit).
4. On `"status": "no_new_leads"`: report it, then check again on a timer (automatic, default 60 s) or wait for a person (manual).
5. On `503`: wait at least `retry_after_seconds` before pinging again (automatic) or wait for a person (manual). On `401` / `421`: stop and alert a person, because that is a configuration problem.
6. Use `lead_id` + `content_version` to recognise a lead; an `event: "updated"` lead was sent before. Whether to call an updated lead again is the caller's decision.
7. Never ping in parallel. Pings are processed one at a time anyway.
8. Use a request timeout of at least **120 s**, and send `Accept-Encoding: gzip`. Most HTTP libraries do the latter by default. A full batch is 300 leads, about 0.7–1 MB of JSON before compression (1,000 leads: 2–3 MB).
9. Treat the whole batch as one unit. All leads of a batch arrive in one response and are already marked as sent when it arrives (§9.4), so the caller saves the batch to disk before processing it.

---

## 17. Live verification checklist

Run these on the scraper server after deployment. Replace `$URL` and `$TOKEN`.

| # | Check | Expected |
|---|---|---|
| 1 | `curl -s https://$URL/health` | `{"status":"alive","database":"ok"}` |
| 2 | `curl -s -o /dev/null -w "%{http_code}" https://$URL/leads/next` (no token) | `401` |
| 3 | `curl -s -H "Authorization: Bearer $TOKEN" https://$URL/stats` | Counts. `eligible` matches SQL A below. |
| 4 | `curl -s -H "Authorization: Bearer $TOKEN" https://$URL/leads/next \| python3 -m json.tool \| head -60` | `"status": "ok"`, `count` ≤ 300, each lead has `lead_id` and `phone` |
| 5 | SQL B below | Increased by exactly the `count` from step 4 |
| 6 | Repeat step 4 | Different `lead_id`s (none repeated) |
| 7 | Keep pinging until done | Ends with `"status": "no_new_leads"`, `"message": "No new Leads available"`; the service stays `active` |
| 8 | `systemctl status lead-streamer` after a day | `active (running)`, memory well under 300M |

```sql
-- A. eligible leads (with a non-placeholder phone)
SELECT count(*) FROM public.leads l
WHERE NULLIF(lower(btrim(COALESCE(l.lead_metadata->>'phone', ''), E' \t\r\n,;')), '') NOT IN ('none', 'null', 'n/a')
   OR (l.organization_id IS NOT NULL AND EXISTS (
       SELECT 1 FROM public.phones p
       WHERE p.organization_id = l.organization_id
         AND (
             NULLIF(lower(btrim(COALESCE(p.phone_raw, ''), E' \t\r\n,;')), '') NOT IN ('none', 'null', 'n/a')
             OR NULLIF(lower(btrim(COALESCE(p.normalized_phone, ''), E' \t\r\n,;')), '') NOT IN ('none', 'null', 'n/a')
         )
   ))
   OR (l.contact_id IS NOT NULL AND EXISTS (
       SELECT 1 FROM public.phones p
       WHERE p.contact_id = l.contact_id
         AND (
             NULLIF(lower(btrim(COALESCE(p.phone_raw, ''), E' \t\r\n,;')), '') NOT IN ('none', 'null', 'n/a')
             OR NULLIF(lower(btrim(COALESCE(p.normalized_phone, ''), E' \t\r\n,;')), '') NOT IN ('none', 'null', 'n/a')
         )
   ));

-- B. how many have been sent
SELECT count(*) FROM lead_stream.sent_leads;

-- C. leads updated since they were sent (due again)
SELECT count(*) FROM lead_stream.sent_leads s JOIN public.leads l ON l.id = s.lead_id
WHERE s.sent_version < l.content_version;
```

For reference, on 2026-11-19 the development copy of the database had **9,843 leads, 9,046 of them with a phone**. That is **10 pings** (9 full batches of 1,000 and one of 46) to send everything.

---

## 18. Operations

### 18.1 Daily
```bash
systemctl status lead-streamer
journalctl -u lead-streamer --since today | tail -50
```

### 18.2 Sending leads again by hand
```sql
-- one lead
DELETE FROM lead_stream.sent_leads WHERE lead_id = '<lead id>';
-- everything sent today
DELETE FROM lead_stream.sent_leads WHERE sent_at >= date_trunc('day', now());
-- start over completely
TRUNCATE lead_stream.sent_leads;
```
They are due again at the next ping. No restart is needed.

### 18.3 Removing the streamer
`systemctl disable --now lead-streamer` and `DROP SCHEMA lead_stream CASCADE;`. The scraper is unaffected.

---

## 19. Acceptance criteria

The coding agent MUST show each of these passing before handing over. Use a test copy of the database, or a throwaway schema name set only during testing; anything test-only is removed afterwards (R12).

1. **Isolation:** `lead_streamer.py` has no imports from the scraper project; `grep` shows no `Backend`, `Database`, `sqlalchemy` or `fastapi`.
2. **Schema:** first start creates `lead_stream.sent_leads` exactly as §6.2; a second start changes nothing.
3. **Batch:** a ping returns ≤1000 leads in one response, all with a phone, oldest first, and exactly those ids appear in `sent_leads` with their `content_version`.
4. **No repeats:** consecutive pings never return the same `lead_id` at the same `content_version`.
5. **Updates:** raising a sent lead's `content_version` makes the next ping include it with `"event": "updated"` and the new version.
6. **Exhaustion:** with nothing due, the ping returns `no_new_leads` / `"No new Leads available"` and the process keeps running.
7. **Atomic:** forcing an error after step 9.2-5 (e.g. a dropped connection) leaves `sent_leads` unchanged and returns 503/500.
8. **Parallel pings:** 10 simultaneous pings return disjoint lead sets in total.
9. **Auth:** no token or a wrong token gives 401; `/health` works without a token and reveals nothing.
10. **Host check:** with `PUBLIC_BASE_URL` set, a wrong `Host` gives 421.
11. **Database outage:** with PostgreSQL stopped, pings get 503 and the process stays up; after PostgreSQL returns, pings succeed without a restart.
12. **Memory:** 500 consecutive 1,000-lead pings grow resident memory by less than 15 MB after warm-up, and peak stays under 100 MB.
13. **Logs:** no lead content, token or password appears in the output of all of the above.
14. **Shutdown:** `SIGTERM` exits within 25 s with code 0.
15. **Clean deliverable:** no dry-run flag, test hooks or debug endpoints remain; the files match §4 exactly.
