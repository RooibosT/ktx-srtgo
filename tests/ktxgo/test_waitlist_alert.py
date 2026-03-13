from __future__ import annotations

import pytest
from click.testing import CliRunner

from ktxgo import cli
from ktxgo.korail import KorailAPI, KorailError, Train


class _DummyManager:
    def __init__(self, headless: bool):
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
    monkeypatch.setattr(cli, "_ensure_login", lambda api, manager, headless: api)
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    monkeypatch.setattr(cli.signal, "signal", lambda *args, **kwargs: None)


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
