"""Simulator tests: the Alexa+ orchestration path and the web app itself."""

import json
import threading
import urllib.error
import urllib.request

import pytest

from amazon_frontdesk.intake import Business
from amazon_frontdesk.simulator import AlexaSimulator, make_sim_handler
from http.server import ThreadingHTTPServer

from conftest import BUSINESS_JSON


@pytest.fixture()
def sim(mcp_server) -> AlexaSimulator:
    return AlexaSimulator(mcp_server["url"])


def test_simulator_first_turn_is_greeting(sim):
    turn = sim.chat("")
    assert "Toledo Shine Mobile Detailing" in turn["reply"]
    assert turn["done"] is False
    assert turn["tool_calls"][0]["tool"] == "frontdesk_answer"


def test_simulator_full_conversation_produces_booking(sim):
    sim.chat("")
    booking = None
    for utterance in [
        "this is Dana",
        "555 214 8690",
        "interior detail please",
        "this Friday",
        "2 pm",
        "yes",
    ]:
        turn = sim.chat(utterance)
        assert "error" not in turn
        assert turn["tool_calls"], "every turn must show its MCP tool call"
        assert turn["tool_calls"][0]["tool"] == "frontdesk_answer"
        assert "reply=" in turn["tool_calls"][0]["result"]
        booking = turn["booking"] or booking
    assert turn["done"] is True
    assert booking is not None
    assert booking["name"] == "Dana" and booking["service"] == "Full Interior Detail"


def test_simulator_reset_starts_new_conversation(sim):
    sim.chat("")
    first = sim.conversation_id
    sim.reset()
    assert sim.conversation_id != first


def test_simulator_reports_error_when_mcp_server_is_down(mcp_server):
    down = AlexaSimulator(mcp_server["url"])
    mcp_server["server"].shutdown()
    mcp_server["server"].server_close()
    turn = down.chat("hello?")
    assert turn["reply"] == "" and "error" in turn


@pytest.fixture()
def sim_server(mcp_server):
    url = mcp_server["url"]
    business = Business.from_json(BUSINESS_JSON)
    handler = make_sim_handler(lambda: AlexaSimulator(url), url, business)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    thread.join(timeout=5)
    server.server_close()


def _post(url: str, payload: dict):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, json.loads(resp.read())


def test_sim_page_serves_html(sim_server):
    with urllib.request.urlopen(sim_server + "/", timeout=10) as resp:
        html = resp.read().decode()
    assert resp.status == 200
    assert "Alexa+ Simulator" in html
    assert "/chat" in html


def test_sim_info_endpoint(sim_server):
    with urllib.request.urlopen(sim_server + "/info", timeout=10) as resp:
        info = json.loads(resp.read())
    assert info["business"] == "Toledo Shine Mobile Detailing"
    assert info["mcp_url"].endswith("/mcp")


def test_sim_chat_endpoint_round_trip(sim_server):
    status, data = _post(sim_server + "/chat", {"message": "Hi, this is Dana"})
    assert status == 200
    assert "Dana" in data["reply"] or "Toledo Shine" in data["reply"]
    assert data["tool_calls"][0]["tool"] == "frontdesk_answer"

    status, data = _post(sim_server + "/reset", {})
    assert status == 200 and data["ok"] is True


def test_sim_chat_rejects_bad_payload(sim_server):
    req = urllib.request.Request(
        sim_server + "/chat", data=b'{"message": 42}',
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(req, timeout=10)
    assert excinfo.value.code == 400
