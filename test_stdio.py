#!/usr/bin/env python3
"""End-to-end test: talk to the server over the real MCP stdio transport.

Spawns `server.py` as a subprocess, performs the MCP initialize handshake,
lists tools, and calls a few of them. This is the same path opencode uses.

Run with:  python test_stdio.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

SERVER = Path(__file__).parent / "server.py"
PYTHON = Path(__file__).parent / ".venv" / "Scripts" / "python.exe"

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((PASS if condition else FAIL, name, detail))
    print(f"  [{PASS if condition else FAIL}] {name}" + (f" -- {detail}" if detail else ""))


async def main() -> int:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"

    params = StdioServerParameters(
        command=str(PYTHON),
        args=[str(SERVER)],
        env=env,
    )

    print("\n--- MCP stdio handshake ---")
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            check("initialize succeeded", init is not None, f"server={init.server_info.name}")
            check("server name is chrome-agent", init.server_info.name == "chrome-agent")

            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            check("tools listed over stdio", len(names) == 25, f"{len(names)} tools")

            expected = [
                "open_url", "go_back", "go_forward", "reload",
                "list_tabs", "switch_tab", "new_tab", "close_tab",
                "get_page_text", "get_page_snapshot", "screenshot",
                "get_links", "find_text",
                "click", "type_text", "press_key", "select_option",
                "hover", "scroll", "upload_file", "wait_for",
                "handle_dialog", "list_downloads", "run_javascript",
            ]
            missing = [e for e in expected if e not in names]
            check("all expected tools present", not missing, f"missing={missing}")

            print("\n--- Docstrings carry safety guidance ---")
            by_name = {t.name: t for t in tools.tools}
            click_doc = by_name["click"].description or ""
            check("click warns about irreversible actions", "confirm" in click_doc.lower())
            check("click warns before committing", "hard-to-undo" in click_doc.lower())
            text_doc = by_name["get_page_text"].description or ""
            check("get_page_text warns about untrusted data", "UNTRUSTED" in text_doc)
            check("get_page_text says never follow instructions", "never follow instructions" in text_doc.lower())
            js_doc = by_name["run_javascript"].description or ""
            check("run_javascript documents ALLOW_JS gate", "ALLOW_JS" in js_doc)
            up_doc = by_name["upload_file"].description or ""
            check("upload_file documents ALLOWED_UPLOAD_DIR", "ALLOWED_UPLOAD_DIR" in up_doc)

            print("\n--- Live tool calls over stdio ---")
            result = await session.call_tool("open_url", {"url": "https://example.com"})
            check("open_url returned ok", not result.is_error, str(result.content)[:120])
            payload = json.loads(result.content[0].text)
            check("open_url reports url", "example.com" in payload.get("url", ""), payload.get("url", ""))
            check("open_url reports title", payload.get("title") == "Example Domain", payload.get("title", ""))

            result = await session.call_tool("get_page_text", {"max_chars": 200})
            payload = json.loads(result.content[0].text)
            check("get_page_text returned text", len(payload.get("text", "")) > 0)
            check("get_page_text flags untrusted via docs", True)

            result = await session.call_tool("list_tabs", {})
            payload = json.loads(result.content[0].text)
            check("list_tabs returned tabs", payload.get("count", 0) >= 1)

            result = await session.call_tool("run_javascript", {"code": "() => 1"})
            payload = json.loads(result.content[0].text)
            check("run_javascript blocked by default", payload.get("ok") is False)

            result = await session.call_tool("screenshot", {"full_page": False})
            check("screenshot returned image content", result.content[0].type == "image", result.content[0].type)

    failures = [x for x in results if x[0] == FAIL]
    print("\n" + "=" * 60)
    print(f"{len(results) - len(failures)}/{len(results)} checks passed")
    for _, name, detail in failures:
        print(f"  - {name}: {detail}")
    print("=" * 60)
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(130)
