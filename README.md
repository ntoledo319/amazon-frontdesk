# Frontdesk on Alexa+ — MCP integration

**Amazon Developer Hackathon 2026 · Alexa+ track · Toledo Technologies LLC**

Exposes the [frontdesk voice agent](../frontdesk-voice-agent) (69-test intake
brain for small service businesses) as an **MCP server over the Streamable
HTTP transport** with an **Agent Skill** describing how an Alexa+-style agent
drives it — exactly the two open standards the Alexa+ track requires. A
stdlib web app simulates the Alexa+ experience end-to-end: caller speaks
(types) → simulator orchestrates MCP tool calls → conversation, tool trace,
and booking confirmation render live.

Everything runs offline, text-in/text-out, `$0` spend. The AssemblyAI
streaming-ASR layer from the voice repo is not required on this path.

## Architecture

```
 caller (you, in the browser)
     │  types an utterance
     ▼
┌────────────────────────────────┐
│ Simulator web app              │  stdlib http.server, port 8090
│  · single-page chat UI         │  plays the "Alexa+" role:
│  · AlexaSimulator orchestrator │  per turn → MCP tools/call
└───────────────┬────────────────┘
                │ JSON-RPC 2.0 over HTTP POST (+Mcp-Session-Id)
                ▼
┌────────────────────────────────┐
│ MCP server (Streamable HTTP)   │  stdlib http.server, port 8080, POST /mcp
│  methods: initialize, ping,    │  protocol 2025-03-26
│           tools/list, tools/call
│  tools: frontdesk_answer       │  stateful dialogue turn
│         frontdesk_book         │  direct structured booking
│         frontdesk_business_info│  catalog from business.json
└───────────────┬────────────────┘
                ▼
┌────────────────────────────────┐
│ Intake brain (ported from      │  slot-filling state machine:
│  frontdesk-voice-agent, MIT)   │  name → phone → service →
│  intake.py + dialogue.py       │  date/time → confirm → DONE
└───────────────┬────────────────┘
                │ completed booking
                ▼
        bookings JSONL sink (+ in-memory)
```

Later, for real deployment (not built here, no fake calls):

```
 Alexa+ ──► Agent Skill (skills/frontdesk-booking/SKILL.md)
                │ MCP Streamable HTTP
                ▼
 API Gateway ──► AWS Lambda (aws_adapter.lambda_handler — works in-process today)
                ▼
 Amazon Bedrock Nova (aws_adapter.BedrockExtractor — optional LLM extractor)
```

## Layout

```
amazon-frontdesk/
├── amazon_frontdesk/
│   ├── intake.py        # ported domain logic: catalog, lead, spoken parsing
│   ├── dialogue.py      # ported IntakeAgent state machine
│   ├── tools.py         # the 3 MCP tools + booking store
│   ├── mcp_server.py    # Streamable HTTP transport + JSON-RPC core
│   ├── mcp_client.py    # stdlib client (session handshake, tools/call)
│   ├── simulator.py     # Alexa+ simulator web app (chat UI + orchestrator)
│   └── aws_adapter.py   # Lambda handler (working) + Bedrock extractor (stub)
├── skills/frontdesk-booking/
│   ├── SKILL.md         # Agent Skills manifest (agentskills.io spec)
│   └── references/mcp-tools.md
├── tests/               # 42 pytest tests
├── business.json        # demo business: Toledo Shine Mobile Detailing
└── pytest.ini
```

## Setup & run

Python 3.10+, stdlib only. No pip installs.

```bash
# one command: MCP server + simulator
python -m amazon_frontdesk.simulator --with-mcp --bookings bookings.jsonl
# open http://127.0.0.1:8090/

# or separately:
python -m amazon_frontdesk.mcp_server --port 8080 --bookings bookings.jsonl
python -m amazon_frontdesk.simulator --mcp-url http://127.0.0.1:8080/mcp
```

Try in the chat: *"Hi, this is Dana"* → answer the phone/service/date/time
prompts → confirm → the booking card appears, the MCP tool trace expands
under the reply, and the booking is appended to `bookings.jsonl`.

Raw MCP over curl:

```bash
curl -i -X POST http://127.0.0.1:8080/mcp -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'
# note the Mcp-Session-Id response header, then:
curl -X POST http://127.0.0.1:8080/mcp \
  -H 'Content-Type: application/json' -H "Mcp-Session-Id: <id>" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call",
       "params":{"name":"frontdesk_business_info","arguments":{}}}'
```

## How this maps to the Alexa+ track

- **"Working MCP integration on the open standards for Agent Skills and
  Streamable HTTP transports"** — `mcp_server.py` implements the Streamable
  HTTP transport (protocol `2025-03-26`: single POST endpoint, JSON-RPC 2.0,
  `Mcp-Session-Id` handshake, 202 for notifications, 405 for GET without an
  SSE stream); `skills/frontdesk-booking/SKILL.md` follows the Agent Skills
  open format (YAML frontmatter `name`/`description`, progressive disclosure
  with `references/mcp-tools.md`).
- **"Simulate an Alexa+ experience using your preferred agentic tools via a
  web app"** — `simulator.py` is that web app; `AlexaSimulator` plays the
  Alexa+ orchestrator role against the real MCP wire protocol.
- **Reuse clause** — the intake brain is ported from our frontdesk-voice-agent
  project (MIT, same author); the MCP transport, tool layer, simulator, Agent
  Skill, and AWS adapters are the significant new work, all in this repo.

## Where AWS slots in later (stub adapters only — nothing fake today)

- **Lambda**: `aws_adapter.lambda_handler` is a working API Gateway proxy
  handler that dispatches to the same `MCPCore` — tested in-process. Deploying
  it is packaging, not new code.
- **Bedrock (Nova)**: `aws_adapter.BedrockExtractor` satisfies the dialogue
  `Extractor` interface; prompt construction is implemented and tested, and
  the `converse` call raises `BedrockNotConfiguredError` unless boto3 +
  credentials exist. It never returns fabricated output.
- **AWS Builder mini-challenge**: wire the Lambda handler behind API Gateway,
  point the Agent Skill at that URL, enable `BedrockExtractor` with the
  hackathon's AWS credits.

## Tests

```bash
pytest            # 42 tests
```

- `test_brain.py` — ported parser + dialogue state machine (8)
- `test_mcp_protocol.py` — JSON-RPC round-trips: initialize, tools/list,
  tools/call, unknown tool/method, batches, notifications, parse errors (14)
- `test_http_flow.py` — live-server session rules + full booking conversation
  over HTTP + direct booking validation (8)
- `test_simulator.py` — AlexaSimulator orchestration, error path, and the web
  app's HTTP endpoints (8)
- `test_aws_adapter.py` — Lambda dispatch + Bedrock stub behavior (4)

## AI assistance disclosure

Designed and built with AI assistance (Kimi Code CLI). MCP and Agent Skills
details verified against the published specs at build time; the 42-test suite
passes locally. Human direction and review: Nick Toledo / Toledo Technologies
LLC.

## License

MIT.
