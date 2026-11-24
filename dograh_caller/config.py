"""
Central configuration for dograh_caller.

Every value can be overridden with an environment variable or a line in a
`.env` file. The `.env` in this folder is used; the parent folder's `.env` is
read only when this folder has none (the two are never mixed, so keys from two
different Dograh deployments can't end up combined).
Secrets (API keys) should ONLY live in `.env`, never in code.

Relative paths in settings (RESULTS_FILE=output/call_results.xlsx, ...) are
relative to this folder, whatever folder the program is started from.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent

# Real environment variables win over the .env file. Only ONE .env file is
# read: this folder's, or - only if this folder has none - the parent folder's.
_LOCAL_ENV = PROJECT_DIR / ".env"
_PARENT_ENV = PROJECT_DIR.parent / ".env"
ENV_FILE: Optional[Path] = _LOCAL_ENV if _LOCAL_ENV.is_file() else (_PARENT_ENV if _PARENT_ENV.is_file() else None)
if ENV_FILE is not None:
    load_dotenv(ENV_FILE, override=False)

_TRUE_WORDS = frozenset({"1", "true", "yes", "y", "on", "t"})
_FALSE_WORDS = frozenset({"0", "false", "no", "n", "off", "f"})


def _env(name: str, default: str = "", *aliases: str) -> str:
    for key in (name, *aliases):
        value = os.environ.get(key)
        if value is not None and value.strip():
            return value.strip()
    return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    """true/yes/on/1 or false/no/off/0 (any case). Anything else keeps the default, with a warning."""
    raw = _env(name, "")
    if not raw:
        return default
    word = raw.lower()
    if word in _TRUE_WORDS:
        return True
    if word in _FALSE_WORDS:
        return False
    print(f"WARNING: {name}={raw!r} is not true/false (use true/false, yes/no, on/off or 1/0); "
          f"using the default ({'true' if default else 'false'}).", file=sys.stderr)
    return default


def project_path(value: "str | Path") -> Path:
    """A path from a setting: relative paths are relative to this folder, not the current directory."""
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_DIR / path


def _env_path(name: str, default: Path) -> Path:
    raw = _env(name, "")
    return project_path(raw) if raw else default


# ------------------------------------------------------------------------------
# Number verifier
# ------------------------------------------------------------------------------
# Phone regions accepted (ISO codes, comma-separated). Default: the 50 states + DC
# only. Other +1 regions are dropped: Canada (CA), Puerto Rico (PR), US Virgin
# Islands (VI), Guam (GU), Northern Mariana Islands (MP), American Samoa (AS),
# and the Caribbean. To include the US territories use "US,PR,VI,GU,MP,AS".
ALLOWED_PHONE_REGIONS = [r for r in _env("ALLOWED_PHONE_REGIONS", "US").split(",") if r.strip()]

# Never call a number again that an earlier run already called (read from the
# results sheet at startup: rows with status "called" or "call_timeout").
SKIP_ALREADY_CALLED: bool = _env_bool("SKIP_ALREADY_CALLED", True)

# ------------------------------------------------------------------------------
# Gemini - decision maker
# ------------------------------------------------------------------------------
GEMINI_API_KEY: str = _env("GEMINI_API_KEY", "", "GEMINI_API")
GEMINI_MODEL: str = _env("GEMINI_MODEL", "gemini-3.8-flash")
GEMINI_THINKING_LEVEL: str = _env("GEMINI_THINKING_LEVEL", "MEDIUM").upper()  # LOW, MEDIUM or HIGH
GEMINI_TIMEOUT_SECONDS: int = _env_int("GEMINI_TIMEOUT_SECONDS", 60)
GEMINI_MAX_ATTEMPTS: int = _env_int("GEMINI_MAX_ATTEMPTS", 4)

# A lead is passed to a service's calling agent only if the probability that
# the lead is a fit for that service is >= this value (0.80 = 80%).
INTEREST_THRESHOLD: float = _env_float("INTEREST_THRESHOLD", 0.80)

# What Gemini gets per lead: only descriptive fields (see decision_maker.lead_state),
# each cut to LEAD_MAX_FIELD_CHARS, all together at most LEAD_MAX_STATE_CHARS.
LEAD_MAX_FIELD_CHARS: int = _env_int("LEAD_MAX_FIELD_CHARS", _env_int("JEV_MAX_FIELD_CHARS", 2000))
LEAD_MAX_STATE_CHARS: int = _env_int("LEAD_MAX_STATE_CHARS", _env_int("JEV_MAX_STATE_CHARS", 12000))

# Where services (DraftLiner, Paradise, ...) are described. See SERVICES.md.
SERVICES_FILE: Path = _env_path("SERVICES_FILE", PROJECT_DIR / "services.yaml")

# ------------------------------------------------------------------------------
# Dograh - calling agents
# ------------------------------------------------------------------------------
DOGRAH_API_KEY: str = _env("DOGRAH_API_KEY")
DOGRAH_BASE_URL: str = _env("DOGRAH_BASE_URL", "https://dograh.grobots.ai/api/v1").rstrip("/")
VERIFY_SSL: bool = _env_bool("DOGRAH_VERIFY_SSL", True)
REQUEST_TIMEOUT: int = _env_int("DOGRAH_REQUEST_TIMEOUT", 30)

# --- What the contacts CSV sends to the agent ----------------------------------
# By default only the {{variables}} the agent's published version uses, plus
# these lead fields (comma-separated, names as in the CSV: lower case, "_").
DOGRAH_EXTRA_FIELDS = [
    f.strip().lower() for f in _env(
        "DOGRAH_EXTRA_FIELDS",
        "company,company_name,business_name,contact_name,contact_title,category,industry,city,state,location,lead_id",
    ).split(",") if f.strip()
]
# true = send every lead field (the old behaviour: big text fields, e-mails, ...).
DOGRAH_SEND_ALL_FIELDS: bool = _env_bool("DOGRAH_SEND_ALL_FIELDS", False)

# --- Campaigns ---------------------------------------------------------------
# Each run creates one Dograh campaign per service that has interested leads,
# named "<service name>_<YYYY-MM-DD>" (e.g. "Draftliner_2026-11-19").
CAMPAIGN_NAME_DATE_FORMAT: str = _env("CAMPAIGN_NAME_DATE_FORMAT", "%Y-%m-%d")

# Retry settings sent with each new campaign (Dograh handles the retries).
CAMPAIGN_RETRY_ENABLED: bool = _env_bool("CAMPAIGN_RETRY_ENABLED", True)
CAMPAIGN_MAX_RETRIES: int = _env_int("CAMPAIGN_MAX_RETRIES", 1)
CAMPAIGN_RETRY_DELAY_SECONDS: int = _env_int("CAMPAIGN_RETRY_DELAY_SECONDS", 120)
CAMPAIGN_RETRY_ON_BUSY: bool = _env_bool("CAMPAIGN_RETRY_ON_BUSY", True)
CAMPAIGN_RETRY_ON_NO_ANSWER: bool = _env_bool("CAMPAIGN_RETRY_ON_NO_ANSWER", True)
CAMPAIGN_RETRY_ON_VOICEMAIL: bool = _env_bool("CAMPAIGN_RETRY_ON_VOICEMAIL", False)

# Seconds between progress checks of running campaigns.
CAMPAIGN_POLL_SECONDS: int = _env_int("CAMPAIGN_POLL_SECONDS", 5)
# If a campaign makes no progress (no new call, no finished call) for this
# long while NO call is in progress, its remaining leads are recorded
# (dialed -> call_timeout, never dialed -> call_failed) and monitoring stops.
CAMPAIGN_IDLE_TIMEOUT_SECONDS: int = _env_int("CAMPAIGN_IDLE_TIMEOUT_SECONDS", 900)
# A call still in progress counts as progress (a transferred call can last long)
# until it has been going for this long; after that it is treated as stuck.
CAMPAIGN_MAX_CALL_SECONDS: int = _env_int("CAMPAIGN_MAX_CALL_SECONDS", 3600)
# After a Stop, wait this long after pausing a campaign before the last look at
# its calls (so a call Dograh started at that moment is still recorded).
STOP_SYNC_SECONDS: float = _env_float("STOP_SYNC_SECONDS", 2.0)

# Call outcomes (dispositions) that mean nobody was reached. Such a call is
# recorded as call_failed (not "called"), its number is not blocked for later
# runs, and with RETRY_FAILED_CALLS it is queued to be called again.
NOT_REACHED_DISPOSITIONS = frozenset(
    d.strip().lower() for d in _env(
        "NOT_REACHED_DISPOSITIONS",
        "busy,no-answer,no_answer,failed,error,canceled,cancelled,voicemail,machine",
    ).split(",") if d.strip()
)
# Default for the "retry failed calls" option (dashboard Start Batch, --retry-failed-calls):
# leads whose call did not reach anyone are saved and called again in a later batch.
# Leads that were never dialed (campaign could not start, ...) are always saved for retry.
RETRY_FAILED_CALLS: bool = _env_bool("RETRY_FAILED_CALLS", False)
# A lead is saved for retry (Gemini failure, failed call) at most this many times.
RETRY_MAX_ATTEMPTS: int = max(0, _env_int("RETRY_MAX_ATTEMPTS", 3))
# At start-up, look up the final result of calls that were still going when an
# earlier run stopped (only rows updated within this many days; 0 = off).
RECOVER_CALLS_DAYS: int = max(0, _env_int("RECOVER_CALLS_DAYS", 7))
# After a call ends, wait up to this long for Dograh to fill in the outcome.
RESULT_WAIT_SECONDS: int = _env_int("RESULT_WAIT_SECONDS", 20)
# Extra wait beyond the retry delay for a scheduled retry call to appear.
RETRY_GRACE_SECONDS: int = _env_int("RETRY_GRACE_SECONDS", 120)

# ------------------------------------------------------------------------------
# Pipeline
# ------------------------------------------------------------------------------
# Leads are processed in batches of this size: a batch is verified, scored,
# put into campaigns and fully called before the next batch is taken.
LEAD_BATCH_SIZE: int = _env_int("LEAD_BATCH_SIZE", 300)

# Max items waiting between two stages (back-pressure: faster stages pause).
QUEUE_MAXSIZE: int = _env_int("QUEUE_MAXSIZE", 100)
# Parallel Gemini requests.
DECISION_CONCURRENCY: int = _env_int("DECISION_CONCURRENCY", 8)

# Most lead fields that get their own column in the results sheet. Leads are
# unstructured, so without a limit every new field name would add a column.
# Fields beyond the limit are kept (as JSON) in the "other_lead_fields" column.
RESULTS_MAX_LEAD_COLUMNS: int = _env_int("RESULTS_MAX_LEAD_COLUMNS", 80)

# Results spreadsheet (created if missing, appended to if it exists).
RESULTS_FILE: Path = _env_path("RESULTS_FILE", PROJECT_DIR / "output" / "call_results.xlsx")
# The results files are rewritten at most this often while a run goes on
# (and always when it ends), instead of after every single result.
RESULTS_SAVE_SECONDS: float = max(0.0, _env_float("RESULTS_SAVE_SECONDS", 15))

# Per-lead Excel pipeline tracker (one row per lead, updated at each stage).
PIPELINE_TRACKER_FILE: Path = _env_path("PIPELINE_TRACKER_FILE", PROJECT_DIR / "output" / "pipeline_tracker.xlsx")
TRACKER_SAVE_SECONDS: int = _env_int("TRACKER_SAVE_SECONDS", 3)
TRACKER_WARN_ROWS: int = _env_int("TRACKER_WARN_ROWS", 20000)
# Finished tracker rows older than this many days are moved to
# <tracker>_archive.xlsx when a run starts, so the live tracker (rewritten every
# few seconds and read by the dashboard) stays small. Their numbers stay blocked
# for SKIP_ALREADY_CALLED. 0 = never archive.
TRACKER_ARCHIVE_DAYS: int = max(0, _env_int("TRACKER_ARCHIVE_DAYS", 30))

# ------------------------------------------------------------------------------
# Dashboard (read-only)
# ------------------------------------------------------------------------------
DASHBOARD_HOST: str = _env("DASHBOARD_HOST", "127.0.0.1")
DASHBOARD_PORT: int = _env_int("DASHBOARD_PORT", 8090)
DASHBOARD_REFRESH_SECONDS: int = _env_int("DASHBOARD_REFRESH_SECONDS", 5)

# Separate spreadsheet with only the businesses whose call the agent transferred
# (to a person / another number), with their details - for whoever takes them over.
TRANSFERS_FILE: Path = _env_path("TRANSFERS_FILE", PROJECT_DIR / "output" / "transferred_calls.xlsx")
# Call dispositions / tags that mean "transferred". Dograh records "transfer_call"
# (phone transfer) or "call_transferred" (PBX transfer); a transfer tool can be set
# to record its own disposition - add that here (comma-separated). Any disposition
# or tag containing "transfer" (but not "fail") also counts.
TRANSFER_DISPOSITIONS = frozenset(
    d.strip().lower() for d in _env("TRANSFER_DISPOSITIONS", "transfer_call,call_transferred").split(",") if d.strip()
)

# ------------------------------------------------------------------------------
# Lead source: the lead streamer on the scraper side (used by pull_leads.py)
# ------------------------------------------------------------------------------
# The streamer's public address (its PUBLIC_BASE_URL). pull_leads.py pings
# <this>/leads/next and gets one batch of up to 300 leads per ping.
LEAD_SOURCE_URL: str = _env("LEAD_SOURCE_URL", "https://ai-sdr.grobots.ai", "STREAMER_URL").rstrip("/")
# The streamer's STREAM_TOKEN (shared secret). Only in .env, never in code.
LEAD_SOURCE_TOKEN: str = _env("LEAD_SOURCE_TOKEN", "", "STREAM_TOKEN")
# Seconds to wait for a ping's answer (the streamer contract asks for >= 120).
LEAD_SOURCE_TIMEOUT: int = max(120, _env_int("LEAD_SOURCE_TIMEOUT", 180))
# Every received batch is saved here first, so a crash or Ctrl+C never loses
# leads (the streamer marks them as sent the moment it hands them out).
PULLED_BATCHES_DIR: Path = _env_path("PULLED_BATCHES_DIR", PROJECT_DIR / "output" / "pulled_batches")
