"""Configuration loading for chrome-agent.

Reads settings from the environment, with values from a `.env` file in the
project root used as defaults (real environment variables always win).
"""

from __future__ import annotations

import os
import platform
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - dotenv is a declared dependency
        return
    load_dotenv(BASE_DIR / ".env", override=False)


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    cdp_url: str
    allow_js: bool
    allowed_upload_dir: Path
    download_dir: Path
    log_dir: Path
    default_timeout_ms: int
    dialog_timeout_ms: int
    follow_active_tab: bool
    profile_dir: Path


def _default_upload_dir() -> Path:
    home = Path.home()
    if platform.system() == "Windows":
        return home / "Downloads"
    return home / "Downloads"


def load_settings() -> Settings:
    _load_dotenv()

    cdp_url = os.environ.get("CHROME_CDP_URL", "http://localhost:9222").strip()
    allow_js = _as_bool(os.environ.get("ALLOW_JS"), default=False)

    upload_raw = os.environ.get("ALLOWED_UPLOAD_DIR", "").strip()
    allowed_upload_dir = (
        Path(upload_raw).expanduser().resolve()
        if upload_raw
        else _default_upload_dir().resolve()
    )

    download_dir = Path(
        os.environ.get("DOWNLOAD_DIR", str(BASE_DIR / "downloads"))
    ).expanduser()
    log_dir = Path(os.environ.get("LOG_DIR", str(BASE_DIR / "logs"))).expanduser()

    return Settings(
        cdp_url=cdp_url,
        allow_js=allow_js,
        allowed_upload_dir=allowed_upload_dir,
        download_dir=download_dir.resolve(),
        log_dir=log_dir.resolve(),
        default_timeout_ms=int(os.environ.get("DEFAULT_TIMEOUT_MS", "15000")),
        dialog_timeout_ms=int(os.environ.get("DIALOG_TIMEOUT_MS", "8000")),
        follow_active_tab=_as_bool(os.environ.get("FOLLOW_ACTIVE_TAB"), default=True),
        profile_dir=Path(
            os.environ.get("CHROME_PROFILE_DIR", str(BASE_DIR / "chrome-profile"))
        ).expanduser(),
    )


SETTINGS = load_settings()


def chrome_launch_command() -> str:
    """The exact command to start Chrome with remote debugging enabled."""
    profile = SETTINGS.profile_dir
    system = platform.system()
    if system == "Windows":
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            str(Path.home() / "AppData/Local/Google/Chrome/Application/chrome.exe"),
        ]
        exe = next((c for c in candidates if Path(c).exists()), candidates[0])
        return (
            f'"{exe}" --remote-debugging-port=9222 --user-data-dir="{profile}"'
        )
    if system == "Darwin":
        exe = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        return f'"{exe}" --remote-debugging-port=9222 --user-data-dir="{profile}"'
    return (
        f'google-chrome --remote-debugging-port=9222 --user-data-dir="{profile}"'
    )


def start_chrome_message(detail: str = "") -> str:
    cmd = chrome_launch_command()
    lines = [
        "Could not connect to a running Chrome instance over CDP.",
        "",
        "Chrome is probably not running with remote debugging enabled.",
        "Close any already-open Chrome windows, then run this in a terminal:",
        "",
        f"  {cmd}",
        "",
        "That starts Chrome with a dedicated profile folder "
        f"({SETTINGS.profile_dir}) and remote debugging on port 9222.",
        "Log in inside that window once and your session will be reused.",
    ]
    if detail:
        lines += ["", f"Underlying error: {detail}"]
    return "\n".join(lines)
