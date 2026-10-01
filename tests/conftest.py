import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from amazon_frontdesk.intake import Business  # noqa: E402
from amazon_frontdesk.mcp_client import MCPClient  # noqa: E402
from amazon_frontdesk.mcp_server import create_server  # noqa: E402

BUSINESS_JSON = Path(__file__).resolve().parent.parent / "business.json"


@pytest.fixture(scope="session")
def business() -> Business:
    return Business.from_json(BUSINESS_JSON)


@pytest.fixture()
def mcp_server(tmp_path):
    """Live MCP server on an ephemeral port; bookings go to a temp JSONL."""
    server = create_server(
        Business.from_json(BUSINESS_JSON),
        host="127.0.0.1",
        port=0,
        bookings_path=tmp_path / "bookings.jsonl",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/mcp"
    yield {"url": url, "server": server, "bookings_path": tmp_path / "bookings.jsonl"}
    server.shutdown()
    thread.join(timeout=5)
    server.server_close()


@pytest.fixture()
def client(mcp_server) -> MCPClient:
    c = MCPClient(mcp_server["url"])
    c.initialize()
    c.notify("notifications/initialized")
    return c


def read_bookings(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
