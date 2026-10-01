"""End-to-end tests over live HTTP: session rules and the full booking flow."""

import json
import urllib.error
import urllib.request

import pytest

from amazon_frontdesk.mcp_client import MCPClient, MCPClientError
from conftest import read_bookings


def _raw_post(url: str, payload: dict, session: str | None = None):
    headers = {"Content-Type": "application/json"}
    if session:
        headers["Mcp-Session-Id"] = session
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def test_initialize_over_http_returns_session_header(mcp_server):
    status, headers, body = _raw_post(
        mcp_server["url"], {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    assert status == 200
    assert headers.get("Mcp-Session-Id")
    assert json.loads(body)["result"]["protocolVersion"] == "2025-03-26"


def test_request_without_session_is_rejected(mcp_server):
    status, _, body = _raw_post(mcp_server["url"], {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert status == 400
    assert "Mcp-Session-Id" in json.loads(body)["error"]["message"]


def test_unknown_session_is_404(mcp_server):
    status, _, _ = _raw_post(
        mcp_server["url"],
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        session="deadbeef" * 4,
    )
    assert status == 404


def test_get_returns_405(mcp_server):
    req = urllib.request.Request(mcp_server["url"], method="GET")
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(req, timeout=10)
    assert excinfo.value.code == 405


def test_tools_list_over_http(client):
    names = {t["name"] for t in client.list_tools()}
    assert names == {"frontdesk_answer", "frontdesk_book", "frontdesk_business_info"}


def test_unknown_tool_error_over_http(client):
    with pytest.raises(MCPClientError) as excinfo:
        client.call_tool("frontdesk_nope", {})
    assert excinfo.value.code == -32602


def test_full_booking_conversation_over_http(client, mcp_server):
    """The demo scenario: caller → agent dialogue → confirmed booking, all MCP."""
    convo = None
    script = [
        "Hi, this is Dana",
        "my number is 555 214 8690",
        "I want the interior detail",
        "this Friday",
        "2 pm",
        "yes, that's right",
    ]
    replies = []
    booking = None
    for utterance in script:
        data = client.call_tool_structured(
            "frontdesk_answer", {"message": utterance, **({"conversation_id": convo} if convo else {})}
        )
        convo = convo or data["conversation_id"]
        assert data["conversation_id"] == convo
        replies.append(data["reply"])
        if data.get("booking"):
            booking = data["booking"]

    assert booking is not None
    assert booking["booking_id"] == "BK-0001"
    assert booking["name"] == "Dana"
    assert booking["phone"] == "(555) 214-8690"
    assert booking["service"] == "Full Interior Detail"
    assert booking["source"] == "conversation"
    assert "Toledo Shine Mobile Detailing" in replies[0]  # greeting on first turn
    assert "Did I get everything right" in replies[-2]
    assert "you're all set" in replies[-1].lower()

    # The booking was also appended to the JSONL sink.
    on_disk = read_bookings(mcp_server["bookings_path"])
    assert [b["booking_id"] for b in on_disk] == ["BK-0001"]


def test_direct_booking_and_validation(client):
    ok = client.call_tool_structured(
        "frontdesk_book",
        {"name": "Sam", "service": "wash and wax", "date": "2026-10-05", "time": "09:30"},
    )
    assert ok["booking"]["service"] == "Exterior Wash & Wax"  # alias resolved
    assert ok["booking"]["booking_id"] == "BK-0001"
    assert "Booked Sam" in ok["confirmation"]

    with pytest.raises(MCPClientError, match="unknown service"):
        client.call_tool(
            "frontdesk_book",
            {"name": "Sam", "service": "hovercraft polish", "date": "2026-10-05", "time": "09:30"},
        )
    with pytest.raises(MCPClientError, match="ISO YYYY-MM-DD"):
        client.call_tool(
            "frontdesk_book",
            {"name": "Sam", "service": "wash", "date": "next Friday", "time": "09:30"},
        )
    with pytest.raises(MCPClientError, match="missing required fields"):
        client.call_tool("frontdesk_book", {"name": "Sam"})
