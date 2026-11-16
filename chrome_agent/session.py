"""Browser session management for chrome-agent.

Owns the single shared Playwright connection to a real Chrome instance
connected over the Chrome DevTools Protocol, tracks the "current" (active)
tab, resolves element targets, and tracks downloads and dialogs.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from playwright.async_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from .config import Settings, start_chrome_message

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

#: Matches an aria ref such as ``e12`` or ``f1e3``.
_ARIA_REF_RE = re.compile(r"^[ef]\d+(?:e\d+)*$")
#: Matches the ``[ref=e12]`` marker inside an aria snapshot line.
_REF_IN_SNAPSHOT_RE = re.compile(r"\[ref=([^\]]+)\]")
#: Matches a numeric index into the cached snapshot element list.
_INDEX_RE = re.compile(r"^\d+$")

#: Explicit target prefixes understood by :meth:`Session.resolve_target`.
_PREFIXES = ("text", "css", "xpath", "role", "ref", "id")

_ROLE_RE = re.compile(
    r"^role=(?P<role>[a-zA-Z]+)\s*(?:\[name=(?P<q1>[\"'])(?P<name>.*?)(?P=q1)\]|\[name=(?P<name2>[^\]]*)\]|"
    r"\s+(?P<name3>\"[^\"]*\"|'[^']*'))?$"
)

#: Attribute/placeholder fragments that indicate a one-time-code field.
_OTP_HINTS = (
    "otp",
    "2fa",
    "two-factor",
    "twofactor",
    "two factor",
    "one-time-code",
    "verification code",
    "security code",
    "auth code",
    "mfa code",
)

#: Attribute/placeholder fragments that indicate a password field.
_PASSWORD_HINTS = ("passw", "pwd", "passphrase", "secret")

_SELECTOR_CHARS = re.compile(r"[#.\[\]()>+~:@]")


def split_prefix(target: str) -> tuple[str | None, str]:
    """Split ``prefix:rest`` into its parts, if the prefix is recognised.

    Returns ``(None, target)`` when the target carries no known prefix. Only
    the first colon is considered, so ``css:a:hover`` keeps its suffix
    intact.
    """
    prefix, sep, rest = target.partition(":")
    if sep and prefix.lower() in _PREFIXES:
        return prefix.lower(), rest
    return None, target


class TargetNotFound(Exception):
    """Raised when a target cannot be resolved to an element."""


class PageNotReady(Exception):
    """Raised when the browser or the current page is unavailable."""


class PageNotFound(Exception):
    """Raised when a requested tab index does not exist."""


@dataclass
class DownloadRecord:
    """A file downloaded during this session."""

    filename: str
    url: str
    saved_path: str | None = None
    size_bytes: int | None = None
    started_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "url": self.url,
            "saved_path": self.saved_path,
            "size_bytes": self.size_bytes,
            "started_at": time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(self.started_at)
            ),
        }


@dataclass
class DialogRecord:
    """A JavaScript dialog that was raised by the page."""

    type: str
    message: str
    default_value: str
    handled_as: str | None = None
    raised_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "message": self.message,
            "default_value": self.default_value,
            "handled_as": self.handled_as,
            "raised_at": time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(self.raised_at)
            ),
        }


class Session:
    """A shared connection to the user's real Chrome browser."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._playwright: Any = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._current_page: Page | None = None
        self._connect_lock = asyncio.Lock()

        #: Downloaded files, in the order they were saved.
        self.downloads: list[DownloadRecord] = []
        #: Dialogs raised by the page, in order.
        self.dialogs: list[DialogRecord] = []
        #: Dialog policy applied to *future* dialogs: True=accept, False=dismiss.
        self.dialog_policy: bool | None = None

        #: Per-page cache of the last aria snapshot, keyed by page id.
        self._snapshots: dict[Any, tuple[float, str, list[str]]] = {}
        #: CDP sessions used to disable Playwright's focus emulation.
        self._cdp_sessions: dict[Any, Any] = {}
        #: Pages whose focus emulation was successfully disabled.
        self._focus_ok: set[Any] = set()
        #: Pages where disabling focus emulation failed.
        self._focus_broken: set[Any] = set()
        #: Pending focus-fix tasks, keyed by page.
        self._focus_tasks: dict[Any, Any] = {}
        #: Browser-level CDP session, used to map pages to Chrome windows.
        self._browser_cdp: Any = None
        #: Whether the pinned tab was the focused one on the last probe.
        self._pinned_focused: bool = False

    # ------------------------------------------------------------------ #
    # Connection
    # ------------------------------------------------------------------ #

    @property
    def connected(self) -> bool:
        return self._browser is not None and self._browser.is_connected()

    async def connect(self) -> Browser:
        """Connect to the running Chrome over CDP, reusing the shared connection."""
        if self.connected and self._browser is not None:
            return self._browser

        async with self._connect_lock:
            if self.connected and self._browser is not None:
                return self._browser

            if self._playwright is None:
                self._playwright = await async_playwright().start()

            try:
                browser = await self._playwright.chromium.connect_over_cdp(
                    self.settings.cdp_url, timeout=15_000
                )
            except Exception as exc:  # noqa: BLE001 - surfaced to the user as text
                await self._disconnect()
                raise PageNotReady(start_chrome_message(str(exc))) from exc

            self._browser = browser
            contexts = browser.contexts
            if contexts:
                self._context = contexts[0]
            else:  # pragma: no cover - a CDP browser always has a default context
                self._context = await browser.new_context()

            self._context.on("page", self._on_page)
            for page in self._context.pages:
                self._bind_page(page)
                await self._disable_focus_emulation(page)

            try:
                self._browser_cdp = await browser.new_browser_cdp_session()
            except Exception:  # pragma: no cover - window grouping is optional
                self._browser_cdp = None

            if self._context.pages:
                self._current_page = self._context.pages[-1]

            return browser

    async def _disconnect(self) -> None:
        self._browser = None
        self._context = None
        self._current_page = None
        self._snapshots.clear()
        self._cdp_sessions.clear()
        self._focus_ok.clear()
        self._focus_broken.clear()
        self._focus_tasks.clear()
        self._browser_cdp = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:  # pragma: no cover - best effort cleanup
                pass
            self._playwright = None

    async def shutdown(self) -> None:
        """Close the connection without closing the user's browser."""
        await self._disconnect()

    # ------------------------------------------------------------------ #
    # Page / tab management
    # ------------------------------------------------------------------ #

    def _on_page(self, page: Page) -> None:
        self._bind_page(page)
        # A brand new tab becomes the interesting one.
        self._current_page = page
        self._schedule_focus_fix(page)

    def _bind_page(self, page: Page) -> None:
        page.on("dialog", self._on_dialog)
        page.on("download", self._on_download)
        page.on("close", lambda p=page: self._on_page_closed(p))

    def _schedule_focus_fix(self, page: Page) -> None:
        """Queue the focus-emulation fix for a page, once."""
        if page in self._focus_tasks:
            return
        try:
            task = asyncio.get_running_loop().create_task(
                self._disable_focus_emulation(page)
            )
        except RuntimeError:  # pragma: no cover - no loop running
            return
        self._focus_tasks[page] = task
        task.add_done_callback(lambda t, p=page: self._focus_tasks.pop(p, None))

    async def _disable_focus_emulation(self, page: Page) -> None:
        """Restore real focus reporting for a page.

        Playwright enables ``Emulation.setFocusEmulationEnabled`` on every page
        it attaches to, which forces ``document.hasFocus()`` to always return
        true. That makes it impossible to tell which tab the user is actually
        looking at, so turn it off for every page we bind.

        Playwright re-applies the override on navigation, so this may be
        called repeatedly for the same page.

        Pages where this fails are remembered in ``_focus_broken`` so they are
        excluded from focus detection (they would always report focus).

        The page object itself is used as the key rather than ``id(page)``:
        CPython reuses ids of freed objects, which would let a new tab inherit
        a stale "focus is reliable" flag and always look active.
        """
        if page in self._focus_broken:
            return
        if self._context is None:
            return
        cdp = self._cdp_sessions.get(page)
        try:
            if cdp is None:
                cdp = await self._context.new_cdp_session(page)
                self._cdp_sessions[page] = cdp
            await cdp.send("Emulation.setFocusEmulationEnabled", {"enabled": False})
            self._focus_ok.add(page)
        except Exception:
            # Not fatal: we simply exclude this page from focus detection.
            self._focus_broken.add(page)
            self._focus_ok.discard(page)

    def _on_page_closed(self, page: Page) -> None:
        self._snapshots.pop(page, None)
        self._focus_ok.discard(page)
        self._focus_broken.discard(page)
        self._cdp_sessions.pop(page, None)
        if self._current_page is page:
            self._current_page = None

    async def _has_focus(self, page: Page) -> bool:
        """True when this page is the tab the user is actually looking at."""
        try:
            return bool(await page.evaluate("() => document.hasFocus()"))
        except Exception:
            return False

    async def pages(self) -> list[Page]:
        """All open tabs in the browser, in creation order."""
        await self.connect()
        assert self._context is not None
        return list(self._context.pages)

    def set_current_page(self, page: Page) -> None:
        """Pin the given tab as the current one."""
        self._current_page = page
        self._pinned_focused = True

    async def _window_key(self, page: Page) -> str:
        """Identify the Chrome window a page belongs to.

        Tabs in different windows can each report ``hasFocus() === true``
        (focus is a per-window property), so pages are grouped by window
        before the active tab is picked.

        The real CDP window id is used. Window geometry is not usable here:
        programmatically created windows overlap and therefore share the same
        screen coordinates.
        """
        cdp = self._cdp_sessions.get(page)
        if cdp is None or self._browser is None:
            return ""
        try:
            info = await cdp.send("Target.getTargetInfo")
            target_id = info["targetInfo"]["targetId"]
            window = await self._browser_cdp.send(
                "Browser.getWindowForTarget", {"targetId": target_id}
            )
            return f"window:{window['windowId']}"
        except Exception:
            return ""

    async def current_page(self) -> Page:
        """Return the active tab, tracking tabs the user switches to manually.

        Chrome's ``document.hasFocus()`` is the only reliable signal for which
        tab is in the foreground, so it is used directly. Pages are probed in
        parallel to keep this fast when many tabs are open.

        Focus is a *per-window* property, so with several Chrome windows open
        more than one tab can report focus. To keep behaviour predictable the
        session stays on the tab it last acted on, and only moves when it can
        tell that the user moved: the pinned tab loses focus *and* another tab
        gains it (a tab switch, in this window or another one).
        """
        await self.connect()
        assert self._context is not None

        pages = [p for p in self._context.pages if not p.is_closed()]
        if not pages:
            page = await self._context.new_page()
            self._bind_page(page)
            self._current_page = page
            self._pinned_focused = True
            return page

        if len(pages) == 1:
            self._current_page = pages[0]
            self._pinned_focused = True
            return pages[0]

        # Make sure every page has real focus reporting before we trust it.
        for page in pages:
            await self._disable_focus_emulation(page)

        trusted = [p for p in pages if p in self._focus_ok]
        if not trusted:
            if self._current_page is not None and not self._current_page.is_closed():
                return self._current_page
            self._current_page = pages[-1]
            return self._current_page

        results = await asyncio.gather(
            *(self._has_focus(p) for p in trusted), return_exceptions=True
        )
        focused = [page for page, ok in zip(trusted, results) if ok is True]

        pinned = self._current_page
        if pinned is None or pinned.is_closed():
            self._current_page = focused[0] if focused else pages[-1]
            self._pinned_focused = bool(focused)
            return self._current_page

        if pinned in focused:
            self._pinned_focused = True
            return pinned

        if not self._pinned_focused:
            # The pinned tab was never the focused one (we navigated a
            # background tab). Stay put rather than guessing.
            return pinned

        # The pinned tab lost focus. Follow the user, preferring a tab in the
        # same window (a tab switch) over a tab in another window.
        if focused:
            pinned_key = await self._window_key(pinned)
            if pinned_key:
                keys = await asyncio.gather(
                    *(self._window_key(p) for p in focused), return_exceptions=True
                )
                for page, key in zip(focused, keys):
                    if key == pinned_key:
                        self._current_page = page
                        self._pinned_focused = True
                        return page
            self._current_page = focused[0]
            self._pinned_focused = True
            return self._current_page

        # Nothing has focus (window minimised or browser in the background).
        self._pinned_focused = False
        return pinned

    async def switch_tab(self, index: int) -> Page:
        """Focus the tab at ``index`` and make it the current tab."""
        await self.connect()
        assert self._context is not None
        pages = [p for p in self._context.pages if not p.is_closed()]
        if not pages:
            raise PageNotReady("There are no open tabs in Chrome.")
        if index < 0 or index >= len(pages):
            raise PageNotFound(
                f"Tab index {index} is out of range (0-{len(pages) - 1})."
                if len(pages) > 1
                else f"Tab index {index} is out of range (only tab 0 exists)."
            )
        page = pages[index]
        await self._disable_focus_emulation(page)
        try:
            await page.bring_to_front()
        except Exception:  # pragma: no cover - not fatal
            pass
        self._current_page = page
        return page

    async def new_tab(self, url: str | None = None) -> Page:
        await self.connect()
        assert self._context is not None
        page = await self._context.new_page()
        self._bind_page(page)
        await self._disable_focus_emulation(page)
        try:
            await page.bring_to_front()
        except Exception:  # pragma: no cover - not fatal
            pass
        self._current_page = page
        if url:
            await page.goto(url, wait_until="domcontentloaded")
        return page

    async def close_tab(self, index: int) -> Page:
        await self.connect()
        assert self._context is not None
        pages = [p for p in self._context.pages if not p.is_closed()]
        if index < 0 or index >= len(pages):
            raise PageNotFound(
                f"Tab index {index} is out of range (0-{len(pages) - 1})."
                if len(pages) > 1
                else f"Tab index {index} is out of range (only tab 0 exists)."
            )
        page = pages[index]
        await page.close()
        self._snapshots.pop(page, None)
        if self._current_page is page:
            self._current_page = None
        return page

    # ------------------------------------------------------------------ #
    # Dialogs and downloads
    # ------------------------------------------------------------------ #

    async def _on_dialog(self, dialog: Any) -> None:
        record = DialogRecord(
            type=dialog.type,
            message=dialog.message,
            default_value=dialog.default_value or "",
        )
        self.dialogs.append(record)
        policy = self.dialog_policy
        try:
            if policy is True:
                await dialog.accept(record.default_value or "")
                record.handled_as = "accepted"
            else:
                await dialog.dismiss()
                record.handled_as = "dismissed"
        except Exception:  # pragma: no cover - dialog already gone
            record.handled_as = "failed"

    async def _on_download(self, download: Any) -> None:
        self.settings.download_dir.mkdir(parents=True, exist_ok=True)
        filename = download.suggested_filename or "download"
        target = self.settings.download_dir / filename
        counter = 1
        while target.exists():
            target = self.settings.download_dir / f"{Path(filename).stem}({counter}){Path(filename).suffix}"
            counter += 1
        record = DownloadRecord(filename=target.name, url=download.url)
        self.downloads.append(record)
        try:
            await download.save_as(str(target))
            record.saved_path = str(target)
            try:
                record.size_bytes = target.stat().st_size
            except OSError:  # pragma: no cover
                record.size_bytes = None
        except Exception:  # pragma: no cover - stream may fail mid-flight
            record.saved_path = None

    # ------------------------------------------------------------------ #
    # Snapshots and target resolution
    # ------------------------------------------------------------------ #

    async def snapshot(self, page: Page) -> tuple[str, list[str]]:
        """Return the aria snapshot text and the ordered list of element refs."""
        text = await page.aria_snapshot(mode="ai")
        refs: list[str] = []
        seen: set[str] = set()
        for match in _REF_IN_SNAPSHOT_RE.finditer(text):
            ref = match.group(1)
            if ref not in seen:
                seen.add(ref)
                refs.append(ref)
        self._snapshots[page] = (time.monotonic(), text, refs)
        return text, refs

    async def resolve_target(self, page: Page, target: str) -> Any:
        """Resolve a target string to a Playwright locator.

        Supported forms, in resolution order:

        * ``e12`` / ``12``   - an element ref from :meth:`snapshot`
        * ``ref:e12``        - the same, explicitly
        * ``text:Sign in``   - visible text
        * ``css:.btn``       - CSS selector
        * ``xpath://button`` - XPath selector
        * ``role=button[name="Save"]`` - role + accessible name
        * ``id:submit``      - element id
        * ``Sign in``        - bare text (falls back to CSS if it looks like one)
        """
        target = (target or "").strip()
        if not target:
            raise TargetNotFound("No target was provided.")

        # Explicit prefixes.
        prefix, rest = split_prefix(target)
        if prefix is not None:
            if prefix == "text":
                return await self._locator_by_text(page, rest)
            if prefix == "css":
                return await self._locator_by_css(page, rest)
            if prefix == "xpath":
                return page.locator(f"xpath={rest}").first
            if prefix == "id":
                return await self._locator_by_css(page, f"#{rest}")
            if prefix == "ref":
                return self._locator_by_ref(page, rest)

        # Role + name.
        role_match = _ROLE_RE.match(target)
        if role_match:
            role = role_match.group("role")
            name = (
                role_match.group("name")
                or role_match.group("name2")
                or role_match.group("name3")
            )
            if name:
                name = name.strip("\"'")
            locator = (
                page.get_by_role(role.lower(), name=name)
                if name
                else page.get_by_role(role.lower())
            )
            return locator.first

        # Aria ref or snapshot index.
        if _ARIA_REF_RE.match(target) or _INDEX_RE.match(target):
            return self._locator_by_ref(page, target)

        # Bare CSS selector vs. bare text.
        if _SELECTOR_CHARS.search(target):
            try:
                locator = page.locator(target)
                if await locator.count() > 0:
                    return locator.first
            except Exception:
                pass
            raise TargetNotFound(
                f"No element matched the CSS selector '{target}'. Take a fresh "
                "get_page_snapshot() and use an element ref instead."
            )
        return await self._locator_by_text(page, target)

    async def _locator_by_css(self, page: Page, selector: str) -> Any:
        """Resolve a CSS selector, failing with a readable message."""
        try:
            locator = page.locator(selector)
        except Exception as exc:
            raise TargetNotFound(
                f"'{selector}' is not a valid CSS selector: {exc}"
            ) from exc
        if await locator.count() == 0:
            raise TargetNotFound(
                f"No element matched the CSS selector '{selector}'. Take a fresh "
                "get_page_snapshot() and use an element ref instead."
            )
        return locator.first

    def _locator_by_ref(self, page: Page, ref: str) -> Any:
        cached = self._snapshots.get(page)
        if cached is None:
            raise TargetNotFound(
                f"No snapshot is available for this tab, so ref '{ref}' cannot be "
                "resolved. Call get_page_snapshot() first."
            )
        _, _, refs = cached
        if _INDEX_RE.match(ref):
            index = int(ref)
            if index < 0 or index >= len(refs):
                raise TargetNotFound(
                    f"Snapshot index {index} is out of range; the last snapshot had "
                    f"{len(refs)} elements."
                )
            ref = refs[index]
        try:
            return page.get_by_ref(ref).first
        except Exception as exc:
            raise TargetNotFound(
                f"Ref '{ref}' is no longer valid (the page changed since the "
                f"snapshot). Take a new snapshot. ({exc})"
            ) from exc

    async def _locator_by_text(self, page: Page, text: str) -> Any:
        """Resolve visible text to an element.

        Uses Playwright's text engine so that text containing spaces, colons
        or quotes is matched literally rather than being parsed as a
        selector.
        """
        locator = page.get_by_text(text, exact=False)
        if await locator.count() > 0:
            return locator.first
        raise TargetNotFound(
            f"No element matched the text '{text}'. Take a fresh "
            "get_page_snapshot() and use the element ref instead."
        )

    # ------------------------------------------------------------------ #
    # Safety helpers
    # ------------------------------------------------------------------ #

    async def locator_is_sensitive(self, locator: Any) -> tuple[bool, str]:
        """Detect password / 2FA inputs so we never read or type into them."""
        try:
            info = await locator.evaluate(
                """el => ({
                    tag: (el.tagName || '').toLowerCase(),
                    type: (el.getAttribute && el.getAttribute('type') || '').toLowerCase(),
                    autocomplete: (el.getAttribute && el.getAttribute('autocomplete') || '').toLowerCase(),
                    name: (el.getAttribute && el.getAttribute('name') || '').toLowerCase(),
                    id: (el.getAttribute && el.getAttribute('id') || '').toLowerCase(),
                    placeholder: (el.getAttribute && el.getAttribute('placeholder') || '').toLowerCase(),
                    ariaLabel: (el.getAttribute && el.getAttribute('aria-label') || '').toLowerCase(),
                })"""
            )
        except Exception:
            return False, ""

        if info.get("type") == "password":
            return True, "password field"

        haystack = " ".join(
            str(info.get(key) or "")
            for key in ("autocomplete", "name", "id", "placeholder", "ariaLabel")
        )
        if "one-time-code" in haystack:
            return True, "one-time-code field"
        for hint in _OTP_HINTS:
            if hint in haystack:
                return True, "two-factor / one-time-code field"
        for hint in _PASSWORD_HINTS:
            if hint in haystack:
                return True, "password field"
        return False, ""

    async def page_has_login_form(self, page: Page) -> bool:
        """True when the current page shows a password input."""
        try:
            count = await page.locator('input[type="password"]').count()
        except Exception:
            return False
        return count > 0

    def upload_path_allowed(self, file_path: str) -> Path:
        """Validate an upload path against ``ALLOWED_UPLOAD_DIR``."""
        allowed = self.settings.allowed_upload_dir
        candidate = Path(os.path.expandvars(os.path.expanduser(file_path))).resolve()
        try:
            candidate.relative_to(allowed)
        except ValueError:
            raise TargetNotFound(
                f"Uploads are restricted to {allowed}. '{file_path}' is outside "
                "that directory."
            ) from None
        if not candidate.exists():
            raise TargetNotFound(f"The file '{candidate}' does not exist.")
        if not candidate.is_file():
            raise TargetNotFound(f"'{candidate}' is not a file.")
        return candidate
