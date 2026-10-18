#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=2.3,<3"]
# ///
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = Path(__file__).resolve().parent
DEFAULT_CALLS = [
    ("status", {}),
    ("list_days", {"limit": 3}),
    ("recent", {"hours": 2}),
    ("search", {"query": "lunes"}),
    ("moments", {}),
    ("transcript_since", {"minutes": 15, "wait_seconds": 5}),
    ("read_day", {"day": "hoy"}),
    ("day_recap", {"day": "hoy"}),
]


def server_parameters(python: str | None) -> StdioServerParameters:
    script = str(HERE / "evoca_link.py")
    environment = dict(os.environ)
    if python:
        return StdioServerParameters(command=python, args=[script, "mcp"], env=environment)
    return StdioServerParameters(command="uv", args=["run", "--quiet", "--script", script, "mcp"], env=environment)


def tool_payload(structured: dict | None, text: str) -> dict:
    payload = structured if structured is not None else json.loads(text)
    if set(payload) == {"result"} and isinstance(payload["result"], dict):
        return payload["result"]
    return payload


def summary(payload: dict) -> str:
    shown = {key: value for key, value in payload.items() if key in ("as_of", "source", "phone", "path", "connected", "complete", "total_chars", "next_offset", "truncated")}
    for key in ("days", "hits", "phrases", "moments", "paragraphs", "phones", "conversations"):
        if key in payload:
            shown[key] = len(payload[key])
    if "markdown" in payload:
        shown["markdown_head"] = payload["markdown"][:80]
    if payload.get("recap"):
        shown["headline"] = payload["recap"].get("headline")
    return json.dumps(shown, ensure_ascii=False)


async def run(calls: list[tuple[str, dict]], python: str | None, full: bool) -> int:
    failures = 0
    async with stdio_client(server_parameters(python)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = sorted(tool.name for tool in (await session.list_tools()).tools)
            print("tools:", ", ".join(tools))
            for name, arguments in calls:
                result = await session.call_tool(name, arguments)
                text = "".join(getattr(block, "text", "") for block in result.content)
                if result.is_error:
                    failures += 1
                    print(f"✗ {name}: {text}")
                    continue
                payload = tool_payload(result.structured_content, text)
                if "as_of" not in payload:
                    failures += 1
                    print(f"✗ {name}: no as_of in {text[:200]}")
                    continue
                print(f"✓ {name}: {json.dumps(payload, ensure_ascii=False, indent=2) if full else summary(payload)}")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Call every evoca-link MCP tool once through a real MCP client over stdio.")
    parser.add_argument("--python", help="run evoca_link.py with this interpreter instead of uv")
    parser.add_argument("--full", action="store_true", help="print whole results")
    parser.add_argument("--call", nargs=2, action="append", metavar=("TOOL", "JSON"), help="call only these tools")
    args = parser.parse_args()
    calls = [(name, json.loads(arguments)) for name, arguments in args.call] if args.call else DEFAULT_CALLS
    return asyncio.run(run(calls, args.python, args.full))


if __name__ == "__main__":
    sys.exit(main())
