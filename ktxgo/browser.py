from __future__ import annotations

import json

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    sync_playwright,
)

from .config import (
    COOKIE_PATH,
    DATA_DIR,
    NAV_TIMEOUT,
    SEARCH_URL,
    STEALTH_SCRIPT,
    STORAGE_STATE_PATH,
)


class BrowserManager:
    def __init__(self, *, headless: bool = True):
        self._headless: bool = headless
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    @staticmethod
    def _secure_state_permissions() -> None:
        # Restrict local session artifacts to the current user.
        try:
            if DATA_DIR.exists():
                DATA_DIR.chmod(0o700)
        except OSError:
            pass

        for path in (COOKIE_PATH, STORAGE_STATE_PATH):
            try:
                if path.exists():
                    path.chmod(0o600)
            except OSError:
                pass

    def start(self) -> Page:
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.firefox.launch(headless=self._headless)
        self._secure_state_permissions()
        context_kwargs: dict[str, str] = {"locale": "ko-KR"}
        if STORAGE_STATE_PATH.is_file():
            context_kwargs["storage_state"] = str(STORAGE_STATE_PATH)
        self._context = self._browser.new_context(**context_kwargs)
        self._context.add_init_script(STEALTH_SCRIPT)
        # Backward compatibility with older cookie-only sessions.
        if not STORAGE_STATE_PATH.is_file():
            self._restore_cookies()
        self._page = self._context.new_page()
        self._page.set_default_timeout(NAV_TIMEOUT)
        _ = self._page.goto(SEARCH_URL, wait_until="networkidle", timeout=NAV_TIMEOUT)
        return self._page

    # ------------------------------------------------------------------
    # Cookie persistence
    # ------------------------------------------------------------------

    def save_cookies(self) -> None:
        """Save browser session to disk for reuse."""
        if self._context is None:
            return
        DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._secure_state_permissions()
        try:
            self._context.storage_state(path=str(STORAGE_STATE_PATH))
            STORAGE_STATE_PATH.chmod(0o600)
        except Exception:
            pass

        # Keep cookie file for compatibility / fallback.
        try:
            cookies = self._context.cookies()
            COOKIE_PATH.write_text(json.dumps(cookies, ensure_ascii=False, indent=2))
            COOKIE_PATH.chmod(0o600)
        except Exception:
            pass

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
        """Delete saved session files."""
        if COOKIE_PATH.is_file():
            COOKIE_PATH.unlink()
        if STORAGE_STATE_PATH.is_file():
            STORAGE_STATE_PATH.unlink()

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
