#!/usr/bin/env python3
"""Run every chrome-agent test suite in sequence.

The suites share one real Chrome instance, so they must not run in
parallel — doing so makes them fight over the active tab.

Usage:
    python run_tests.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PYTHON = HERE / ".venv" / "Scripts" / "python.exe"
if not PYTHON.exists():
    PYTHON = Path(sys.executable)

SUITES = [
    ("smoke (connect/read/screenshot/tools)", "test_chrome_agent.py"),
    ("interaction tools", "test_interaction.py"),
    ("safety rails", "test_safety.py"),
    ("active-tab tracking", "test_tabs.py"),
    ("dialogs & downloads", "test_dialogs.py"),
    ("MCP stdio end-to-end", "test_stdio.py"),
]


def main() -> int:
    failures: list[str] = []
    for label, script in SUITES:
        print("\n" + "=" * 70)
        print(f"RUNNING: {label}  ({script})")
        print("=" * 70)

        # Write output to a file rather than a pipe: Playwright's node driver
        # inherits stdout and keeps the pipe open, so communicate() would
        # block forever waiting for EOF even after the test exits.
        out_path = HERE / f".test-output-{script}.log"
        try:
            with out_path.open("w", encoding="utf-8") as out:
                result = subprocess.run(
                    [str(PYTHON), str(HERE / script)],
                    cwd=str(HERE),
                    stdout=out,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=300,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
        except subprocess.TimeoutExpired:
            print("   TIMED OUT after 300s")
            failures.append(label)
            continue
        finally:
            output = out_path.read_text(encoding="utf-8", errors="replace") if out_path.exists() else ""

        # Show the summary lines and any failures.
        for line in output.splitlines():
            if "checks passed" in line or line.strip().startswith("[FAIL]"):
                print("   " + line.strip())
        if result.returncode != 0:
            failures.append(label)
            if output.strip():
                print("\n   --- output tail ---")
                for line in output.splitlines()[-25:]:
                    print("   " + line)

    print("\n" + "=" * 70)
    if failures:
        print(f"FAILED suites: {len(failures)}/{len(SUITES)}")
        for name in failures:
            print(f"  - {name}")
        return 1
    print(f"ALL {len(SUITES)} SUITES PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
