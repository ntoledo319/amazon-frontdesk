"""Alexa+ experience simulator — single-page chat web app, stdlib only.

Plays the role of an Alexa+-style agent: the user "speaks" (types) to the
page; the simulator orchestrates MCP tool calls against the frontdesk MCP
server (Streamable HTTP) and renders the conversation, the tool-call trace,
and the booking outcome. This is the demo surface for the hackathon video.

The orchestration logic (``AlexaSimulator``) is plain Python and unit-tested
directly; the HTTP layer is a thin stdlib wrapper around it.

Run:
    python -m amazon_frontdesk.simulator --with-mcp        # one command, both servers
    python -m amazon_frontdesk.simulator --mcp-url http://127.0.0.1:8080/mcp
"""

from __future__ import annotations

import argparse
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .intake import Business
from .mcp_client import MCPClient, MCPClientError
from .mcp_server import create_server

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Alexa+ Simulator — Frontdesk</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font: 15px/1.5 system-ui, sans-serif; background: #0e1116; color: #e6e9ee; }
  header { padding: 14px 20px; border-bottom: 1px solid #232a35; display: flex; gap: 12px; align-items: baseline; }
  header h1 { font-size: 16px; margin: 0; }
  header .sub { color: #7d8899; font-size: 13px; }
  header button { margin-left: auto; background: #232a35; color: #e6e9ee; border: 1px solid #39414f;
                  border-radius: 6px; padding: 5px 12px; cursor: pointer; }
  #log { max-width: 760px; margin: 0 auto; padding: 20px; }
  .turn { margin-bottom: 14px; }
  .bubble { max-width: 85%; padding: 10px 14px; border-radius: 14px; white-space: pre-wrap; }
  .user .bubble { margin-left: auto; background: #1f6feb; border-bottom-right-radius: 4px; }
  .agent .bubble { background: #1c2330; border-bottom-left-radius: 4px; }
  .who { font-size: 11px; color: #7d8899; margin: 0 4px 4px; }
  .user .who { text-align: right; }
  .tools { font: 12px/1.6 ui-monospace, monospace; color: #8fa3c8; background: #141a24;
           border: 1px solid #232a35; border-radius: 8px; padding: 8px 12px; margin-top: 6px; }
  .tools summary { cursor: pointer; color: #7d8899; }
  .booking { border: 1px solid #2ea043; background: #12261a; border-radius: 10px;
             padding: 10px 14px; margin-top: 6px; font-size: 14px; }
  .booking b { color: #3fb950; }
  form { max-width: 760px; margin: 0 auto; padding: 12px 20px 24px; display: flex; gap: 8px; }
  input { flex: 1; background: #1c2330; border: 1px solid #39414f; border-radius: 8px;
          color: #e6e9ee; padding: 10px 12px; font-size: 15px; }
  button.send { background: #1f6feb; color: #fff; border: 0; border-radius: 8px; padding: 0 18px; cursor: pointer; }
</style>
</head>
<body>
<header>
  <h1>Alexa+ Simulator</h1>
  <span class="sub" id="business"></span>
  <button onclick="reset()">New conversation</button>
</header>
<div id="log"></div>
<form id="f">
  <input id="msg" autocomplete="off" placeholder="Speak to the frontdesk, e.g. “Hi, I need my car detailed”" autofocus>
  <button class="send" type="submit">Send</button>
</form>
<script>
const log = document.getElementById('log');
function add(cls, who, text) {
  const turn = document.createElement('div'); turn.className = 'turn ' + cls;
  const w = document.createElement('div'); w.className = 'who'; w.textContent = who;
  const b = document.createElement('div'); b.className = 'bubble'; b.textContent = text;
  turn.append(w, b); log.appendChild(turn);
  return turn;
}
function addTools(calls) {
  const d = document.createElement('details'); d.className = 'tools';
  const s = document.createElement('summary');
  s.textContent = calls.length + ' MCP tool call' + (calls.length === 1 ? '' : 's') + ' (Streamable HTTP)';
  d.appendChild(s);
  for (const c of calls) {
    const pre = document.createElement('pre');
    pre.textContent = '→ ' + c.tool + ' ' + JSON.stringify(c.arguments) + '\\n' + c.result;
    d.appendChild(pre);
  }
  log.appendChild(d);
}
function addBooking(b) {
  const el = document.createElement('div'); el.className = 'booking';
  el.innerHTML = '<b>✓ Booking confirmed ' + b.booking_id + '</b><br>' +
    b.name + ' — ' + b.service + '<br>' + b.date + ' at ' + b.time +
    (b.phone ? '<br>Callback: ' + b.phone : '');
  log.appendChild(el);
}
async function reset() {
  await fetch('/reset', {method: 'POST'});
  log.innerHTML = '';
  const r = await fetch('/chat', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                  body: JSON.stringify({message: ''})});
  const data = await r.json();
  add('agent', 'Frontdesk (via MCP)', data.reply);
}
document.getElementById('f').addEventListener('submit', async (e) => {
  e.preventDefault();
  const input = document.getElementById('msg');
  const text = input.value.trim(); if (!text) return;
  input.value = '';
  add('user', 'You (as Alexa+ caller)', text);
  const r = await fetch('/chat', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                  body: JSON.stringify({message: text})});
  const data = await r.json();
  if (data.error) { add('agent', 'error', data.error); return; }
  add('agent', 'Frontdesk (via MCP)', data.reply);
  if (data.tool_calls && data.tool_calls.length) addTools(data.tool_calls);
  if (data.booking) addBooking(data.booking);
  window.scrollTo(0, document.body.scrollHeight);
});
fetch('/info').then(r => r.json()).then(d => {
  document.getElementById('business').textContent =
    d.business + ' · MCP: ' + d.mcp_url;
});
reset();
</script>
</body>
</html>
"""


class AlexaSimulator:
    """The 'Alexa+' half of the demo: orchestrates MCP tool calls per turn.

    For each user utterance it calls ``frontdesk_answer``; the tool result
    carries the agent reply, dialogue state, and (on completion) the booking.
    Every call is recorded in a trace so the UI can show the wire activity.
    """

    def __init__(self, mcp_url: str, client: MCPClient | None = None) -> None:
        self.mcp_url = mcp_url
        self.client = client or MCPClient(mcp_url)
        self.client.initialize()
        self.client.notify("notifications/initialized")
        self.conversation_id = uuid.uuid4().hex[:12]

    def reset(self) -> None:
        self.conversation_id = uuid.uuid4().hex[:12]

    def chat(self, message: str) -> dict:
        """One conversational turn. Returns reply, state, booking, tool trace."""
        args = {"message": message, "conversation_id": self.conversation_id}
        trace = [{"tool": "frontdesk_answer", "arguments": dict(args), "result": ""}]
        try:
            data = self.client.call_tool_structured("frontdesk_answer", args)
        except MCPClientError as exc:
            trace[0]["result"] = f"error: {exc}"
            return {"reply": "", "done": False, "booking": None,
                    "tool_calls": trace, "error": str(exc)}
        trace[0]["result"] = (
            f"reply={data['reply']!r} done={data['done']}"
            + (f" booking={data['booking']['booking_id']}" if data.get("booking") else "")
        )
        return {
            "reply": data["reply"],
            "done": data["done"],
            "booking": data.get("booking"),
            "tool_calls": trace,
        }


def make_sim_handler(sim_factory, mcp_url: str, business: Business) -> type[BaseHTTPRequestHandler]:
    """HTTP handler. ``sim_factory()`` returns a fresh AlexaSimulator."""

    class SimHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        _sim = None

        @classmethod
        def sim(cls) -> AlexaSimulator:
            if cls._sim is None:
                cls._sim = sim_factory()
            return cls._sim

        def _json(self, payload: dict, status: int = 200) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _html(self, text: str) -> None:
            body = text.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/":
                self._html(PAGE)
            elif self.path == "/info":
                self._json({"business": business.name, "mcp_url": mcp_url})
            elif self.path == "/health":
                self._json({"ok": True})
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except ValueError:
                self._json({"error": "invalid JSON"}, 400)
                return
            if self.path == "/chat":
                message = payload.get("message")
                if not isinstance(message, str):
                    self._json({"error": "'message' must be a string"}, 400)
                    return
                self._json(self.sim().chat(message))
            elif self.path == "/reset":
                self.sim().reset()
                self._json({"ok": True})
            else:
                self._json({"error": "not found"}, 404)

        def log_message(self, fmt: str, *args) -> None:
            pass

    return SimHandler


def main() -> None:
    parser = argparse.ArgumentParser(description="Alexa+ simulator web app for the frontdesk MCP server")
    parser.add_argument("--mcp-url", default=None, help="MCP endpoint, e.g. http://127.0.0.1:8080/mcp")
    parser.add_argument("--with-mcp", action="store_true",
                        help="also start an MCP server in-process (no separate process needed)")
    parser.add_argument("--mcp-port", type=int, default=8080)
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--business",
                        default=str(Path(__file__).resolve().parent.parent / "business.json"))
    parser.add_argument("--bookings", default=None, help="JSONL file for bookings (--with-mcp only)")
    args = parser.parse_args()

    business = Business.from_json(args.business)
    mcp_url = args.mcp_url
    if args.with_mcp:
        mcp_server = create_server(business, "127.0.0.1", args.mcp_port, args.bookings)
        threading.Thread(target=mcp_server.serve_forever, daemon=True).start()
        mcp_url = f"http://127.0.0.1:{args.mcp_port}/mcp"
        print(f"MCP server listening on {mcp_url}")
    if not mcp_url:
        parser.error("pass --mcp-url or --with-mcp")

    handler = make_sim_handler(lambda: AlexaSimulator(mcp_url), mcp_url, business)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    print(f"Simulator UI on http://127.0.0.1:{args.port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
