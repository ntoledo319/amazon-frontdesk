"""Dialogue state machine for the intake call.

Ported from the frontdesk-voice-agent project
(``frontdesk-voice-agent/src/frontdesk/dialogue.py``); the ``llm.py``
extractor hook mentioned below maps to ``aws_adapter.BedrockExtractor`` in
this package.

The agent walks the caller through the missing lead fields one at a time,
confirms the captured details, and finishes. Extraction is pluggable: the
default is the offline rule-based parser from ``intake``, and an LLM-backed
extractor can be injected without changing this machine.
"""

from __future__ import annotations

import re
from datetime import date
from enum import Enum, auto
from typing import Callable

from .intake import Business, Lead, extract_fields

# An extractor maps one caller utterance to any fields it can find.
Extractor = Callable[[str], dict[str, str]]


class State(Enum):
    NAME = auto()
    PHONE = auto()
    SERVICE = auto()
    DATETIME = auto()
    CONFIRM = auto()
    DONE = auto()


_FIELD_ORDER = {
    State.NAME: "name",
    State.PHONE: "phone",
    State.SERVICE: "service",
    State.DATETIME: "datetime",
}

_YES = re.compile(r"\b(yes|yeah|yep|correct|right|sure|confirm|sounds good|that's right)\b", re.I)
_NO = re.compile(r"\b(no|nope|wrong|incorrect|change|fix|actually)\b", re.I)


class IntakeAgent:
    """Voice intake agent for one call."""

    def __init__(
        self,
        business: Business,
        extractor: Extractor | None = None,
        today: date | None = None,
    ) -> None:
        self.business = business
        self.today = today or date.today()
        self.lead = Lead(business=business.name)
        self.state = State.NAME
        self._default_extractor: Extractor = (
            lambda text: extract_fields(text, business.services, self.today)
        )
        self._extractor = extractor
        self.history: list[tuple[str, str]] = []  # (speaker, text)

    # -- public API ---------------------------------------------------------

    @property
    def done(self) -> bool:
        return self.state is State.DONE

    def greeting(self) -> str:
        text = (
            f"Thanks for calling {self.business.name}, this is "
            f"{self.business.agent_name}. I can get you booked or get a quote "
            f"started. May I have your name?"
        )
        self.history.append(("agent", text))
        return text

    def handle(self, utterance: str) -> str:
        """Process one caller turn and return the agent's spoken reply."""
        utterance = utterance.strip()
        if not utterance:
            reply = "Sorry, I didn't catch that. Could you say it again?"
            self.history.append(("agent", reply))
            return reply
        self.history.append(("caller", utterance))

        if self.state is State.CONFIRM:
            reply = self._handle_confirmation(utterance)
        elif self.state is State.DONE:
            reply = "Is there anything else I can help you with?"
        else:
            self._fill_slots(utterance)
            reply = self._advance()

        self.history.append(("agent", reply))
        return reply

    # -- internals ----------------------------------------------------------

    def _extract(self, utterance: str) -> dict[str, str]:
        found = self._default_extractor(utterance)
        if self._extractor is not None:
            try:
                llm_found = self._extractor(utterance) or {}
            except Exception:
                llm_found = {}
            # LLM results fill gaps only; deterministic parsing wins ties.
            for key, value in llm_found.items():
                found.setdefault(key, value)
        return found

    def _fill_slots(self, utterance: str) -> None:
        found = self._extract(utterance)
        for key in ("name", "phone", "service", "date", "time", "address", "notes"):
            if key in found and getattr(self.lead, key) in (None, ""):
                setattr(self.lead, key, found[key])
        # Caller asked for their name often answers with a bare "Dana".
        if (
            self.lead.name is None
            and self.state is State.NAME
            and not found
        ):
            candidate = utterance.strip().strip(".,!")
            words = candidate.split()
            if 1 <= len(words) <= 3 and all(
                re.fullmatch(r"[A-Za-z'\-]+", w) for w in words
            ):
                self.lead.name = " ".join(w.capitalize() for w in words)
        # Anything the caller says while giving contact details that looks
        # like a request detail becomes a note.
        if self.lead.notes is None and re.search(
            r"\b(dog|pet|stain|scratch|smell|odor|suv|truck|sedan|van|seats?|leather)\b",
            utterance,
            re.I,
        ):
            self.lead.notes = utterance

    def _advance(self) -> str:
        while self.state is not State.CONFIRM:
            if self.state is State.NAME and not self.lead.name:
                return "May I have your name?"
            if self.state is State.NAME:
                self.state = State.PHONE
                continue
            if self.state is State.PHONE and not self.lead.phone:
                return f"Thanks {self.lead.name}. What's the best phone number to reach you?"
            if self.state is State.PHONE:
                self.state = State.SERVICE
                continue
            if self.state is State.SERVICE and not self.lead.service:
                return (
                    f"Got it. Which service are you interested in? We offer "
                    f"{self.business.service_menu()}."
                )
            if self.state is State.SERVICE:
                self.state = State.DATETIME
                continue
            if self.state is State.DATETIME:
                if not self.lead.date:
                    return "What day works best for you?"
                if not self.lead.time:
                    return "And what time of day?"
                self.state = State.CONFIRM
                continue
        return (
            "Let me confirm what I have. " + self.lead.summary() + ". Did I get everything right?"
        )

    def _handle_confirmation(self, utterance: str) -> str:
        if _NO.search(utterance):
            change = self._extract(utterance)
            changed = False
            for key in ("name", "phone", "service", "date", "time"):
                if key in change:
                    setattr(self.lead, key, change[key])
                    changed = True
            if not changed:
                # Ask which field to fix.
                field_m = re.search(
                    r"\b(name|phone|number|service|date|day|time)\b", utterance, re.I
                )
                if field_m:
                    which = field_m.group(1).lower()
                    which = {"number": "phone", "day": "date"}.get(which, which)
                    if which in ("name", "phone", "service", "date", "time"):
                        return self._reask(which)
                return (
                    "No problem. What should I change — your name, phone "
                    "number, service, date, or time?"
                )
            return "Updated. " + self.lead.summary() + ". Is everything correct now?"

        if _YES.search(utterance):
            self.state = State.DONE
            return (
                f"You're all set, {self.lead.name}. {self.business.name} will "
                f"call {self.lead.phone} to confirm your {self.lead.service} "
                f"on {self.lead.date} at {self.lead.time}. Thanks for calling!"
            )

        # Unclear answer: treat any extracted fields as corrections, else re-ask.
        change = self._extract(utterance)
        if any(k in change for k in ("name", "phone", "service", "date", "time")):
            for key in ("name", "phone", "service", "date", "time"):
                if key in change:
                    setattr(self.lead, key, change[key])
            return "Updated. " + self.lead.summary() + ". Is everything correct now?"
        return "Sorry — was that a yes or a no?"

    def _reask(self, field_name: str) -> str:
        setattr(self.lead, field_name, None)
        prompts = {
            "name": "Sure — what name should I put down?",
            "phone": "Sure — what's the correct phone number?",
            "service": (
                f"Sure — which service did you want? We offer "
                f"{self.business.service_menu()}."
            ),
            "date": "Sure — what day should I book?",
            "time": "Sure — what time works?",
        }
        return prompts[field_name]
