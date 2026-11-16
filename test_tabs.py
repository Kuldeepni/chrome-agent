#!/usr/bin/env python3
"""Stage 5 test: active-tab tracking.

Verifies that the session follows the tab the user actually switches to.

Run with:  python test_tabs.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import server  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((PASS if condition else FAIL, name, detail))
    print(f"  [{PASS if condition else FAIL}] {name}" + (f" -- {detail}" if detail else ""))


async def main() -> int:
    await server.SESSION.connect()

    print("\n--- Setup: three distinct tabs ---")
    for url in ("https://example.com", "https://example.org", "https://example.net"):
        await server.open_url(url)
        await server.new_tab(url)
    r = await server.list_tabs()
    check("list_tabs ok", r.get("ok"))
    check("multiple tabs open", r.get("count", 0) >= 3, f"count={r.get('count')}")
    active = [t["index"] for t in r.get("tabs", []) if t.get("active")]
    check("exactly one tab marked active", len(active) == 1, str(active))

    print("\n--- Session follows bring_to_front ---")
    pages = await server.SESSION.pages()
    target = pages[0]
    await target.bring_to_front()
    await asyncio.sleep(0.5)

    current = await server.SESSION.current_page()
    check("current_page follows the focused tab", current is target, f"got {current.url[:40]}")

    r = await server.list_tabs()
    active = [t["index"] for t in r.get("tabs", []) if t.get("active")]
    check("list_tabs marks the focused tab active", active == [0], str(active))

    print("\n--- Session follows a switch to another tab ---")
    other = pages[-1]
    await other.bring_to_front()
    await asyncio.sleep(0.5)
    current = await server.SESSION.current_page()
    check("current_page follows the new tab", current is other, f"got {current.url[:40]}")

    print("\n--- switch_tab() sets the active tab ---")
    r = await server.switch_tab(0)
    check("switch_tab ok", r.get("ok"), r.get("error", ""))
    current = await server.SESSION.current_page()
    check("current_page is the switched tab", current is pages[0], f"got {current.url[:40]}")

    print("\n--- Tools act on the active tab ---")
    r = await server.get_page_text(max_chars=200)
    check("get_page_text reads the active tab", r.get("ok"))
    check("active tab url matches", r.get("url") == pages[0].url, f"{r.get('url')} vs {pages[0].url}")

    await server.SESSION.shutdown()

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
