"""
Helpers for reading unstructured lead dicts.

Leads can arrive with any field names ("Phone(s)", "phoneNumber", "Tel",
"contact_info"), any nesting (dicts inside dicts, lists of dicts) and JSON
stored as text. Nothing here relies on fixed field names: field names are
split into words ("faxNumber" -> ["fax", "number"]) and classified by those
words, and nested values are walked recursively.

Used by the number verifier (finding phone and fax numbers) and the decision
maker (building the clean, descriptive input Gemini sees).
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterator, List, Tuple

_CAMEL = re.compile(r"([a-z0-9])([A-Z])")

# Words that mark a field as holding phone numbers
PHONE_WORDS = frozenset({
    "phone", "phones", "telephone", "telephones", "tel", "mobile", "mobiles",
    "cell", "cellphone", "cellular", "landline", "ph",
})
# Words that mark a field as holding fax numbers (never called)
FAX_WORDS = frozenset({"fax", "faxes", "telefax", "facsimile"})
# Words that mark a field as holding e-mail addresses
EMAIL_WORDS = frozenset({"email", "emails", "mail", "mails"})

# Values that mean "nothing here"
PLACEHOLDERS = frozenset({
    "", "-", "--", "—", "none", "null", "nil", "n/a", "na", "nan", "unknown",
    "not specified", "not available", "unspecified", "unassigned", "default",
    "no email address", "no phone number", "no website", "tbd", "?",
})

PathT = Tuple[str, ...]


def key_words(key: Any) -> List[str]:
    """Split a field name into lowercase words: 'faxNumber' -> ['fax', 'number'], 'Phone(s)' -> ['phone', 's']."""
    text = _CAMEL.sub(r"\1 \2", str(key))
    return re.findall(r"[a-z]+|\d+", text.lower())


def is_fax_key(key: Any) -> bool:
    return any(w in FAX_WORDS or w.startswith("fax") for w in key_words(key))


def is_phone_key(key: Any) -> bool:
    """True if a field name looks like a phone field (and not a fax field)."""
    words = key_words(key)
    if not words or is_fax_key(key):
        return False
    if any(w in PHONE_WORDS or w.startswith("phone") or w.endswith("phone") for w in words):
        return True
    # "number", "contact number(s)", "contact no"
    return words in (["number"], ["numbers"], ["contact", "number"], ["contact", "numbers"],
                     ["contact", "no"], ["contact", "nos"])


def is_email_key(key: Any) -> bool:
    words = key_words(key)
    return any(w in EMAIL_WORDS or w.startswith("email") for w in words) or words[-2:] == ["e", "mail"]


def is_placeholder(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip().lower() in PLACEHOLDERS)


def parse_json_text(value: Any) -> Any:
    """Turn JSON stored as text ('{"a": 1}' or '[...]') into the real structure; anything else unchanged."""
    if isinstance(value, str):
        s = value.strip()
        if len(s) >= 2 and ((s[0] == "{" and s[-1] == "}") or (s[0] == "[" and s[-1] == "]")):
            try:
                return json.loads(s)
            except ValueError:
                pass
    return value


def walk_scalars(value: Any, path: PathT = ()) -> Iterator[Tuple[PathT, Any]]:
    """Yield (key path, value) for every non-container value in a nested structure."""
    value = parse_json_text(value)
    if isinstance(value, dict):
        for k, v in value.items():
            yield from walk_scalars(v, path + (str(k),))
    elif isinstance(value, (list, tuple, set)):
        for v in value:
            yield from walk_scalars(v, path)
    else:
        yield path, value


def cell_text(value: Any) -> str:
    """A scalar as clean text (spreadsheets store numbers as floats: 14155550199.0 -> '14155550199')."""
    if isinstance(value, bool):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, int):
        return str(value)
    return str(value).strip() if value is not None else ""
