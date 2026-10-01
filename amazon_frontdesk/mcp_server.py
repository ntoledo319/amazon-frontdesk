"""MCP server over the Streamable HTTP transport (protocol 2025-03-26).

Single HTTP endpoint (default ``/mcp``) accepting JSON-RPC 2.0 messages via
POST. Implemented on ``http.server`` only — no web framework, no SDK.

Spec behavior implemented:

- ``POST`` with a JSON-RPC request returns ``200 application/json`` with the
  JSON-RPC response (the server never upgrades to SSE; JSON responses are the
  spec-sanctioned non-streaming path).
- ``POST`` containing only notifications/responses returns ``202 Accepted``
  with no body.
- ``GET`` returns ``405`` (this server offers no standalone SSE stream).
- ``initialize`` mints a session id returned in the ``Mcp-Session-Id``
  header; subsequent requests must echo it. A missing session id on
  non-initialize requests is a ``400``; an unknown one is a ``404``.
- Methods: ``initialize``, ``ping``, ``tools/list``, ``tools/call``, plus
  ``notifications/*`` acknowledgment. Batches are supported.

The JSON-RPC dispatch is transport-agnostic (``MCPCore.handle``) so the same
core serves HTTP here and the Lambda adapter in ``aws_adapter``.
"""

from __future__ import annotations

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .intake import Business
from .tools import FrontdeskTools

PROTOCOL_VERSION = "2025-03-26"
SERVER_NAME = "amazon-frontdesk"
SERVER_VERSION = "0.1.0"
MCP_SESSION_HEADER = "Mcp-Session-Id"

# JSON-RPC 2.0 error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def _error(code: int, message: str, req_id=None) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _result(result, req_id) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


class MCPCore:
    """Transport-agnostic JSON-RPC dispatch for the MCP methods."""

    def __init__(self, tools: FrontdeskTools) -> None:
        self.tools = tools
        self.sessions: set[str] = set()
        self._lock = threading.Lock()

    def new_session(self) -> str:
        session_id = uuid.uuid4().hex
        with self._lock:
            self.sessions.add(session_id)
        return session_id

    def has_session(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self.sessions

    def handle(self, message: dict) -> tuple[dict | None, str | None]:
        """Handle one JSON-RPC message.

        Returns ``(response, new_session_id)``. ``response`` is ``None`` for
        notifications; ``new_session_id`` is set only by ``initialize``.
        """
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(INVALID_REQUEST, "not a JSON-RPC 2.0 message", message.get("id") if isinstance(message, dict) else None), None

        method = message.get("method")
        req_id = message.get("id")

        # Notifications (no id) never produce a response.
        if req_id is None:
            if isinstance(method, str) and method.startswith("notifications/"):
                return None, None
            return _error(INVALID_REQUEST, "request is missing an id", None), None

        if method == "initialize":
            session_id = self.new_session()
            return (
                _result(
                    {
                        "protocolVersion": PROTOCOL_VERSION,
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                    },
                    req_id,
                ),
                session_id,
            )

        if method == "ping":
            return _result({}, req_id), None

        if method == "tools/list":
            return _result({"tools": self.tools.schemas()}, req_id), None

        if method == "tools/call":
            params = message.get("params")
            if not isinstance(params, dict) or not isinstance(params.get("name"), str):
                return _error(INVALID_PARAMS, "tools/call requires params.name", req_id), None
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                return _error(INVALID_PARAMS, "params.arguments must be an object", req_id), None
            try:
                result = self.tools.call(params["name"], arguments)
            except KeyError:
                return _error(INVALID_PARAMS, f"unknown tool: {params['name']}", req_id), None
            except Exception as exc:  # defensive: never leak a bare 500
                return _error(INTERNAL_ERROR, f"tool execution failed: {exc}", req_id), None
            return _result(result, req_id), None

        return _error(METHOD_NOT_FOUND, f"unknown method: {method}", req_id), None


def dispatch(core: MCPCore, raw_body: bytes) -> tuple[int, dict, bytes]:
    """Map one HTTP POST body to (status, headers, response_body)."""
    headers = {"Content-Type": "application/json"}
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return 400, headers, json.dumps(_error(PARSE_ERROR, "invalid JSON")).encode()

    messages = payload if isinstance(payload, list) else [payload]
    if not messages:
        return 400, headers, json.dumps(_error(INVALID_REQUEST, "empty batch")).encode()

    responses: list[dict] = []
    for message in messages:
        response, session_id = core.handle(message)
        if session_id:
            headers[MCP_SESSION_HEADER] = session_id
        if response is not None:
            responses.append(response)

    if not responses:
        return 202, {}, b""
    body = responses if isinstance(payload, list) else responses[0]
    return 200, headers, json.dumps(body).encode()


def make_handler(core: MCPCore, path: str = "/mcp") -> type[BaseHTTPRequestHandler]:
    class MCPRequestHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = f"{SERVER_NAME}/{SERVER_VERSION}"

        def _send(self, status: int, headers: dict, body: bytes) -> None:
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_POST(self) -> None:
            if self.path != path:
                self._send(404, {"Content-Type": "application/json"},
                           json.dumps(_error(INVALID_REQUEST, f"unknown path: {self.path}")).encode())
                return
            session_id = self.headers.get(MCP_SESSION_HEADER)
            if session_id is not None and not core.has_session(session_id):
                self._send(404, {"Content-Type": "application/json"},
                           json.dumps(_error(INVALID_REQUEST, "unknown session")).encode())
                return
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)
            # Peek: session enforcement applies to everything except initialize.
            try:
                payload = json.loads(body.decode("utf-8"))
                messages = payload if isinstance(payload, list) else [payload]
                needs_session = any(
                    isinstance(m, dict) and m.get("method") != "initialize" for m in messages
                )
            except (ValueError, UnicodeDecodeError):
                needs_session = False
            if session_id is None and needs_session:
                self._send(400, {"Content-Type": "application/json"},
                           json.dumps(_error(INVALID_REQUEST, f"missing {MCP_SESSION_HEADER} header")).encode())
                return
            status, resp_headers, resp_body = dispatch(core, body)
            self._send(status, resp_headers, resp_body)

        def do_GET(self) -> None:
            # No standalone SSE stream is offered.
            self._send(405, {"Allow": "POST"}, b"")

        def log_message(self, fmt: str, *args) -> None:  # keep test output clean
            pass

    return MCPRequestHandler


def create_server(
    business: Business,
    host: str = "127.0.0.1",
    port: int = 8080,
    bookings_path: str | Path | None = None,
) -> ThreadingHTTPServer:
    """Build (but do not start) the MCP HTTP server."""
    tools = FrontdeskTools(business, bookings_path=bookings_path)
    core = MCPCore(tools)
    server = ThreadingHTTPServer((host, port), make_handler(core))
    server.mcp_core = core  # type: ignore[attr-defined]
    return server


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Frontdesk MCP server (Streamable HTTP)")
    parser.add_argument("--business", default=str(Path(__file__).resolve().parent.parent / "business.json"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--bookings", default=None, help="JSONL file to append bookings to")
    args = parser.parse_args()

    business = Business.from_json(args.business)
    server = create_server(business, args.host, args.port, args.bookings)
    print(f"MCP server for {business.name} listening on http://{args.host}:{args.port}/mcp")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
