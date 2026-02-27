from __future__ import annotations

import json
from pathlib import Path

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    sync_playwright,
)

from .config import COOKIE_PATH, DATA_DIR, NAV_TIMEOUT, SEARCH_URL, STEALTH_SCRIPT


class BrowserManager:
    def __init__(self, *, headless: bool = True):
        self._headless: bool = headless
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    def start(self) -> Page:
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.firefox.launch(headless=self._headless)
        self._context = self._browser.new_context(locale="ko-KR")
        self._context.add_init_script(STEALTH_SCRIPT)
        self._restore_cookies()
        self._page = self._context.new_page()
        self._page.set_default_timeout(NAV_TIMEOUT)
        _ = self._page.goto(SEARCH_URL, wait_until="networkidle", timeout=NAV_TIMEOUT)
        return self._page

    # ------------------------------------------------------------------
    # Cookie persistence
    # ------------------------------------------------------------------

    def save_cookies(self) -> None:
        """Save browser cookies to disk for session reuse."""
        if self._context is None:
            return
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        cookies = self._context.cookies()
        COOKIE_PATH.write_text(json.dumps(cookies, ensure_ascii=False, indent=2))

    def _restore_cookies(self) -> None:
        """Restore previously saved cookies into the browser context."""
        if self._context is None:
            return
        if not COOKIE_PATH.is_file():
            return
        try:
            cookies = json.loads(COOKIE_PATH.read_text())
            if cookies:
                self._context.add_cookies(cookies)
        except (json.JSONDecodeError, OSError):
            pass

    def clear_cookies(self) -> None:
        """Delete saved cookie file."""
        if COOKIE_PATH.is_file():
            COOKIE_PATH.unlink()

    # ------------------------------------------------------------------

    def close(self) -> None:
        if self._context is not None:
            try:
                self._context.close()
            except Exception:
                pass
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
        self._context = None
        self._browser = None
        self._playwright = None
        self._page = None

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("Browser not started. Call start() first.")
        return self._page

    def __enter__(self) -> BrowserManager:
        _ = self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
