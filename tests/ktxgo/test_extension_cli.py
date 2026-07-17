from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from ktxgo import cli
from ktxgo.korail import KorailError


def test_session_expired_error_detects_login_missing_message() -> None:
    assert cli._is_session_expired_error(KorailError("로그인 정보가 없습니다.", ""))


def test_extension_login_prompt_warns_not_to_close_chromium(monkeypatch) -> None:
    messages: list[str] = []

    class DummyAPI:
        def __init__(self) -> None:
            self.wait_count = 0

        def wait_for_login_stable(self, **kwargs):
            self.wait_count += 1
            return self.wait_count >= 2

    class DummyRunner:
        def navigate(self, url: str) -> None:
            pass

    monkeypatch.setattr(
        cli.sys,
        "stdin",
        type("DummyStdin", (), {"isatty": lambda self: True})(),
    )
    monkeypatch.setattr(cli.click, "pause", lambda message="": messages.append(message))

    cli._ensure_extension_login(DummyAPI(), DummyRunner(), force_relogin=False)

    assert len(messages) == 1
    assert "열린 창에서 Korail 로그인을 완료한 뒤, 이 터미널에서 Enter를 누르세요." in messages[0]
    assert "예매 중 작업표시줄의 chromium-browser를 닫지마세요" in messages[0]


def test_extension_login_confirmation_can_retry(monkeypatch) -> None:
    messages: list[str] = []
    confirms: list[str] = []

    class DummyAPI:
        def __init__(self) -> None:
            self.wait_count = 0

        def wait_for_login_stable(self, **kwargs):
            self.wait_count += 1
            return self.wait_count >= 3

    class DummyRunner:
        def navigate(self, url: str) -> bool:
            return True

    monkeypatch.setattr(
        cli.sys,
        "stdin",
        type("DummyStdin", (), {"isatty": lambda self: True})(),
    )
    monkeypatch.setattr(cli.click, "pause", lambda message="": messages.append(message))
    monkeypatch.setattr(
        cli.click,
        "confirm",
        lambda message, default=True: confirms.append(message) or True,
    )

    cli._ensure_extension_login(DummyAPI(), DummyRunner(), force_relogin=False)

    assert len(messages) == 2
    assert len(confirms) == 1
    assert "로그인이 아직 확인되지 않았습니다" in confirms[0]


def test_extension_login_confirmation_uses_short_single_check(monkeypatch) -> None:
    wait_kwargs: list[dict[str, object]] = []

    class DummyAPI:
        def wait_for_login_stable(self, **kwargs):
            wait_kwargs.append(kwargs)
            return len(wait_kwargs) >= 2

    class DummyRunner:
        def navigate(self, url: str) -> bool:
            return True

    monkeypatch.setattr(
        cli.sys,
        "stdin",
        type("DummyStdin", (), {"isatty": lambda self: True})(),
    )
    monkeypatch.setattr(cli.click, "pause", lambda message="": None)

    cli._ensure_extension_login(DummyAPI(), DummyRunner(), force_relogin=False)

    assert wait_kwargs[-1]["timeout_s"] <= 8
    assert wait_kwargs[-1]["stable_checks"] == 1


def test_extension_login_does_not_requeue_navigation_when_started_on_login_url(
    monkeypatch,
) -> None:
    class DummyAPI:
        def __init__(self) -> None:
            self.wait_count = 0

        def wait_for_login_stable(self, **kwargs):
            self.wait_count += 1
            return self.wait_count >= 1

    class DummyRunner:
        initial_url = cli.LOGIN_URL

        def navigate(self, url: str) -> bool:
            raise AssertionError("login runner already started on login URL")

    monkeypatch.setattr(
        cli.sys,
        "stdin",
        type("DummyStdin", (), {"isatty": lambda self: True})(),
    )
    monkeypatch.setattr(cli.click, "pause", lambda message="": None)

    cli._ensure_extension_login(DummyAPI(), DummyRunner(), force_relogin=True)


def test_extension_login_reuses_visible_profile_before_prompt(
    monkeypatch,
) -> None:
    events: list[str] = []

    class DummyAPI:
        def wait_for_login_stable(self, **kwargs):
            events.append("wait")
            return True

    class DummyRunner:
        initial_url = cli.LOGIN_URL
        headless = False

        def navigate(self, url: str) -> bool:
            raise AssertionError("login runner already started on login URL")

    monkeypatch.setattr(
        cli.sys,
        "stdin",
        type("DummyStdin", (), {"isatty": lambda self: True})(),
    )
    monkeypatch.setattr(
        cli.click,
        "pause",
        lambda message="": (_ for _ in ()).throw(
            AssertionError("valid profile should not require Enter")
        ),
    )

    cli._ensure_extension_login(DummyAPI(), DummyRunner(), force_relogin=False)

    assert events == ["wait"]


def test_cli_extension_backend_uses_extension_runner(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FailManager:
        def __init__(self, **kwargs):
            raise AssertionError("extension backend must not start Playwright")

    class DummyRunner:
        def __init__(self, **kwargs):
            captured["runner_kwargs"] = kwargs

        def start(self):
            captured["runner_entered"] = True

        def close(self):
            captured["runner_exited"] = True

        def __enter__(self):
            self.start()
            return self

        def __exit__(self, exc_type, exc, tb):
            self.close()

    class DummyAPI:
        def __init__(self, runner):
            captured["api_runner"] = runner

        def wait_for_login_stable(self, **kwargs):
            return True

        def search(self, *args, **kwargs):
            captured["search_args"] = args
            captured["search_kwargs"] = kwargs
            return []

    monkeypatch.setattr(cli, "configure_keyring_backend", lambda: None)
    monkeypatch.setattr(cli, "BrowserManager", FailManager)
    monkeypatch.setattr(cli, "ExtensionBrowserRunner", DummyRunner)
    monkeypatch.setattr(cli, "ExtensionKorailAPI", DummyAPI)
    monkeypatch.setattr(cli, "extension_login_cookie_cache_available", lambda: True)
    monkeypatch.setattr(
        cli,
        "_default_extension_chromium_executable",
        lambda: Path("/tmp/chromium-123"),
    )
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(cli.signal, "signal", lambda *args, **kwargs: None)

    result = CliRunner().invoke(
        cli.main,
        [
            "--no-interactive",
            "--departure",
            "서울",
            "--arrival",
            "부산",
            "--date",
            "20260422",
            "--time",
            "06",
            "--max-attempts",
            "1",
        ],
    )

    assert result.exit_code == 0
    assert captured["runner_entered"] is True
    assert captured["runner_exited"] is True
    assert captured["runner_kwargs"]["chromium_executable"] == Path("/tmp/chromium-123")
    assert captured["runner_kwargs"]["headless"] is True
    assert captured["search_args"] == ("서울", "부산", "20260422", "06")


def test_cli_visible_extension_starts_on_login_url(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FailManager:
        def __init__(self, **kwargs):
            raise AssertionError("extension backend must not start Playwright")

    class DummyRunner:
        def __init__(self, **kwargs):
            captured["runner_kwargs"] = kwargs

        def start(self):
            pass

        def close(self):
            pass

        def __enter__(self):
            self.start()
            return self

        def __exit__(self, exc_type, exc, tb):
            self.close()

    class DummyAPI:
        def __init__(self, runner):
            self.runner = runner

        def wait_for_login_stable(self, **kwargs):
            return True

        def search(self, *args, **kwargs):
            return []

    monkeypatch.setattr(cli, "configure_keyring_backend", lambda: None)
    monkeypatch.setattr(cli, "BrowserManager", FailManager)
    monkeypatch.setattr(cli, "ExtensionBrowserRunner", DummyRunner)
    monkeypatch.setattr(cli, "ExtensionKorailAPI", DummyAPI)
    monkeypatch.setattr(
        cli,
        "_default_extension_chromium_executable",
        lambda: Path("/tmp/chromium-123"),
    )
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(cli.signal, "signal", lambda *args, **kwargs: None)

    result = CliRunner().invoke(
        cli.main,
        [
            "--no-headless",
            "--no-interactive",
            "--departure",
            "서울",
            "--arrival",
            "부산",
            "--date",
            "20260422",
            "--time",
            "06",
            "--max-attempts",
            "1",
        ],
    )

    assert result.exit_code == 0
    assert captured["runner_kwargs"]["headless"] is False
    assert captured["runner_kwargs"]["initial_url"] == cli.LOGIN_URL


def test_cli_headless_extension_uses_single_visible_login_when_cache_invalid(
    monkeypatch,
) -> None:
    events: list[str] = []
    runner_initial_urls: list[str] = []

    class FailManager:
        def __init__(self, **kwargs):
            raise AssertionError("extension backend must not start Playwright")

    class DummyRunner:
        def __init__(self, **kwargs):
            self.headless = bool(kwargs["headless"])
            runner_initial_urls.append(str(kwargs["initial_url"]))
            events.append(f"runner:init:{self.headless}")

        def start(self):
            events.append(f"runner:start:{self.headless}")

        def close(self):
            events.append(f"runner:close:{self.headless}")

        def __enter__(self):
            self.start()
            return self

        def __exit__(self, exc_type, exc, tb):
            self.close()

        def minimize(self) -> bool:
            events.append(f"minimize:{self.headless}")
            return True

    class DummyAPI:
        def __init__(self, runner):
            self.runner = runner

        def wait_for_login_stable(self, **kwargs):
            events.append(f"wait:{self.runner.headless}")
            return False

        def search(self, *args, **kwargs):
            events.append(f"search:{self.runner.headless}")
            return []

    def fake_ensure_extension_login(api, runner, *, force_relogin: bool = False):
        events.append(f"ensure:{runner.headless}:{force_relogin}")
        return api

    monkeypatch.setattr(cli, "configure_keyring_backend", lambda: None)
    monkeypatch.setattr(cli, "BrowserManager", FailManager)
    monkeypatch.setattr(cli, "ExtensionBrowserRunner", DummyRunner)
    monkeypatch.setattr(cli, "ExtensionKorailAPI", DummyAPI)
    monkeypatch.setattr(cli, "_ensure_extension_login", fake_ensure_extension_login)
    monkeypatch.setattr(cli, "extension_login_cookie_cache_available", lambda: False)
    monkeypatch.setattr(
        cli,
        "_default_extension_chromium_executable",
        lambda: Path("/tmp/chromium-123"),
    )
    monkeypatch.setattr(cli.click, "pause", lambda message="": events.append("pause"))
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(cli.signal, "signal", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        cli.sys,
        "stdin",
        type("DummyStdin", (), {"isatty": lambda self: True})(),
    )

    result = CliRunner().invoke(
        cli.main,
        [
            "--no-interactive",
            "--departure",
            "서울",
            "--arrival",
            "부산",
            "--date",
            "20260422",
            "--time",
            "06",
            "--max-attempts",
            "1",
        ],
    )

    assert result.exit_code == 0
    assert events == [
        "runner:init:True",
        "runner:start:True",
        "wait:True",
        "runner:close:True",
        "runner:init:False",
        "runner:start:False",
        "ensure:False:True",
        "minimize:False",
        "search:False",
        "runner:close:False",
    ]
    assert runner_initial_urls == [cli.LOGIN_URL, cli.LOGIN_URL]


def test_cli_headless_extension_force_relogin_uses_single_visible_login(
    monkeypatch,
) -> None:
    events: list[str] = []

    class FailManager:
        def __init__(self, **kwargs):
            raise AssertionError("extension backend must not start Playwright")

    class DummyRunner:
        def __init__(self, **kwargs):
            self.headless = bool(kwargs["headless"])
            events.append(f"runner:init:{self.headless}")

        def start(self):
            events.append(f"runner:start:{self.headless}")

        def close(self):
            events.append(f"runner:close:{self.headless}")

        def __enter__(self):
            self.start()
            return self

        def __exit__(self, exc_type, exc, tb):
            self.close()

        def minimize(self) -> bool:
            events.append(f"minimize:{self.headless}")
            return True

    class DummyAPI:
        def __init__(self, runner):
            self.runner = runner

        def wait_for_login_stable(self, **kwargs):
            events.append(f"wait:{self.runner.headless}")
            return False

        def search(self, *args, **kwargs):
            events.append(f"search:{self.runner.headless}")
            return []

    def fake_ensure_extension_login(api, runner, *, force_relogin: bool = False):
        events.append(f"ensure:{runner.headless}:{force_relogin}")
        return api

    monkeypatch.setattr(cli, "configure_keyring_backend", lambda: None)
    monkeypatch.setattr(cli, "BrowserManager", FailManager)
    monkeypatch.setattr(cli, "ExtensionBrowserRunner", DummyRunner)
    monkeypatch.setattr(cli, "ExtensionKorailAPI", DummyAPI)
    monkeypatch.setattr(cli, "_ensure_extension_login", fake_ensure_extension_login)
    monkeypatch.setattr(cli, "extension_login_cookie_cache_available", lambda: False)
    monkeypatch.setattr(
        cli,
        "_default_extension_chromium_executable",
        lambda: Path("/tmp/chromium-123"),
    )
    monkeypatch.setattr(cli.click, "pause", lambda message="": events.append("pause"))
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(cli.signal, "signal", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        cli.sys,
        "stdin",
        type("DummyStdin", (), {"isatty": lambda self: True})(),
    )

    result = CliRunner().invoke(
        cli.main,
        [
            "--no-interactive",
            "--departure",
            "서울",
            "--arrival",
            "부산",
            "--date",
            "20260422",
            "--time",
            "06",
            "--force-relogin",
            "--max-attempts",
            "1",
        ],
    )

    assert result.exit_code == 0
    assert events == [
        "runner:init:False",
        "runner:start:False",
        "ensure:False:True",
        "minimize:False",
        "search:False",
        "runner:close:False",
    ]


def test_cli_headless_extension_reuses_valid_profile_when_cookie_cache_missing(
    monkeypatch,
) -> None:
    events: list[str] = []
    runner_initial_urls: list[str] = []

    class FailManager:
        def __init__(self, **kwargs):
            raise AssertionError("extension backend must not start Playwright")

    class DummyRunner:
        def __init__(self, **kwargs):
            self.headless = bool(kwargs["headless"])
            runner_initial_urls.append(str(kwargs["initial_url"]))
            events.append(f"runner:init:{self.headless}")

        def start(self):
            events.append(f"runner:start:{self.headless}")

        def close(self):
            events.append(f"runner:close:{self.headless}")

    class DummyAPI:
        def __init__(self, runner):
            self.runner = runner

        def wait_for_login_stable(self, **kwargs):
            events.append(f"wait:{self.runner.headless}")
            return True

        def search(self, *args, **kwargs):
            events.append(f"search:{self.runner.headless}")
            return []

    monkeypatch.setattr(cli, "configure_keyring_backend", lambda: None)
    monkeypatch.setattr(cli, "BrowserManager", FailManager)
    monkeypatch.setattr(cli, "ExtensionBrowserRunner", DummyRunner)
    monkeypatch.setattr(cli, "ExtensionKorailAPI", DummyAPI)
    monkeypatch.setattr(
        cli,
        "_ensure_extension_login",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("fresh cookie cache should not open visible login")
        ),
    )
    monkeypatch.setattr(cli, "extension_login_cookie_cache_available", lambda: False)
    monkeypatch.setattr(
        cli,
        "_default_extension_chromium_executable",
        lambda: Path("/tmp/chromium-123"),
    )
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(cli.signal, "signal", lambda *args, **kwargs: None)

    result = CliRunner().invoke(
        cli.main,
        [
            "--no-interactive",
            "--departure",
            "서울",
            "--arrival",
            "부산",
            "--date",
            "20260422",
            "--time",
            "06",
            "--max-attempts",
            "1",
        ],
    )

    assert result.exit_code == 0
    assert events == [
        "runner:init:True",
        "runner:start:True",
        "wait:True",
        "search:True",
        "runner:close:True",
    ]
    assert runner_initial_urls == [cli.LOGIN_URL]


def test_cli_headless_extension_confirms_cookie_cache_login_before_search(
    monkeypatch,
) -> None:
    events: list[str] = []

    class FailManager:
        def __init__(self, **kwargs):
            raise AssertionError("extension backend must not start Playwright")

    class DummyRunner:
        def __init__(self, **kwargs):
            self.headless = bool(kwargs["headless"])
            events.append(f"runner:init:{self.headless}")

        def start(self):
            events.append(f"runner:start:{self.headless}")

        def close(self):
            events.append(f"runner:close:{self.headless}")

        def restore_login_cookie_cache(self) -> bool:
            events.append(f"restore:{self.headless}")
            return True

    class DummyAPI:
        def __init__(self, runner):
            self.runner = runner

        def wait_for_login_stable(self, **kwargs):
            events.append(f"wait:{self.runner.headless}")
            return True

        def search(self, *args, **kwargs):
            events.append(f"search:{self.runner.headless}")
            return []

    monkeypatch.setattr(cli, "configure_keyring_backend", lambda: None)
    monkeypatch.setattr(cli, "BrowserManager", FailManager)
    monkeypatch.setattr(cli, "ExtensionBrowserRunner", DummyRunner)
    monkeypatch.setattr(cli, "ExtensionKorailAPI", DummyAPI)
    monkeypatch.setattr(
        cli,
        "_ensure_extension_login",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("confirmed cookie cache login should not open visible login")
        ),
    )
    monkeypatch.setattr(cli, "extension_login_cookie_cache_available", lambda: True)
    monkeypatch.setattr(
        cli,
        "_default_extension_chromium_executable",
        lambda: Path("/tmp/chromium-123"),
    )
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(cli.signal, "signal", lambda *args, **kwargs: None)

    result = CliRunner().invoke(
        cli.main,
        [
            "--no-interactive",
            "--departure",
            "서울",
            "--arrival",
            "부산",
            "--date",
            "20260422",
            "--time",
            "06",
            "--max-attempts",
            "1",
        ],
    )

    assert result.exit_code == 0
    assert events == [
        "runner:init:True",
        "runner:start:True",
        "restore:True",
        "wait:True",
        "search:True",
        "runner:close:True",
    ]
