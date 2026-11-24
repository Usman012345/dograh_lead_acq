"""
Dograh client - HTTP calls to the Dograh API used by the campaign stage.

  GET  /workflow/fetch/{agent_id}         -> agent name, publish status
  GET  /workflow/{agent_id}/versions      -> {{variables}} the published agent needs
  POST /s3/presigned-upload-url  + PUT    -> upload a campaign's contacts CSV
  POST /campaign/create                   -> campaign for one agent + that CSV
  POST /campaign/{id}/start               -> start dialing
  POST /campaign/{id}/pause               -> stop dialing (when finished, or on exit)
  GET  /campaign/{id}                     -> state, retry config, counts
  GET  /campaign/{id}/runs                -> one run per call attempt

Campaign contacts are fixed when the campaign is created (Dograh has no API
to add numbers to an existing campaign).
"""

from __future__ import annotations

import csv
import io
import json
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests

import config


class DograhError(RuntimeError):
    pass


@dataclass(frozen=True)
class Agent:
    id: int
    name: str
    version_status: str


@dataclass
class CallOutcome:
    status: str                       # "called" | "call_failed" | "call_timeout"
    run_id: Optional[int] = None
    disposition: Optional[str] = None
    duration_seconds: Optional[float] = None
    tags: list = field(default_factory=list)
    transcript_url: Optional[str] = None
    recording_url: Optional[str] = None
    attempts: int = 0
    error: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)   # the (last) run returned by Dograh


def _detail(resp: requests.Response) -> str:
    try:
        data = resp.json()
        return str(data.get("detail", data)) if isinstance(data, dict) else str(data)
    except ValueError:
        return (resp.text or "").strip()[:300] or "<empty response>"


class DograhClient:
    def __init__(self, base_url: str = config.DOGRAH_BASE_URL, api_key: str = config.DOGRAH_API_KEY):
        if not api_key:
            raise ValueError("Dograh API key not found. Put it in .env as DOGRAH_API_KEY=...")
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._local = threading.local()
        self._sessions_lock = threading.Lock()
        self._sessions: List[requests.Session] = []
        self._agents_lock = threading.Lock()
        self._agents: Dict[int, Agent] = {}

    # -- low level -------------------------------------------------------------

    def _get_session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update({"X-API-Key": self._api_key, "Accept": "application/json"})
            self._local.session = session
            with self._sessions_lock:
                self._sessions.append(session)
        return session

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{self.base_url}{path}"
        try:
            return self._get_session().request(
                method, url, timeout=config.REQUEST_TIMEOUT, verify=config.VERIFY_SSL, **kwargs
            )
        except requests.RequestException as e:
            raise DograhError(f"Could not reach Dograh at {url}: {e}") from e

    def _json(self, resp: requests.Response, action: str) -> Any:
        if resp.status_code == 401:
            raise DograhError(f"{action}: Dograh rejected the API key (HTTP 401).")
        if resp.status_code != 200:
            raise DograhError(f"{action} failed (HTTP {resp.status_code}): {_detail(resp)}")
        try:
            return resp.json()
        except ValueError:
            raise DograhError(f"{action}: Dograh returned a non-JSON response.") from None

    # -- agents ----------------------------------------------------------------

    def get_agent(self, agent_id: int) -> Agent:
        """Look up an agent (workflow) by its numeric ID. Cached."""
        with self._agents_lock:
            if agent_id in self._agents:
                return self._agents[agent_id]
        resp = self._request("GET", f"/workflow/fetch/{agent_id}")
        if resp.status_code == 404:
            raise DograhError(f"Dograh agent {agent_id} does not exist.")
        data = self._json(resp, f"Fetching agent {agent_id}")
        agent = Agent(
            id=int(data["id"]),
            name=str(data.get("name") or f"Agent {agent_id}"),
            version_status=str(data.get("version_status") or ""),
        )
        with self._agents_lock:
            self._agents[agent_id] = agent
        return agent

    def _published_workflow(self, agent_id: int) -> Dict[str, Any]:
        versions = self._json(self._request("GET", f"/workflow/{agent_id}/versions"),
                              f"Fetching versions of agent {agent_id}")
        if isinstance(versions, dict):          # tolerate a wrapped list
            versions = versions.get("versions") or versions.get("items") or []
        if not isinstance(versions, list):
            return {}
        published = next((v for v in versions if isinstance(v, dict) and v.get("status") == "published"), None)
        return (published or {}).get("workflow_json") or {}

    def get_required_variables(self, agent_id: int) -> Set[str]:
        """
        Template variables the agent's PUBLISHED version needs from the contact
        list. Dograh refuses a campaign if any of these columns is missing or
        empty in any row.
        """
        return required_template_variables(self._published_workflow(agent_id))

    def get_agent_variables(self, agent_id: int) -> Tuple[Set[str], Set[str]]:
        """(required, used): the variables a campaign CSV must fill, and every
        lead variable the published agent refers to anywhere (with or without a default)."""
        workflow = self._published_workflow(agent_id)
        return required_template_variables(workflow), used_template_variables(workflow)

    # -- campaigns -------------------------------------------------------------

    def upload_csv(self, rows: List[Dict[str, Any]], file_name: str) -> str:
        """Upload contact rows as a CSV via Dograh's presigned upload; returns the file key."""
        headers: List[str] = ["phone_number"]
        for row in rows:
            for key in row:
                if key not in headers:
                    headers.append(key)
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({h: "" if row.get(h) is None else row.get(h) for h in headers})
        data = buf.getvalue().encode("utf-8")

        resp = self._request("POST", "/s3/presigned-upload-url",
                             json={"file_name": file_name, "file_size": len(data), "content_type": "text/csv"})
        presigned = self._json(resp, "Requesting upload URL")
        upload_url, file_key = presigned.get("upload_url"), presigned.get("file_key")
        if not upload_url or not file_key:
            raise DograhError(f"Invalid upload URL response: {presigned}")
        try:
            put = requests.put(upload_url, data=data, headers={"Content-Type": "text/csv"},
                               timeout=60, verify=config.VERIFY_SSL)
        except requests.RequestException as e:
            raise DograhError(f"Uploading contacts to storage ({urlparse(upload_url).hostname}) failed: {e}") from e
        if put.status_code not in (200, 204):
            raise DograhError(f"Uploading contacts failed (HTTP {put.status_code}): {(put.text or '')[:300]}")
        return file_key

    def create_campaign(self, name: str, agent_id: int, file_key: str) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "name": name,
            "workflow_id": agent_id,
            "source_type": "csv",
            "source_id": file_key,
            "retry_config": {
                "enabled": config.CAMPAIGN_RETRY_ENABLED,
                "max_retries": config.CAMPAIGN_MAX_RETRIES,
                "retry_delay_seconds": config.CAMPAIGN_RETRY_DELAY_SECONDS,
                "retry_on_busy": config.CAMPAIGN_RETRY_ON_BUSY,
                "retry_on_no_answer": config.CAMPAIGN_RETRY_ON_NO_ANSWER,
                "retry_on_voicemail": config.CAMPAIGN_RETRY_ON_VOICEMAIL,
            },
        }
        data = self._json(self._request("POST", "/campaign/create", json=body), f"Creating campaign '{name}'")
        if not data.get("id"):
            raise DograhError(f"Creating campaign '{name}': unexpected response {data}")
        return data

    def start_campaign(self, campaign_id: int) -> None:
        resp = self._request("POST", f"/campaign/{campaign_id}/start")
        if resp.status_code != 200:
            raise DograhError(f"Starting campaign {campaign_id} failed (HTTP {resp.status_code}): {_detail(resp)}")

    def stop_campaign(self, campaign_id: int) -> str:
        """
        Make sure a campaign is no longer dialing. Dograh has no "complete" or
        "cancel" API, so a campaign still in 'running'/'syncing' is paused.
        Returns the campaign's state afterwards.
        """
        state = str(self.get_campaign(campaign_id).get("state") or "").lower()
        if state not in ("running", "syncing"):
            return state
        resp = self._request("POST", f"/campaign/{campaign_id}/pause")
        if resp.status_code != 200:
            raise DograhError(f"Pausing campaign {campaign_id} failed (HTTP {resp.status_code}): {_detail(resp)}")
        return "paused"

    def get_campaign(self, campaign_id: int) -> Dict[str, Any]:
        return self._json(self._request("GET", f"/campaign/{campaign_id}"), f"Fetching campaign {campaign_id}")

    def get_campaign_runs(self, campaign_id: int) -> List[Dict[str, Any]]:
        """All runs (call attempts) of a campaign, across pages."""
        runs: List[Dict[str, Any]] = []
        page = 1
        while True:
            data = self._json(
                self._request("GET", f"/campaign/{campaign_id}/runs", params={"page": page, "limit": 100}),
                f"Fetching runs of campaign {campaign_id}",
            )
            if isinstance(data, list):
                return runs + data
            batch = data.get("runs") or []
            runs.extend(batch)
            if page >= (data.get("total_pages") or 1) or not batch:
                return runs
            page += 1

    def close(self) -> None:
        with self._sessions_lock:
            sessions, self._sessions = self._sessions, []
        for session in sessions:
            session.close()
        self._local = threading.local()


# ------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------

# Same rules Dograh uses to decide which {{variables}} a campaign's CSV must supply
_TEMPLATE_VAR_RE = re.compile(r"\{\{\s*([^|\s}]+)(?:\s*\|\s*([^:}]+)(?::([^}]+))?)?\s*\}\}")
_SYSTEM_VARIABLES = {"campaign_id", "provider", "source_uuid"}


def _is_builtin_variable(name: str) -> bool:
    """current_time / current_weekday (and their _<Area/City> forms) are computed by Dograh at call time."""
    return name in ("current_time", "current_weekday") or name.startswith(("current_time_", "current_weekday_"))


def required_template_variables(workflow_json: Dict[str, Any]) -> Set[str]:
    """Variables in prompts / greetings / transition speeches that need a CSV value."""
    texts: List[str] = []
    for node in workflow_json.get("nodes") or []:
        data = node.get("data") or {}
        texts += [data.get("prompt") or "", data.get("greeting") or ""]
    for edge in workflow_json.get("edges") or []:
        texts.append((edge.get("data") or {}).get("transition_speech") or "")

    required: Set[str] = set()
    for text in texts:
        if not isinstance(text, str):
            continue
        for m in _TEMPLATE_VAR_RE.finditer(text):
            name, has_filter = m.group(1).strip(), m.group(2) is not None
            if "." in name or has_filter or name in _SYSTEM_VARIABLES:
                continue  # nested path, has a default, or filled in by Dograh
            if _is_builtin_variable(name):
                continue  # computed by Dograh at call time
            required.add(name)
    return required


def _walk_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _walk_strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _walk_strings(v)


def used_template_variables(workflow_json: Dict[str, Any]) -> Set[str]:
    """
    Every lead variable the agent refers to ANYWHERE in its workflow (prompts,
    greetings, tool settings, ...), with or without a default:
    {{company}}, {{city | there}}, {{initial_context.city}} -> company, city.
    """
    used: Set[str] = set()
    for text in _walk_strings(workflow_json or {}):
        if "{{" not in text:
            continue
        for m in _TEMPLATE_VAR_RE.finditer(text):
            name = m.group(1).strip()
            if name.startswith("initial_context."):
                name = name[len("initial_context."):]
            name = name.split(".", 1)[0]
            if name and name not in _SYSTEM_VARIABLES and not _is_builtin_variable(name):
                used.add(name)
    return used


# Dograh's campaign-creation errors that name variables its contact check wanted
_MISSING_COLUMNS_RE = re.compile(r"missing from the source data:\s*(.+?)(?:\.\s|\.$|$)", re.IGNORECASE)
_EMPTY_VARIABLE_RE = re.compile(r"Template variable '([^']+)' is empty", re.IGNORECASE)


def variables_in_campaign_error(message: str) -> Set[str]:
    """
    Variable names in a Dograh campaign-creation error about the contacts file:
      "Workflow uses template variables that are missing from the source data: 'city', 'zip'. ..."
      "Template variable 'city' is empty in rows: 2, 5. ..."
    Empty set for any other error.
    """
    text = str(message or "")
    found: Set[str] = set(_EMPTY_VARIABLE_RE.findall(text))
    m = _MISSING_COLUMNS_RE.search(text)
    if m:
        found |= set(re.findall(r"'([^']+)'", m.group(1)))
    return {v.strip().lower() for v in found if v.strip()}


def run_disposition(run: Dict[str, Any]) -> Optional[str]:
    g = run.get("gathered_context") or {}
    for key in ("call_disposition", "mapped_call_disposition", "disposition"):
        if g.get(key):
            return str(g[key])
    return None


def is_transferred(run: Dict[str, Any]) -> bool:
    """
    True if the agent transferred this call. Dograh marks it in gathered_context:
    disposition "transfer_call" (phone transfer) or "call_transferred" (PBX
    transfer, also external_pbx_transferred=True), or a disposition configured on
    the transfer tool (see config.TRANSFER_DISPOSITIONS). A failed transfer is
    not a transfer.
    """
    g = run.get("gathered_context") or {}
    if g.get("external_pbx_transferred") is True or str(g.get("transfer_state") or "").lower() == "complete":
        return True
    tags = g.get("call_tags") or []
    values = [g.get("call_disposition"), g.get("mapped_call_disposition"),
              *(tags if isinstance(tags, list) else [tags])]
    for value in values:
        v = str(value or "").strip().lower()
        if not v or "fail" in v:
            continue
        if v in config.TRANSFER_DISPOSITIONS or "transfer" in v:
            return True
    return False


def is_not_reached(disposition: Optional[str]) -> bool:
    """True if the call's outcome means nobody was reached (busy, no answer, failed, voicemail, ...)."""
    return str(disposition or "").strip().lower() in config.NOT_REACHED_DISPOSITIONS


def outcome_from_run(run: Dict[str, Any], attempts: int = 1) -> CallOutcome:
    """
    The outcome of a lead's last call attempt. A call that reached nobody
    (see config.NOT_REACHED_DISPOSITIONS) is "call_failed", never "called", so
    its number is not treated as already called. A transferred call is always "called".
    """
    g = run.get("gathered_context") or {}
    cost = run.get("cost_info") or {}
    duration = cost.get("call_duration_seconds", cost.get("duration"))
    try:
        duration = float(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration = None
    tags = g.get("call_tags") or []
    disposition = run_disposition(run)
    status, error = "called", None
    if is_not_reached(disposition) and not is_transferred(run):
        status, error = "call_failed", f"Call did not reach anyone ({disposition})"
    return CallOutcome(
        status=status,
        run_id=run.get("id"),
        disposition=disposition,
        duration_seconds=duration,
        tags=tags if isinstance(tags, list) else [tags],
        transcript_url=run.get("transcript_public_url") or run.get("transcript_url"),
        recording_url=run.get("recording_public_url") or run.get("recording_url"),
        attempts=attempts,
        error=error,
        raw={k: v for k, v in run.items() if k != "logs"},
    )


def safe_key(key: Any) -> str:
    """Column/variable name usable as a Dograh template variable: 'Company Name' -> 'company_name'."""
    return "".join(c if c.isalnum() else "_" for c in str(key).strip().lower()).strip("_")


def _lead_zone(time_zone: str) -> Optional[ZoneInfo]:
    """The first IANA zone of a phone_timezone value ("America/Chicago" or "America/Chicago, America/Denver")."""
    name = str(time_zone or "").split(",")[0].strip()
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except Exception:
        return None


# Variables this program always fills in itself (a lead field with one of these names is replaced)
OWN_CONTEXT_KEYS = frozenset({"phone_number", "interested_service", "current_datetime", "current_date",
                              "lead_timezone", "lead_ref"})


def call_context(
    lead: Dict[str, Any],
    phone_e164: str,
    service_name: str,
    *,
    fields: Optional[Iterable[str]] = None,
    time_zone: str = "",
    collisions: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Variables passed to the Dograh agent (as campaign CSV columns). They can be
    used in the agent's prompts as {{template_variables}}, e.g. {{company}} or
    {{interested_service}}.

    fields:     only these lead fields (CSV names, e.g. "company_name") are sent;
                None = every lead field.
    time_zone:  the number's IANA time zone (phone_timezone): current_datetime /
                current_date are given in the LEAD's local time. They are fixed
                when the campaign is created; for the time at the moment of the
                call use Dograh's {{current_time_<Area/City>}}.
    collisions: receives a warning for each pair of lead fields that map to the
                same variable ("Company Name" and "company_name"); the first
                non-empty value is kept.
    """
    wanted = None if fields is None else {str(f).strip().lower() for f in fields}
    ctx: Dict[str, Any] = {}
    origin: Dict[str, str] = {}
    for key, value in lead.items():
        k = safe_key(key)
        if not k or value in (None, "") or k in OWN_CONTEXT_KEYS:
            continue
        if wanted is not None and k not in wanted:
            continue
        if k in ctx:
            if collisions is not None:
                collisions.append(f"lead fields '{origin[k]}' and '{key}' are both sent as {{{{{k}}}}}; "
                                  f"keeping the value of '{origin[k]}'")
            continue
        ctx[k] = value if isinstance(value, (str, int, float, bool)) else json.dumps(value, default=str)
        origin[k] = str(key)
    zone = _lead_zone(time_zone)
    now = datetime.now(zone) if zone is not None else datetime.now().astimezone()
    ctx["phone_number"] = phone_e164
    ctx["interested_service"] = service_name
    ctx["current_datetime"] = now.strftime("%A, %B %d, %Y, %I:%M %p %Z")
    ctx["current_date"] = now.strftime("%A, %B %d, %Y")
    if zone is not None:
        ctx["lead_timezone"] = str(zone.key)
    return ctx
