"""
Central configuration for dograh_caller.

Every value can be overridden with an environment variable or a line in a
`.env` file (looked up in this folder first, then in its parent folder).
Secrets (API keys) should ONLY live in `.env`, never in code.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent

# .env in this folder wins over .env in the parent folder; real environment
# variables win over both.
load_dotenv(PROJECT_DIR / ".env", override=False)
load_dotenv(PROJECT_DIR.parent / ".env", override=False)


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
SKIP_ALREADY_CALLED: bool = _env("SKIP_ALREADY_CALLED", "true").lower() not in ("0", "false", "no")

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
SERVICES_FILE: Path = Path(_env("SERVICES_FILE", str(PROJECT_DIR / "services.yaml")))

# ------------------------------------------------------------------------------
# Dograh - calling agents
# ------------------------------------------------------------------------------
DOGRAH_API_KEY: str = _env("DOGRAH_API_KEY")
DOGRAH_BASE_URL: str = _env("DOGRAH_BASE_URL", "https://dograh.grobots.ai/api/v1").rstrip("/")
VERIFY_SSL: bool = _env("DOGRAH_VERIFY_SSL", "true").lower() not in ("0", "false", "no")
REQUEST_TIMEOUT: int = _env_int("DOGRAH_REQUEST_TIMEOUT", 30)

# --- Campaigns ---------------------------------------------------------------
# Each run creates one Dograh campaign per service that has interested leads,
# named "<service name>_<YYYY-MM-DD>" (e.g. "Draftliner_2026-11-19").
CAMPAIGN_NAME_DATE_FORMAT: str = _env("CAMPAIGN_NAME_DATE_FORMAT", "%Y-%m-%d")

# Retry settings sent with each new campaign (Dograh handles the retries).
CAMPAIGN_RETRY_ENABLED: bool = _env("CAMPAIGN_RETRY_ENABLED", "true").lower() not in ("0", "false", "no")
CAMPAIGN_MAX_RETRIES: int = _env_int("CAMPAIGN_MAX_RETRIES", 1)
CAMPAIGN_RETRY_DELAY_SECONDS: int = _env_int("CAMPAIGN_RETRY_DELAY_SECONDS", 120)
CAMPAIGN_RETRY_ON_BUSY: bool = _env("CAMPAIGN_RETRY_ON_BUSY", "true").lower() not in ("0", "false", "no")
CAMPAIGN_RETRY_ON_NO_ANSWER: bool = _env("CAMPAIGN_RETRY_ON_NO_ANSWER", "true").lower() not in ("0", "false", "no")
CAMPAIGN_RETRY_ON_VOICEMAIL: bool = _env("CAMPAIGN_RETRY_ON_VOICEMAIL", "false").lower() not in ("0", "false", "no")

# Seconds between progress checks of running campaigns.
CAMPAIGN_POLL_SECONDS: int = _env_int("CAMPAIGN_POLL_SECONDS", 5)
# If a campaign makes no progress (no new call, no finished call) for this
# long, its remaining leads are recorded as call_timeout and monitoring stops.
CAMPAIGN_IDLE_TIMEOUT_SECONDS: int = _env_int("CAMPAIGN_IDLE_TIMEOUT_SECONDS", 900)
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
RESULTS_FILE: Path = Path(_env("RESULTS_FILE", str(PROJECT_DIR / "output" / "call_results.xlsx")))

# Per-lead Excel pipeline tracker (one row per lead, updated at each stage).
PIPELINE_TRACKER_FILE: Path = Path(_env("PIPELINE_TRACKER_FILE", str(PROJECT_DIR / "output" / "pipeline_tracker.xlsx")))
TRACKER_SAVE_SECONDS: int = _env_int("TRACKER_SAVE_SECONDS", 3)
TRACKER_WARN_ROWS: int = _env_int("TRACKER_WARN_ROWS", 20000)

# ------------------------------------------------------------------------------
# Dashboard (read-only)
# ------------------------------------------------------------------------------
DASHBOARD_HOST: str = _env("DASHBOARD_HOST", "127.0.0.1")
DASHBOARD_PORT: int = _env_int("DASHBOARD_PORT", 8090)
DASHBOARD_REFRESH_SECONDS: int = _env_int("DASHBOARD_REFRESH_SECONDS", 5)

# Separate spreadsheet with only the businesses whose call the agent transferred
# (to a person / another number), with their details - for whoever takes them over.
TRANSFERS_FILE: Path = Path(_env("TRANSFERS_FILE", str(PROJECT_DIR / "output" / "transferred_calls.xlsx")))
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
PULLED_BATCHES_DIR: Path = Path(_env("PULLED_BATCHES_DIR", str(PROJECT_DIR / "output" / "pulled_batches")))
