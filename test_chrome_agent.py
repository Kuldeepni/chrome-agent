#!/usr/bin/env python3
"""Smoke test for chrome-agent.

Connects to the running Chrome over CDP, opens example.com, reads the page
text, takes a screenshot, and prints the full tool list.

Usage:
    python test_chrome_agent.py
"""

from __future__ import annotations

import asyncio
import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Windows consoles default to cp1252 and blow up on non-ASCII output.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from mcp.server.mcpserver import Image  # noqa: E402

import server  # noqa: E402
from chrome_agent.config import SETTINGS  # noqa: E402


async def main() -> int:
    print("=" * 70)
    print("chrome-agent smoke test")
    print("=" * 70)
    print(f"CDP URL:      {SETTINGS.cdp_url}")
    print(f"ALLOW_JS:     {SETTINGS.allow_js}")
    print(f"Upload dir:   {SETTINGS.allowed_upload_dir}")
    print(f"Download dir: {SETTINGS.download_dir}")
    print(f"Audit log:    {SETTINGS.log_dir / 'audit.log'}")
    print()

    # ----------------------------------------------------------------- #
    # 1. Connection
    # ----------------------------------------------------------------- #
    print("[1/5] Connecting to Chrome over CDP ...")
    try:
        browser = await server.SESSION.connect()
    except Exception as exc:  # noqa: BLE001
        print(f"    FAILED: {exc}")
        await server.SESSION.shutdown()
        return 1
    print(f"    OK — connected, version {browser.version}")
    pages = await server.SESSION.pages()
    print(f"    Open tabs: {len(pages)}")
    print()

    # ----------------------------------------------------------------- #
    # 2. Open example.com
    # ----------------------------------------------------------------- #
    print("[2/5] Opening https://example.com ...")
    result = await server.open_url("https://example.com")
    if not result.get("ok"):
        print(f"    FAILED: {result.get('error')}")
        await server.SESSION.shutdown()
        return 1
    print(f"    OK — url={result['url']} title={result['title']!r}")
    print()

    # ----------------------------------------------------------------- #
    # 3. Read the page text
    # ----------------------------------------------------------------- #
    print("[3/5] Reading page text ...")
    text_result = await server.get_page_text(max_chars=500)
    if not text_result.get("ok"):
        print(f"    FAILED: {text_result.get('error')}")
        await server.SESSION.shutdown()
        return 1
    body = text_result.get("text", "")
    print(f"    OK — {len(body)} chars")
    for line in body.splitlines()[:6]:
        print(f"      | {line}")
    print()

    # ----------------------------------------------------------------- #
    # 4. Screenshot
    # ----------------------------------------------------------------- #
    print("[4/5] Taking a screenshot ...")
    image = await server.screenshot(full_page=False)
    if isinstance(image, Image):
        data = image.data or b""
        out_path = Path(__file__).parent / "downloads" / "smoke-test.png"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(data)
        print(f"    OK — {len(data)} bytes, saved to {out_path}")
    else:  # pragma: no cover
        print(f"    FAILED — unexpected result {type(image)}")
    print()

    # ----------------------------------------------------------------- #
    # 5. Tool list
    # ----------------------------------------------------------------- #
    print("[5/5] Registered tools:")
    from server import mcp

    registered = await mcp.list_tools()
    for tool in registered:
        summary = (tool.description or "").strip().splitlines()[0]
        print(f"    {tool.name:<20} {summary[:78]}")
    print(f"\n    Total: {len(registered)} tools")

    await server.SESSION.shutdown()
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(130)
