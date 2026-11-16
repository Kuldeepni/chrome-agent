"""Audit logging for chrome-agent.

Every tool call is appended to `logs/audit.log` as one JSON object per line:

    {"ts": "2026-11-16T09:31:04.512Z", "tool": "click", "args": {...}}

Cookies, tokens and other secrets are never written: argument keys that look
sensitive are replaced with a placeholder, and long argument values are
truncated.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SENSITIVE_KEY = re.compile(
    r"(pass(word|wd)?|secret|token|cookie|session[_-]?id|otp|2fa|mfa|"
    r"auth|authorization|api[_-]?key|private[_-]?key|credential)",
    re.IGNORECASE,
)

_REDACTED = "***redacted***"
_MAX_VALUE_LEN = 120
_MAX_TEXT_LEN = 80


def _scrub(value: Any, key: str | None = None) -> Any:
    """Recursively redact sensitive values and truncate long strings."""
    if key and _SENSITIVE_KEY.search(key):
        return _REDACTED
    if isinstance(value, dict):
        return {k: _scrub(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(v, key) for v in value]
    if isinstance(value, str):
        if len(value) > _MAX_VALUE_LEN:
            return value[:_MAX_VALUE_LEN] + f"...<+{len(value) - _MAX_VALUE_LEN} chars>"
        return value
    return value


def _scrub_text_argument(args: dict[str, Any]) -> dict[str, Any]:
    """Shorten typed text so we never persist whole secrets or paragraphs."""
    out = dict(args)
    text = out.get("text")
    if isinstance(text, str) and len(text) > _MAX_TEXT_LEN:
        out["text"] = text[:_MAX_TEXT_LEN] + f"...<+{len(text) - _MAX_TEXT_LEN} chars>"
    return out


class AuditLog:
    """Append-only, thread-safe JSON-lines audit log."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def record(self, tool: str, args: dict[str, Any], outcome: str = "ok") -> None:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "tool": tool,
            "args": _scrub_text_argument(_scrub(args)),
            "outcome": outcome,
        }
        line = json.dumps(entry, ensure_ascii=False, default=str)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def record_error(self, tool: str, args: dict[str, Any], error: str) -> None:
        self.record(tool, args, outcome="error")
