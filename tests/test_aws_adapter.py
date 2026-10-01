"""AWS adapter tests: Lambda handler (real dispatch) and the Bedrock stub."""

import json

import pytest

from amazon_frontdesk.aws_adapter import (
    BedrockExtractor,
    BedrockNotConfiguredError,
    build_extraction_prompt,
    lambda_handler,
)
from amazon_frontdesk.mcp_server import MCPCore
from amazon_frontdesk.tools import FrontdeskTools


def test_build_extraction_prompt(business):
    prompt = build_extraction_prompt("I'm Dana, book me Friday at 2", business, "2026-09-30")
    assert "Toledo Shine Mobile Detailing" in prompt
    assert "Full Interior Detail" in prompt
    assert "today=2026-09-30" in prompt
    assert "I'm Dana, book me Friday at 2" in prompt


def test_bedrock_extractor_raises_clear_error_without_boto3(business):
    """No boto3 in this environment: the stub must fail loudly, never fake."""
    pytest.importorskip("importlib.util")
    import importlib.util

    if importlib.util.find_spec("boto3") is not None:
        pytest.skip("boto3 installed; stub path not exercised")
    extractor = BedrockExtractor(business)
    with pytest.raises(BedrockNotConfiguredError, match="boto3"):
        extractor("book me Friday")


def test_lambda_handler_initialize(business):
    core = MCPCore(FrontdeskTools(business))
    event = {
        "body": json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
        "isBase64Encoded": False,
    }
    resp = lambda_handler(event, None, core=core)
    assert resp["statusCode"] == 200
    assert resp["headers"]["Content-Type"] == "application/json"
    result = json.loads(resp["body"])["result"]
    assert result["serverInfo"]["name"] == "amazon-frontdesk"


def test_lambda_handler_tool_call_and_parse_error(business):
    core = MCPCore(FrontdeskTools(business))
    event = {
        "body": json.dumps({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "frontdesk_business_info", "arguments": {}},
        })
    }
    resp = lambda_handler(event, None, core=core)
    info = json.loads(resp["body"])["result"]["structuredContent"]
    assert info["business"] == "Toledo Shine Mobile Detailing"

    resp = lambda_handler({"body": "{broken"}, None, core=core)
    assert resp["statusCode"] == 400
    assert json.loads(resp["body"])["error"]["code"] == -32700
