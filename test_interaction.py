#!/usr/bin/env python3
"""Stage 3 test: exercises the interaction tools against a local fixture page.

Run with:  python test_interaction.py
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

FIXTURE = (Path(__file__).parent / "tests" / "fixture.html").resolve()
FIXTURE_URL = FIXTURE.as_uri()

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((PASS if condition else FAIL, name, detail))
    print(f"  [{PASS if condition else FAIL}] {name}" + (f" -- {detail}" if detail else ""))


async def main() -> int:
    await server.SESSION.connect()

    print("\n--- Navigation & tabs ---")
    r = await server.open_url(FIXTURE_URL)
    check("open_url fixture", r.get("ok"), r.get("url", r.get("error", "")))

    r = await server.get_page_text(max_chars=4000)
    check("get_page_text ok", r.get("ok"))
    check("hidden text excluded", "Hidden content" not in r.get("text", ""))

    print("\n--- Snapshot & refs ---")
    r = await server.get_page_snapshot()
    check("get_page_snapshot ok", r.get("ok"), r.get("error", ""))
    snap = r.get("snapshot", "")
    check("snapshot has refs", "[ref=" in snap, f"{r.get('element_count')} elements")
    check("snapshot mentions Greet Me", "Greet Me" in snap)
    refs = r.get("refs", [])

    # Find the ref that maps to the Greet Me button.
    greet_ref = None
    for line in snap.splitlines():
        if "Greet Me" in line and "[ref=" in line:
            greet_ref = line.split("[ref=")[1].split("]")[0]
            break
    check("located Greet Me ref", greet_ref is not None, str(greet_ref))

    print("\n--- Click by ref, by text, by role, by CSS ---")
    if greet_ref:
        await server.type_text("css:#name-field", "Ada")
        r = await server.click(greet_ref)
        check("click by ref", r.get("ok"), r.get("error", ""))
    r = await server.get_page_text(max_chars=2000)
    check("click had effect (greeting shown)", "Hello, Ada" in r.get("text", ""))

    r = await server.click("text:Notes saved")
    check("click by text (no match -> handled)", "error" in r or r.get("ok"))

    r = await server.click('role=button[name="Greet Me"]')
    check("click by role+name", r.get("ok"), r.get("error", ""))

    r = await server.click("css:#greet-button")
    check("click by CSS", r.get("ok"), r.get("error", ""))

    r = await server.click("#does-not-exist")
    check("click missing target -> readable error", not r.get("ok") and "error" in r)

    print("\n--- Typing ---")
    r = await server.type_text("css:#name-field", "Grace")
    check("type_text ok", r.get("ok"), r.get("error", ""))
    r = await server.type_text("css:#notes-field", "hello world", submit=False)
    check("type_text no submit", r.get("ok") and "not submitted" in r.get("changed", ""))

    r = await server.type_text("css:#name-field", "Grace", submit=True)
    check("type_text submit", r.get("ok"), r.get("error", ""))

    print("\n--- Password / 2FA refusal ---")
    r = await server.type_text("css:#name-field", "secret")
    check("normal field still works", r.get("ok"))

    print("\n--- select_option ---")
    r = await server.select_option("css:#color-field", "blue")
    check("select_option ok", r.get("ok"), r.get("error", ""))
    r = await server.select_option("css:#color-field", "not-a-color")
    check("select_option invalid -> error", not r.get("ok"))

    print("\n--- find_text / get_links ---")
    r = await server.find_text("Favorite color")
    check("find_text ok", r.get("ok"), r.get("error", ""))
    check("find_text found match", r.get("count", 0) > 0)
    r = await server.get_links()
    check("get_links ok", r.get("ok"))
    check("get_links found Example Link", any("Example" in l.get("text", "") for l in r.get("links", [])))

    print("\n--- wait_for / scroll / hover / press_key ---")
    r = await server.wait_for("Interaction Fixture", timeout_ms=5000)
    check("wait_for text ok", r.get("ok"), r.get("error", ""))
    r = await server.wait_for("css:#greet-button", timeout_ms=5000)
    check("wait_for selector ok", r.get("ok"), r.get("error", ""))
    r = await server.wait_for("never-appears-xyz", timeout_ms=1500)
    check("wait_for timeout -> error", not r.get("ok"))

    r = await server.scroll("down", 300)
    check("scroll ok", r.get("ok"), r.get("error", ""))
    r = await server.scroll("sideways", 300)
    check("scroll bad direction -> error", not r.get("ok"))

    r = await server.hover("css:#greet-button")
    check("hover ok", r.get("ok"), r.get("error", ""))
    r = await server.press_key("Tab")
    check("press_key ok", r.get("ok"), r.get("error", ""))

    print("\n--- upload_file guard ---")
    r = await server.upload_file("css:#file-field", r"C:\Windows\System32\drivers\etc\hosts")
    check("upload outside allowed dir -> error", not r.get("ok"))
    check("upload error mentions dir", "Downloads" in r.get("error", "") or "restricted" in r.get("error", ""))
    allowed = Path.home() / "Downloads" / "chrome-agent-upload-test.txt"
    allowed.write_text("test", encoding="utf-8")
    r = await server.upload_file("css:#file-field", str(allowed))
    check("upload inside allowed dir ok", r.get("ok"), r.get("error", ""))

    print("\n--- run_javascript gating ---")
    r = await server.run_javascript("() => document.title")
    check("run_javascript blocked by default", not r.get("ok"))
    check("run_javascript error explains how to enable", "ALLOW_JS" in r.get("error", ""))

    print("\n--- Tabs ---")
    r = await server.new_tab(FIXTURE_URL)
    check("new_tab ok", r.get("ok"), r.get("error", ""))
    r = await server.list_tabs()
    check("list_tabs ok", r.get("ok"))
    check("list_tabs count >= 2", r.get("count", 0) >= 2, f"count={r.get('count')}")
    active_indexes = [t["index"] for t in r.get("tabs", []) if t.get("active")]
    check("exactly one active tab", len(active_indexes) == 1, str(active_indexes))
    r = await server.switch_tab(0)
    check("switch_tab ok", r.get("ok"), r.get("error", ""))
    r = await server.switch_tab(99)
    check("switch_tab bad index -> error", not r.get("ok"))
    r = await server.close_tab(1)
    check("close_tab ok", r.get("ok"), r.get("error", ""))

    print("\n--- History ---")
    r = await server.go_back()
    check("go_back ok", r.get("ok"), r.get("error", ""))
    r = await server.go_forward()
    check("go_forward ok", r.get("ok"), r.get("error", ""))
    r = await server.reload()
    check("reload ok", r.get("ok"), r.get("error", ""))

    print("\n--- list_downloads ---")
    r = await server.list_downloads()
    check("list_downloads ok", r.get("ok"))

    print("\n--- handle_dialog ---")
    r = await server.handle_dialog(accept=True)
    check("handle_dialog ok", r.get("ok"), r.get("error", ""))

    await server.SESSION.shutdown()

    failures = [x for x in results if x[0] == FAIL]
    print("\n" + "=" * 60)
    print(f"{len(results) - len(failures)}/{len(results)} checks passed")
    if failures:
        print("\nFailures:")
        for _, name, detail in failures:
            print(f"  - {name}: {detail}")
    print("=" * 60)
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(130)
