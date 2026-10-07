# Friction Log — Amazon Developer Hackathon

Logged while building amazon-frontdesk (Alexa+ track, simulated path).

## Entry 1
- **Task attempted:** implement an MCP server over Streamable HTTP from the spec docs.
- **Steps taken:** read modelcontextprotocol.io transport + server sections; implemented initialize/tools-list/tools-call in stdlib Python.
- **Expected:** revision differences between protocol versions documented.
- **Actual:** the spec moved from 2025-03-26 to 2025-11-25 with no published changelog; we had to diff revisions by hand to see what compliance requires.
- **Severity:** medium.
- **Workaround:** pinned the implemented protocol revision in code and tested against it (42 tests).
- **Suggestion:** publish per-revision migration notes (what a 2025-03-26 server must change for 2025-11-25).

## Entry 2
- **Task attempted:** validate our Agent Skills manifest (agentskills.io spec).
- **Expected:** a JSON schema or validator exists.
- **Actual:** none; manifest verified by manual cross-check against the spec text.
- **Severity:** low.
- **Workaround:** manual review + unit test asserting required keys.
- **Suggestion:** ship a JSON schema for the manifest.