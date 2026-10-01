"""Brain adapter tests: the ported intake parser + dialogue state machine."""

from datetime import date

from amazon_frontdesk.dialogue import IntakeAgent, State
from amazon_frontdesk.intake import (
    match_service,
    normalize_phone,
    parse_date,
    parse_time,
)

TODAY = date(2026, 9, 30)  # a Wednesday


def test_business_loads_from_business_json(business):
    assert business.name == "Toledo Shine Mobile Detailing"
    assert business.agent_name == "Riley"
    assert len(business.services) == 4
    assert "Full Interior Detail (from $120)" in business.service_menu()


def test_normalize_phone_spoken_digits():
    assert normalize_phone("five five five, two one four, eight six nine zero") == "(555) 214-8690"
    assert normalize_phone("call me at 555-214-8690") == "(555) 214-8690"
    assert normalize_phone("no digits here") is None


def test_parse_date_relative_and_weekday():
    assert parse_date("tomorrow", TODAY) == "2026-10-01"
    assert parse_date("day after tomorrow", TODAY) == "2026-10-02"
    assert parse_date("this Friday", TODAY) == "2026-10-02"
    assert parse_date("next Friday", TODAY) == "2026-10-09"
    assert parse_date("October 23", TODAY) == "2026-10-23"


def test_parse_time_variants():
    assert parse_time("2 pm") == "14:00"
    assert parse_time("two thirty pm") == "14:30"
    assert parse_time("noon") == "12:00"
    assert parse_time("9:15 am") == "09:15"
    assert parse_time("sometime later") is None


def test_match_service_prefers_longest_alias(business):
    assert match_service("I need an interior detail", business.services).name == "Full Interior Detail"
    assert match_service("just a wash", business.services).name == "Exterior Wash & Wax"
    assert match_service("give me the works", business.services).name == "Full Detail Package"
    assert match_service("paint correction", business.services) is None


def _drive(agent: IntakeAgent, utterances: list[str]) -> list[str]:
    return [agent.handle(u) for u in utterances]


def test_dialogue_happy_path_reaches_done(business):
    agent = IntakeAgent(business, today=TODAY)
    greeting = agent.greeting()
    assert business.name in greeting and "name" in greeting.lower()

    replies = _drive(
        agent,
        [
            "Hi, this is Dana",
            "555 214 8690",
            "I want the interior detail",
            "this Friday",
            "2 pm",
            "yes",
        ],
    )
    assert agent.done
    assert agent.state is State.DONE
    lead = agent.lead
    assert (lead.name, lead.phone, lead.service) == ("Dana", "(555) 214-8690", "Full Interior Detail")
    assert lead.date == "2026-10-02" and lead.time == "14:00"
    assert "you're all set" in replies[-1].lower()
    assert "Did I get everything right" in replies[-2]


def test_dialogue_handles_correction(business):
    agent = IntakeAgent(business, today=TODAY)
    agent.greeting()
    _drive(agent, ["Dana", "555 214 8690", "interior detail", "this Friday", "2 pm"])
    reply = agent.handle("actually, make it 3 pm")
    assert "Updated" in reply and agent.lead.time == "15:00"
    agent.handle("yes")
    assert agent.done and agent.lead.time == "15:00"


def test_dialogue_reprompts_on_empty_utterance(business):
    agent = IntakeAgent(business, today=TODAY)
    agent.greeting()
    assert "didn't catch that" in agent.handle("   ")
    assert not agent.done
