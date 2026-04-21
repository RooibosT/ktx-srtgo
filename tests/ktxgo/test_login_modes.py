from __future__ import annotations

from click.testing import CliRunner

from ktxgo import cli
from ktxgo.korail import Train


class _ManualLoginAPI:
    def __init__(self) -> None:
        self.prefill_called = False
        self.manual_login_calls: list[tuple[int, bool, bool]] = []
        self.wait_calls = 0

    def wait_for_login_stable(self, **kwargs) -> bool:
        self.wait_calls += 1
        return False

    def prefill_login_form(self, login_id: str, login_pass: str) -> bool:
        self.prefill_called = True
        raise AssertionError("manual login only must not prefill credentials")

    def login_manual(
        self,
        timeout_s: int,
        *,
        open_login_page: bool = True,
        touch_password_on_comm_error: bool = True,
    ) -> bool:
        self.manual_login_calls.append(
            (timeout_s, open_login_page, touch_password_on_comm_error)
        )
        return True


class _HeadedManager:
    _headless = False
    page = object()

    def __init__(self) -> None:
        self.saved = False

    def close(self) -> None:
        raise AssertionError("headed manual login should not restart the browser")

    def start(self) -> None:
        raise AssertionError("headed manual login should not restart the browser")

    def save_cookies(self) -> None:
        self.saved = True


def test_ensure_login_manual_login_only_skips_prefill_and_no_input_touch(
    monkeypatch,
) -> None:
    api = _ManualLoginAPI()
    manager = _HeadedManager()

    monkeypatch.setattr(
        cli,
        "_load_login_credentials",
        lambda: (_ for _ in ()).throw(
            AssertionError("manual login only must not load saved credentials")
        ),
    )

    result = cli._ensure_login(
        api,
        manager,
        headless=False,
        manual_login_only=True,
    )

    assert result is api
    assert api.prefill_called is False
    assert api.manual_login_calls == [(300, True, False)]
    assert manager.saved is True


def test_ensure_login_force_relogin_skips_valid_session_check(monkeypatch) -> None:
    class ForceReloginAPI(_ManualLoginAPI):
        def wait_for_login_stable(self, **kwargs) -> bool:
            raise AssertionError("force relogin must not reuse the saved session")

    api = ForceReloginAPI()
    manager = _HeadedManager()
    monkeypatch.setattr(cli, "_load_login_credentials", lambda: None)

    result = cli._ensure_login(
        api,
        manager,
        headless=False,
        manual_login_only=True,
        force_relogin=True,
    )

    assert result is api
    assert api.manual_login_calls == [(300, True, False)]
    assert manager.saved is True


def test_cli_passes_manual_login_flags_and_disables_saved_session(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class DummyManager:
        def __init__(
            self,
            *,
            headless: bool,
            use_saved_session: bool = True,
            browser_name: str = "firefox",
            browser_channel: str | None = None,
            browser_executable=None,
            browser_profile_dir=None,
            **kwargs,
        ):
            captured["manager_headless"] = headless
            captured["use_saved_session"] = use_saved_session
            captured["browser_name"] = browser_name
            captured["browser_channel"] = browser_channel
            captured["browser_executable"] = browser_executable
            captured["browser_profile_dir"] = browser_profile_dir
            captured["extra_kwargs"] = kwargs
            self.page = object()

        def __enter__(self) -> DummyManager:
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

    class DummyAPI:
        def __init__(self, page: object):
            del page

        def search(self, *args, **kwargs) -> list[Train]:
            return []

    def fake_ensure_login(
        api: DummyAPI,
        manager: DummyManager,
        headless: bool,
        *,
        manual_login_only: bool = False,
        force_relogin: bool = False,
        external_firefox: str = "firefox",
        external_firefox_profile=None,
    ) -> DummyAPI:
        captured["ensure_headless"] = headless
        captured["manual_login_only"] = manual_login_only
        captured["force_relogin"] = force_relogin
        captured["external_firefox"] = external_firefox
        captured["external_firefox_profile"] = external_firefox_profile
        return api

    monkeypatch.setattr(cli, "configure_keyring_backend", lambda: None)
    monkeypatch.setattr(cli, "BrowserManager", DummyManager)
    monkeypatch.setattr(cli, "KorailAPI", DummyAPI)
    monkeypatch.setattr(cli, "_ensure_login", fake_ensure_login)
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    monkeypatch.setattr(cli.signal, "signal", lambda *args, **kwargs: None)

    result = CliRunner().invoke(
        cli.main,
        [
            "--no-interactive",
            "--max-attempts",
            "1",
            "--manual-login-only",
            "--force-relogin",
        ],
    )

    assert result.exit_code == 0
    assert captured == {
        "manager_headless": True,
        "use_saved_session": False,
        "browser_name": "firefox",
        "browser_channel": None,
        "browser_executable": None,
        "browser_profile_dir": None,
        "extra_kwargs": {
            "locale": "ko-KR",
            "user_agent": None,
            "viewport": None,
                "screen": None,
                "device_scale_factor": None,
                "webdriver_mode": "default",
            },
        "ensure_headless": True,
        "manual_login_only": True,
        "force_relogin": True,
        "external_firefox": "firefox",
        "external_firefox_profile": None,
    }


def test_force_relogin_headless_reload_uses_newly_saved_session(monkeypatch) -> None:
    class VisibleAPI(_ManualLoginAPI):
        pass

    class HeadlessAPI:
        def wait_for_login_stable(self, **kwargs) -> bool:
            return True

    class RestartingManager:
        def __init__(self) -> None:
            self._headless = True
            self._use_saved_session = False
            self.page = object()
            self.started_with_saved_session: list[bool] = []

        def close(self) -> None:
            pass

        def start(self) -> None:
            self.started_with_saved_session.append(self._use_saved_session)

        def save_cookies(self) -> None:
            pass

    visible_api = VisibleAPI()
    headless_api = HeadlessAPI()
    api_results = iter([visible_api, headless_api])
    manager = RestartingManager()

    monkeypatch.setattr(cli, "KorailAPI", lambda page: next(api_results))
    monkeypatch.setattr(cli, "_load_login_credentials", lambda: None)

    result = cli._ensure_login(
        _ManualLoginAPI(),
        manager,
        headless=True,
        manual_login_only=True,
        force_relogin=True,
    )

    assert result is headless_api
    assert manager.started_with_saved_session == [False, True]


def test_ensure_login_defaults_to_external_firefox_when_session_invalid(monkeypatch) -> None:
    events: list[str] = []

    class InitialAPI:
        def wait_for_login_stable(self, **kwargs) -> bool:
            events.append("initial_wait")
            return False

    class ReloadedAPI:
        def wait_for_login_stable(self, **kwargs) -> bool:
            events.append("reloaded_wait")
            return True

    class RestartingManager:
        def __init__(self) -> None:
            self._headless = True
            self._use_saved_session = True
            self.page = object()
            self.started: list[tuple[bool, bool]] = []

        def close(self) -> None:
            events.append("close")

        def start(self) -> None:
            events.append("start")
            self.started.append((self._headless, self._use_saved_session))

    reloaded_api = ReloadedAPI()
    monkeypatch.setattr(cli, "KorailAPI", lambda page: reloaded_api)
    monkeypatch.setattr(
        cli,
        "_run_external_firefox_login",
        lambda **kwargs: events.append(f"external:{kwargs['firefox_executable']}") or 2,
    )
    monkeypatch.setattr(
        cli,
        "_load_login_credentials",
        lambda: (_ for _ in ()).throw(
            AssertionError("default login must not use stored credential prefill first")
        ),
    )

    manager = RestartingManager()
    result = cli._ensure_login(
        InitialAPI(),
        manager,
        headless=True,
        external_firefox="/usr/bin/firefox",
    )

    assert result is reloaded_api
    assert events == [
        "initial_wait",
        "external:/usr/bin/firefox",
        "close",
        "start",
        "reloaded_wait",
    ]
    assert manager.started == [(True, True)]


def test_cli_pure_login_window_starts_at_login_without_ensure_or_polling(
    monkeypatch,
) -> None:
    from ktxgo.config import LOGIN_URL

    events: list[str] = []
    captured: dict[str, object] = {}

    class DummyManager:
        def __init__(
            self,
            *,
            headless: bool,
            use_saved_session: bool = True,
            initial_url: str | None = None,
            use_stealth: bool = True,
            browser_name: str = "firefox",
            browser_channel: str | None = None,
            browser_executable=None,
            browser_profile_dir=None,
            **kwargs,
        ):
            captured["manager_headless"] = headless
            captured["use_saved_session"] = use_saved_session
            captured["initial_url"] = initial_url
            captured["use_stealth"] = use_stealth
            captured["browser_name"] = browser_name
            captured["browser_channel"] = browser_channel
            captured["browser_executable"] = browser_executable
            captured["browser_profile_dir"] = browser_profile_dir
            captured["extra_kwargs"] = kwargs
            self.page = object()
            self.saved = False

        def __enter__(self) -> DummyManager:
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

        def save_cookies(self) -> None:
            events.append("save_cookies")
            self.saved = True

    class DummyAPI:
        def __init__(self, page: object):
            del page

        def wait_for_login_stable(self, **kwargs) -> bool:
            events.append("wait_for_login_stable")
            assert events == ["pause", "wait_for_login_stable"]
            return True

        def search(self, *args, **kwargs) -> list[Train]:
            events.append("search")
            return []

    def fail_ensure_login(*args, **kwargs):
        raise AssertionError("pure login window must not call _ensure_login")

    monkeypatch.setattr(cli, "configure_keyring_backend", lambda: None)
    monkeypatch.setattr(cli, "BrowserManager", DummyManager)
    monkeypatch.setattr(cli, "KorailAPI", DummyAPI)
    monkeypatch.setattr(cli, "_ensure_login", fail_ensure_login)
    monkeypatch.setattr(cli.click, "pause", lambda message="": events.append("pause"))
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    monkeypatch.setattr(cli.signal, "signal", lambda *args, **kwargs: None)

    result = CliRunner().invoke(
        cli.main,
        [
            "--no-interactive",
            "--max-attempts",
            "1",
            "--no-headless",
            "--pure-login-window",
            "--browser",
            "chromium",
            "--browser-channel",
            "chrome",
        ],
    )

    assert result.exit_code == 0
    assert captured == {
        "manager_headless": False,
        "use_saved_session": False,
        "initial_url": LOGIN_URL,
        "use_stealth": False,
        "browser_name": "chromium",
        "browser_channel": "chrome",
        "browser_executable": None,
        "browser_profile_dir": None,
        "extra_kwargs": {
            "locale": "ko-KR",
            "user_agent": None,
            "viewport": None,
                "screen": None,
                "device_scale_factor": None,
                "webdriver_mode": "default",
            },
    }
    assert events == ["pause", "wait_for_login_stable", "save_cookies", "search"]



def test_confirm_pure_login_window_writes_debug_fingerprints(monkeypatch, tmp_path) -> None:
    snapshots: list[str] = []

    class DummyPage:
        def on(self, event_name: str, callback) -> None:
            del callback
            snapshots.append(f"on:{event_name}")

        def evaluate(self, script: str):
            del script
            snapshots.append("evaluate")
            return {"webdriver": False, "userAgent": "dummy"}

    class DummyManager:
        page = DummyPage()

        def save_cookies(self) -> None:
            snapshots.append("save_cookies")

    class DummyAPI:
        def wait_for_login_stable(self, **kwargs) -> bool:
            snapshots.append("wait_for_login_stable")
            return True

    monkeypatch.setattr(cli.click, "pause", lambda message="": snapshots.append("pause"))

    result = cli._confirm_pure_login_window(
        DummyAPI(),
        DummyManager(),
        login_debug_dir=tmp_path,
    )

    assert isinstance(result, DummyAPI)
    assert (tmp_path / "fingerprint-before-login.json").is_file()
    assert (tmp_path / "fingerprint-after-enter-before-login-check.json").is_file()
    assert (tmp_path / "events.jsonl").is_file()
    assert snapshots == [
        "on:console",
        "on:pageerror",
        "on:request",
        "on:response",
        "evaluate",
        "pause",
        "evaluate",
        "wait_for_login_stable",
        "save_cookies",
    ]


def test_cli_pure_login_debug_configures_browser_har(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    class DummyPage:
        def on(self, event_name: str, callback) -> None:
            del event_name, callback

        def evaluate(self, script: str):
            del script
            return {"webdriver": False}

    class DummyManager:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.page = DummyPage()

        def __enter__(self) -> DummyManager:
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

        def save_cookies(self) -> None:
            pass

    class DummyAPI:
        def __init__(self, page: object):
            del page

        def wait_for_login_stable(self, **kwargs) -> bool:
            return True

        def search(self, *args, **kwargs) -> list[Train]:
            return []

    monkeypatch.setattr(cli, "configure_keyring_backend", lambda: None)
    monkeypatch.setattr(cli, "BrowserManager", DummyManager)
    monkeypatch.setattr(cli, "KorailAPI", DummyAPI)
    monkeypatch.setattr(cli.click, "pause", lambda message="": None)
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    monkeypatch.setattr(cli.signal, "signal", lambda *args, **kwargs: None)

    result = CliRunner().invoke(
        cli.main,
        [
            "--no-interactive",
            "--max-attempts",
            "1",
            "--pure-login-window",
            "--login-debug-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert captured["record_har_path"] == tmp_path / "browser.har"



def test_cli_pure_login_window_can_enable_stealth(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class DummyManager:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.page = object()

        def __enter__(self) -> DummyManager:
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

        def save_cookies(self) -> None:
            pass

    class DummyAPI:
        def __init__(self, page: object):
            del page

        def wait_for_login_stable(self, **kwargs) -> bool:
            return True

        def search(self, *args, **kwargs) -> list[Train]:
            return []

    monkeypatch.setattr(cli, "configure_keyring_backend", lambda: None)
    monkeypatch.setattr(cli, "BrowserManager", DummyManager)
    monkeypatch.setattr(cli, "KorailAPI", DummyAPI)
    monkeypatch.setattr(cli.click, "pause", lambda message="": None)
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    monkeypatch.setattr(cli.signal, "signal", lambda *args, **kwargs: None)

    result = CliRunner().invoke(
        cli.main,
        [
            "--no-interactive",
            "--max-attempts",
            "1",
            "--pure-login-window",
            "--pure-login-stealth",
        ],
    )

    assert result.exit_code == 0
    assert captured["use_stealth"] is True


def test_cli_passes_browser_executable_and_profile_dir(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}
    executable = tmp_path / "firefox"
    profile_dir = tmp_path / "profile"

    class DummyManager:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.page = object()

        def __enter__(self) -> DummyManager:
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

        def save_cookies(self) -> None:
            pass

    class DummyAPI:
        def __init__(self, page: object):
            del page

        def wait_for_login_stable(self, **kwargs) -> bool:
            return True

        def search(self, *args, **kwargs) -> list[Train]:
            return []

    monkeypatch.setattr(cli, "configure_keyring_backend", lambda: None)
    monkeypatch.setattr(cli, "BrowserManager", DummyManager)
    monkeypatch.setattr(cli, "KorailAPI", DummyAPI)
    monkeypatch.setattr(cli.click, "pause", lambda message="": None)
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    monkeypatch.setattr(cli.signal, "signal", lambda *args, **kwargs: None)

    result = CliRunner().invoke(
        cli.main,
        [
            "--no-interactive",
            "--max-attempts",
            "1",
            "--pure-login-window",
            "--pure-login-stealth",
            "--browser-executable",
            str(executable),
            "--browser-profile-dir",
            str(profile_dir),
        ],
    )

    assert result.exit_code == 0
    assert captured["browser_executable"] == executable
    assert captured["browser_profile_dir"] == profile_dir
    assert captured["use_stealth"] is True


def test_browser_manager_uses_persistent_context_with_executable(monkeypatch, tmp_path) -> None:
    calls: list[tuple[str, object]] = []

    class DummyPage:
        def set_default_timeout(self, timeout: int) -> None:
            calls.append(("set_default_timeout", timeout))

        def goto(self, url: str, **kwargs) -> None:
            calls.append(("goto", url))

    class DummyContext:
        def add_init_script(self, script: str) -> None:
            calls.append(("add_init_script", bool(script)))

        def new_page(self) -> DummyPage:
            calls.append(("new_page", None))
            return DummyPage()

        def close(self) -> None:
            calls.append(("context_close", None))

    class DummyLauncher:
        def launch_persistent_context(self, user_data_dir, **kwargs):
            calls.append(("launch_persistent_context", (user_data_dir, kwargs)))
            return DummyContext()

    class DummyPlaywright:
        firefox = DummyLauncher()

        def stop(self) -> None:
            calls.append(("stop", None))

    class DummySync:
        def start(self) -> DummyPlaywright:
            calls.append(("start", None))
            return DummyPlaywright()

    monkeypatch.setattr("ktxgo.browser.sync_playwright", lambda: DummySync())

    manager = cli.BrowserManager(
        headless=False,
        use_saved_session=False,
        initial_url="https://example.test/login",
        use_stealth=True,
        browser_name="firefox",
        browser_executable=tmp_path / "firefox",
        browser_profile_dir=tmp_path / "profile",
    )

    page = manager.start()

    assert isinstance(page, DummyPage)
    launch_call = calls[1]
    assert launch_call[0] == "launch_persistent_context"
    user_data_dir, kwargs = launch_call[1]
    assert user_data_dir == str(tmp_path / "profile")
    assert kwargs["executable_path"] == str(tmp_path / "firefox")
    assert kwargs["headless"] is False
    assert ("add_init_script", True) in calls
    assert ("goto", "https://example.test/login") in calls


def test_browser_manager_persistent_context_reuses_existing_page(monkeypatch, tmp_path) -> None:
    calls: list[tuple[str, object]] = []

    class DummyPage:
        def set_default_timeout(self, timeout: int) -> None:
            calls.append(("set_default_timeout", timeout))

        def goto(self, url: str, **kwargs) -> None:
            calls.append(("goto", url))

    existing_page = DummyPage()

    class DummyContext:
        pages = [existing_page]

        def add_init_script(self, script: str) -> None:
            calls.append(("add_init_script", bool(script)))

        def new_page(self) -> DummyPage:
            raise AssertionError("persistent context should reuse existing first page")

    class DummyLauncher:
        def launch_persistent_context(self, user_data_dir, **kwargs):
            calls.append(("launch_persistent_context", (user_data_dir, kwargs)))
            return DummyContext()

    class DummyPlaywright:
        firefox = DummyLauncher()

        def stop(self) -> None:
            pass

    class DummySync:
        def start(self) -> DummyPlaywright:
            return DummyPlaywright()

    monkeypatch.setattr("ktxgo.browser.sync_playwright", lambda: DummySync())

    manager = cli.BrowserManager(
        headless=False,
        use_saved_session=False,
        initial_url="https://www.korail.com/ticket/login",
        browser_profile_dir=tmp_path / "profile",
    )

    assert manager.start() is existing_page
    assert ("goto", "https://www.korail.com/ticket/login") in calls


def test_cli_passes_fingerprint_context_options(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class DummyManager:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.page = object()

        def __enter__(self) -> DummyManager:
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

        def save_cookies(self) -> None:
            pass

    class DummyAPI:
        def __init__(self, page: object):
            del page

        def wait_for_login_stable(self, **kwargs) -> bool:
            return True

        def search(self, *args, **kwargs) -> list[Train]:
            return []

    monkeypatch.setattr(cli, "configure_keyring_backend", lambda: None)
    monkeypatch.setattr(cli, "BrowserManager", DummyManager)
    monkeypatch.setattr(cli, "KorailAPI", DummyAPI)
    monkeypatch.setattr(cli.click, "pause", lambda message="": None)
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    monkeypatch.setattr(cli.signal, "signal", lambda *args, **kwargs: None)

    result = CliRunner().invoke(
        cli.main,
        [
            "--no-interactive",
            "--max-attempts",
            "1",
            "--pure-login-window",
            "--browser-locale",
            "en-US",
            "--browser-user-agent",
            "UA",
            "--viewport-size",
            "1280x925",
            "--screen-size",
            "1080x1920",
            "--device-scale-factor",
            "2",
        ],
    )

    assert result.exit_code == 0
    assert captured["locale"] == "en-US"
    assert captured["user_agent"] == "UA"
    assert captured["viewport"] == {"width": 1280, "height": 925}
    assert captured["screen"] == {"width": 1080, "height": 1920}
    assert captured["device_scale_factor"] == 2.0


def test_cli_passes_webdriver_mode_false(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class DummyManager:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.page = object()

        def __enter__(self) -> DummyManager:
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

        def save_cookies(self) -> None:
            pass

    class DummyAPI:
        def __init__(self, page: object):
            del page

        def wait_for_login_stable(self, **kwargs) -> bool:
            return True

        def search(self, *args, **kwargs) -> list[Train]:
            return []

    monkeypatch.setattr(cli, "configure_keyring_backend", lambda: None)
    monkeypatch.setattr(cli, "BrowserManager", DummyManager)
    monkeypatch.setattr(cli, "KorailAPI", DummyAPI)
    monkeypatch.setattr(cli.click, "pause", lambda message="": None)
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    monkeypatch.setattr(cli.signal, "signal", lambda *args, **kwargs: None)

    result = CliRunner().invoke(
        cli.main,
        [
            "--no-interactive",
            "--max-attempts",
            "1",
            "--pure-login-window",
            "--webdriver-mode",
            "false",
        ],
    )

    assert result.exit_code == 0
    assert captured["webdriver_mode"] == "false"


def test_browser_manager_webdriver_false_script(monkeypatch) -> None:
    scripts: list[str] = []

    class DummyPage:
        def set_default_timeout(self, timeout: int) -> None:
            pass

    class DummyContext:
        pages: list[DummyPage] = []

        def add_init_script(self, script: str) -> None:
            scripts.append(script)

        def new_page(self) -> DummyPage:
            return DummyPage()

    class DummyBrowser:
        def new_context(self, **kwargs) -> DummyContext:
            return DummyContext()

    class DummyLauncher:
        def launch(self, **kwargs) -> DummyBrowser:
            return DummyBrowser()

    class DummyPlaywright:
        firefox = DummyLauncher()

        def stop(self) -> None:
            pass

    class DummySync:
        def start(self) -> DummyPlaywright:
            return DummyPlaywright()

    monkeypatch.setattr("ktxgo.browser.sync_playwright", lambda: DummySync())

    manager = cli.BrowserManager(
        initial_url=None,
        webdriver_mode="false",
    )
    manager.start()

    assert len(scripts) == 1
    assert "webdriver" in scripts[0]
    assert "false" in scripts[0]
