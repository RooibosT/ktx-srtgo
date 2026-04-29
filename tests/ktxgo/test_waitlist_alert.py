from __future__ import annotations

import click
import pytest
from click.core import ParameterSource
from click.testing import CliRunner

from ktxgo import cli
from ktxgo.korail import KorailAPI, KorailError, Train


class _DummyManager:
    def __init__(self, headless: bool, use_saved_session: bool = True, **kwargs):
        del headless, use_saved_session, kwargs
        self.page = object()

    def __enter__(self) -> _DummyManager:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


def _make_waitlist_train() -> Train:
    return Train.from_schedule(
        {
            "h_trn_no": "00123",
            "h_car_tp_nm": "KTX",
            "h_trn_clsf_nm": "KTX",
            "h_trn_gp_nm": "KTX",
            "h_dpt_rs_stn_nm": "서울",
            "h_arv_rs_stn_nm": "부산",
            "h_dpt_tm_qb": "08:10",
            "h_arv_tm_qb": "10:59",
            "h_dpt_dt": "20260320",
            "h_gen_rsv_nm": "매진",
            "h_gen_rsv_cd": "13",
            "h_spe_rsv_nm": "매진",
            "h_spe_rsv_cd": "13",
            "h_stnd_rsv_nm": "없음",
            "h_wait_rsv_nm": "가능",
            "h_wait_rsv_cd": "09",
            "h_rcvd_amt": "0059800",
            "h_trn_clsf_cd": "100",
            "h_trn_gp_cd": "100",
            "h_dpt_rs_stn_cd": "0001",
            "h_arv_rs_stn_cd": "0020",
            "h_run_dt": "20260320",
        }
    )


def _patch_cli_runtime(monkeypatch) -> None:
    monkeypatch.setattr(cli, "BrowserManager", _DummyManager)
    monkeypatch.setattr(
        cli,
        "_ensure_login",
        lambda api, manager, headless, **kwargs: api,
    )
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    monkeypatch.setattr(cli.signal, "signal", lambda *args, **kwargs: None)


def _set_parameter_source(
    ctx: click.Context, param_name: str, source: ParameterSource
) -> None:
    if hasattr(ctx, "set_parameter_source"):
        ctx.set_parameter_source(param_name, source)
        return
    ctx._parameter_source[param_name] = source  # type: ignore[attr-defined]


def test_set_waitlist_alert_uses_korail_wait_endpoint() -> None:
    api = KorailAPI.__new__(KorailAPI)
    captured: dict[str, object] = {}

    def fake_api_call(endpoint: str, params: dict[str, str]) -> dict[str, object]:
        captured["endpoint"] = endpoint
        captured["params"] = params
        return {"strResult": "SUCC"}

    api._api_call = fake_api_call  # type: ignore[attr-defined]

    api.set_waitlist_alert("PNR123", "01012341234")

    assert captured["endpoint"] == "/classes/com.korail.mobile.reservationWait.ReservationWait"
    assert captured["params"] == {
        "Device": "AD",
        "Version": "250601002",
        "Key": "korail1234567890",
        "txtPnrNo": "PNR123",
        "txtPsrmClChgFlg": "N",
        "txtSmsSndFlg": "Y",
        "txtCpNo": "01012341234",
    }


def test_resolve_waitlist_alert_phone_prefers_cli_over_keyring(monkeypatch) -> None:
    monkeypatch.setattr(
        cli.keyring,
        "get_password",
        lambda service, key: {
            ("KTX", "waitlist_alert_phone"): "01099998888",
        }.get((service, key)),
    )

    assert cli._resolve_waitlist_alert_phone("01012341234") == "01012341234"


def test_set_waitlist_alert_phone_interactive_saves_normalized_phone(
    monkeypatch,
) -> None:
    stored: dict[tuple[str, str], str] = {}

    monkeypatch.setattr(
        cli,
        "_prompt_guarded",
        lambda questions: {"phone": "010-1234-5678"},
    )
    monkeypatch.setattr(
        cli.keyring,
        "get_password",
        lambda service, key: {
            ("KTX", "waitlist_alert_phone"): "01000000000",
        }.get((service, key)),
    )
    monkeypatch.setattr(
        cli.keyring,
        "set_password",
        lambda service, key, value: stored.__setitem__((service, key), value),
    )

    assert cli._set_waitlist_alert_phone_interactive() is True
    assert stored == {("KTX", "waitlist_alert_phone"): "01012345678"}


def test_interactive_menu_dispatches_waitlist_alert_setting(monkeypatch) -> None:
    actions = iter(["waitlist-alert", "exit"])
    calls: list[str] = []

    monkeypatch.setattr(cli, "_prompt_main_menu", lambda: next(actions))
    monkeypatch.setattr(
        cli,
        "_set_waitlist_alert_phone_interactive",
        lambda: calls.append("waitlist-alert") or True,
    )
    monkeypatch.setattr(cli, "_load_visible_stations", lambda: ["서울", "부산"])
    monkeypatch.setattr(
        cli.sys,
        "stdin",
        type("DummyStdin", (), {"isatty": lambda self: True})(),
    )
    monkeypatch.setattr(cli, "configure_keyring_backend", lambda: None)

    with pytest.raises(SystemExit) as exc_info:
        cli.main.callback(
            departure="서울",
            arrival="부산",
            date="20260320",
            time_str="07",
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
        )

    assert exc_info.value.code == 0
    assert calls == ["waitlist-alert"]


def test_cli_registers_waitlist_alert_after_waitlist_success(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    class DummyAPI:
        def __init__(self, page: object):
            del page

        def search(self, *args, **kwargs) -> list[Train]:
            return [_make_waitlist_train()]

        def reserve(
            self,
            train: Train,
            seat_type: str = "general",
            adults: int = 1,
            waitlist: bool = False,
        ) -> dict[str, object]:
            del train, seat_type, adults
            assert waitlist is True
            return {"h_pnr_no": "PNR123", "strResult": "SUCC"}

        def set_waitlist_alert(self, pnr_no: str, phone: str) -> dict[str, object]:
            calls.append((pnr_no, phone))
            return {"strResult": "SUCC"}

    _patch_cli_runtime(monkeypatch)
    monkeypatch.setattr(cli, "KorailAPI", DummyAPI)

    runner = CliRunner()
    result = runner.invoke(
        cli.main,
        [
            "--api-backend",
            "playwright",
            "--no-interactive",
            "--max-attempts",
            "1",
            "--waitlist-alert-phone",
            "01012341234",
        ],
    )

    assert result.exit_code == 0
    assert calls == [("PNR123", "01012341234")]
    assert "좌석배정 알림 등록완료" in result.output


def test_cli_keeps_waitlist_success_when_alert_registration_fails(monkeypatch) -> None:
    class DummyAPI:
        def __init__(self, page: object):
            del page

        def search(self, *args, **kwargs) -> list[Train]:
            return [_make_waitlist_train()]

        def reserve(
            self,
            train: Train,
            seat_type: str = "general",
            adults: int = 1,
            waitlist: bool = False,
        ) -> dict[str, object]:
            del train, seat_type, adults
            assert waitlist is True
            return {"h_pnr_no": "PNR123", "strResult": "SUCC"}

        def set_waitlist_alert(self, pnr_no: str, phone: str) -> dict[str, object]:
            del pnr_no, phone
            raise KorailError("alert registration failed", "ERR")

    _patch_cli_runtime(monkeypatch)
    monkeypatch.setattr(cli, "KorailAPI", DummyAPI)

    runner = CliRunner()
    result = runner.invoke(
        cli.main,
        [
            "--api-backend",
            "playwright",
            "--no-interactive",
            "--max-attempts",
            "1",
            "--waitlist-alert-phone",
            "01012341234",
        ],
    )

    assert result.exit_code == 0
    assert "예약대기 신청완료" in result.output
    assert "alert registration failed" in result.output


def test_reservation_loop_keeps_login_alive_between_search_attempts(
    monkeypatch,
) -> None:
    events: list[str] = []
    train = _make_waitlist_train()

    class DummyAPI:
        def __init__(self) -> None:
            self.search_count = 0

        def search(self, *args, **kwargs) -> list[Train]:
            del args, kwargs
            self.search_count += 1
            events.append(f"search:{self.search_count}")
            if self.search_count == 1:
                return []
            return [train]

        def is_logged_in(self) -> bool:
            events.append("keepalive")
            return True

        def reserve(
            self,
            train: Train,
            seat_type: str = "general",
            adults: int = 1,
            waitlist: bool = False,
        ) -> dict[str, object]:
            del train, seat_type, adults
            assert waitlist is True
            events.append("reserve")
            return {"h_pnr_no": "PNR123", "strResult": "SUCC"}

        def set_waitlist_alert(self, pnr_no: str, phone: str) -> dict[str, object]:
            raise AssertionError("phone is not configured in this test")

    monkeypatch.setattr(cli, "_LOGIN_KEEPALIVE_INTERVAL_S", 0.0)
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(cli.keyring, "get_password", lambda service, key: None)

    cli._run_reservation_loop(
        DummyAPI(),  # type: ignore[arg-type]
        reauthenticate=lambda api, stage: api,
        interactive_mode=False,
        departure="서울",
        arrival="부산",
        date="20260320",
        time_str="07",
        adults=1,
        train_types=("ktx",),
        seat="any",
        auto_pay=False,
        smart_ticket=True,
        telegram=False,
        waitlist_alert_phone=None,
        max_attempts=2,
    )

    assert events == ["search:1", "keepalive", "search:2", "reserve"]


def test_reservation_loop_does_not_reauthenticate_on_single_keepalive_miss(
    monkeypatch,
) -> None:
    events: list[str] = []

    class DummyAPI:
        def __init__(self) -> None:
            self.search_count = 0

        def search(self, *args, **kwargs) -> list[Train]:
            del args, kwargs
            self.search_count += 1
            events.append(f"search:{self.search_count}")
            return []

        def is_logged_in(self) -> bool:
            events.append("keepalive:false")
            return False

    def fail_reauthenticate(api: object, stage: str) -> object:
        raise AssertionError(f"single keepalive miss must not reauthenticate: {stage}")

    monkeypatch.setattr(cli, "_LOGIN_KEEPALIVE_INTERVAL_S", 0.0)
    monkeypatch.setattr(cli, "_LOGIN_KEEPALIVE_FAILURES_BEFORE_REAUTH", 2)
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)

    cli._run_reservation_loop(
        DummyAPI(),  # type: ignore[arg-type]
        reauthenticate=fail_reauthenticate,  # type: ignore[arg-type]
        interactive_mode=False,
        departure="서울",
        arrival="부산",
        date="20260320",
        time_str="07",
        adults=1,
        train_types=("ktx",),
        seat="any",
        auto_pay=False,
        smart_ticket=True,
        telegram=False,
        waitlist_alert_phone=None,
        max_attempts=1,
    )

    assert events == ["search:1", "keepalive:false"]


def test_load_saved_interactive_defaults_sanitizes_invalid_values(monkeypatch) -> None:
    monkeypatch.setattr(
        cli.keyring,
        "get_password",
        lambda service, key: {
            ("KTX", "departure"): "없는역",
            ("KTX", "arrival"): "서울",
            ("KTX", "date"): "2026-03-20",
            ("KTX", "time"): "99",
            ("KTX", "adults"): "0",
            ("KTX", "train_types"): "invalid-type",
            ("KTX", "seat"): "invalid-seat",
            ("KTX", "auto_pay"): "maybe",
            ("KTX", "smart_ticket"): "off",
        }.get((service, key)),
    )

    defaults = cli._load_saved_interactive_reservation_defaults(
        stations=["서울", "부산", "대전"],
        departure="서울",
        arrival="부산",
        date="20260320",
        time_str="07",
        adults=1,
        train_types=("ktx",),
        seat="any",
        auto_pay=False,
        smart_ticket=True,
    )

    assert defaults == (
        "서울",
        "부산",
        "20260320",
        "07",
        1,
        ("ktx",),
        "any",
        False,
        True,
    )


def test_apply_saved_interactive_defaults_preserves_explicit_cli_sources(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        cli.keyring,
        "get_password",
        lambda service, key: {
            ("KTX", "departure"): "대전",
            ("KTX", "arrival"): "부산",
            ("KTX", "date"): "20260320",
            ("KTX", "time"): "13",
            ("KTX", "adults"): "2",
            ("KTX", "train_types"): "legacy-all",
            ("KTX", "seat"): "special",
            ("KTX", "auto_pay"): "1",
            ("KTX", "smart_ticket"): "0",
        }.get((service, key)),
    )
    ctx = click.Context(cli.main)
    _set_parameter_source(ctx, "departure", ParameterSource.DEFAULT)
    _set_parameter_source(ctx, "arrival", ParameterSource.COMMANDLINE)
    _set_parameter_source(ctx, "date", ParameterSource.DEFAULT)
    _set_parameter_source(ctx, "time_str", ParameterSource.DEFAULT)
    _set_parameter_source(ctx, "adults", ParameterSource.COMMANDLINE)
    _set_parameter_source(ctx, "train_types", ParameterSource.DEFAULT)
    _set_parameter_source(ctx, "seat", ParameterSource.COMMANDLINE)
    _set_parameter_source(ctx, "auto_pay", ParameterSource.COMMANDLINE)
    _set_parameter_source(ctx, "smart_ticket", ParameterSource.DEFAULT)

    merged = cli._apply_saved_interactive_reservation_defaults(
        ctx,
        stations=["서울", "대전", "부산"],
        departure="서울",
        arrival="광명",
        date="20260319",
        time_str="08",
        adults=1,
        train_types=("ktx",),
        seat="any",
        auto_pay=False,
        smart_ticket=True,
    )

    assert merged == (
        "대전",
        "광명",
        "20260320",
        "13",
        1,
        ("ktx", "itx-saemaeul", "mugunghwa", "tonggeun", "itx-cheongchun", "itx-maeum", "airport"),
        "any",
        False,
        True,
    )


def test_apply_saved_interactive_defaults_keeps_default_map_values(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        cli.keyring,
        "get_password",
        lambda service, key: {
            ("KTX", "departure"): "대전",
            ("KTX", "arrival"): "부산",
            ("KTX", "date"): "20260320",
            ("KTX", "time"): "13",
            ("KTX", "adults"): "2",
            ("KTX", "train_types"): "legacy-all",
            ("KTX", "seat"): "special",
            ("KTX", "auto_pay"): "1",
            ("KTX", "smart_ticket"): "0",
        }.get((service, key)),
    )
    ctx = click.Context(cli.main)
    for param_name in (
        "departure",
        "arrival",
        "date",
        "time_str",
        "adults",
        "train_types",
        "seat",
        "auto_pay",
        "smart_ticket",
    ):
        _set_parameter_source(ctx, param_name, ParameterSource.DEFAULT_MAP)

    merged = cli._apply_saved_interactive_reservation_defaults(
        ctx,
        stations=["서울", "대전", "부산"],
        departure="서울",
        arrival="광명",
        date="20260319",
        time_str="08",
        adults=1,
        train_types=("ktx",),
        seat="any",
        auto_pay=False,
        smart_ticket=True,
    )

    assert merged == (
        "서울",
        "광명",
        "20260319",
        "08",
        1,
        ("ktx",),
        "any",
        False,
        True,
    )


def test_prompt_conditions_persists_partial_progress_before_cancellation(
    monkeypatch,
) -> None:
    answers = iter(
        [
            {"departure": "서울"},
            {"arrival": "부산"},
            None,
        ]
    )
    stored: list[tuple[str, str, str]] = []

    monkeypatch.setattr(cli, "_prompt_guarded", lambda questions: next(answers))
    monkeypatch.setattr(
        cli.keyring,
        "set_password",
        lambda service, key, value: stored.append((service, key, value)),
    )

    with pytest.raises(SystemExit) as exc_info:
        cli._prompt_conditions(
            departure="서울",
            arrival="대전",
            date="20260320",
            time_str="07",
            adults=1,
            stations=["서울", "부산", "대전"],
            train_types=("ktx",),
        )

    assert exc_info.value.code == 0
    assert stored == [
        ("KTX", "departure", "서울"),
        ("KTX", "arrival", "부산"),
    ]


def test_prompt_reservation_options_persists_reservation_defaults(monkeypatch) -> None:
    answers = iter([
        {"seat": "special"},
        {"auto_pay": True},
    ])
    stored: list[tuple[str, str, str]] = []

    monkeypatch.setattr(cli, "_prompt_guarded", lambda questions: next(answers))
    monkeypatch.setattr(
        cli.keyring,
        "set_password",
        lambda service, key, value: stored.append((service, key, value)),
    )

    result = cli._prompt_reservation_options("any", False, False)

    assert result == ("special", True, False)
    assert stored == [
        ("KTX", "seat", "special"),
        ("KTX", "auto_pay", "1"),
    ]


def test_prompt_reservation_options_persists_partial_progress_on_cancellation(
    monkeypatch,
) -> None:
    answers = iter([
        {"seat": "general"},
        None,
    ])
    stored: list[tuple[str, str, str]] = []

    monkeypatch.setattr(cli, "_prompt_guarded", lambda questions: next(answers))
    monkeypatch.setattr(
        cli.keyring,
        "set_password",
        lambda service, key, value: stored.append((service, key, value)),
    )

    with pytest.raises(SystemExit) as exc_info:
        cli._prompt_reservation_options("any", False, True)

    assert exc_info.value.code == 0
    assert stored == [
        ("KTX", "seat", "general"),
    ]


def test_main_persists_auto_pay_false_after_card_check_fallback(monkeypatch) -> None:
    monkeypatch.setattr(cli, "configure_keyring_backend", lambda: None)
    monkeypatch.setattr(cli, "_load_visible_stations", lambda: ["서울", "부산"])
    monkeypatch.setattr(cli, "_prompt_main_menu", lambda: "reserve")
    monkeypatch.setattr(
        cli,
        "_prompt_conditions",
        lambda departure, arrival, date, time_str, adults, stations, train_types: (
            departure,
            arrival,
            date,
            time_str,
            adults,
            train_types,
        ),
    )
    monkeypatch.setattr(
        cli,
        "_prompt_target_trains",
        lambda api, departure, arrival, date, time_str, adults, train_types: [
            (date, "00123", "0810", departure, arrival)
        ],
    )
    monkeypatch.setattr(
        cli,
        "_prompt_reservation_options",
        lambda seat, auto_pay, smart_ticket: ("any", True, smart_ticket),
    )
    monkeypatch.setattr(cli, "_ensure_card_for_auto_pay", lambda: False)
    monkeypatch.setattr(cli.click, "confirm", lambda message, default=True: True)

    stored: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        cli.keyring,
        "set_password",
        lambda service, key, value: stored.append((service, key, value)),
    )

    class DummyAPI:
        def __init__(self, page: object):
            del page

        def search(self, *args, **kwargs) -> list[Train]:
            return []

    _patch_cli_runtime(monkeypatch)
    monkeypatch.setattr(cli, "KorailAPI", DummyAPI)
    monkeypatch.setattr(
        cli.sys,
        "stdin",
        type("DummyStdin", (), {"isatty": lambda self: True})(),
    )

    cli.main.callback(
        departure="서울",
        arrival="부산",
        date="20260320",
        time_str="07",
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
        api_backend="playwright",
    )

    assert ("KTX", "auto_pay", "0") in stored


def test_ensure_login_uses_updated_assisted_login_text(monkeypatch) -> None:
    messages: list[str] = []

    class DummyAPI:
        def wait_for_login_stable(self, **kwargs) -> bool:
            return False

        def prefill_login_form(self, login_id: str, login_pass: str) -> bool:
            assert login_id == "member1234"
            assert login_pass == "secret"
            return True

        def login_manual(self, timeout_s: int, open_login_page: bool = True) -> bool:
            assert timeout_s == 300
            assert open_login_page is False
            return True

    class DummyManager:
        _headless = False
        page = object()

        def close(self) -> None:
            raise AssertionError("close should not be called in headed mode")

        def start(self) -> None:
            raise AssertionError("start should not be called in headed mode")

        def save_cookies(self) -> None:
            messages.append("saved")

    monkeypatch.setattr(cli, "_load_login_credentials", lambda: ("member1234", "secret"))
    monkeypatch.setattr(cli, "colored", lambda text, *args, **kwargs: text)
    monkeypatch.setattr(cli.click, "echo", lambda message="": messages.append(str(message)))

    result = cli._ensure_login(
        DummyAPI(),
        DummyManager(),
        headless=False,
        use_external_firefox_login=False,
    )

    assert isinstance(result, DummyAPI)
    assert "[로그인 필요] 자동으로 접속된 브라우저에서 로그인 버튼을 직접 눌러주세요" in messages
    assert "saved" in messages
    assert messages[-1] == "[20:49:33] Login successful — session saved." or messages[-1].endswith("Login successful — session saved.")
