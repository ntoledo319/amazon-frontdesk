"""Minimal MCP client over Streamable HTTP, stdlib ``urllib`` only.

Used by the simulator web app and by the tests. Handles the session
handshake: ``initialize()`` captures the ``Mcp-Session-Id`` response header
and echoes it on every subsequent request.
"""

from __future__ import annotations

import itertools
import json
import urllib.error
import urllib.request

MCP_SESSION_HEADER = "Mcp-Session-Id"


class MCPClientError(Exception):
    """Transport-level failure (HTTP error, JSON-RPC error response)."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class MCPClient:
    def __init__(self, url: str, timeout: float = 10.0) -> None:
        self.url = url
        self.timeout = timeout
        self.session_id: str | None = None
        self._ids = itertools.count(1)

    def _post(self, payload: dict) -> dict | None:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.session_id:
            headers[MCP_SESSION_HEADER] = self.session_id
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                session = resp.headers.get(MCP_SESSION_HEADER)
                if session:
                    self.session_id = session
                body = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise MCPClientError(f"HTTP {exc.code}: {detail}", code=exc.code) from exc
        except urllib.error.URLError as exc:
            raise MCPClientError(f"connection failed: {exc.reason}") from exc
        if not body:
            return None  # 202 Accepted (notification)
        return json.loads(body.decode("utf-8"))

    def _request(self, method: str, params: dict | None = None) -> dict:
        message: dict = {"jsonrpc": "2.0", "id": next(self._ids), "method": method}
        if params is not None:
            message["params"] = params
        response = self._post(message)
        if response is None:
            raise MCPClientError("expected a JSON-RPC response, got 202")
        if "error" in response:
            err = response["error"]
            raise MCPClientError(f"JSON-RPC error {err['code']}: {err['message']}", code=err["code"])
        return response["result"]

    def notify(self, method: str, params: dict | None = None) -> None:
        message: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._post(message)

    # -- MCP methods -----------------------------------------------------------

    def initialize(self, client_name: str = "alexa-simulator", client_version: str = "0.1.0") -> dict:
        return self._request(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": client_name, "version": client_version},
            },
        )

    def list_tools(self) -> list[dict]:
        return self._request("tools/list")["tools"]

    def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        """Call a tool; returns the MCP tool result object.

        Raises ``MCPClientError`` when the tool result has ``isError: true``.
        """
        result = self._request("tools/call", {"name": name, "arguments": arguments or {}})
        if result.get("isError"):
            detail = result.get("structuredContent") or {}
            raise MCPClientError(f"tool '{name}' failed: {detail.get('error', result)}")
        return result

    def call_tool_structured(self, name: str, arguments: dict | None = None) -> dict:
        """Call a tool and return its ``structuredContent`` payload."""
        result = self.call_tool(name, arguments)
        structured = result.get("structuredContent")
        if structured is None:
            # Fall back to the JSON-encoded text content.
            structured = json.loads(result["content"][0]["text"])
        return structured
