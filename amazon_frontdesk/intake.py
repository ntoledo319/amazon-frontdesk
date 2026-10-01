"""Intake domain logic: business catalog, lead model, and spoken-field parsing.

Ported verbatim (MIT, same author) from the frontdesk-voice-agent project
(``frontdesk-voice-agent/src/frontdesk/intake.py``) so this package is
self-contained and the original repo stays untouched.

Everything in this module is pure Python with no network or SDK imports, so it
is fully unit-testable. Date parsing takes an explicit ``today`` argument so
tests are deterministic.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Business catalog
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Service:
    name: str
    price_from: float
    duration_min: int
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class Business:
    name: str
    agent_name: str
    services: tuple[Service, ...]

    @classmethod
    def from_json(cls, path: str | Path) -> "Business":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        services = tuple(
            Service(
                name=s["name"],
                price_from=float(s["price_from"]),
                duration_min=int(s["duration_min"]),
                aliases=tuple(a.lower() for a in s.get("aliases", [])),
            )
            for s in raw["services"]
        )
        if not services:
            raise ValueError("business config must define at least one service")
        return cls(
            name=raw["name"],
            agent_name=raw.get("agent_name", "Riley"),
            services=services,
        )

    def service_menu(self) -> str:
        return ", ".join(
            f"{s.name} (from ${s.price_from:.0f})" for s in self.services
        )


# ---------------------------------------------------------------------------
# Lead model
# ---------------------------------------------------------------------------

LEAD_FIELDS = ("name", "phone", "service", "date", "time")
CSV_HEADER = (
    "captured_at",
    "business",
    "name",
    "phone",
    "service",
    "date",
    "time",
    "address",
    "notes",
)


@dataclass
class Lead:
    business: str
    name: str | None = None
    phone: str | None = None
    service: str | None = None
    date: str | None = None  # ISO YYYY-MM-DD
    time: str | None = None  # 24h HH:MM
    address: str | None = None
    notes: str | None = None

    def missing_fields(self) -> list[str]:
        return [f for f in LEAD_FIELDS if getattr(self, f) in (None, "")]

    def is_complete(self) -> bool:
        return not self.missing_fields()

    def to_row(self, captured_at: str) -> list[str]:
        return [
            captured_at,
            self.business,
            self.name or "",
            self.phone or "",
            self.service or "",
            self.date or "",
            self.time or "",
            self.address or "",
            self.notes or "",
        ]

    def summary(self) -> str:
        parts = [
            f"Name: {self.name}",
            f"Phone: {self.phone}",
            f"Service: {self.service}",
            f"Date: {self.date}",
            f"Time: {self.time}",
        ]
        if self.address:
            parts.append(f"Address: {self.address}")
        if self.notes:
            parts.append(f"Notes: {self.notes}")
        return "; ".join(parts)


# ---------------------------------------------------------------------------
# Spoken-field parsing
# ---------------------------------------------------------------------------

_WORD_NUM = {
    "zero": 0, "oh": 0, "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
}

_WORD_HOUR = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}

_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def _spoken_digit_runs(text: str) -> list[str]:
    """Collapse consecutive spoken digit words/numbers into digit strings.

    A run breaks at any non-digit token, so 'at 2 pm, call 555 214 8690'
    yields ['2', '5552148690'] rather than one merged bogus number.
    """
    tokens = re.findall(r"[a-z]+|\d+", text.lower())
    runs: list[str] = []
    current: list[str] = []
    for tok in tokens:
        if tok.isdigit():
            current.append(tok)
        elif tok in _WORD_NUM:
            current.append(str(_WORD_NUM[tok]))
        elif current:
            runs.append("".join(current))
            current = []
    if current:
        runs.append("".join(current))
    return runs


def normalize_phone(text: str) -> str | None:
    """Extract a US phone number from speech, formatted '(XXX) XXX-XXXX'.

    Handles digit strings ('555-123-4567'), spoken digits ('five five five,
    one two three, four five six seven'), and 11-digit numbers with a
    leading country code 1.
    """
    candidates = [r for r in _spoken_digit_runs(text) if 10 <= len(r) <= 11]
    if not candidates:
        return None
    number = max(candidates, key=len)
    if len(number) == 11 and number.startswith("1"):
        number = number[1:]
    if len(number) != 10:
        return None
    return f"({number[0:3]}) {number[3:6]}-{number[6:10]}"


def parse_date(text: str, today: date) -> str | None:
    """Parse a spoken date into ISO format relative to ``today``."""
    t = text.lower()

    if "day after tomorrow" in t:
        return (today + timedelta(days=2)).isoformat()
    if re.search(r"\btomorrow\b", t):
        return (today + timedelta(days=1)).isoformat()
    if re.search(r"\btoday\b", t):
        return today.isoformat()

    # 'next Friday' / 'this Friday' / bare 'Friday'
    m = re.search(r"\b(?:(next|this)\s+)?(" + "|".join(_WEEKDAYS) + r")\b", t)
    if m:
        qualifier = m.group(1)
        weekday = _WEEKDAYS[m.group(2)]
        delta = (weekday - today.weekday()) % 7
        if qualifier == "next":
            delta += 7
        elif qualifier == "this":
            pass  # same week; delta == 0 means today
        elif delta == 0:
            delta = 7  # bare weekday on that weekday means a week out
        return (today + timedelta(days=delta)).isoformat()

    # 'September 26' / 'September 26th'
    month_alt = "|".join(_MONTHS)
    m = re.search(rf"\b({month_alt})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", t)
    if m:
        month = _MONTHS[m.group(1)]
        day = int(m.group(2))
        try:
            target = date(today.year, month, day)
        except ValueError:
            return None
        if target < today:
            target = date(today.year + 1, month, day)
        return target.isoformat()

    # '9/26' or '09-26'
    m = re.search(r"\b(\d{1,2})[/\-](\d{1,2})\b", t)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        try:
            target = date(today.year, month, day)
        except ValueError:
            return None
        if target < today:
            target = date(today.year + 1, month, day)
        return target.isoformat()

    return None


def parse_time(text: str) -> str | None:
    """Parse a spoken time into 24h 'HH:MM'."""
    t = text.lower()

    if re.search(r"\bnoon\b", t):
        return "12:00"
    if re.search(r"\bmidnight\b", t):
        return "00:00"

    # '2:30 pm' / '14:30'
    m = re.search(r"\b(\d{1,2}):(\d{2})\s*(a\.?m\.?|p\.?m\.?)?\b", t)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        meridiem = (m.group(3) or "").replace(".", "")
        if meridiem == "pm" and hour < 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{hour:02d}:{minute:02d}"
        return None

    # '2 pm' / 'two thirty pm' / '3 o'clock' / '2 in the afternoon'
    # A meridiem or daypart cue is required: a bare hour is ambiguous.
    hour_words = "|".join(_WORD_HOUR)
    m = re.search(
        rf"\b(\d{{1,2}}|{hour_words})"
        r"(?:\s+(thirty|fifteen|forty[- ]five|oh\s+\w+|\d{2}))?"
        r"(?:\s*(a\.?m\.?|p\.?m\.?|o'clock)|\s+in\s+the\s+(morning|afternoon|evening))\b",
        t,
    )
    if not m:
        return None

    raw_hour = m.group(1)
    hour = int(raw_hour) if raw_hour.isdigit() else _WORD_HOUR[raw_hour]
    minute = 0
    if m.group(2):
        minute = _word_minute(m.group(2).replace("-", " "))
        if minute is None:
            return None
    meridiem = (m.group(3) or "").replace(".", "")
    daypart = m.group(4)

    if meridiem == "pm" or daypart in ("afternoon", "evening"):
        if hour < 12:
            hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0

    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return f"{hour:02d}:{minute:02d}"
    return None


def _word_minute(text: str) -> int | None:
    text = text.strip()
    if text.isdigit():
        return int(text)
    named = {"thirty": 30, "fifteen": 15, "forty five": 45}
    if text in named:
        return named[text]
    if text.startswith("oh "):
        tail = text[3:].strip()
        if tail.isdigit():
            return int(tail)
        if tail in _WORD_NUM:
            return _WORD_NUM[tail]
    return None


def match_service(text: str, services: tuple[Service, ...]) -> Service | None:
    """Match an utterance against the service catalog (name or alias)."""
    t = text.lower()
    best: Service | None = None
    best_len = 0
    for svc in services:
        for needle in (svc.name.lower(), *svc.aliases):
            if needle and needle in t and len(needle) > best_len:
                best, best_len = svc, len(needle)
    return best


_NAME_RE = re.compile(
    r"\b(?:my name is|this is|i am|i'm|it's|its)\s+([a-z]+(?:\s+[a-z]+)?)\b",
    re.IGNORECASE,
)
_NAME_STOPWORDS = {
    "calling", "here", "looking", "interested", "wondering", "hoping",
    "the", "a", "an", "not", "just", "about", "for",
}


def extract_name(text: str) -> str | None:
    m = _NAME_RE.search(text)
    if not m:
        return None
    words = [w for w in m.group(1).split() if w.lower() not in _NAME_STOPWORDS]
    if not words:
        return None
    return " ".join(w.capitalize() for w in words[:3])


_ADDRESS_RE = re.compile(
    r"\b(?:at|address is|located at|i live at)\s+"
    r"(\d{1,6}\s+[a-z0-9 .'-]{2,50}?"
    r"(?:street|st|avenue|ave|road|rd|lane|ln|drive|dr|boulevard|blvd|way|court|ct)"
    r"\b[a-z0-9 .'-]{0,20})",
    re.IGNORECASE,
)


def extract_fields(
    text: str, services: tuple[Service, ...], today: date
) -> dict[str, str]:
    """Extract every intake field present in one caller utterance."""
    found: dict[str, str] = {}
    name = extract_name(text)
    if name:
        found["name"] = name
    phone = normalize_phone(text)
    if phone:
        found["phone"] = phone
    svc = match_service(text, services)
    if svc:
        found["service"] = svc.name
    day = parse_date(text, today)
    if day:
        found["date"] = day
    tm = parse_time(text)
    if tm:
        found["time"] = tm
    addr = _ADDRESS_RE.search(text)
    if addr:
        found["address"] = addr.group(1).strip().title()
    return found
