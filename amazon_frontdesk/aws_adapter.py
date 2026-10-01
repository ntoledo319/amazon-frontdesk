"""AWS slot-in points for the hackathon's AWS Builder mini-challenge.

Two adapters live here:

- ``lambda_handler`` — a *working* AWS Lambda entry point for API Gateway
  proxy events. It dispatches the same JSON-RPC payload to the same
  ``MCPCore`` the HTTP server uses, in-process. It runs today with zero AWS
  calls; deploying it behind API Gateway + Lambda is packaging, not new code.

- ``BedrockExtractor`` — the dialogue LLM extractor interface backed by
  Amazon Bedrock (Nova). The prompt construction is implemented and tested;
  the actual ``converse`` call requires ``boto3`` and AWS credentials, which
  are intentionally NOT bundled ($0 spend rule). Without boto3 it raises a
  clear error instead of pretending to work.
"""

from __future__ import annotations

import json

from .intake import Business
from .mcp_server import MCPCore, dispatch
from .tools import FrontdeskTools

EXTRACTION_INSTRUCTIONS = """\
You extract appointment-intake fields from one caller utterance for {business}.
Return a JSON object with any of these keys you can find, omitting the rest:
  "name"    caller's name, capitalized
  "phone"   US phone formatted (XXX) XXX-XXXX
  "service" one of: {services}
  "date"    ISO YYYY-MM-DD (resolve relative dates against today={today})
  "time"    24h HH:MM
Respond with the JSON object only, no prose."""


def build_extraction_prompt(utterance: str, business: Business, today: str) -> str:
    """Prompt body for the Bedrock extraction call. Pure and testable."""
    services = ", ".join(s.name for s in business.services)
    instructions = EXTRACTION_INSTRUCTIONS.format(
        business=business.name, services=services, today=today
    )
    return f"{instructions}\n\nCaller: {utterance}"


class BedrockNotConfiguredError(RuntimeError):
    pass


class BedrockExtractor:
    """Dialogue ``Extractor`` backed by Amazon Bedrock (Nova models).

    Satisfies the ``Extractor = Callable[[str], dict[str, str]]`` interface in
    ``dialogue.IntakeAgent``. The network call is deliberately lazy: boto3 is
    imported at call time, and a clear error is raised when boto3 or
    credentials are unavailable. No fake responses are ever returned.
    """

    def __init__(self, business: Business, model_id: str = "amazon.nova-micro-v1:0",
                 region: str | None = None, today: str | None = None) -> None:
        self.business = business
        self.model_id = model_id
        self.region = region
        self.today = today

    def __call__(self, utterance: str) -> dict[str, str]:
        try:
            import boto3  # noqa: PLC0415 — optional dependency, lazy on purpose
        except ImportError as exc:
            raise BedrockNotConfiguredError(
                "BedrockExtractor requires boto3 and AWS credentials "
                "(pip install boto3; configure credentials or an IAM role). "
                "The deterministic offline parser remains active without it."
            ) from exc

        from datetime import date  # noqa: PLC0415

        today = self.today or date.today().isoformat()
        client = boto3.client("bedrock-runtime", region_name=self.region)
        response = client.converse(
            modelId=self.model_id,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"text": build_extraction_prompt(utterance, self.business, today)}
                    ],
                }
            ],
            inferenceConfig={"maxTokens": 200, "temperature": 0.0},
        )
        text = response["output"]["message"]["content"][0]["text"]
        try:
            parsed = json.loads(text)
        except ValueError:
            return {}
        if not isinstance(parsed, dict):
            return {}
        return {k: str(v) for k, v in parsed.items() if v}


def lambda_handler(event: dict, context=None, *, core: MCPCore | None = None,
                   business: Business | None = None) -> dict:
    """AWS Lambda entry point for API Gateway proxy events (v1 or v2 shape).

    Fully functional in-process: give it an event with a JSON-RPC body and it
    returns the proxy response. No AWS services are contacted. In deployment,
    ``core`` is built once per cold start from the bundled ``business.json``.
    """
    if core is None:
        if business is None:
            from pathlib import Path

            business = Business.from_json(
                Path(__file__).resolve().parent.parent / "business.json"
            )
        core = MCPCore(FrontdeskTools(business))

    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        import base64  # noqa: PLC0415

        raw = base64.b64decode(body)
    else:
        raw = body.encode("utf-8")

    status, headers, resp_body = dispatch(core, raw)
    headers.setdefault("Content-Type", "application/json")
    return {"statusCode": status, "headers": headers, "body": resp_body.decode("utf-8")}
