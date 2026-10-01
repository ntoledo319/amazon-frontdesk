"""MCP protocol tests: JSON-RPC 2.0 dispatch against MCPCore (no HTTP)."""

import json

import pytest

from amazon_frontdesk.mcp_server import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    PROTOCOL_VERSION,
    MCPCore,
    dispatch,
)
from amazon_frontdesk.tools import FrontdeskTools


@pytest.fixture()
def core(business) -> MCPCore:
    return MCPCore(FrontdeskTools(business))


def _req(method, params=None, req_id=1):
    msg = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def test_initialize_returns_protocol_capabilities_serverinfo(core):
    resp, session = core.handle(_req("initialize", {"protocolVersion": PROTOCOL_VERSION}))
    assert session  # a session id was minted
    assert core.has_session(session)
    result = resp["result"]
    assert result["protocolVersion"] == PROTOCOL_VERSION
    assert "tools" in result["capabilities"]
    assert result["serverInfo"]["name"] == "amazon-frontdesk"
    assert resp["id"] == 1 and resp["jsonrpc"] == "2.0"


def test_tools_list_exposes_three_tools_with_schemas(core):
    resp, _ = core.handle(_req("tools/list"))
    tools = {t["name"]: t for t in resp["result"]["tools"]}
    assert set(tools) == {"frontdesk_answer", "frontdesk_book", "frontdesk_business_info"}
    for tool in tools.values():
        assert tool["description"]
        assert tool["inputSchema"]["type"] == "object"
    assert "message" in tools["frontdesk_answer"]["inputSchema"]["required"]
    assert tools["frontdesk_book"]["inputSchema"]["required"] == ["name", "service", "date", "time"]


def test_tools_call_frontdesk_answer_happy_path(core):
    resp, _ = core.handle(
        _req("tools/call", {"name": "frontdesk_answer", "arguments": {"message": "Hi, this is Dana"}})
    )
    result = resp["result"]
    assert result["content"][0]["type"] == "text"
    data = result["structuredContent"]
    assert data["conversation_id"]
    assert business_greeting(data["reply"])
    assert data["done"] is False
    assert data["lead"]["name"] == "Dana"


def business_greeting(reply: str) -> bool:
    return "Toledo Shine Mobile Detailing" in reply


def test_tools_call_business_info(core):
    resp, _ = core.handle(_req("tools/call", {"name": "frontdesk_business_info", "arguments": {}}))
    info = resp["result"]["structuredContent"]
    assert info["business"] == "Toledo Shine Mobile Detailing"
    assert len(info["services"]) == 4
    assert info["services"][0]["price_from"] == 120.0


def test_tools_call_unknown_tool_is_invalid_params(core):
    resp, _ = core.handle(_req("tools/call", {"name": "frontdesk_nope", "arguments": {}}))
    assert resp["error"]["code"] == INVALID_PARAMS
    assert "unknown tool" in resp["error"]["message"]


def test_tools_call_missing_name_is_invalid_params(core):
    resp, _ = core.handle(_req("tools/call", {"arguments": {}}))
    assert resp["error"]["code"] == INVALID_PARAMS


def test_tool_validation_error_is_iserror_result_not_rpc_error(core):
    resp, _ = core.handle(
        _req("tools/call", {"name": "frontdesk_book", "arguments": {"name": "Dana", "service": "flux polish", "date": "2026-10-02", "time": "14:00"}})
    )
    result = resp["result"]
    assert "error" not in resp
    assert result["isError"] is True
    assert "unknown service" in result["structuredContent"]["error"]


def test_unknown_method_is_method_not_found(core):
    resp, _ = core.handle(_req("resources/list"))
    assert resp["error"]["code"] == METHOD_NOT_FOUND


def test_missing_jsonrpc_version_is_invalid_request(core):
    resp, _ = core.handle({"id": 9, "method": "ping"})
    assert resp["error"]["code"] == INVALID_REQUEST


def test_ping_returns_empty_result(core):
    resp, _ = core.handle(_req("ping"))
    assert resp == {"jsonrpc": "2.0", "id": 1, "result": {}}


def test_notification_produces_no_response(core):
    resp, session = core.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert resp is None and session is None


def test_dispatch_parse_error(core):
    status, _, body = dispatch(core, b"{not json")
    assert status == 400
    assert json.loads(body)["error"]["code"] == PARSE_ERROR


def test_dispatch_batch_and_notification_only(core):
    batch = [
        _req("ping", req_id=1),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        _req("tools/list", req_id=2),
    ]
    status, _, body = dispatch(core, json.dumps(batch).encode())
    assert status == 200
    responses = json.loads(body)
    assert [r["id"] for r in responses] == [1, 2]

    status, _, body = dispatch(core, json.dumps([{"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {}}]).encode())
    assert status == 202 and body == b""


def test_dispatch_initialize_sets_session_header(core):
    status, headers, body = dispatch(core, json.dumps(_req("initialize", {})).encode())
    assert status == 200
    assert headers["Mcp-Session-Id"]
    assert core.has_session(headers["Mcp-Session-Id"])
