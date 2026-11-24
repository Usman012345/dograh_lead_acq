#!/usr/bin/env python3
"""Standalone HTTP lead streamer backed by PostgreSQL."""

from __future__ import annotations

import dataclasses
import datetime
import hmac
import http
import http.server
import json
import math
import os
import pathlib
import re
import signal
import sys
import threading
import time
import typing
import urllib.parse

import psycopg
from psycopg import errors as pg_errors

# ─────────────────────────────────────────────────────────────────────────────
# PASTE YOUR COMPANY SUBDOMAIN HERE once it is ready (HTTPS address, no path),
# e.g. "https://leads.yourcompany.com". Leave "" to skip the host check.
# An environment variable PUBLIC_BASE_URL (or the .env file) overrides this.
PUBLIC_BASE_URL = "https://ai-sdr.grobots.ai"
# ─────────────────────────────────────────────────────────────────────────────

VERSION = "1.0"

_EMPTY_STRINGS = frozenset({"", "none", "null", "n/a"})
_MISSING = object()

_EXTRA_TOP_LEVEL_KEYS = frozenset({
    "organization_name",
    "company_name",
    "contact_name",
    "contact_title",
    "phone",
    "email",
    "website",
    "category",
    "professional_category",
    "address",
    "city",
    "state",
    "postal_code",
    "zip",
    "country",
    "source_url",
})

_WRAPPER_KEYS = frozenset({
    "extra",
    "raw_data",
    "scraped_data",
    "metadata",
    "details",
    "custom_fields",
    "properties",
})

_EXTRA_INTERNAL_KEYS = frozenset({
    "retry_count",
    "email_status",
    "email_score",
    "email_verified_at",
    "email_retry_count",
    "email_source",
    "auto_enriched",
    "profile_enriched",
    "_is_enriched_update",
    "scrapper_code",
})

_LOG_LEVELS = {
    "DEBUG": 10,
    "INFO": 20,
    "WARNING": 30,
    "ERROR": 40,
}

_LOG_LABELS = {
    10: "DEBUG",
    20: "INFO ",
    30: "WARN ",
    40: "ERROR",
}

_ELIGIBLE_SQL = """(
    NULLIF(lower(btrim(COALESCE(l.lead_metadata->>'phone', ''), E' \\t\\r\\n,;')), '') NOT IN ('none', 'null', 'n/a')
    OR (l.organization_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM public.phones p
        WHERE p.organization_id = l.organization_id
          AND (
              NULLIF(lower(btrim(COALESCE(p.phone_raw, ''), E' \\t\\r\\n,;')), '') NOT IN ('none', 'null', 'n/a')
              OR NULLIF(lower(btrim(COALESCE(p.normalized_phone, ''), E' \\t\\r\\n,;')), '') NOT IN ('none', 'null', 'n/a')
          )
    ))
    OR (l.contact_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM public.phones p
        WHERE p.contact_id = l.contact_id
          AND (
              NULLIF(lower(btrim(COALESCE(p.phone_raw, ''), E' \\t\\r\\n,;')), '') NOT IN ('none', 'null', 'n/a')
              OR NULLIF(lower(btrim(COALESCE(p.normalized_phone, ''), E' \\t\\r\\n,;')), '') NOT IN ('none', 'null', 'n/a')
          )
    ))
)"""


class DbUnavailableError(Exception):
    """Raised when PostgreSQL is unreachable, times out, or drops a connection."""

    def __init__(self, code: str = "database_unavailable") -> None:
        super().__init__(code)
        self.code = code


class SchemaMismatchError(Exception):
    """The scraper's tables lack columns the streamer reads (wrong database or old scraper version)."""


# Every scraper column the streamer reads (spec section 6.1). Checked once per
# connection before the first ping, so a wrong database fails with a clear message.
REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "leads": (
        "id", "organization_id", "contact_id", "record_kind", "title", "category", "city",
        "us_state", "source_code", "source_url", "due_at", "lead_metadata", "content_version",
        "created_at", "updated_at", "notes", "status", "last_activity", "next_follow_up",
        "external_id", "first_seen_at", "last_seen_at",
    ),
    "organizations": ("id", "name", "website", "industry", "domain", "company_size", "linkedin_url", "raw_data"),
    "contacts": ("id", "full_name", "title", "first_name", "last_name", "department", "linkedin_url", "raw_data"),
    "phones": ("organization_id", "contact_id", "phone_raw", "normalized_phone", "is_primary", "created_at"),
    "emails": ("organization_id", "contact_id", "email", "email_status", "is_primary", "created_at"),
    "locations": (
        "organization_id", "address_line1", "address_line2", "city", "state", "postal_code",
        "country", "normalized_location", "raw_location", "is_headquarters", "created_at",
    ),
}


def missing_columns(present: typing.Iterable[tuple[str, str]]) -> list[str]:
    """`present` = (table, column) pairs found in schema public. Returns 'table.column' still missing."""
    have = {(t, c) for t, c in present}
    missing = []
    for table, cols in REQUIRED_COLUMNS.items():
        if not any(t == table for t, _ in have):
            missing.append(f"{table} (table)")
            continue
        missing.extend(f"{table}.{c}" for c in cols if (table, c) not in have)
    return missing


def _unavailable(exc: BaseException) -> DbUnavailableError:
    if isinstance(exc, SchemaMismatchError):
        return DbUnavailableError("schema_mismatch")
    return DbUnavailableError()


@dataclasses.dataclass(frozen=True)
class Config:
    public_base_url: str
    expected_host: str
    ping_url: str
    database_url: str
    stream_token_bytes: bytes
    bind_host: str
    bind_port: int
    batch_size: int
    max_concurrent_requests: int
    db_statement_timeout_ms: int
    request_timeout_seconds: float
    log_level: int
    secret_values: tuple[str, ...]
    startup_warnings: tuple[str, ...]


def _read_dotenv(path: pathlib.Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"Configuration error: failed to read {path.name}: {exc}", file=sys.stderr)
        sys.exit(1)

    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, raw_val = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        val = raw_val.strip()
        if len(val) >= 2 and (
            (val[0] == '"' and val[-1] == '"') or (val[0] == "'" and val[-1] == "'")
        ):
            val = val[1:-1]
        values[key] = val
    return values


def _get_setting(
    key: str,
    dotenv: dict[str, str],
    default: str | None = None,
) -> str | None:
    if key in os.environ:
        return os.environ[key]
    if key in dotenv:
        return dotenv[key]
    return default


def _parse_int_setting(
    name: str,
    raw: str,
    errors: list[str],
    min_val: int | None = None,
    max_val: int | None = None,
) -> int | None:
    try:
        value = int(raw.strip())
    except ValueError:
        errors.append(f"{name} must be an integer.")
        return None
    if min_val is not None and value < min_val:
        errors.append(f"{name} must be at least {min_val}.")
        return None
    if max_val is not None and value > max_val:
        errors.append(f"{name} must be at most {max_val}.")
        return None
    return value


def _normalize_host_header(raw_host: str) -> str:
    host = raw_host.strip().lower()
    if not host:
        return ""
    if host.startswith("["):
        end_bracket = host.find("]")
        if end_bracket == -1:
            return ""
        name = host[: end_bracket + 1]
        rest = host[end_bracket + 1 :]
        if not rest:
            return name
        if not rest.startswith(":"):
            return ""
        port = rest[1:]
        if not port.isdigit():
            return ""
        return name if port == "443" else f"{name}:{port}"
    if ":" in host:
        name, port = host.rsplit(":", 1)
        if not name or not port.isdigit():
            return ""
        return name if port == "443" else f"{name}:{port}"
    return host


def load_config() -> Config:
    env_path = pathlib.Path(__file__).resolve().parent / ".env"
    dotenv = _read_dotenv(env_path)

    errors: list[str] = []
    warnings: list[str] = []
    secrets: list[str] = []

    # Environment variable -> .env -> the value at the top of this file. A line
    # "PUBLIC_BASE_URL=" (empty) in .env turns the host check off.
    raw_public = (_get_setting("PUBLIC_BASE_URL", dotenv, PUBLIC_BASE_URL) or "").strip()

    public_base_url = raw_public.rstrip("/")
    expected_host = ""
    if public_base_url:
        if not public_base_url.startswith("https://"):
            errors.append("PUBLIC_BASE_URL must start with https:// when set.")
        else:
            parsed_pub = urllib.parse.urlsplit(public_base_url)
            if (
                not parsed_pub.hostname
                or parsed_pub.query
                or parsed_pub.fragment
                or parsed_pub.username is not None
                or parsed_pub.password is not None
            ):
                errors.append("PUBLIC_BASE_URL must be a valid https:// URL without query or fragment.")
            else:
                pub_host = parsed_pub.hostname.lower()
                if ":" in pub_host:
                    pub_host = f"[{pub_host}]"
                try:
                    pub_port = parsed_pub.port
                except ValueError:
                    errors.append("PUBLIC_BASE_URL has an invalid port.")
                    pub_port = None
                if pub_port is not None and pub_port != 443:
                    expected_host = f"{pub_host}:{pub_port}"
                else:
                    expected_host = pub_host

    raw_db_url = (_get_setting("DATABASE_URL", dotenv, "") or "").strip()
    database_url = ""
    if not raw_db_url:
        errors.append("DATABASE_URL is required.")
    else:
        if raw_db_url.startswith("postgresql+psycopg://"):
            database_url = "postgresql://" + raw_db_url[len("postgresql+psycopg://") :]
        else:
            database_url = raw_db_url
        parsed_db = urllib.parse.urlsplit(database_url)
        if parsed_db.scheme not in ("postgresql", "postgres"):
            errors.append("DATABASE_URL must use postgresql:// or postgresql+psycopg://.")
        elif not parsed_db.hostname and "host=" not in parsed_db.query:
            errors.append("DATABASE_URL must include a PostgreSQL host.")
        if parsed_db.password:
            secrets.append(parsed_db.password)
            unquoted_pw = urllib.parse.unquote(parsed_db.password)
            if unquoted_pw and unquoted_pw != parsed_db.password:
                secrets.append(unquoted_pw)

    raw_token = (_get_setting("STREAM_TOKEN", dotenv, "") or "").strip()
    if not raw_token:
        errors.append("STREAM_TOKEN is required.")
    elif len(raw_token) < 32:
        errors.append("STREAM_TOKEN must be at least 32 characters long.")
    if raw_token:
        secrets.append(raw_token)

    bind_host = (_get_setting("BIND_HOST", dotenv, "127.0.0.1") or "").strip()
    if not bind_host:
        errors.append("BIND_HOST must not be empty.")

    raw_port = _get_setting("BIND_PORT", dotenv, "8090") or "8090"
    bind_port = _parse_int_setting("BIND_PORT", raw_port, errors, min_val=1, max_val=65535)

    raw_batch = _get_setting("BATCH_SIZE", dotenv, "300") or "300"
    batch_size = _parse_int_setting("BATCH_SIZE", raw_batch, errors, min_val=1)
    if batch_size is not None and batch_size > 1000:
        warnings.append(f"batch_size_clamped requested={batch_size} effective=1000")
        batch_size = 1000

    raw_max_conc = _get_setting("MAX_CONCURRENT_REQUESTS", dotenv, "8") or "8"
    max_concurrent = _parse_int_setting("MAX_CONCURRENT_REQUESTS", raw_max_conc, errors, min_val=1)

    raw_stmt_timeout = _get_setting("DB_STATEMENT_TIMEOUT_MS", dotenv, "15000") or "15000"
    db_statement_timeout_ms = _parse_int_setting(
        "DB_STATEMENT_TIMEOUT_MS", raw_stmt_timeout, errors, min_val=1
    )

    raw_req_timeout = (_get_setting("REQUEST_TIMEOUT_SECONDS", dotenv, "30") or "30").strip()
    request_timeout_seconds: float | None = None
    try:
        parsed_timeout = float(raw_req_timeout)
        if not math.isfinite(parsed_timeout) or parsed_timeout <= 0:
            errors.append("REQUEST_TIMEOUT_SECONDS must be greater than 0.")
        else:
            request_timeout_seconds = parsed_timeout
    except ValueError:
        errors.append("REQUEST_TIMEOUT_SECONDS must be a number.")

    raw_log_level = (_get_setting("LOG_LEVEL", dotenv, "INFO") or "INFO").strip().upper()
    if raw_log_level not in _LOG_LEVELS:
        errors.append("LOG_LEVEL must be one of DEBUG, INFO, WARNING, ERROR.")
        log_level = _LOG_LEVELS["INFO"]
    else:
        log_level = _LOG_LEVELS[raw_log_level]

    if errors:
        for err in errors:
            print(f"Configuration error: {err}", file=sys.stderr)
        sys.exit(1)

    assert bind_port is not None
    assert batch_size is not None
    assert max_concurrent is not None
    assert db_statement_timeout_ms is not None
    assert request_timeout_seconds is not None

    if public_base_url:
        ping_url = f"{public_base_url}/leads/next"
    else:
        host_for_url = f"[{bind_host}]" if ":" in bind_host else bind_host
        ping_url = f"http://{host_for_url}:{bind_port}/leads/next"

    unique_secrets = tuple(sorted(set(secrets), key=len, reverse=True))

    return Config(
        public_base_url=public_base_url,
        expected_host=expected_host,
        ping_url=ping_url,
        database_url=database_url,
        stream_token_bytes=raw_token.encode("utf-8"),
        bind_host=bind_host,
        bind_port=bind_port,
        batch_size=batch_size,
        max_concurrent_requests=max_concurrent,
        db_statement_timeout_ms=db_statement_timeout_ms,
        request_timeout_seconds=request_timeout_seconds,
        log_level=log_level,
        secret_values=unique_secrets,
        startup_warnings=tuple(warnings),
    )


def _format_log_value(val: typing.Any) -> str:
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, int):
        return str(val)
    text = str(val).replace("\r", " ").replace("\n", " ").replace("\t", " ")
    if not text or any(ch in text for ch in (' ', '=', '"', "'", "\\")):
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text


class StructuredLogger:
    def __init__(self, level: int, secret_values: tuple[str, ...]) -> None:
        self._level = level
        self._secrets = secret_values
        self._lock = threading.Lock()

    def sanitize_error(self, exc: BaseException) -> str:
        raw = str(exc)
        first_line = raw.splitlines()[0] if raw else type(exc).__name__
        text = " ".join(first_line.split())
        text = re.sub(r"postgres(?:ql)?://[^\s@]+@", "postgresql://***@", text)
        text = re.sub(r"password='[^']*'", "password=***", text, flags=re.IGNORECASE)
        text = re.sub(r"password=\S+", "password=***", text, flags=re.IGNORECASE)
        for secret in self._secrets:
            if secret and len(secret) >= 8:
                text = text.replace(secret, "***")
        if len(text) > 200:
            text = text[:197] + "..."
        return text or type(exc).__name__

    def log(self, level: int, event: str, **fields: typing.Any) -> None:
        if level < self._level:
            return
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        label = _LOG_LABELS.get(level, "INFO ")
        parts = [f"{ts} {label} {event}"]
        for key, value in fields.items():
            if value is None:
                continue
            parts.append(f"{key}={_format_log_value(value)}")
        line = " ".join(parts)
        with self._lock:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()

    def debug(self, event: str, **fields: typing.Any) -> None:
        self.log(10, event, **fields)

    def info(self, event: str, **fields: typing.Any) -> None:
        self.log(20, event, **fields)

    def warn(self, event: str, **fields: typing.Any) -> None:
        self.log(30, event, **fields)

    def error(self, event: str, **fields: typing.Any) -> None:
        self.log(40, event, **fields)


def _clean_str(val: typing.Any) -> str | None:
    if val is None or isinstance(val, (bool, list, dict)):
        return None
    text = str(val).strip()
    if text.lower() in _EMPTY_STRINGS:
        return None
    return text


def _first_str(*candidates: typing.Any) -> str | None:
    for item in candidates:
        cleaned = _clean_str(item)
        if cleaned is not None:
            return cleaned
    return None


def _split_candidates(val: typing.Any) -> list[str]:
    if val is None:
        return []
    raw_items: list[typing.Any] = val if isinstance(val, list) else [val]
    out: list[str] = []
    for item in raw_items:
        if item is None or isinstance(item, (bool, dict, list)):
            continue
        for token in re.split(r"[,;]", str(item)):
            cleaned = _clean_str(token)
            if cleaned is not None:
                out.append(cleaned)
    return out


def _format_utc_iso(dt: datetime.datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    else:
        dt = dt.astimezone(datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _as_dict(raw: typing.Any) -> dict[str, typing.Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _to_snake_case(key: str) -> str:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key))
    text = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", text)
    text = re.sub(r"[^a-zA-Z0-9]+", "_", text)
    return text.strip("_").lower()


def _clean_extra_value(val: typing.Any) -> typing.Any:
    if val is None:
        return _MISSING
    if isinstance(val, bool):
        return val
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return val if math.isfinite(val) else _MISSING
    if isinstance(val, str):
        if val.strip().lower() in _EMPTY_STRINGS:
            return _MISSING
        return val
    if isinstance(val, dict):
        cleaned_dict: dict[str, typing.Any] = {}
        for key, item in val.items():
            cleaned_item = _clean_extra_value(item)
            if cleaned_item is not _MISSING:
                cleaned_dict[str(key)] = cleaned_item
        return cleaned_dict if cleaned_dict else _MISSING
    if isinstance(val, list):
        cleaned_list: list[typing.Any] = []
        for item in val:
            cleaned_item = _clean_extra_value(item)
            if cleaned_item is not _MISSING:
                cleaned_list.append(cleaned_item)
        return cleaned_list if cleaned_list else _MISSING
    return val


def _merge_extra_sources(*sources: typing.Any) -> dict[str, typing.Any]:
    merged: dict[str, typing.Any] = {}
    for raw_src in sources:
        src = _as_dict(raw_src)
        if not src:
            continue
        unpacked_keys: set[str] = set()
        for key, raw_nested in src.items():
            if str(key).lower() in _WRAPPER_KEYS:
                nested = _as_dict(raw_nested)
                if nested:
                    unpacked_keys.add(str(key))
                    for sub_k, sub_v in nested.items():
                        if _clean_extra_value(sub_v) is not _MISSING or sub_k not in merged:
                            merged[str(sub_k)] = sub_v
        for key, val in src.items():
            if str(key) in unpacked_keys:
                continue
            if _clean_extra_value(val) is not _MISSING or key not in merged:
                merged[str(key)] = val
    return merged


def _flatten_extra(
    extra_dict: dict[str, typing.Any],
    prefix: str = "",
    depth: int = 0,
) -> list[tuple[str, typing.Any]]:
    items: list[tuple[str, typing.Any]] = []
    for raw_key, raw_val in extra_dict.items():
        key_str = str(raw_key)
        snake_key = _to_snake_case(key_str)
        if not snake_key:
            continue
        if depth == 0:
            lowered_key = key_str.lower()
            if (
                lowered_key in _EXTRA_TOP_LEVEL_KEYS
                or snake_key in _EXTRA_TOP_LEVEL_KEYS
                or lowered_key in _EXTRA_INTERNAL_KEYS
                or snake_key in _EXTRA_INTERNAL_KEYS
            ):
                continue
        cleaned = _clean_extra_value(raw_val)
        if cleaned is _MISSING:
            continue
        full_key = f"{prefix}_{snake_key}" if prefix else snake_key
        if isinstance(cleaned, dict) and depth < 8:
            items.extend(_flatten_extra(cleaned, prefix=full_key, depth=depth + 1))
        else:
            items.append((full_key, cleaned))
    return items


def _set_if_diff(
    payload: dict[str, typing.Any],
    key: str,
    candidates: tuple[typing.Any, ...],
    *existing_values: typing.Any,
) -> None:
    existing_norm = {
        val.strip().casefold()
        for val in existing_values
        if isinstance(val, str) and val.strip()
    }
    for candidate in candidates:
        cleaned = _clean_str(candidate)
        if cleaned is not None and cleaned.casefold() not in existing_norm:
            payload[key] = cleaned
            return


def _build_phones(
    meta: dict[str, typing.Any],
    contact_phones: list[str],
    org_phones: list[str],
) -> list[str]:
    result: list[str] = []
    seen_keys: set[str] = set()
    for candidate in (
        *_split_candidates(meta.get("phone")),
        *contact_phones,
        *org_phones,
    ):
        digits = "".join(ch for ch in candidate if ch.isdigit())
        dedup_key = digits if digits else f"raw:{candidate.casefold()}"
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)
        result.append(candidate)
    return result


def _build_emails(
    meta: dict[str, typing.Any],
    contact_emails: list[str],
    org_emails: list[str],
    invalid_emails: set[str],
) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for candidate in (
        *_split_candidates(meta.get("email")),
        *contact_emails,
        *org_emails,
    ):
        lowered = candidate.lower()
        if lowered in invalid_emails or lowered in seen:
            continue
        seen.add(lowered)
        result.append(candidate)
    return result


def _build_lead_payload(
    row: tuple[typing.Any, ...],
    phones_by_contact: dict[str, list[str]],
    phones_by_org: dict[str, list[str]],
    emails_by_contact: dict[str, list[str]],
    emails_by_org: dict[str, list[str]],
    invalid_by_contact: dict[str, set[str]],
    invalid_by_org: dict[str, set[str]],
    locations_by_org: dict[str, list[tuple[typing.Any, ...]]],
    *,
    phones_by_org_direct: dict[str, list[str]] | None = None,
    emails_by_org_direct: dict[str, list[str]] | None = None,
    invalid_by_org_direct: dict[str, set[str]] | None = None,
) -> dict[str, typing.Any]:
    (
        lead_id,
        org_id,
        contact_id,
        record_kind,
        lead_title,
        lead_category,
        lead_city,
        lead_us_state,
        source_code,
        source_url,
        due_at,
        raw_meta,
        content_version,
        created_at,
        updated_at,
        lead_notes,
        lead_status,
        lead_last_activity,
        lead_next_follow_up,
        lead_external_id,
        lead_first_seen_at,
        lead_last_seen_at,
        org_name,
        org_website,
        org_industry,
        org_domain,
        org_company_size,
        org_linkedin_url,
        org_raw_data,
        contact_full_name,
        contact_title,
        contact_first_name,
        contact_last_name,
        contact_department,
        contact_linkedin_url,
        contact_raw_data,
        was_sent,
    ) = row

    meta = _as_dict(raw_meta)

    payload: dict[str, typing.Any] = {
        "lead_id": str(lead_id),
        "content_version": int(content_version),
        "event": "updated" if was_sent else "new",
    }

    cleaned_record_kind = _clean_str(record_kind)
    if cleaned_record_kind is not None:
        payload["record_kind"] = cleaned_record_kind

    company = _first_str(
        org_name,
        meta.get("organization_name"),
        meta.get("company_name"),
    )
    if company is not None:
        payload["company"] = company

    contact_name = _first_str(contact_full_name, meta.get("contact_name"))
    if contact_name is not None and not (
        company is not None and contact_name.casefold() == company.casefold()
    ):
        payload["contact_name"] = contact_name

    resolved_contact_title = _first_str(contact_title, meta.get("contact_title"))
    if resolved_contact_title is not None:
        payload["contact_title"] = resolved_contact_title

    cleaned_title = _clean_str(lead_title)
    if cleaned_title is not None and not (
        company is not None and cleaned_title == company
    ):
        payload["title"] = cleaned_title

    direct_org_phones = (
        phones_by_org_direct.get(org_id, [])
        if phones_by_org_direct is not None
        else phones_by_org.get(org_id, [])
    ) if org_id is not None else []
    if contact_id is not None:
        phones = _build_phones(
            meta,
            phones_by_contact.get(contact_id, []),
            direct_org_phones,
        )
        if not phones and org_id is not None:
            phones = _build_phones(meta, [], phones_by_org.get(org_id, []))
    else:
        phones = _build_phones(
            meta,
            [],
            phones_by_org.get(org_id, []) if org_id is not None else [],
        )
    if phones:
        payload["phone"] = phones[0]
        payload["phones"] = phones

    direct_org_emails = (
        emails_by_org_direct.get(org_id, [])
        if emails_by_org_direct is not None
        else emails_by_org.get(org_id, [])
    ) if org_id is not None else []
    direct_org_invalid = (
        invalid_by_org_direct.get(org_id, set())
        if invalid_by_org_direct is not None
        else invalid_by_org.get(org_id, set())
    ) if org_id is not None else set()

    invalid_emails: set[str] = set()
    if contact_id is not None and contact_id in invalid_by_contact:
        invalid_emails.update(invalid_by_contact[contact_id])
    invalid_emails.update(direct_org_invalid)

    if contact_id is not None:
        emails = _build_emails(
            meta,
            emails_by_contact.get(contact_id, []),
            direct_org_emails,
            invalid_emails,
        )
        if not emails and org_id is not None:
            fallback_invalid = set(invalid_emails) | invalid_by_org.get(org_id, set())
            emails = _build_emails(meta, [], emails_by_org.get(org_id, []), fallback_invalid)
    else:
        all_invalid = invalid_by_org.get(org_id, set()) if org_id is not None else set()
        emails = _build_emails(
            meta,
            [],
            emails_by_org.get(org_id, []) if org_id is not None else [],
            all_invalid,
        )
    if emails:
        payload["email"] = emails[0]
        payload["emails"] = emails

    website = _first_str(meta.get("website"), org_website)
    if website is not None:
        payload["website"] = website

    category = _first_str(
        lead_category,
        org_industry,
        meta.get("category"),
        meta.get("professional_category"),
    )
    if category is not None:
        payload["category"] = category

    locs = locations_by_org.get(org_id, []) if org_id is not None else []
    loc = locs[0] if locs else None
    if loc is not None:
        _, addr1, addr2, loc_city, loc_state, loc_postal, loc_country, loc_norm, loc_raw, _ = loc
        clean_a1 = _clean_str(addr1)
        clean_a2 = _clean_str(addr2)
        if clean_a1 and clean_a2:
            loc_street = f"{clean_a1}, {clean_a2}"
        else:
            loc_street = clean_a1 or clean_a2
    else:
        loc_street = None
        loc_city = None
        loc_state = None
        loc_postal = None
        loc_country = None
        loc_norm = None
        loc_raw = None

    address = _first_str(meta.get("address"), loc_street, loc_norm, loc_raw)
    if address is not None:
        payload["address"] = address

    city = _first_str(lead_city, loc_city, meta.get("city"))
    if city is not None:
        payload["city"] = city

    state = _first_str(lead_us_state, loc_state, meta.get("state"))
    if state is not None:
        payload["state"] = state

    postal_code = _first_str(loc_postal, meta.get("postal_code"), meta.get("zip"))
    if postal_code is not None:
        payload["postal_code"] = postal_code

    country = _first_str(loc_country, meta.get("country"), meta.get("raw_country"))
    if country is not None:
        payload["country"] = country

    if city and state:
        payload["location"] = f"{city}, {state}"
    elif city:
        payload["location"] = city
    elif state:
        payload["location"] = state

    source = _clean_str(source_code)
    if source is not None:
        payload["source"] = source

    resolved_source_url = _first_str(source_url, meta.get("source_url"))
    if resolved_source_url is not None:
        payload["source_url"] = resolved_source_url

    formatted_due_at = _format_utc_iso(due_at)
    if formatted_due_at is not None:
        payload["due_at"] = formatted_due_at

    formatted_created_at = _format_utc_iso(created_at)
    if formatted_created_at is not None:
        payload["created_at"] = formatted_created_at

    formatted_updated_at = _format_utc_iso(updated_at)
    if formatted_updated_at is not None:
        payload["updated_at"] = formatted_updated_at

    cleaned_notes = _clean_str(lead_notes)
    if cleaned_notes is not None:
        payload["notes"] = cleaned_notes

    cleaned_lead_status = _clean_str(lead_status)
    if cleaned_lead_status is not None:
        payload["lead_status"] = cleaned_lead_status

    cleaned_external_id = _clean_str(lead_external_id)
    if cleaned_external_id is not None:
        payload["external_id"] = cleaned_external_id

    cleaned_last_activity = _clean_str(lead_last_activity)
    if cleaned_last_activity is not None:
        payload["last_activity"] = cleaned_last_activity

    formatted_next_follow_up = _format_utc_iso(lead_next_follow_up)
    if formatted_next_follow_up is not None:
        payload["next_follow_up"] = formatted_next_follow_up

    formatted_first_seen = _format_utc_iso(lead_first_seen_at)
    if formatted_first_seen is not None:
        payload["first_seen_at"] = formatted_first_seen

    formatted_last_seen = _format_utc_iso(lead_last_seen_at)
    if formatted_last_seen is not None:
        payload["last_seen_at"] = formatted_last_seen

    cleaned_domain = _clean_str(org_domain)
    if cleaned_domain is not None:
        payload["domain"] = cleaned_domain

    cleaned_industry = _clean_str(org_industry)
    if cleaned_industry is not None and not (
        category is not None and cleaned_industry.casefold() == category.casefold()
    ):
        payload["industry"] = cleaned_industry

    cleaned_company_size = _clean_str(org_company_size)
    if cleaned_company_size is not None:
        payload["company_size"] = cleaned_company_size

    cleaned_org_linkedin = _clean_str(org_linkedin_url)
    if cleaned_org_linkedin is not None:
        payload["organization_linkedin_url"] = cleaned_org_linkedin

    cleaned_first_name = _clean_str(contact_first_name)
    if cleaned_first_name is not None:
        payload["contact_first_name"] = cleaned_first_name

    cleaned_last_name = _clean_str(contact_last_name)
    if cleaned_last_name is not None:
        payload["contact_last_name"] = cleaned_last_name

    cleaned_department = _clean_str(contact_department)
    if cleaned_department is not None:
        payload["contact_department"] = cleaned_department

    cleaned_contact_linkedin = _clean_str(contact_linkedin_url)
    if cleaned_contact_linkedin is not None:
        payload["contact_linkedin_url"] = cleaned_contact_linkedin

    if len(locs) > 1:
        other_locs: list[dict[str, typing.Any]] = []
        for extra_loc in locs[1:]:
            _, e_a1, e_a2, e_city, e_state, e_postal, e_country, e_norm, e_raw, e_hq = extra_loc
            ea1 = _clean_str(e_a1)
            ea2 = _clean_str(e_a2)
            e_street = f"{ea1}, {ea2}" if (ea1 and ea2) else (ea1 or ea2)
            e_addr = _first_str(e_street, e_norm, e_raw)
            e_c = _clean_str(e_city)
            e_s = _clean_str(e_state)
            e_p = _clean_str(e_postal)
            e_co = _clean_str(e_country)
            if not any((e_addr, e_c, e_s, e_p, e_co)):
                continue
            loc_obj: dict[str, typing.Any] = {}
            if e_addr is not None:
                loc_obj["address"] = e_addr
            if e_c is not None:
                loc_obj["city"] = e_c
            if e_s is not None:
                loc_obj["state"] = e_s
            if e_p is not None:
                loc_obj["postal_code"] = e_p
            if e_co is not None:
                loc_obj["country"] = e_co
            loc_obj["is_headquarters"] = bool(e_hq)
            other_locs.append(loc_obj)
        if other_locs:
            payload["other_locations"] = other_locs

    _set_if_diff(payload, "other_website", (org_website, meta.get("website")), website)
    _set_if_diff(
        payload,
        "alternate_company_name",
        (meta.get("organization_name"), meta.get("company_name")),
        company,
    )
    _set_if_diff(
        payload,
        "alternate_contact_name",
        (meta.get("contact_name"),),
        contact_name,
        company,
    )
    _set_if_diff(
        payload,
        "alternate_contact_title",
        (meta.get("contact_title"),),
        resolved_contact_title,
    )
    _set_if_diff(
        payload,
        "alternate_category",
        (meta.get("category"),),
        category,
        cleaned_industry,
    )
    _set_if_diff(
        payload,
        "professional_category",
        (meta.get("professional_category"),),
        category,
        cleaned_industry,
        payload.get("alternate_category"),
    )
    _set_if_diff(payload, "alternate_address", (loc_street, loc_norm, loc_raw), address)
    _set_if_diff(payload, "alternate_city", (loc_city, meta.get("city")), city)
    _set_if_diff(payload, "alternate_state", (loc_state, meta.get("state")), state)
    _set_if_diff(
        payload,
        "alternate_postal_code",
        (meta.get("postal_code"), meta.get("zip")),
        postal_code,
    )
    _set_if_diff(
        payload,
        "alternate_country",
        (meta.get("country"), meta.get("raw_country")),
        country,
    )
    _set_if_diff(
        payload,
        "alternate_source_url",
        (meta.get("source_url"),),
        resolved_source_url,
    )

    merged_extra = _merge_extra_sources(org_raw_data, contact_raw_data, meta)
    for extra_key, extra_val in _flatten_extra(merged_extra):
        if extra_key not in payload:
            payload[extra_key] = extra_val
        else:
            existing_val = payload[extra_key]
            if (
                not isinstance(existing_val, (dict, list, bool))
                and not isinstance(extra_val, (dict, list, bool))
                and str(existing_val).strip().casefold() == str(extra_val).strip().casefold()
            ):
                continue
            alt_key = f"extra_{extra_key}"
            if alt_key not in payload:
                payload[alt_key] = extra_val

    return payload


class PgStore:
    """Single-connection PostgreSQL store for lead streaming."""

    def __init__(self, config: Config, logger: StructuredLogger) -> None:
        self._config = config
        self._logger = logger
        self._conn_lock = threading.Lock()
        self.batch_lock = threading.Lock()
        self._conn: psycopg.Connection | None = None
        self._schema_ready = False
        self._retry_thread: threading.Thread | None = None

    def _close_locked(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def _safe_rollback_locked(self, conn: psycopg.Connection | None) -> None:
        if conn is None or conn.closed:
            return
        try:
            conn.rollback()
        except Exception:
            pass

    def _check_scraper_schema_locked(self, conn: psycopg.Connection) -> None:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_name, column_name
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = ANY(%s);
                """,
                (list(REQUIRED_COLUMNS),),
            )
            present = cur.fetchall()
        conn.rollback()
        missing = missing_columns(present)
        if missing:
            shown = ", ".join(missing[:8]) + (f" (+{len(missing) - 8} more)" if len(missing) > 8 else "")
            raise SchemaMismatchError(
                f"scraper tables in this database do not match: missing {shown}. "
                "Check DATABASE_URL points at the scraper's database and its migrations are applied."
            )

    def _ensure_schema_locked(self, conn: psycopg.Connection) -> None:
        self._check_scraper_schema_locked(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('lead_stream.sent_leads') IS NOT NULL;")
            exists_row = cur.fetchone()
        conn.rollback()
        if exists_row and exists_row[0]:
            self._schema_ready = True
            return

        with conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS lead_stream;")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS lead_stream.sent_leads (
                    lead_id      varchar(100) PRIMARY KEY,
                    sent_version integer      NOT NULL,
                    sent_at      timestamptz  NOT NULL DEFAULT now()
                );
                """
            )
        conn.commit()
        self._schema_ready = True

    def _connect_locked(self) -> psycopg.Connection:
        self._close_locked()
        conn = psycopg.connect(
            self._config.database_url,
            autocommit=False,
            connect_timeout=3,
            application_name="lead_streamer",
            options=f"-c statement_timeout={self._config.db_statement_timeout_ms}",
        )
        try:
            if not self._schema_ready:
                self._ensure_schema_locked(conn)
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            raise
        self._conn = conn
        self._logger.info("db_connected", application_name="lead_streamer")
        return conn

    def _ensure_connected_locked(self) -> psycopg.Connection:
        if self._conn is not None and not self._conn.closed:
            if not self._schema_ready:
                self._ensure_schema_locked(self._conn)
            return self._conn
        return self._connect_locked()

    def connect_on_startup(self, shutdown_event: threading.Event) -> None:
        with self._conn_lock:
            try:
                self._connect_locked()
                return
            except Exception as exc:
                self._close_locked()
                self._logger.error(
                    "schema_mismatch" if isinstance(exc, SchemaMismatchError) else "db_unavailable",
                    error=self._logger.sanitize_error(exc),
                    action="retry_startup",
                )

        def _retry_loop() -> None:
            while not shutdown_event.wait(5.0):
                with self._conn_lock:
                    if self._conn is not None and not self._conn.closed and self._schema_ready:
                        return
                    try:
                        self._connect_locked()
                        return
                    except Exception as exc:
                        self._close_locked()
                        self._logger.error(
                            "schema_mismatch" if isinstance(exc, SchemaMismatchError) else "db_unavailable",
                            error=self._logger.sanitize_error(exc),
                            action="retry_startup",
                        )

        self._retry_thread = threading.Thread(
            target=_retry_loop,
            name="db-startup-retry",
            daemon=True,
        )
        self._retry_thread.start()

    def close(self) -> None:
        with self._conn_lock:
            self._close_locked()

    def check_health(self) -> str:
        acquired = self._conn_lock.acquire(timeout=2.0)
        if not acquired:
            return "busy"
        try:
            try:
                conn = self._ensure_connected_locked()
            except Exception as exc:
                self._close_locked()
                mismatch = isinstance(exc, SchemaMismatchError)
                self._logger.error(
                    "schema_mismatch" if mismatch else "db_unavailable",
                    error=self._logger.sanitize_error(exc),
                    action="reconnect_next_request",
                )
                return "schema_mismatch" if mismatch else "unavailable"

            for attempt in range(2):
                try:
                    with conn.cursor() as cur:
                        cur.execute("SELECT 1;")
                        cur.fetchone()
                    conn.rollback()
                    return "ok"
                except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
                    self._safe_rollback_locked(conn)
                    is_timeout = isinstance(exc, pg_errors.QueryCanceled)
                    if is_timeout or attempt == 1:
                        if not is_timeout:
                            self._close_locked()
                        self._logger.error(
                            "db_unavailable",
                            error=self._logger.sanitize_error(exc),
                            action="reconnect_next_request",
                        )
                        return "unavailable"
                    try:
                        conn = self._connect_locked()
                    except Exception as recon_exc:
                        self._close_locked()
                        self._logger.error(
                            "db_unavailable",
                            error=self._logger.sanitize_error(recon_exc),
                            action="reconnect_next_request",
                        )
                        return "unavailable"
                except psycopg.Error as exc:
                    self._safe_rollback_locked(conn)
                    self._logger.error(
                        "db_unavailable",
                        error=self._logger.sanitize_error(exc),
                        action="health_error",
                    )
                    return "unavailable"
        finally:
            self._conn_lock.release()
        return "unavailable"

    def fetch_stats(self) -> dict[str, typing.Any]:
        with self._conn_lock:
            try:
                conn = self._ensure_connected_locked()
            except Exception as exc:
                self._close_locked()
                self._logger.error(
                    "schema_mismatch" if isinstance(exc, SchemaMismatchError) else "db_unavailable",
                    error=self._logger.sanitize_error(exc),
                    action="reconnect_next_request",
                )
                raise _unavailable(exc) from exc

            for attempt in range(2):
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            f"""
                            SELECT
                                count(*) AS eligible,
                                count(*) FILTER (
                                    WHERE s.lead_id IS NOT NULL AND s.sent_version >= l.content_version
                                ) AS sent_current,
                                count(*) FILTER (
                                    WHERE s.lead_id IS NOT NULL AND s.sent_version < l.content_version
                                ) AS updated_since_sent,
                                count(*) FILTER (
                                    WHERE s.lead_id IS NULL
                                ) AS never_sent
                            FROM public.leads l
                            LEFT JOIN lead_stream.sent_leads s ON s.lead_id = l.id
                            WHERE {_ELIGIBLE_SQL};
                            """
                        )
                        counts = cur.fetchone()
                        cur.execute("SELECT max(sent_at) FROM lead_stream.sent_leads;")
                        last_sent_row = cur.fetchone()
                    conn.rollback()
                    assert counts is not None
                    last_sent_dt = last_sent_row[0] if last_sent_row else None
                    return {
                        "status": "ok",
                        "eligible": int(counts[0]),
                        "sent_current": int(counts[1]),
                        "updated_since_sent": int(counts[2]),
                        "never_sent": int(counts[3]),
                        "last_sent_at": _format_utc_iso(last_sent_dt),
                    }
                except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
                    self._safe_rollback_locked(conn)
                    is_timeout = isinstance(exc, pg_errors.QueryCanceled)
                    if is_timeout or attempt == 1:
                        if not is_timeout:
                            self._close_locked()
                        self._logger.error(
                            "db_unavailable",
                            error=self._logger.sanitize_error(exc),
                            action="reconnect_next_request",
                        )
                        raise _unavailable(exc) from exc
                    try:
                        conn = self._connect_locked()
                    except Exception as recon_exc:
                        self._close_locked()
                        self._logger.error(
                            "db_unavailable",
                            error=self._logger.sanitize_error(recon_exc),
                            action="reconnect_next_request",
                        )
                        raise _unavailable(recon_exc) from recon_exc
                except psycopg.Error as exc:
                    self._safe_rollback_locked(conn)
                    self._logger.error(
                        "db_unavailable",
                        error=self._logger.sanitize_error(exc),
                        action="stats_query_error",
                    )
                    raise _unavailable(exc) from exc
        raise DbUnavailableError()

    def _begin_batch_tx_locked(self) -> psycopg.Connection:
        try:
            conn = self._ensure_connected_locked()
        except Exception as exc:
            self._close_locked()
            self._logger.error(
                "schema_mismatch" if isinstance(exc, SchemaMismatchError) else "db_unavailable",
                error=self._logger.sanitize_error(exc),
                action="reconnect_next_request",
            )
            raise _unavailable(exc) from exc

        for attempt in range(2):
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_xact_lock(hashtext('lead_stream:next_batch'));")
                return conn
            except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
                self._safe_rollback_locked(conn)
                is_timeout = isinstance(exc, pg_errors.QueryCanceled)
                if is_timeout or attempt == 1:
                    if not is_timeout:
                        self._close_locked()
                    self._logger.error(
                        "db_unavailable",
                        error=self._logger.sanitize_error(exc),
                        action="reconnect_next_request",
                    )
                    raise _unavailable(exc) from exc
                try:
                    conn = self._connect_locked()
                except Exception as recon_exc:
                    self._close_locked()
                    self._logger.error(
                        "db_unavailable",
                        error=self._logger.sanitize_error(recon_exc),
                        action="reconnect_next_request",
                    )
                    raise _unavailable(recon_exc) from recon_exc
            except psycopg.Error as exc:
                self._safe_rollback_locked(conn)
                self._logger.error(
                    "db_unavailable",
                    error=self._logger.sanitize_error(exc),
                    action="advisory_lock_error",
                )
                raise _unavailable(exc) from exc
        raise DbUnavailableError()

    def fetch_next_batch(self, limit: int | None = None) -> dict[str, typing.Any]:
        effective_limit = (
            self._config.batch_size
            if limit is None
            else max(1, min(1000, int(limit)))
        )
        with self._conn_lock:
            conn = self._begin_batch_tx_locked()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT l.id
                        FROM public.leads l
                        LEFT JOIN lead_stream.sent_leads s ON s.lead_id = l.id
                        WHERE (s.lead_id IS NULL OR s.sent_version < l.content_version)
                          AND {_ELIGIBLE_SQL}
                        ORDER BY l.created_at ASC, l.id ASC
                        LIMIT %(batch_size)s;
                        """,
                        {"batch_size": effective_limit},
                    )
                    due_ids = [str(row[0]) for row in cur.fetchall()]

                    if not due_ids:
                        conn.rollback()
                        served_at = _format_utc_iso(datetime.datetime.now(datetime.timezone.utc))
                        return {
                            "status": "no_new_leads",
                            "message": "No new Leads available",
                            "count": 0,
                            "served_at": served_at,
                            "leads": [],
                        }

                    cur.execute(
                        """
                        SELECT
                            l.id,
                            l.organization_id,
                            l.contact_id,
                            l.record_kind,
                            l.title,
                            l.category,
                            l.city,
                            l.us_state,
                            l.source_code,
                            l.source_url,
                            l.due_at,
                            l.lead_metadata,
                            l.content_version,
                            l.created_at,
                            l.updated_at,
                            l.notes,
                            l.status,
                            l.last_activity,
                            l.next_follow_up,
                            l.external_id,
                            l.first_seen_at,
                            l.last_seen_at,
                            o.name,
                            o.website,
                            o.industry,
                            o.domain,
                            o.company_size,
                            o.linkedin_url,
                            o.raw_data,
                            c.full_name,
                            c.title,
                            c.first_name,
                            c.last_name,
                            c.department,
                            c.linkedin_url,
                            c.raw_data,
                            (s.lead_id IS NOT NULL) AS was_sent
                        FROM public.leads l
                        LEFT JOIN public.organizations o ON o.id = l.organization_id
                        LEFT JOIN public.contacts c ON c.id = l.contact_id
                        LEFT JOIN lead_stream.sent_leads s ON s.lead_id = l.id
                        WHERE l.id = ANY(%(ids)s::varchar[])
                        ORDER BY l.created_at ASC, l.id ASC;
                        """,
                        {"ids": due_ids},
                    )
                    lead_rows = cur.fetchall()

                    if not lead_rows:
                        conn.rollback()
                        served_at = _format_utc_iso(datetime.datetime.now(datetime.timezone.utc))
                        return {
                            "status": "no_new_leads",
                            "message": "No new Leads available",
                            "count": 0,
                            "served_at": served_at,
                            "leads": [],
                        }

                    org_id_set = {row[1] for row in lead_rows if row[1] is not None}
                    contact_id_set = {row[2] for row in lead_rows if row[2] is not None}
                    org_ids = list(org_id_set)
                    contact_ids = list(contact_id_set)

                    phones_by_contact: dict[str, list[str]] = {}
                    phones_by_org: dict[str, list[str]] = {}
                    phones_by_org_direct: dict[str, list[str]] = {}
                    if org_ids or contact_ids:
                        if org_ids and contact_ids:
                            phone_where = (
                                "organization_id = ANY(%(org_ids)s::varchar[]) "
                                "OR contact_id = ANY(%(contact_ids)s::varchar[])"
                            )
                        elif org_ids:
                            phone_where = "organization_id = ANY(%(org_ids)s::varchar[])"
                        else:
                            phone_where = "contact_id = ANY(%(contact_ids)s::varchar[])"
                        cur.execute(
                            f"""
                            SELECT organization_id, contact_id, phone_raw, normalized_phone
                            FROM public.phones
                            WHERE {phone_where}
                            ORDER BY is_primary DESC, created_at ASC;
                            """,
                            {"org_ids": org_ids, "contact_ids": contact_ids},
                        )
                        for p_org_id, p_contact_id, phone_raw, normalized_phone in cur.fetchall():
                            phone_val = _first_str(phone_raw, normalized_phone)
                            if phone_val is None:
                                continue
                            if p_contact_id is not None and p_contact_id in contact_id_set:
                                phones_by_contact.setdefault(p_contact_id, []).append(phone_val)
                            if p_org_id is not None and p_org_id in org_id_set:
                                phones_by_org.setdefault(p_org_id, []).append(phone_val)
                                if p_contact_id is None:
                                    phones_by_org_direct.setdefault(p_org_id, []).append(phone_val)

                    emails_by_contact: dict[str, list[str]] = {}
                    emails_by_org: dict[str, list[str]] = {}
                    emails_by_org_direct: dict[str, list[str]] = {}
                    invalid_by_contact: dict[str, set[str]] = {}
                    invalid_by_org: dict[str, set[str]] = {}
                    invalid_by_org_direct: dict[str, set[str]] = {}
                    if org_ids or contact_ids:
                        if org_ids and contact_ids:
                            email_where = (
                                "organization_id = ANY(%(org_ids)s::varchar[]) "
                                "OR contact_id = ANY(%(contact_ids)s::varchar[])"
                            )
                        elif org_ids:
                            email_where = "organization_id = ANY(%(org_ids)s::varchar[])"
                        else:
                            email_where = "contact_id = ANY(%(contact_ids)s::varchar[])"
                        cur.execute(
                            f"""
                            SELECT organization_id, contact_id, email, email_status
                            FROM public.emails
                            WHERE {email_where}
                            ORDER BY
                                CASE
                                    WHEN email_status = 'VALID' THEN 0
                                    WHEN email_status = 'RISKY' THEN 1
                                    WHEN email_status = 'UNVERIFIED' THEN 2
                                    WHEN email_status IS NULL THEN 3
                                    ELSE 4
                                END ASC,
                                is_primary DESC,
                                created_at ASC;
                            """,
                            {"org_ids": org_ids, "contact_ids": contact_ids},
                        )
                        for e_org_id, e_contact_id, email_addr, email_status in cur.fetchall():
                            cleaned_email = _clean_str(email_addr)
                            if cleaned_email is None:
                                continue
                            if email_status == "INVALID":
                                lowered = cleaned_email.lower()
                                if e_contact_id is not None and e_contact_id in contact_id_set:
                                    invalid_by_contact.setdefault(e_contact_id, set()).add(lowered)
                                if e_org_id is not None and e_org_id in org_id_set:
                                    invalid_by_org.setdefault(e_org_id, set()).add(lowered)
                                    if e_contact_id is None:
                                        invalid_by_org_direct.setdefault(e_org_id, set()).add(lowered)
                            else:
                                if e_contact_id is not None and e_contact_id in contact_id_set:
                                    emails_by_contact.setdefault(e_contact_id, []).append(cleaned_email)
                                if e_org_id is not None and e_org_id in org_id_set:
                                    emails_by_org.setdefault(e_org_id, []).append(cleaned_email)
                                    if e_contact_id is None:
                                        emails_by_org_direct.setdefault(e_org_id, []).append(cleaned_email)

                    locations_by_org: dict[str, list[tuple[typing.Any, ...]]] = {}
                    if org_ids:
                        cur.execute(
                            """
                            SELECT
                                organization_id,
                                address_line1,
                                address_line2,
                                city,
                                state,
                                postal_code,
                                country,
                                normalized_location,
                                raw_location,
                                is_headquarters
                            FROM public.locations
                            WHERE organization_id = ANY(%(org_ids)s::varchar[])
                            ORDER BY organization_id, is_headquarters DESC, created_at ASC;
                            """,
                            {"org_ids": org_ids},
                        )
                        for loc_row in cur.fetchall():
                            locations_by_org.setdefault(loc_row[0], []).append(loc_row)

                    leads: list[dict[str, typing.Any]] = []
                    sent_ids: list[str] = []
                    sent_versions: list[int] = []
                    new_count = 0
                    updated_count = 0

                    for row in lead_rows:
                        payload = _build_lead_payload(
                            row,
                            phones_by_contact,
                            phones_by_org,
                            emails_by_contact,
                            emails_by_org,
                            invalid_by_contact,
                            invalid_by_org,
                            locations_by_org,
                            phones_by_org_direct=phones_by_org_direct,
                            emails_by_org_direct=emails_by_org_direct,
                            invalid_by_org_direct=invalid_by_org_direct,
                        )
                        leads.append(payload)
                        sent_ids.append(payload["lead_id"])
                        sent_versions.append(payload["content_version"])
                        if payload["event"] == "updated":
                            updated_count += 1
                        else:
                            new_count += 1

                    served_dt = datetime.datetime.now(datetime.timezone.utc)
                    cur.execute(
                        """
                        INSERT INTO lead_stream.sent_leads (lead_id, sent_version, sent_at)
                        SELECT u.lead_id, u.sent_version, %(sent_at)s::timestamptz
                        FROM unnest(%(ids)s::varchar[], %(versions)s::int[]) AS u(lead_id, sent_version)
                        ON CONFLICT (lead_id) DO UPDATE
                          SET sent_version = EXCLUDED.sent_version,
                              sent_at = EXCLUDED.sent_at;
                        """,
                        {
                            "ids": sent_ids,
                            "versions": sent_versions,
                            "sent_at": served_dt,
                        },
                    )
                conn.commit()
                return {
                    "status": "ok",
                    "count": len(leads),
                    "new": new_count,
                    "updated": updated_count,
                    "served_at": _format_utc_iso(served_dt),
                    "leads": leads,
                }
            except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
                self._safe_rollback_locked(conn)
                if not isinstance(exc, pg_errors.QueryCanceled):
                    self._close_locked()
                self._logger.error(
                    "schema_mismatch" if isinstance(exc, SchemaMismatchError) else "db_unavailable",
                    error=self._logger.sanitize_error(exc),
                    action="reconnect_next_request",
                )
                raise _unavailable(exc) from exc
            except psycopg.Error as exc:
                self._safe_rollback_locked(conn)
                self._logger.error(
                    "db_unavailable",
                    error=self._logger.sanitize_error(exc),
                    action="batch_query_error",
                )
                raise _unavailable(exc) from exc
            except Exception:
                self._safe_rollback_locked(conn)
                raise


class LeadStreamerServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[http.server.BaseHTTPRequestHandler],
        config: Config,
        logger: StructuredLogger,
        store: PgStore,
    ) -> None:
        self.config = config
        self.logger = logger
        self.store = store
        self.request_semaphore = threading.BoundedSemaphore(config.max_concurrent_requests)
        self._active_lock = threading.Lock()
        self._active_cond = threading.Condition(self._active_lock)
        self._active_requests = 0
        super().__init__(server_address, handler_class)

    def enter_request(self) -> None:
        with self._active_lock:
            self._active_requests += 1

    def exit_request(self) -> None:
        with self._active_lock:
            self._active_requests -= 1
            if self._active_requests <= 0:
                self._active_cond.notify_all()

    def wait_for_active_requests(self, timeout_seconds: float) -> None:
        deadline = time.monotonic() + timeout_seconds
        with self._active_lock:
            while self._active_requests > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._active_cond.wait(timeout=remaining)

    def handle_error(self, request: typing.Any, client_address: typing.Any) -> None:
        _, exc, _ = sys.exc_info()
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, TimeoutError, OSError)):
            return
        if exc is not None:
            self.logger.error("server_error", error_type=type(exc).__name__)


class LeadStreamerHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: LeadStreamerServer

    def setup(self) -> None:
        super().setup()
        try:
            self.connection.settimeout(self.server.config.request_timeout_seconds)
        except OSError:
            pass

    def log_message(self, format: str, *args: typing.Any) -> None:
        return

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        self.close_connection = True
        if code == 400:
            body_obj = {"status": "error", "error": "bad_request"}
        elif code in (414, 431):
            body_obj = {"status": "error", "error": "request_too_large"}
        else:
            body_obj = {"status": "error", "error": "internal_error"}
        try:
            self._send_json(code, body_obj, send_body=(getattr(self, "command", None) != "HEAD"))
        except OSError:
            pass

    def _client_ip(self) -> str:
        peer_ip = self.client_address[0] if self.client_address else "unknown"
        if peer_ip in ("127.0.0.1", "::1"):
            xff = self.headers.get("X-Forwarded-For")
            if xff:
                first = xff.split(",", 1)[0].strip()
                if first and not any(ch.isspace() for ch in first):
                    return first
        return peer_ip

    def _check_bearer_token(self) -> bool:
        auth_header = self.headers.get("Authorization")
        if not auth_header:
            return False
        scheme, sep, token = auth_header.strip().partition(" ")
        if not sep or scheme.lower() != "bearer":
            return False
        token = token.strip()
        if not token:
            return False
        try:
            token_bytes = token.encode("utf-8")
        except UnicodeEncodeError:
            return False
        return hmac.compare_digest(token_bytes, self.server.config.stream_token_bytes)

    def _send_bytes(
        self,
        status_code: int,
        body: bytes,
        extra_headers: dict[str, str] | None = None,
        send_body: bool = True,
    ) -> None:
        self.close_connection = True
        try:
            self.send_response_only(status_code, http.HTTPStatus(status_code).phrase)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            if extra_headers:
                for hdr_name, hdr_val in extra_headers.items():
                    self.send_header(hdr_name, hdr_val)
            self.end_headers()
            if send_body and body:
                self.wfile.write(body)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            pass

    def _send_json(
        self,
        status_code: int,
        payload: dict[str, typing.Any],
        extra_headers: dict[str, str] | None = None,
        send_body: bool | None = None,
    ) -> int:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        allow_body = getattr(self, "_send_body_allowed", True) if send_body is None else send_body
        self._send_bytes(status_code, body, extra_headers=extra_headers, send_body=allow_body)
        return len(body)

    def _dispatch(self, method: str) -> None:
        self.close_connection = True
        self._send_body_allowed = method != "HEAD"
        if not self.server.request_semaphore.acquire(blocking=False):
            client = self._client_ip()
            self.server.logger.warn("busy", client=client)
            self._send_json(
                503,
                {"status": "error", "error": "busy", "retry_after_seconds": 5},
                extra_headers={"Retry-After": "5"},
            )
            return

        self.server.enter_request()
        try:
            parsed_url = urllib.parse.urlsplit(self.path)
            path = parsed_url.path
            client = self._client_ip()

            if path == "/health":
                if method not in ("GET", "HEAD"):
                    self._send_json(
                        405,
                        {"status": "error", "error": "method_not_allowed"},
                        extra_headers={"Allow": "GET"},
                    )
                    return
                db_state = self.server.store.check_health()
                self._send_json(
                    200,
                    {"status": "alive", "database": db_state},
                    send_body=(method == "GET"),
                )
                return

            expected_host = self.server.config.expected_host
            if expected_host:
                incoming_host = _normalize_host_header(self.headers.get("Host") or "")
                if incoming_host != expected_host:
                    self.server.logger.warn("wrong_host", client=client, path=path)
                    self._send_json(421, {"status": "error", "error": "wrong_host"})
                    return

            if path not in ("/leads/next", "/stats"):
                self._send_json(404, {"status": "error", "error": "not_found"})
                return

            if method != "GET":
                self._send_json(
                    405,
                    {"status": "error", "error": "method_not_allowed"},
                    extra_headers={"Allow": "GET"},
                )
                return

            if not self._check_bearer_token():
                self.server.logger.warn("unauthorized", client=client, path=path)
                self._send_json(401, {"status": "error", "error": "unauthorized"})
                return

            if path == "/leads/next":
                params = urllib.parse.parse_qs(parsed_url.query, keep_blank_values=True)
                raw_vals = params.get("limit") if "limit" in params else params.get("batch_size")
                requested_limit: int | None = None
                if raw_vals is not None:
                    raw_limit = raw_vals[-1].strip()
                    if not re.fullmatch(r"[+-]?\d+", raw_limit):
                        self._send_json(400, {"status": "error", "error": "bad_request"})
                        return
                    parsed_limit = int(raw_limit)
                    if parsed_limit < 1:
                        self._send_json(400, {"status": "error", "error": "bad_request"})
                        return
                    requested_limit = min(1000, parsed_limit)
                with self.server.store.batch_lock:
                    t0 = time.monotonic()
                    batch = self.server.store.fetch_next_batch(limit=requested_limit)
                    body = json.dumps(batch, ensure_ascii=False, separators=(",", ":")).encode(
                        "utf-8"
                    )
                    status = batch["status"]
                    count = batch["count"]
                    new_count = batch.get("new")
                    updated_count = batch.get("updated")
                    del batch
                    self._send_bytes(200, body)
                    nbytes = len(body)
                    del body
                    elapsed_ms = int((time.monotonic() - t0) * 1000)
                    if status == "ok":
                        self.server.logger.info(
                            "ping",
                            client=client,
                            status=status,
                            count=count,
                            new=new_count,
                            updated=updated_count,
                            bytes=nbytes,
                            ms=elapsed_ms,
                        )
                    else:
                        self.server.logger.info(
                            "ping",
                            client=client,
                            status=status,
                            count=0,
                            ms=elapsed_ms,
                        )
                return

            if path == "/stats":
                t0 = time.monotonic()
                stats_payload = self.server.store.fetch_stats()
                nbytes = self._send_json(200, stats_payload)
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                self.server.logger.info("stats", client=client, bytes=nbytes, ms=elapsed_ms)
                return

        except DbUnavailableError as exc:
            # "schema_mismatch": wrong database / scraper migrations missing (see the log)
            self._send_json(
                503,
                {"status": "error", "error": exc.code, "retry_after_seconds": 30},
                extra_headers={"Retry-After": "30"},
            )
        except Exception as exc:
            self.server.logger.error(
                "internal_error",
                client=self._client_ip(),
                path=urllib.parse.urlsplit(self.path).path,
                error_type=type(exc).__name__,
            )
            self._send_json(500, {"status": "error", "error": "internal_error"})
        finally:
            self.server.exit_request()
            self.server.request_semaphore.release()

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_HEAD(self) -> None:
        self._dispatch("HEAD")

    def __getattr__(self, name: str) -> typing.Any:
        if name.startswith("do_"):
            method = name[3:]
            return lambda: self._dispatch(method)
        raise AttributeError(name)


def check_setup(config: Config, logger: StructuredLogger) -> int:
    """`python lead_streamer.py --check`: test the database and schema, change nothing, exit."""
    print(f"Ping URL:   {config.ping_url}")
    print(f"Batch size: {config.batch_size}")
    try:
        conn = psycopg.connect(config.database_url, connect_timeout=5, autocommit=True,
                               application_name="lead_streamer_check")
    except Exception as exc:
        print(f"FAIL database: cannot connect: {logger.sanitize_error(exc)}")
        return 1
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database(), current_user, split_part(version(), ' ', 2);")
            db, user, version = cur.fetchone()
            print(f"OK   database: {db} as {user} (PostgreSQL {version})")
            cur.execute(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = ANY(%s);",
                (list(REQUIRED_COLUMNS),),
            )
            missing = missing_columns(cur.fetchall())
            if missing:
                print("FAIL schema: missing " + ", ".join(missing))
                print("     DATABASE_URL must point at the scraper's database with its migrations applied.")
                return 1
            print("OK   schema: every table and column the streamer reads exists")
            cur.execute(f"SELECT count(*), count(*) FILTER (WHERE {_ELIGIBLE_SQL}) FROM public.leads l;")
            total, eligible = cur.fetchone()
            print(f"OK   leads: {total} in total, {eligible} with a phone (these are what gets sent)")
            cur.execute("SELECT to_regclass('lead_stream.sent_leads') IS NOT NULL;")
            if cur.fetchone()[0]:
                cur.execute("SELECT count(*), max(sent_at) FROM lead_stream.sent_leads;")
                sent, last = cur.fetchone()
                print(f"OK   lead_stream.sent_leads: {sent} sent so far (last {last})")
            else:
                cur.execute("SELECT has_database_privilege(current_user, current_database(), 'CREATE');")
                can_create = cur.fetchone()[0]
                state = "will be created on first start" if can_create else                     "MISSING and this user cannot create it (see README: least-privilege setup)"
                print(f"{'OK  ' if can_create else 'FAIL'} lead_stream.sent_leads: {state}")
                if not can_create:
                    return 1
    except Exception as exc:
        print(f"FAIL check: {logger.sanitize_error(exc)}")
        return 1
    finally:
        conn.close()
    print("All checks passed.")
    return 0


def main() -> int:
    config = load_config()
    logger = StructuredLogger(config.log_level, config.secret_values)
    if "--check" in sys.argv[1:]:
        return check_setup(config, logger)
    for warning in config.startup_warnings:
        logger.warn("config_warning", detail=warning)

    logger.info(
        "startup",
        version=VERSION,
        bind=f"{config.bind_host}:{config.bind_port}",
        batch_size=config.batch_size,
        ping_url=config.ping_url,
    )

    shutdown_event = threading.Event()

    def _on_signal(signum: int, _frame: typing.Any) -> None:
        shutdown_event.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _on_signal)

    store = PgStore(config, logger)
    store.connect_on_startup(shutdown_event)

    try:
        server = LeadStreamerServer(
            (config.bind_host, config.bind_port),
            LeadStreamerHandler,
            config,
            logger,
            store,
        )
    except OSError as exc:
        store.close()
        print(f"Startup error: failed to bind {config.bind_host}:{config.bind_port}: {exc}", file=sys.stderr)
        return 1

    server_thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.2},
        name="http-server",
        daemon=True,
    )
    server_thread.start()

    while not shutdown_event.is_set():
        shutdown_event.wait(0.5)

    logger.info("shutdown", action="draining")
    server.shutdown()
    server.server_close()
    server.wait_for_active_requests(20.0)
    server_thread.join(timeout=5.0)
    store.close()
    logger.info("shutdown", status="stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
