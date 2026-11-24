"""
Number verifier - first stage of the pipeline.

Receives raw lead dicts (from the AI-powered scraper or a spreadsheet), finds
the phone number(s), and only lets through a valid, callable, not-yet-seen US
number in E.164 format (+1XXXXXXXXXX). Everything else is dropped with a reason.

Leads may be unstructured: any field names, nested dicts, lists of dicts, JSON
stored as text. See lead_fields.py.

How a lead's number is chosen
-----------------------------
1. Phone fields are found at ANY depth by the words in their name ("Phone(s)",
   "phoneNumber", "Tel", "Mobile", "Cell", "contact > phone", "phones[] > number",
   ...). Fields such as "Phone Verified: Yes" simply yield no number.
2. Numbers are pulled out of each value with Google's libphonenumber number
   finder, so cells like "+19292659900, +19174102250",
   "512-555-0102 / 512-555-0103" or "Office: (512) 555-0104 Cell: ..." work.
   Vanity numbers ("1-800-FLOWERS") are handled by a direct parse.
3. Fax numbers are NEVER called. A number counts as a fax number when:
     - its field name says fax ("fax", "fax_phone", "faxNumber", "telefax"), or
     - it sits in an entry typed as fax ({"type": "fax", "number": "..."}), or
     - its label in the text says fax ("Fax: ...", "F: ...", "... (fax)").
   Every fax number found anywhere in the lead is excluded, even if the same
   number also appears in a phone field. A lead with only fax numbers is
   dropped as "dropped_fax_only".
4. Each number is checked against the real numbering plan:
     - valid number (real area code, correct length, ...);
     - region is allowed (default: US only - Canada, Puerto Rico, Guam and other
       +1 regions are dropped; see ALLOWED_PHONE_REGIONS in config.py);
     - callable type: premium-rate (900), shared-cost, personal (500-type),
       pager and similar special numbers are dropped.
5. Repeat protection - the lead is dropped if any of its passing numbers is:
     - already called in an earlier run (results sheet)    -> dropped_already_called
     - already seen earlier in this run                    -> dropped_duplicate
6. The first number that passes is the one called. Its US state and time zone
   are recorded (from the area code) for calling-hours checks and reporting.

Limitation: "valid" means the number is correctly formed for the US numbering
plan. Whether it is currently in service can only be checked with a carrier
lookup service (e.g. Twilio Lookup).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Set, Tuple

import phonenumbers
from phonenumbers import Leniency, NumberParseException, PhoneNumberFormat, PhoneNumberMatcher
from phonenumbers import PhoneNumberType as NT
from phonenumbers import geocoder
from phonenumbers import timezone as pn_timezone

import config
from lead_fields import cell_text, is_email_key, is_fax_key, is_phone_key, key_words, parse_json_text

# Number types that may be called. Everything else (premium rate, shared cost,
# personal/500-type, pager, UAN, voicemail-only, unknown) is dropped.
CALLABLE_TYPES = frozenset({NT.FIXED_LINE, NT.MOBILE, NT.FIXED_LINE_OR_MOBILE, NT.TOLL_FREE})
_TYPE_NAMES = {v: k for k, v in vars(NT).items() if isinstance(v, int)}

# When a lead has no usable number, the most informative reason is reported.
_REASON_RANK = {
    "dropped_not_us": 5,
    "dropped_fax_only": 4,
    "dropped_special_number": 3,
    "dropped_invalid_number": 2,
    "dropped_unreadable_number": 1,
}

# A label in the text that marks the number next to it as a fax number:
# "Fax:", "fax", "Telefax", "Facsimile", "F:" / "F#" / "F. 512..." (but not "Fairfax" or "F. Smith").
_FAX_LABEL = re.compile(r"\b(?:tele)?fax(?:es)?\b|\bfacsimile\b|\bf\s*[:#]|\bf\.\s*(?=$|[\d+(])", re.IGNORECASE)
# Separators between numbers listed in one value
_SEPARATOR = re.compile(r"[,;|/\n]")
# A bracketed label right after a number belongs to that number: "512-555-0101 (fax)"
_BRACKET_LABEL = re.compile(r"^\s*[\(\[]([^\)\]]*)[\)\]]")
# Field names that describe what kind of number an entry is: {"type": "fax", "number": ...}
_TYPE_WORDS = frozenset({"type", "label", "kind", "usage", "name", "line"})
# Fax typing also accepts "category" ({"category": "fax", ...}). It is only checked on
# nested entries, never on the lead itself, so a lead's own category can't trigger it.
_FAX_TYPE_WORDS = _TYPE_WORDS | {"category"}
# An entry in an unknown container is only treated as a labelled phone line when it is
# small, like {"label": "Main", "value": ...}; this check is not applied to fax typing.
_MAX_LABELLED_ENTRY_FIELDS = 6
# Fields inside a label-recognized entry that hold IDs/metadata rather than phone numbers
_NON_PHONE_VALUE_WORDS = frozenset({
    "id", "ids", "uuid", "guid", "url", "uri", "link", "href", "zip", "postal",
    "postcode", "date", "time", "timestamp", "created", "updated", "modified",
    "seen", "ext", "extension", "lat", "lng", "lon", "latitude", "longitude",
    "revenue", "employees", "size", "score", "version",
})
_FAX_TYPE_VALUE = re.compile(r"^\W*(?:tele)?fax(?:\s*(?:number|no\.?|line|#))?\W*$|^\W*facsimile\W*$", re.IGNORECASE)
# ...and type values that mark an entry as a phone line: {"label": "Main", "value": ...}
_PHONE_TYPE_VALUE = re.compile(
    r"^\W*(main|office|work|business|direct|phone|telephone|tel|mobile|cell|cellphone|landline|"
    r"primary|toll[\s-]?free|hq|headquarters|sales|support|home)(\s*(phone|line|number|no\.?|#))?\W*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Verification:
    """Outcome of verifying one lead's phone number."""
    ok: bool
    raw: str                 # phone field value(s) as found in the lead
    e164: Optional[str]      # the number that will be called (+1XXXXXXXXXX) when ok
    reason: str              # "ok" or why it was dropped
    location: str = ""       # US state of the number's area code, e.g. "Texas"
    time_zone: str = ""      # e.g. "America/Chicago" ("" for toll-free / multi-zone numbers)


def number_location(e164: str) -> Tuple[str, str]:
    """(state, time zone) of a number from its area code; blanks when not location-specific."""
    try:
        parsed = phonenumbers.parse(e164)
    except NumberParseException:
        return "", ""
    location = geocoder.description_for_number(parsed, "en") or ""
    zones = [z for z in pn_timezone.time_zones_for_number(parsed) if z != "Etc/Unknown"]
    time_zone = zones[0] if len(zones) == 1 else (", ".join(zones) if 1 < len(zones) <= 3 else "")
    return location, time_zone


def _typed_entry(entry: Dict[str, Any], pattern: "re.Pattern[str]", type_words: frozenset = _TYPE_WORDS) -> bool:
    """True when one of the entry's type/label fields matches the pattern."""
    for k, v in entry.items():
        if isinstance(v, str) and any(w in type_words for w in key_words(k)) and pattern.match(v):
            return True
    return False


def _is_fax_typed_entry(entry: Dict[str, Any]) -> bool:
    """True for an entry like {"type": "fax", "number": "..."}, {"label": "Fax", ...}, {"category": "fax", ...}."""
    return _typed_entry(entry, _FAX_TYPE_VALUE, _FAX_TYPE_WORDS)


def _is_phone_typed_entry(entry: Dict[str, Any]) -> bool:
    """True for an entry like {"label": "Main", "value": "..."} or {"type": "mobile", "number": "..."}."""
    return _typed_entry(entry, _PHONE_TYPE_VALUE)


def _collect_number_fields(lead: Any) -> Tuple[List[Tuple[str, str]], List[str]]:
    """
    Walk a (possibly nested, unstructured) lead and return
      phone_fields: [(field path, text)] from phone-like fields, in order
      fax_texts:    [text] from fax fields / fax-typed entries
    A value counts as a phone value when its field name, or the name of any
    field it is nested under, looks like a phone field.
    """
    phone_fields: List[Tuple[str, str]] = []
    fax_texts: List[str] = []

    def visit(value: Any, path: Tuple[str, ...], in_phone: bool, in_fax: bool, by_label: bool) -> None:
        # by_label: inside an entry recognised only by its label ({"label": "Main", ...}),
        # so only values that actually look like numbers are taken.
        value = parse_json_text(value)
        if isinstance(value, dict):
            nested = bool(path)            # never treat the lead itself as a typed entry
            # Fax typing: any nested entry, whatever its size - a fax must never be called
            entry_is_fax = in_fax or (nested and _is_fax_typed_entry(value))
            # Phone-line guessing in unknown containers: small entries only
            labelled_phone = (not in_phone and nested and len(value) <= _MAX_LABELLED_ENTRY_FIELDS
                              and _is_phone_typed_entry(value))
            for k, v in value.items():
                key_phone = is_phone_key(k)
                key_fax = is_fax_key(k)
                child_by_label = (by_label or labelled_phone) and not (in_phone or key_phone)
                if child_by_label and not (key_phone or key_fax):
                    if is_email_key(k) or (set(key_words(k)) & _NON_PHONE_VALUE_WORDS):
                        continue
                visit(v, path + (str(k),),
                      in_phone or key_phone or labelled_phone,
                      entry_is_fax or key_fax,
                      child_by_label)
        elif isinstance(value, (list, tuple, set)):
            for v in value:
                visit(v, path, in_phone, in_fax, by_label)
        else:
            text = cell_text(value)
            if not text:
                return
            if by_label and sum(c.isdigit() for c in text) < 7:
                return
            if in_fax:
                fax_texts.append(text)
            elif in_phone:
                phone_fields.append((" > ".join(path), text))

    visit(lead, (), False, False, False)
    return phone_fields, fax_texts


def phone_values(lead: Dict[str, Any]) -> List[Tuple[str, str]]:
    """All non-empty (field path, value) pairs from phone-like fields, at any depth (fax fields excluded)."""
    return _collect_number_fields(lead)[0]


def find_phone(lead: Dict[str, Any]) -> str:
    """The lead's phone field value(s) as one string, or '' if there are none."""
    values = phone_values(lead)
    if len(values) == 1:
        return values[0][1]
    return " | ".join(f"{k}: {v}" for k, v in values)


def extract_labelled_numbers(text: str) -> Tuple[List[Tuple[phonenumbers.PhoneNumber, bool]], Optional[str]]:
    """
    Find every phone number in a text and whether its label marks it as a fax.
    Returns ([(number, is_fax)], failure_reason).

    The label for a number is the text just before it ("Fax: 512...") or a label
    right after it ("512... (fax)", "512... fax, 713..."):
      "Phone: (512) 555-0101 Fax: (512) 555-0102"  -> 0101 phone, 0102 fax
      "512-555-0101 (fax), 512-555-0102 (office)"  -> 0101 fax,   0102 phone
      "P: 512-555-0101 F: 512-555-0102"            -> 0101 phone, 0102 fax
    """
    matches = list(PhoneNumberMatcher(text, "US", leniency=Leniency.VALID))
    if not matches:
        # Fallback: parse the whole value (vanity numbers like 1-800-FLOWERS, and
        # invalid numbers so they can report why they are invalid).
        try:
            return [(phonenumbers.parse(text, "US"), bool(_FAX_LABEL.search(text)))], None
        except NumberParseException:
            return [], "dropped_unreadable_number"

    out: List[Tuple[phonenumbers.PhoneNumber, bool]] = []
    prev_bracketed = False
    for i, m in enumerate(matches):
        prev_end = matches[i - 1].end if i else 0
        next_start = matches[i + 1].start if i + 1 < len(matches) else len(text)
        before, after = text[prev_end:m.start], text[m.end:next_start]

        # Label before this number: drop the part that belongs to the previous number
        prefix = before
        if i:
            if prev_bracketed:
                prefix = _BRACKET_LABEL.sub("", prefix, count=1)
                prefix = re.sub(r"^\s*[,;|/\n]", "", prefix)
            elif _SEPARATOR.search(prefix):
                prefix = _SEPARATOR.split(prefix, maxsplit=1)[1]

        # Label after this number: "(fax)" right after it, or text up to the next
        # separator (only when a separator follows, or this is the last number)
        bracket = _BRACKET_LABEL.match(after)
        if bracket:
            suffix = bracket.group(1)
        else:
            parts = _SEPARATOR.split(after, maxsplit=1)
            suffix = parts[0] if (len(parts) > 1 or i == len(matches) - 1) else ""
        prev_bracketed = bool(bracket)

        out.append((m.number, bool(_FAX_LABEL.search(prefix) or _FAX_LABEL.search(suffix))))
    return out, None


def extract_numbers(text: str) -> Tuple[List[phonenumbers.PhoneNumber], Optional[str]]:
    """Every phone number in a text (fax labels ignored). Returns (numbers, failure_reason)."""
    labelled, failure = extract_labelled_numbers(text)
    return [n for n, _ in labelled], failure


def _e164_if_valid(number: phonenumbers.PhoneNumber) -> Optional[str]:
    return phonenumbers.format_number(number, PhoneNumberFormat.E164) if phonenumbers.is_valid_number(number) else None


def load_called_numbers(results_file: Path, statuses: Iterable[str] = ("called", "call_timeout")) -> Set[str]:
    """Numbers already dialed in earlier runs, read from the results sheet (and any fallback copies)."""
    path = Path(results_file)
    files: List[Path] = []
    if path.is_file():
        files.append(path)
    if path.parent.is_dir():
        for fallback in sorted(path.parent.glob(f"{path.stem}_[0-9]*{path.suffix}")):
            if fallback.is_file() and fallback not in files:
                files.append(fallback)
    if not files:
        return set()

    from openpyxl import load_workbook
    from results_store import SHEET_NAME

    wanted = set(statuses)
    called: Set[str] = set()
    for file_path in files:
        wb = load_workbook(file_path, read_only=True, data_only=True)
        try:
            ws = wb[SHEET_NAME] if SHEET_NAME in wb.sheetnames else wb.active
            rows = ws.iter_rows(values_only=True)
            header = list(next(rows, []) or [])
            if "status" not in header or "phone_e164" not in header:
                continue
            si, pi = header.index("status"), header.index("phone_e164")
            for r in rows:
                if len(r) > max(si, pi) and r[si] in wanted and r[pi]:
                    called.add(str(r[pi]))
        finally:
            wb.close()
    return called


class NumberVerifier:
    """Validates phone numbers; drops non-US, invalid, special, already-called and duplicate ones."""

    def __init__(
        self,
        allowed_regions: Optional[Iterable[str]] = None,
        already_called: Iterable[str] = (),
    ) -> None:
        regions = allowed_regions if allowed_regions is not None else config.ALLOWED_PHONE_REGIONS
        self.allowed_regions: FrozenSet[str] = frozenset(r.strip().upper() for r in regions if r.strip())
        self.already_called: Set[str] = set(already_called)
        self._seen: Set[str] = set()

    def mark_called(self, e164: str) -> None:
        """Record a number as already called in this process (for long-running streams)."""
        if e164:
            self.already_called.add(e164)
            self._seen.add(e164)

    def check_number(self, number: Any) -> Tuple[Optional[str], str]:
        """
        Validate one number (a parsed PhoneNumber or text).
        Returns (e164, "ok") or (None, drop reason).
        """
        if not isinstance(number, phonenumbers.PhoneNumber):
            try:
                number = phonenumbers.parse(str(number), "US")
            except NumberParseException:
                return None, "dropped_unreadable_number"
        if not phonenumbers.is_valid_number(number):
            return None, "dropped_invalid_number"
        region = phonenumbers.region_code_for_number(number)
        if region not in self.allowed_regions:
            return None, f"dropped_not_us ({region or '+' + str(number.country_code)})"
        number_type = phonenumbers.number_type(number)
        if number_type not in CALLABLE_TYPES:
            return None, f"dropped_special_number ({_TYPE_NAMES.get(number_type, number_type)})"
        return phonenumbers.format_number(number, PhoneNumberFormat.E164), "ok"

    def verify(self, lead: Dict[str, Any]) -> Verification:
        phone_fields, fax_texts = _collect_number_fields(lead)
        raw = (phone_fields[0][1] if len(phone_fields) == 1
               else " | ".join(f"{k}: {v}" for k, v in phone_fields))
        if not phone_fields:
            return Verification(False, raw, None, "dropped_fax_only" if fax_texts else "dropped_no_phone")

        best_reason: Optional[str] = None

        def note(reason: str) -> None:
            nonlocal best_reason
            rank = _REASON_RANK.get(reason.split(" ")[0], 0)
            if best_reason is None or rank > _REASON_RANK.get(best_reason.split(" ")[0], 0):
                best_reason = reason

        # Every fax number anywhere in the lead: from fax fields and fax labels
        fax_numbers: Set[str] = set()
        for text in fax_texts:
            for number, _ in extract_labelled_numbers(text)[0]:
                e164 = _e164_if_valid(number)
                if e164:
                    fax_numbers.add(e164)

        candidates: List[str] = []
        for _field, text in phone_fields:
            numbers, failure = extract_labelled_numbers(text)
            if failure:
                note(failure)
            for number, is_fax in numbers:
                if is_fax:
                    e164 = _e164_if_valid(number)
                    if e164:
                        fax_numbers.add(e164)
                    note("dropped_fax_only")
                    continue
                e164, reason = self.check_number(number)
                if e164 is None:
                    note(reason)
                elif e164 not in candidates:
                    candidates.append(e164)

        # A number listed as fax anywhere in the lead is never called
        callable_numbers = [n for n in candidates if n not in fax_numbers]
        if len(callable_numbers) < len(candidates):
            note("dropped_fax_only")

        if not callable_numbers:
            return Verification(False, raw, None, best_reason or "dropped_unreadable_number")

        # Repeat protection: any matching number blocks the whole lead (same business)
        called = next((n for n in callable_numbers if n in self.already_called), None)
        seen = next((n for n in callable_numbers if n in self._seen), None)
        self._seen.update(callable_numbers)
        if called:
            return Verification(False, raw, called, "dropped_already_called", *number_location(called))
        if seen:
            return Verification(False, raw, seen, "dropped_duplicate", *number_location(seen))
        chosen = callable_numbers[0]
        return Verification(True, raw, chosen, "ok", *number_location(chosen))
