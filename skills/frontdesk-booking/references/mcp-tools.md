# MCP tool reference — amazon-frontdesk

Transport: MCP Streamable HTTP (protocol `2025-03-26`). One endpoint,
`POST /mcp`, JSON-RPC 2.0 bodies, `application/json` responses. `GET`
returns 405 (no SSE stream). `initialize` returns the session id in the
`Mcp-Session-Id` header; send it on all later requests.

## initialize

```json
{"jsonrpc": "2.0", "id": 1, "method": "initialize",
 "params": {"protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "my-agent", "version": "1.0"}}}
```

→ `{"protocolVersion": "2025-03-26", "capabilities": {"tools": ...}, "serverInfo": ...}`
plus the `Mcp-Session-Id` header.

## tools/list

Returns the three tools below with JSON Schema `inputSchema`s.

## frontdesk_answer

```json
{"jsonrpc": "2.0", "id": 2, "method": "tools/call",
 "params": {"name": "frontdesk_answer",
            "arguments": {"message": "Hi, I'm Dana, I need an interior detail",
                          "conversation_id": "abc123"}}}
```

`structuredContent`:

```json
{"conversation_id": "abc123", "reply": "Thanks Dana. What's the best phone...",
 "done": false,
 "lead": {"name": "Dana", "phone": null, "service": "Full Interior Detail",
          "date": null, "time": null}}
```

When the caller confirms and the dialogue completes, `done` is `true` and a
`booking` object is included exactly once:

```json
{"booking": {"booking_id": "BK-0001", "name": "Dana", "phone": "(555) 214-8690",
             "service": "Full Interior Detail", "date": "2026-10-02",
             "time": "14:00", "source": "conversation", ...}}
```

Omit `conversation_id` to start a new conversation; the first reply includes
the agent's greeting.

## frontdesk_book

```json
{"name": "frontdesk_book",
 "arguments": {"name": "Dana", "service": "interior detail",
               "date": "2026-10-02", "time": "14:00", "phone": "555-214-8690"}}
```

Validates the service against the catalog (names and aliases), the date as
ISO `YYYY-MM-DD`, and the time as 24h `HH:MM`. Failures come back as a tool
result with `isError: true` and `{"error": "..."}` in `structuredContent`.

## frontdesk_business_info

No arguments. Returns `{"business", "agent_name", "services": [{"name",
"price_from", "duration_min", "aliases"}]}`.

## Error shapes

- Unknown tool: JSON-RPC error `-32602` ("unknown tool: ...").
- Unknown method: `-32601`. Malformed JSON: `-32700` (HTTP 400).
- Missing `Mcp-Session-Id` on non-initialize requests: HTTP 400.
- Unknown `Mcp-Session-Id`: HTTP 404.
- Tool-level validation failures: HTTP 200, result with `isError: true`.
