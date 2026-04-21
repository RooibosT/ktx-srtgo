from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from ktxgo import cli
from ktxgo.korail import KorailError


def test_build_external_firefox_search_url_uses_official_search_list_params() -> None:
    url = cli._build_external_firefox_search_url(
        departure="서울",
        arrival="부산",
        date="20260422",
        time_str="6",
        adults=2,
        train_types=("ktx",),
    )

    assert url.startswith("https://www.korail.com/ticket/search/list?")
    assert "txtGoStart=%EC%84%9C%EC%9A%B8" in url
    assert "txtGoEnd=%EB%B6%80%EC%82%B0" in url
    assert "txtGoStartCode=0001" in url
    assert "txtGoEndCode=0020" in url
    assert "txtGoAbrdDt=20260422" in url
    assert "txtGoHour=060000" in url
    assert "txtPsgFlg_1=2" in url
    assert "selGoTrain=100" in url
    assert "txtTrnGpCd=100" in url


def test_cli_external_firefox_search_opens_official_search_without_playwright(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_external_firefox_search(**kwargs) -> None:
        captured.update(kwargs)
        cli.click.echo("Opening external Firefox search page")

    class FailManager:
        def __init__(self, **kwargs):
            raise AssertionError("external search mode must not start Playwright")

    monkeypatch.setattr(cli, "configure_keyring_backend", lambda: None)
    monkeypatch.setattr(cli, "BrowserManager", FailManager)
    monkeypatch.setattr(cli, "_run_external_firefox_search", fake_run_external_firefox_search)
    monkeypatch.setattr(cli.signal, "signal", lambda *args, **kwargs: None)

    result = CliRunner().invoke(
        cli.main,
        [
            "--no-interactive",
            "--external-firefox-search",
            "--departure",
            "서울",
            "--arrival",
            "부산",
            "--date",
            "20260422",
            "--time",
            "6",
            "--adults",
            "2",
            "--external-firefox",
            "/usr/bin/firefox",
            "--external-firefox-profile",
            "/tmp/ktxgo-profile",
        ],
    )

    assert result.exit_code == 0
    assert captured == {
        "firefox_executable": "/usr/bin/firefox",
        "profile_dir": Path("/tmp/ktxgo-profile"),
        "departure": "서울",
        "arrival": "부산",
        "date": "20260422",
        "time_str": "06",
        "adults": 2,
        "train_types": ("ktx",),
    }
    assert "Opening external Firefox search page" in result.output


def test_run_external_firefox_search_uses_requested_profile(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    class DummyProcess:
        pass

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return DummyProcess()

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(cli.click, "pause", lambda *args, **kwargs: None)

    cli._run_external_firefox_search(
        firefox_executable="/usr/bin/firefox",
        profile_dir=tmp_path / "profile",
        departure="서울",
        arrival="부산",
        date="20260422",
        time_str="06",
        adults=1,
        train_types=("ktx",),
    )

    command = captured["command"]
    assert command[:4] == [
        "/usr/bin/firefox",
        "--no-remote",
        "--profile",
        str(tmp_path / "profile"),
    ]
    assert command[4].startswith("https://www.korail.com/ticket/search/list?")


def test_interactive_macro_error_does_not_handoff_to_external_firefox(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class DummyManager:
        page = object()

        def __init__(self, **kwargs):
            pass

        def __enter__(self) -> DummyManager:
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

    class DummyAPI:
        def __init__(self, page: object):
            del page

        def search(self, *args, **kwargs):
            raise KorailError(
                "원활한 서비스 이용을 위해 앱을 최신 버전으로 업데이트한 뒤 재실행 후 안정적인 환경에서 사용해 주시기 바랍니다.",
                "MACRO ERROR",
            )

    def fake_run_external_firefox_search(**kwargs) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(cli, "configure_keyring_backend", lambda: None)
    monkeypatch.setattr(cli, "BrowserManager", DummyManager)
    monkeypatch.setattr(cli, "KorailAPI", DummyAPI)
    monkeypatch.setattr(cli, "_ensure_login", lambda api, manager, headless, **kwargs: api)
    monkeypatch.setattr(cli, "_load_visible_stations", lambda: ["서울", "부산"])
    monkeypatch.setattr(cli, "_prompt_main_menu", lambda: "reserve")
    monkeypatch.setattr(
        cli,
        "_apply_saved_interactive_reservation_defaults",
        lambda ctx, **kwargs: (
            kwargs["departure"],
            kwargs["arrival"],
            kwargs["date"],
            kwargs["time_str"],
            kwargs["adults"],
            kwargs["train_types"],
            kwargs["seat"],
            kwargs["auto_pay"],
            kwargs["smart_ticket"],
        ),
    )
    monkeypatch.setattr(
        cli,
        "_prompt_conditions",
        lambda *args, **kwargs: ("서울", "부산", "20260422", "06", 1, ("ktx",)),
    )
    monkeypatch.setattr(cli, "_run_external_firefox_search", fake_run_external_firefox_search)
    monkeypatch.setattr(cli.signal, "signal", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        cli.sys,
        "stdin",
        type("DummyStdin", (), {"isatty": lambda self: True})(),
    )

    exit_code: object = None
    try:
        cli.main.callback(
            departure="서울",
            arrival="부산",
            date="20260422",
            time_str="06",
            adults=1,
            headless=True,
            manual_login_only=False,
            force_relogin=False,
            pure_login_window=False,
            pure_login_stealth=False,
            webdriver_mode="default",
            browser_name="firefox",
            browser_channel=None,
            browser_executable=None,
            browser_profile_dir=None,
            browser_locale="ko-KR",
            browser_user_agent=None,
            viewport_size=None,
            screen_size=None,
            device_scale_factor=None,
            login_debug_dir=None,
            interactive=True,
            max_attempts=1,
            train_types=("ktx",),
            seat="any",
            set_card_mode=False,
            auto_pay=False,
            smart_ticket=True,
            telegram=False,
            waitlist_alert_phone=None,
            import_cookies_path=None,
            check_login_session=False,
            external_firefox_login=False,
            external_firefox_search=False,
            external_firefox="/usr/bin/firefox",
            external_firefox_profile=None,
            api_backend="playwright",
        )
    except SystemExit as exc:
        exit_code = exc.code

    assert exit_code == 1
    assert captured == {}


def test_search_loop_macro_error_does_not_handoff_to_external_firefox(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class DummyManager:
        page = object()

        def __init__(self, **kwargs):
            pass

        def __enter__(self) -> DummyManager:
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

    class DummyAPI:
        def __init__(self, page: object):
            del page

        def search(self, *args, **kwargs):
            raise KorailError(
                "원활한 서비스 이용을 위해 앱을 최신 버전으로 업데이트한 뒤 재실행 후 안정적인 환경에서 사용해 주시기 바랍니다.",
                "MACRO ERROR",
            )

    def fake_run_external_firefox_search(**kwargs) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(cli, "configure_keyring_backend", lambda: None)
    monkeypatch.setattr(cli, "BrowserManager", DummyManager)
    monkeypatch.setattr(cli, "KorailAPI", DummyAPI)
    monkeypatch.setattr(cli, "_ensure_login", lambda api, manager, headless, **kwargs: api)
    monkeypatch.setattr(cli, "_load_visible_stations", lambda: ["서울", "부산"])
    monkeypatch.setattr(cli, "_run_external_firefox_search", fake_run_external_firefox_search)
    monkeypatch.setattr(cli.signal, "signal", lambda *args, **kwargs: None)

    exit_code: object = None
    try:
        cli.main.callback(
            departure="서울",
            arrival="부산",
            date="20260422",
            time_str="06",
            adults=1,
            headless=True,
            manual_login_only=False,
            force_relogin=False,
            pure_login_window=False,
            pure_login_stealth=False,
            webdriver_mode="default",
            browser_name="firefox",
            browser_channel=None,
            browser_executable=None,
            browser_profile_dir=None,
            browser_locale="ko-KR",
            browser_user_agent=None,
            viewport_size=None,
            screen_size=None,
            device_scale_factor=None,
            login_debug_dir=None,
            interactive=False,
            max_attempts=1,
            train_types=("ktx",),
            seat="any",
            set_card_mode=False,
            auto_pay=False,
            smart_ticket=True,
            telegram=False,
            waitlist_alert_phone=None,
            import_cookies_path=None,
            check_login_session=False,
            external_firefox_login=False,
            external_firefox_search=False,
            external_firefox="/usr/bin/firefox",
            external_firefox_profile=None,
            api_backend="playwright",
        )
    except SystemExit as exc:
        exit_code = exc.code

    assert exit_code == 1
    assert captured == {}
