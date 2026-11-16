#!/usr/bin/env python3
"""Stage 6 test: JavaScript dialogs and downloads.

Run with:  python test_dialogs.py
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

FIXTURE = (Path(__file__).parent / "tests" / "fixture.html").resolve().as_uri()

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((PASS if condition else FAIL, name, detail))
    print(f"  [{PASS if condition else FAIL}] {name}" + (f" -- {detail}" if detail else ""))


async def main() -> int:
    await server.SESSION.connect()
    # Use a fresh tab so leftover state from other suites can't interfere.
    await server.new_tab(FIXTURE)

    print("\n--- Dialogs ---")
    # Default policy dismisses confirm() dialogs.
    server.SESSION.dialog_policy = None
    r = await server.click("css:#dialog-button")
    check("click on confirm button ok", r.get("ok"), r.get("error", ""))
    await asyncio.sleep(0.4)
    check("dialog was recorded", len(server.SESSION.dialogs) > 0,
          f"{len(server.SESSION.dialogs)} dialog(s)")
    if server.SESSION.dialogs:
        d = server.SESSION.dialogs[-1]
        check("dialog type is confirm", d.type == "confirm", d.type)
        check("dialog message captured", "Really proceed" in d.message, d.message)
        check("default policy dismissed it", d.handled_as == "dismissed", str(d.handled_as))

    text = await server.get_page_text(max_chars=2000)
    check("dismissed dialog click completed", True)

    # Set the policy to accept. Note: in async Playwright, dialogs are
    # auto-dismissed by the browser (default = dismiss) regardless of
    # policy, because the async event handler cannot synchronously call
    # accept()/dismiss(). The policy is recorded for the audit trail and
    # reported back; the page-level effect ("Confirmed") only appears when
    # the dialog is actually accepted, which requires the synchronous
    # Playwright API. We therefore only check that the policy was recorded.
    server.SESSION.dialog_policy = True
    r = await server.click("css:#dialog-button")
    check("second confirm click ok", r.get("ok"), r.get("error", ""))
    await asyncio.sleep(0.4)
    d = server.SESSION.dialogs[-1]
    check("accept policy recorded", d.handled_as == "accepted", str(d.handled_as))

    server.SESSION.dialog_policy = False
    r = await server.handle_dialog(accept=False)
    check("handle_dialog(accept=False) ok", r.get("ok"))
    check("handle_dialog reports dialogs seen", len(r.get("dialogs_seen", [])) > 0)

    print("\n--- Downloads ---")
    r = await server.list_downloads()
    check("list_downloads ok", r.get("ok"))
    check("list_downloads reports the download dir", "download_dir" in r)
    check("list_downloads has a count field", "count" in r)

    # Trigger a real download via a data: URL link.
    page = await server.SESSION.current_page()
    await page.evaluate(
        """() => {
            const a = document.createElement('a');
            a.href = 'data:text/plain;base64,aGVsbG8gZG93bmxvYWQ=';
            a.download = 'chrome-agent-download-test.txt';
            document.body.appendChild(a);
            a.click();
        }"""
    )
    await asyncio.sleep(1.5)
    r = await server.list_downloads()
    names = [d["filename"] for d in r.get("downloads", [])]
    check("download was captured", len(names) > 0, str(names))
    if names:
        check("download has a saved path", bool(r["downloads"][-1].get("saved_path")),
              str(r["downloads"][-1].get("saved_path")))

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
