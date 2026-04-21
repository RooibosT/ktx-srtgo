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

from .config import (
    COOKIE_PATH,
    DATA_DIR,
    NAV_TIMEOUT,
    SEARCH_URL,
    STORAGE_STATE_PATH,
)


WEBDRIVER_HIDDEN_SCRIPT = (
    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
)
WEBDRIVER_FALSE_SCRIPT = (
    "Object.defineProperty(navigator, 'webdriver', {get: () => false});"
)


class BrowserManager:
    def __init__(
        self,
        *,
        headless: bool = True,
        use_saved_session: bool = True,
        initial_url: str | None = SEARCH_URL,
        use_stealth: bool = True,
        browser_name: str = "firefox",
        browser_channel: str | None = None,
        browser_executable: str | Path | None = None,
        browser_profile_dir: str | Path | None = None,
        record_har_path: str | Path | None = None,
        locale: str = "ko-KR",
        user_agent: str | None = None,
        viewport: dict[str, int] | None = None,
        screen: dict[str, int] | None = None,
        device_scale_factor: float | None = None,
        webdriver_mode: str = "default",
    ):
        self._headless: bool = headless
        self._use_saved_session: bool = use_saved_session
        self._initial_url: str | None = initial_url
        self._use_stealth: bool = use_stealth
        self._browser_name: str = browser_name
        self._browser_channel: str | None = browser_channel
        self._browser_executable: str | Path | None = browser_executable
        self._browser_profile_dir: str | Path | None = browser_profile_dir
        self._record_har_path: str | Path | None = record_har_path
        self._locale: str = locale
        self._user_agent: str | None = user_agent
        self._viewport: dict[str, int] | None = viewport
        self._screen: dict[str, int] | None = screen
        self._device_scale_factor: float | None = device_scale_factor
        self._webdriver_mode: str = webdriver_mode
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
        launcher = getattr(self._playwright, self._browser_name)
        launch_kwargs: dict[str, object] = {"headless": self._headless}
        if self._browser_channel:
            launch_kwargs["channel"] = self._browser_channel
        if self._browser_executable:
            launch_kwargs["executable_path"] = str(self._browser_executable)
        self._secure_state_permissions()
        context_kwargs: dict[str, object] = {"locale": self._locale}
        if self._user_agent:
            context_kwargs["user_agent"] = self._user_agent
        if self._viewport:
            context_kwargs["viewport"] = self._viewport
        if self._screen:
            context_kwargs["screen"] = self._screen
        if self._device_scale_factor is not None:
            context_kwargs["device_scale_factor"] = self._device_scale_factor
        if self._record_har_path:
            context_kwargs["record_har_path"] = str(self._record_har_path)
            context_kwargs["record_har_content"] = "omit"
        if (
            self._use_saved_session
            and self._browser_profile_dir is None
            and STORAGE_STATE_PATH.is_file()
        ):
            context_kwargs["storage_state"] = str(STORAGE_STATE_PATH)
        if self._browser_profile_dir is None:
            self._browser = launcher.launch(**launch_kwargs)
            self._context = self._browser.new_context(**context_kwargs)
        else:
            self._context = launcher.launch_persistent_context(
                str(self._browser_profile_dir),
                **launch_kwargs,
                **context_kwargs,
            )
            try:
                self._browser = self._context.browser
            except Exception:
                self._browser = None
        webdriver_script: str | None = None
        if self._webdriver_mode == "hidden" or (
            self._webdriver_mode == "default" and self._use_stealth
        ):
            webdriver_script = WEBDRIVER_HIDDEN_SCRIPT
        elif self._webdriver_mode == "false":
            webdriver_script = WEBDRIVER_FALSE_SCRIPT
        if webdriver_script is not None:
            self._context.add_init_script(webdriver_script)
        # Backward compatibility with older cookie-only sessions.
        if (
            self._use_saved_session
            and self._browser_profile_dir is None
            and not STORAGE_STATE_PATH.is_file()
        ):
            self._restore_cookies()
        pages = list(getattr(self._context, "pages", []))
        self._page = pages[0] if pages else self._context.new_page()
        self._page.set_default_timeout(NAV_TIMEOUT)
        if self._initial_url is not None:
            _ = self._page.goto(
                self._initial_url,
                wait_until="networkidle",
                timeout=NAV_TIMEOUT,
            )
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
