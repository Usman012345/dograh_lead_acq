"""
Read-only access to call transcripts and recordings for the dashboard.

Dograh stores these as private files ("recordings/123.wav", "transcripts/123.txt").
The dashboard server asks Dograh for a short-lived link and streams the file to
the browser, so neither the Dograh API key nor the link ever reaches the page.
Only file keys recorded on a lead are served: the browser cannot request any
other file.
"""

from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

import requests

STORAGE_KEY_RE = re.compile(r"(?:recordings|transcripts)/[A-Za-z0-9_\-./]+\.(?:wav|mp3|m4a|ogg|txt)")
RANGE_RE = re.compile(r"bytes=\d*-\d*")
MAX_TRANSCRIPT_BYTES = 2_000_000
SIGNED_URL_SECONDS = 300
_SIGNED_URL_REUSE_SECONDS = 240
_MAX_CACHED_TRANSCRIPTS = 200
_MAX_CACHED_LINKS = 500
_CHUNK_SIZE = 64 * 1024

AUDIO_TYPES = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".ogg": "audio/ogg",
}

_TURN_RE = re.compile(r"^\[(?P<at>[^\]]+)\]\s*(?P<role>[A-Za-z][A-Za-z _-]*):\s?(?P<text>.*)$")
_SPEAKERS = {
    "assistant": "agent",
    "agent": "agent",
    "bot": "agent",
    "ai": "agent",
    "user": "customer",
    "customer": "customer",
    "caller": "customer",
    "human": "customer",
}


class MediaUnavailable(RuntimeError):
    """The transcript or recording could not be fetched right now."""


@dataclass
class AudioStream:
    status_code: int
    headers: Dict[str, str]
    chunks: Iterator[bytes]
    close: Callable[[], None]


def is_storage_key(value: Any) -> bool:
    text = str(value or "").strip()
    return bool(STORAGE_KEY_RE.fullmatch(text)) and ".." not in text


def audio_content_type(key: str) -> str:
    return AUDIO_TYPES.get(PurePosixPath(key).suffix.lower(), "application/octet-stream")


def parse_transcript(text: str) -> List[Dict[str, Optional[str]]]:
    """Turn '[time] assistant: words' lines into conversation turns."""
    turns: List[Dict[str, Optional[str]]] = []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        match = _TURN_RE.match(line)
        if match:
            role = match.group("role").strip().lower()
            turns.append({
                "speaker": _SPEAKERS.get(role, "other"),
                "at": match.group("at").strip(),
                "text": match.group("text").strip(),
            })
        elif turns:
            turns[-1]["text"] = f"{turns[-1]['text']}\n{line.strip()}".strip()
        else:
            turns.append({"speaker": "other", "at": None, "text": line.strip()})
    return [t for t in turns if t["text"]]


class DograhMedia:
    def __init__(self, base_url: str, api_key: str, *, verify: bool = True, timeout: int = 20):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._verify = verify
        self._timeout = timeout
        self._lock = threading.Lock()
        self._links: Dict[str, Tuple[str, float]] = {}
        self._transcripts: "OrderedDict[str, str]" = OrderedDict()

    def _signed_url(self, key: str) -> str:
        if not is_storage_key(key):
            raise MediaUnavailable("This file is not available.")
        now = time.monotonic()
        with self._lock:
            cached = self._links.get(key)
            if cached and cached[1] > now:
                return cached[0]
        try:
            resp = requests.get(
                f"{self._base_url}/s3/signed-url",
                params={"key": key, "inline": "true", "expires_in": SIGNED_URL_SECONDS},
                headers={"X-API-Key": self._api_key, "Accept": "application/json"},
                timeout=self._timeout,
                verify=self._verify,
            )
            url = resp.json().get("url") if resp.status_code == 200 else None
        except (requests.RequestException, ValueError, AttributeError) as e:
            raise MediaUnavailable("The call file could not be reached.") from e
        if not isinstance(url, str) or not url.lower().startswith(("https://", "http://")):
            raise MediaUnavailable("The call file could not be reached.")
        with self._lock:
            if len(self._links) >= _MAX_CACHED_LINKS:
                self._links.clear()
            self._links[key] = (url, now + _SIGNED_URL_REUSE_SECONDS)
        return url

    def fetch_text(self, key: str) -> str:
        with self._lock:
            if key in self._transcripts:
                self._transcripts.move_to_end(key)
                return self._transcripts[key]
        url = self._signed_url(key)
        try:
            with requests.get(url, stream=True, timeout=self._timeout, verify=self._verify) as resp:
                if resp.status_code != 200:
                    raise MediaUnavailable("The transcript could not be loaded.")
                data = bytearray()
                for chunk in resp.iter_content(_CHUNK_SIZE):
                    data.extend(chunk)
                    if len(data) > MAX_TRANSCRIPT_BYTES:
                        raise MediaUnavailable("The transcript is too large to display.")
        except requests.RequestException as e:
            raise MediaUnavailable("The transcript could not be loaded.") from e
        text = bytes(data).decode("utf-8", errors="replace")
        with self._lock:
            self._transcripts[key] = text
            while len(self._transcripts) > _MAX_CACHED_TRANSCRIPTS:
                self._transcripts.popitem(last=False)
        return text

    def open_audio(self, key: str, range_header: Optional[str] = None) -> AudioStream:
        url = self._signed_url(key)
        headers = {}
        if range_header and RANGE_RE.fullmatch(range_header.strip()):
            headers["Range"] = range_header.strip()
        try:
            resp = requests.get(url, stream=True, headers=headers,
                                timeout=(self._timeout, 60), verify=self._verify)
        except requests.RequestException as e:
            raise MediaUnavailable("The recording could not be loaded.") from e
        if resp.status_code not in (200, 206):
            resp.close()
            raise MediaUnavailable("The recording could not be loaded.")
        out_headers = {"Accept-Ranges": "bytes", "Content-Type": audio_content_type(key)}
        for name in ("Content-Length", "Content-Range"):
            if resp.headers.get(name):
                out_headers[name] = resp.headers[name]
        return AudioStream(resp.status_code, out_headers, resp.iter_content(_CHUNK_SIZE), resp.close)
