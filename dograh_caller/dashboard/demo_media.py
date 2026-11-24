"""Fictional transcripts and a generated audio tone for `dashboard_server.py --demo`."""

from __future__ import annotations

import math
import re
import struct
from pathlib import PurePosixPath
from typing import Dict, List, Optional, Tuple

from dashboard.media import AudioStream, MediaUnavailable, is_storage_key

_AGENTS = {
    "draftliner": ("Danial", "Draftliner", "remote drafting and BIM support"),
    "paradise": ("Olivia", "Paradise Estimating", "construction estimating and takeoffs"),
}

_SCRIPTS: Dict[str, List[Tuple[str, str]]] = {
    "transfer": [
        ("assistant", "Hi, this is {agent} calling from {company}. Do you have a quick minute?"),
        ("user", "Sure, what is this about?"),
        ("assistant", "We help contractors with {offer}. Are you handling many bids at the moment?"),
        ("user", "Yes, we have several due this month and we are short on people."),
        ("assistant", "That is exactly where we help. Would you like to speak with a specialist now?"),
        ("user", "Yes, please connect me."),
        ("assistant", "Great, I am transferring you now. Thanks for your time."),
    ],
    "positive": [
        ("assistant", "Hi, this is {agent} from {company}. We provide {offer}."),
        ("user", "We might need that next quarter. Can you send some information?"),
        ("assistant", "Absolutely. I will have the team email you a sample and next steps."),
        ("user", "Perfect, thank you."),
    ],
    "hangup": [
        ("assistant", "Hi, this is {agent} from {company}. Do you have a moment?"),
        ("user", "Not right now, sorry."),
    ],
    "voicemail": [
        ("assistant", "Hi, this is {agent} from {company}. Sorry I missed you. We provide {offer}, "
                      "and I will try you again soon."),
    ],
}

_SAMPLE_RATE = 8000
_SECONDS = 8


def _demo_wav() -> bytes:
    samples = bytearray()
    for n in range(_SAMPLE_RATE * _SECONDS):
        t = n / _SAMPLE_RATE
        pulse = max(0.0, math.sin(2 * math.pi * 0.75 * t)) ** 2
        tone = math.sin(2 * math.pi * 220 * t) + 0.45 * math.sin(2 * math.pi * 330 * t)
        samples += struct.pack("<h", int(2600 * pulse * tone))
    header = b"RIFF" + struct.pack("<I", 36 + len(samples)) + b"WAVEfmt " + struct.pack(
        "<IHHIIHH", 16, 1, 1, _SAMPLE_RATE, _SAMPLE_RATE * 2, 2, 16
    ) + b"data" + struct.pack("<I", len(samples))
    return header + bytes(samples)


class DemoMedia:
    def __init__(self) -> None:
        self._wav = _demo_wav()

    def fetch_text(self, key: str) -> str:
        if not is_storage_key(key) or "/demo/" not in key:
            raise MediaUnavailable("This file is not available.")
        parts = PurePosixPath(key).stem.split("-")
        scenario = parts[0] if parts and parts[0] in _SCRIPTS else "hangup"
        service = parts[1] if len(parts) > 1 and parts[1] in _AGENTS else "paradise"
        agent, company, offer = _AGENTS[service]
        lines = []
        for index, (role, text) in enumerate(_SCRIPTS[scenario]):
            seconds = 4 + index * 9
            body = text.format(agent=agent, company=company, offer=offer)
            lines.append(f"[2026-11-23T15:00:{seconds:02d}.000+00:00] {role}: {body}")
        return "\n".join(lines)

    def open_audio(self, key: str, range_header: Optional[str] = None) -> AudioStream:
        if not is_storage_key(key) or "/demo/" not in key:
            raise MediaUnavailable("This file is not available.")
        total = len(self._wav)
        start, end, status = 0, total - 1, 200
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", (range_header or "").strip())
        if total > 0 and match and (match.group(1) or match.group(2)):
            if match.group(1):
                req_start = int(match.group(1))
                req_end = min(int(match.group(2)), total - 1) if match.group(2) else total - 1
                if 0 <= req_start < total and req_start <= req_end:
                    start, end, status = req_start, req_end, 206
            else:
                suffix = int(match.group(2))
                if suffix > 0:
                    start, end, status = max(0, total - suffix), total - 1, 206
        body = self._wav[start:end + 1]
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Type": "audio/wav",
            "Content-Length": str(len(body)),
        }
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{total}"
        return AudioStream(status, headers, iter([body]), lambda: None)
