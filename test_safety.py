#!/usr/bin/env python3
"""Stage 4 test: safety rails (password/2FA refusal), ALLOW_JS gating, audit log.

Run with:  python test_safety.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import server  # noqa: E402
from chrome_agent.config import SETTINGS  # noqa: E402

LOGIN = (Path(__file__).parent / "tests" / "login.html").resolve().as_uri()

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((PASS if condition else FAIL, name, detail))
    print(f"  [{PASS if condition else FAIL}] {name}" + (f" -- {detail}" if detail else ""))


async def main() -> int:
    await server.SESSION.connect()
    # Record where the audit log ends so we only check this run's entries.
    log_path = SETTINGS.log_dir / "audit.log"
    log_offset = 0
    if log_path.exists():
        log_offset = len(
            [l for l in log_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        )
    await server.open_url(LOGIN)

    print("\n--- Password / 2FA refusal ---")
    r = await server.type_text("css:#pass", "hunter2")
    check("type_text refuses password field", not r.get("ok"), r.get("error", ""))
    check("password refusal mentions logging in", "log in" in r.get("error", "").lower())

    r = await server.type_text("css:#otp", "123456")
    check("type_text refuses OTP field", not r.get("ok"), r.get("error", ""))

    r = await server.click("css:#pass")
    check("click refuses password field", not r.get("ok"), r.get("error", ""))

    r = await server.type_text("css:#user", "alice")
    check("normal field still accepted", r.get("ok"), r.get("error", ""))

    r = await server.get_page_snapshot()
    check("login_form_detected reported", r.get("login_form_detected") is True)

    r = await server.get_page_text(max_chars=2000)
    check("password value never in page text", "hunter2" not in r.get("text", ""))

    print("\n--- run_javascript gating ---")
    r = await server.run_javascript("() => document.title")
    check("blocked when ALLOW_JS=false", not r.get("ok"))
    check("error names the env var", "ALLOW_JS" in r.get("error", ""))

    # Re-load settings with ALLOW_JS enabled to verify the enabled path works.
    from chrome_agent import config as config_mod

    original = config_mod.SETTINGS
    object.__setattr__(original, "allow_js", True)
    server.SETTINGS = original
    try:
        r = await server.run_javascript("() => document.title")
        check("runs when ALLOW_JS=true", r.get("ok"), r.get("error", ""))
        check("returns the JS result", r.get("result") == "Login fixture", str(r.get("result")))
    finally:
        object.__setattr__(original, "allow_js", False)

    print("\n--- Audit log ---")
    log_path = SETTINGS.log_dir / "audit.log"
    check("audit log exists", log_path.exists(), str(log_path))
    # Only inspect entries written by this run, not the whole history.
    all_lines = [l for l in log_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    run_lines = all_lines[log_offset:]
    check("audit log has entries", len(run_lines) > 0, f"{len(run_lines)} entries")

    entries = [json.loads(l) for l in run_lines]
    check("entries have ts/tool/args", all("ts" in e and "tool" in e and "args" in e for e in entries))
    tools_seen = {e["tool"] for e in entries}
    for expected in ("open_url", "type_text", "click", "get_page_snapshot", "run_javascript"):
        check(f"logged tool '{expected}'", expected in tools_seen)

    blob = "\n".join(run_lines).lower()
    check("no password value logged", "hunter2" not in blob)
    check("no cookie/token values logged", "cookie" not in blob and "bearer" not in blob)

    # A redacted field must never be written verbatim.
    from chrome_agent.audit import AuditLog

    probe = AuditLog(log_path)
    probe.record("type_text", {"target": "x", "text": "ok", "api_token": "SECRETVALUE123"})
    tail = log_path.read_text(encoding="utf-8").splitlines()[-1]
    check("sensitive arg redacted in audit log", "SECRETVALUE123" not in tail, tail[:120])

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
