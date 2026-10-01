"""MCP tool surface for the frontdesk agent.

Three tools are exposed over MCP (see ``mcp_server.py``):

- ``frontdesk_answer`` — drive one turn of the intake dialogue (text in,
  agent reply out). Conversations are stateful, keyed by ``conversation_id``.
- ``frontdesk_book`` — book an appointment directly from structured slot
  values, for agents that already extracted them.
- ``frontdesk_business_info`` — the business catalog (name, agent, services,
  prices, durations) straight from ``business.json``.

Bookings are kept in memory and optionally appended to a JSONL file so the
demo has a durable artifact without any external service.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .dialogue import IntakeAgent
from .intake import Business, match_service

ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ISO_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class ToolError(Exception):
    """A tool-level failure, surfaced to the caller as an MCP isError result."""


def _text_result(payload: dict, *, is_error: bool = False) -> dict:
    """Build an MCP tool result: human-readable text + structured content."""
    result = {
        "content": [{"type": "text", "text": json.dumps(payload, indent=2)}],
        "structuredContent": payload,
    }
    if is_error:
        result["isError"] = True
    return result


class FrontdeskTools:
    """Stateful tool implementations backing the MCP server."""

    def __init__(self, business: Business, bookings_path: str | Path | None = None) -> None:
        self.business = business
        self._conversations: dict[str, IntakeAgent] = {}
        self._booked_conversations: set[str] = set()
        self._bookings: list[dict] = []
        self._lock = threading.Lock()
        self._bookings_path = Path(bookings_path) if bookings_path else None

    # -- MCP metadata --------------------------------------------------------

    def schemas(self) -> list[dict]:
        """Tool descriptors for the MCP ``tools/list`` method."""
        return [
            {
                "name": "frontdesk_answer",
                "description": (
                    "Send one caller utterance to the frontdesk intake agent for "
                    f"{self.business.name} and get the agent's spoken reply. The "
                    "agent walks the caller through name, phone, service, date "
                    "and time, confirms, and completes the booking. Pass the same "
                    "conversation_id across turns to continue a conversation; omit "
                    "it to start a new one."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "message": {
                            "type": "string",
                            "description": "What the caller said this turn.",
                        },
                        "conversation_id": {
                            "type": "string",
                            "description": (
                                "Conversation handle returned by a previous call. "
                                "Omit to start a new conversation."
                            ),
                        },
                    },
                    "required": ["message"],
                },
            },
            {
                "name": "frontdesk_book",
                "description": (
                    "Book an appointment directly from structured slot values. Use "
                    "this when the caller's details are already known. The service "
                    "is matched against the business catalog; unknown services are "
                    "rejected."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Customer name."},
                        "service": {
                            "type": "string",
                            "description": "Service name or alias, e.g. 'interior detail'.",
                        },
                        "date": {
                            "type": "string",
                            "description": "Appointment date, ISO YYYY-MM-DD.",
                        },
                        "time": {
                            "type": "string",
                            "description": "Appointment start time, 24h HH:MM.",
                        },
                        "phone": {
                            "type": "string",
                            "description": "Callback phone number (optional).",
                        },
                    },
                    "required": ["name", "service", "date", "time"],
                },
            },
            {
                "name": "frontdesk_business_info",
                "description": (
                    "Get the business profile the frontdesk agent works from: "
                    "business name, agent name, and the service catalog with "
                    "starting prices and durations."
                ),
                "inputSchema": {"type": "object", "properties": {}},
            },
        ]

    # -- dispatch --------------------------------------------------------------

    def call(self, name: str, arguments: dict | None) -> dict:
        """Execute a tool and return an MCP tool result object."""
        arguments = arguments or {}
        handlers = {
            "frontdesk_answer": self._answer,
            "frontdesk_book": self._book,
            "frontdesk_business_info": self._business_info,
        }
        handler = handlers.get(name)
        if handler is None:
            raise KeyError(name)
        try:
            return handler(arguments)
        except ToolError as exc:
            return _text_result({"error": str(exc)}, is_error=True)

    # -- tool implementations --------------------------------------------------

    def _answer(self, args: dict) -> dict:
        message = args.get("message")
        if not isinstance(message, str):
            raise ToolError("'message' must be a string")
        conversation_id = args.get("conversation_id")
        with self._lock:
            if conversation_id is None:
                conversation_id = uuid.uuid4().hex[:12]
            agent = self._conversations.get(conversation_id)
            is_new = agent is None
            if is_new:
                agent = IntakeAgent(self.business)
                self._conversations[conversation_id] = agent

            if is_new:
                greeting = agent.greeting()
                reply = greeting if not message.strip() else f"{greeting} {agent.handle(message)}"
            else:
                reply = agent.handle(message)

            booking = None
            if agent.done and conversation_id not in self._booked_conversations:
                booking = self._register_booking(
                    {
                        "name": agent.lead.name,
                        "phone": agent.lead.phone,
                        "service": agent.lead.service,
                        "date": agent.lead.date,
                        "time": agent.lead.time,
                        "source": "conversation",
                        "conversation_id": conversation_id,
                    }
                )
                self._booked_conversations.add(conversation_id)

            payload = {
                "conversation_id": conversation_id,
                "reply": reply,
                "done": agent.done,
                "lead": {
                    "name": agent.lead.name,
                    "phone": agent.lead.phone,
                    "service": agent.lead.service,
                    "date": agent.lead.date,
                    "time": agent.lead.time,
                },
            }
            if booking:
                payload["booking"] = booking
            return _text_result(payload)

    def _book(self, args: dict) -> dict:
        missing = [k for k in ("name", "service", "date", "time") if not args.get(k)]
        if missing:
            raise ToolError(f"missing required fields: {', '.join(missing)}")
        service = match_service(str(args["service"]), self.business.services)
        if service is None:
            raise ToolError(
                f"unknown service '{args['service']}'; catalog: "
                f"{self.business.service_menu()}"
            )
        date_str = str(args["date"])
        if not ISO_DATE_RE.match(date_str):
            raise ToolError(f"'date' must be ISO YYYY-MM-DD, got '{date_str}'")
        time_str = str(args["time"])
        if not ISO_TIME_RE.match(time_str):
            raise ToolError(f"'time' must be 24h HH:MM, got '{time_str}'")
        booking = self._register_booking(
            {
                "name": str(args["name"]),
                "phone": str(args.get("phone") or "") or None,
                "service": service.name,
                "date": date_str,
                "time": time_str,
                "source": "direct",
            }
        )
        return _text_result(
            {
                "booking": booking,
                "confirmation": (
                    f"Booked {booking['name']} for {booking['service']} on "
                    f"{booking['date']} at {booking['time']} "
                    f"({booking['booking_id']})."
                ),
            }
        )

    def _business_info(self, _args: dict) -> dict:
        return _text_result(
            {
                "business": self.business.name,
                "agent_name": self.business.agent_name,
                "services": [
                    {
                        "name": s.name,
                        "price_from": s.price_from,
                        "duration_min": s.duration_min,
                        "aliases": list(s.aliases),
                    }
                    for s in self.business.services
                ],
            }
        )

    # -- bookings ----------------------------------------------------------------

    def _register_booking(self, fields: dict) -> dict:
        booking = {
            "booking_id": f"BK-{len(self._bookings) + 1:04d}",
            "business": self.business.name,
            "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **fields,
        }
        self._bookings.append(booking)
        if self._bookings_path:
            with self._bookings_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(booking) + "\n")
        return booking

    @property
    def bookings(self) -> list[dict]:
        return list(self._bookings)
