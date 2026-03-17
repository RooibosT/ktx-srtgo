from __future__ import annotations

from click.testing import CliRunner

from ktxgo import cli
from ktxgo.config import API_SCHEDULE
from ktxgo.korail import KorailAPI, Train


class _DummyManager:
    def __init__(self, headless: bool):
        self.page = object()

    def __enter__(self) -> _DummyManager:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


class _LoginManager:
    def __init__(self, headless: bool):
        self.page = object()
        self._headless = headless
        self.saved = False

    def close(self) -> None:
        return None

    def start(self) -> None:
        return None

    def save_cookies(self) -> None:
        self.saved = True


def _make_train(
    *,
    train_no: str = "00123",
    train_type: str = "KTX",
    train_group: str = "100",
    train_class_name: str | None = None,
) -> Train:
    return Train.from_schedule(
        {
            "h_trn_no": train_no,
            "h_car_tp_nm": train_type,
            "h_trn_clsf_nm": train_class_name or train_type,
            "h_trn_gp_nm": train_type,
            "h_dpt_rs_stn_nm": "서울",
            "h_arv_rs_stn_nm": "부산",
            "h_dpt_tm_qb": "08:10",
            "h_arv_tm_qb": "10:59",
            "h_dpt_dt": "20260320",
            "h_gen_rsv_nm": "예약가능",
            "h_gen_rsv_cd": "11",
            "h_spe_rsv_nm": "매진",
            "h_spe_rsv_cd": "13",
            "h_stnd_rsv_nm": "없음",
            "h_wait_rsv_nm": "가능",
            "h_wait_rsv_cd": "09",
            "h_rcvd_amt": "0059800",
            "h_trn_clsf_cd": train_group,
            "h_trn_gp_cd": train_group,
            "h_dpt_rs_stn_cd": "0001",
            "h_arv_rs_stn_cd": "0020",
            "h_run_dt": "20260320",
        }
    )


def test_cli_defaults_to_ktx_train_type(monkeypatch) -> None:
    search_calls: list[tuple[str, ...] | None] = []

    class DummyAPI:
        def __init__(self, page: object):
            del page

        def search(
            self,
            departure: str,
            arrival: str,
            date: str,
            time_str: str,
            adults: int = 1,
            train_types: tuple[str, ...] | None = None,
        ) -> list[object]:
            del departure, arrival, date, time_str, adults
            search_calls.append(train_types)
            return []

    monkeypatch.setattr(cli, "BrowserManager", _DummyManager)
    monkeypatch.setattr(cli, "KorailAPI", DummyAPI)
    monkeypatch.setattr(
        cli,
        "_ensure_login",
        lambda api, manager, headless, manual_login_only=False: api,
    )
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    monkeypatch.setattr(cli.signal, "signal", lambda *args, **kwargs: None)

    runner = CliRunner()
    result = runner.invoke(cli.main, ["--no-interactive", "--max-attempts", "1"])

    assert result.exit_code == 0
    assert search_calls == [("ktx",)]


def test_cli_passes_multiple_train_types(monkeypatch) -> None:
    search_calls: list[tuple[str, ...] | None] = []

    class DummyAPI:
        def __init__(self, page: object):
            del page

        def search(
            self,
            departure: str,
            arrival: str,
            date: str,
            time_str: str,
            adults: int = 1,
            train_types: tuple[str, ...] | None = None,
        ) -> list[object]:
            del departure, arrival, date, time_str, adults
            search_calls.append(train_types)
            return []

    monkeypatch.setattr(cli, "BrowserManager", _DummyManager)
    monkeypatch.setattr(cli, "KorailAPI", DummyAPI)
    monkeypatch.setattr(
        cli,
        "_ensure_login",
        lambda api, manager, headless, manual_login_only=False: api,
    )
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    monkeypatch.setattr(cli.signal, "signal", lambda *args, **kwargs: None)

    runner = CliRunner()
    result = runner.invoke(
        cli.main,
        [
            "--no-interactive",
            "--max-attempts",
            "1",
            "--train-type",
            "itx-saemaeul",
            "--train-type",
            "nuriro",
        ],
    )

    assert result.exit_code == 0
    assert search_calls == [("itx-saemaeul", "mugunghwa")]


def test_cli_passes_manual_login_only_flag(monkeypatch) -> None:
    search_calls: list[tuple[str, ...] | None] = []
    captured: dict[str, bool] = {}

    class DummyAPI:
        def __init__(self, page: object):
            del page

        def search(
            self,
            departure: str,
            arrival: str,
            date: str,
            time_str: str,
            adults: int = 1,
            train_types: tuple[str, ...] | None = None,
        ) -> list[object]:
            del departure, arrival, date, time_str, adults
            search_calls.append(train_types)
            return []

    def fake_ensure_login(
        api: object, manager: object, headless: bool, manual_login_only: bool
    ) -> object:
        del manager, headless
        captured["manual_login_only"] = manual_login_only
        return api

    monkeypatch.setattr(cli, "BrowserManager", _DummyManager)
    monkeypatch.setattr(cli, "KorailAPI", DummyAPI)
    monkeypatch.setattr(cli, "_ensure_login", fake_ensure_login)
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    monkeypatch.setattr(cli.signal, "signal", lambda *args, **kwargs: None)

    runner = CliRunner()
    result = runner.invoke(
        cli.main,
        ["--no-interactive", "--max-attempts", "1", "--manual-login-only"],
    )

    assert result.exit_code == 0
    assert captured == {"manual_login_only": True}
    assert search_calls == [("ktx",)]


def test_ensure_login_manual_login_only_skips_prefill(monkeypatch) -> None:
    class DummyAPI:
        def __init__(self) -> None:
            self.manual_login_called = False
            self.prefill_called = False

        def wait_for_login_stable(
            self, timeout_s: float, interval_s: float, stable_checks: int
        ) -> bool:
            del timeout_s, interval_s, stable_checks
            return False

        def prefill_login_form(self, member_id: str, password: str) -> bool:
            del member_id, password
            self.prefill_called = True
            return True

        def login_manual(self, timeout_s: int, open_login_page: bool = True) -> bool:
            del timeout_s, open_login_page
            self.manual_login_called = True
            return True

    monkeypatch.setattr(cli, "_load_login_credentials", lambda: ("id", "pw"))
    monkeypatch.setattr(cli.click, "echo", lambda *args, **kwargs: None)

    api = DummyAPI()
    manager = _LoginManager(headless=False)

    result = cli._ensure_login(api, manager, headless=False, manual_login_only=True)

    assert result is api
    assert api.manual_login_called is True
    assert api.prefill_called is False
    assert manager.saved is True


def test_ensure_login_default_uses_manual_guidance_and_prints_saved_credentials(
    monkeypatch,
) -> None:
    messages: list[str] = []

    class DummyAPI:
        def __init__(self) -> None:
            self.manual_login_called = False
            self.prefill_called = False

        def wait_for_login_stable(
            self, timeout_s: float, interval_s: float, stable_checks: int
        ) -> bool:
            del timeout_s, interval_s, stable_checks
            return False

        def prefill_login_form(self, member_id: str, password: str) -> bool:
            del member_id, password
            self.prefill_called = True
            return True

        def login_manual(self, timeout_s: int, open_login_page: bool = True) -> bool:
            del timeout_s, open_login_page
            self.manual_login_called = True
            return True

    monkeypatch.setattr(
        cli, "_load_login_credentials", lambda: ("12345678", "안녕123!")
    )
    monkeypatch.setattr(
        cli.click,
        "echo",
        lambda message="", *args, **kwargs: messages.append(str(message)),
    )

    api = DummyAPI()
    manager = _LoginManager(headless=False)

    result = cli._ensure_login(api, manager, headless=False)

    assert result is api
    assert api.manual_login_called is True
    assert api.prefill_called is False
    assert manager.saved is True
    joined = "\n".join(messages)
    assert "회원번호: 12345678" in joined
    assert "비밀번호: 안녕123!" in joined


def test_configure_login_interactive_uses_login_account_wording(monkeypatch) -> None:
    messages: list[str] = []
    captured_choices: list[list[tuple[str, str]]] = []

    class _FakeCookiePath:
        def is_file(self) -> bool:
            return False

    monkeypatch.setattr(cli, "_load_login_credentials", lambda: None)
    monkeypatch.setattr(cli, "COOKIE_PATH", _FakeCookiePath())
    monkeypatch.setattr(
        cli.click,
        "echo",
        lambda message="", *args, **kwargs: messages.append(str(message)),
    )

    def fake_list_input_guarded(
        *, message: str, choices: list[object], **kwargs: object
    ) -> str:
        del message, kwargs
        captured_choices.append(list(choices))
        return "cancel"

    monkeypatch.setattr(cli, "_list_input_guarded", fake_list_input_guarded)

    cli._configure_login_interactive()

    joined = "\n".join(messages)
    assert "로그인 계정: 미설정" in joined
    assert captured_choices[0][0] == ("로그인 계정 등록/수정", "credentials")


def test_normalize_train_types_expands_aliases() -> None:
    assert cli._normalize_train_types(("legacy-all", "nuriro", "ktx")) == (
        "ktx",
        "itx-saemaeul",
        "mugunghwa",
        "tonggeun",
        "itx-cheongchun",
        "itx-maeum",
        "airport",
    )


def test_interactive_train_scope_from_types() -> None:
    assert cli._interactive_train_scope_from_types(("ktx",)) == "ktx_only"
    assert (
        cli._interactive_train_scope_from_types(("ktx", "itx-saemaeul", "mugunghwa"))
        == "ktx_plus_general"
    )


def test_train_types_from_interactive_scope() -> None:
    assert cli._train_types_from_interactive_scope("ktx_only") == ("ktx",)
    assert cli._train_types_from_interactive_scope("ktx_plus_general") == (
        "ktx",
        "itx-saemaeul",
        "mugunghwa",
        "tonggeun",
        "itx-cheongchun",
        "itx-maeum",
        "airport",
    )


def test_interactive_train_scope_label_uses_itx_wording() -> None:
    assert (
        "KTX + ITX/무궁화 등",
        "ktx_plus_general",
    ) in cli._INTERACTIVE_TRAIN_SCOPE_CHOICES


def test_prompt_conditions_uses_train_scope_preset(monkeypatch) -> None:
    captured_names: list[str] = []

    def fake_prompt(questions: list[object]) -> dict[str, object]:
        captured_names.extend(question.name for question in questions)
        return {
            "departure": "서울",
            "arrival": "부산",
            "date": "20260320",
            "time": "07",
            "adults": 1,
            "train_scope": "ktx_plus_general",
        }

    monkeypatch.setattr(cli, "_prompt_guarded", fake_prompt)

    result = cli._prompt_conditions(
        "서울",
        "부산",
        "20260320",
        "07",
        1,
        ["서울", "부산"],
        ("ktx",),
    )

    assert "train_scope" in captured_names
    assert "train_types" not in captured_names
    assert result[-1] == (
        "ktx",
        "itx-saemaeul",
        "mugunghwa",
        "tonggeun",
        "itx-cheongchun",
        "itx-maeum",
        "airport",
    )


def test_format_train_type_normalizes_display_names() -> None:
    assert cli._format_train_type(_make_train(train_type="KTX")) == "KTX"
    assert (
        cli._format_train_type(
            _make_train(train_type="", train_group="101", train_class_name="ITX-새마을")
        )
        == "ITX-새마을"
    )
    assert (
        cli._format_train_type(
            _make_train(train_type="", train_group="102", train_class_name="무궁화호")
        )
        == "무궁화"
    )
    assert (
        cli._format_train_type(_make_train(train_type="ITX-청춘", train_group="104"))
        == "ITX-청춘"
    )
    assert (
        cli._format_train_type(
            _make_train(train_type="", train_group="101", train_class_name="ITX-마음")
        )
        == "ITX-마음"
    )


def test_train_choice_label_includes_normalized_train_type() -> None:
    label = cli._train_choice_label(
        0,
        _make_train(train_type="무궁화호", train_group="102"),
    )

    assert "무궁화" in label
    assert "무궁화호" not in label


def test_print_results_uses_normalized_train_type(capsys) -> None:
    cli._print_results([_make_train(train_type="무궁화호", train_group="102")])

    output = capsys.readouterr().out
    assert "무궁화" in output
    assert "무궁화호" not in output


def test_search_requests_each_selected_train_type_and_merges_sorted() -> None:
    api = KorailAPI.__new__(KorailAPI)
    calls: list[tuple[str, str, str]] = []

    def fake_api_call(endpoint: str, params: dict[str, str]) -> dict[str, object]:
        calls.append((endpoint, params["selGoTrain"], params["txtTrnGpCd"]))
        train_group = params["txtTrnGpCd"]
        rows = {
            "100": [
                {
                    "h_trn_no": "00123",
                    "h_car_tp_nm": "KTX",
                    "h_trn_gp_nm": "KTX",
                    "h_dpt_rs_stn_nm": "서울",
                    "h_arv_rs_stn_nm": "부산",
                    "h_dpt_tm_qb": "08:10",
                    "h_arv_tm_qb": "10:59",
                    "h_dpt_dt": "20260320",
                    "h_gen_rsv_nm": "예약가능",
                    "h_gen_rsv_cd": "11",
                    "h_spe_rsv_nm": "매진",
                    "h_spe_rsv_cd": "13",
                    "h_stnd_rsv_nm": "없음",
                    "h_wait_rsv_nm": "없음",
                    "h_wait_rsv_cd": "13",
                    "h_rcvd_amt": "0059800",
                    "h_trn_clsf_cd": "100",
                    "h_trn_gp_cd": "100",
                    "h_dpt_rs_stn_cd": "0001",
                    "h_arv_rs_stn_cd": "0020",
                    "h_run_dt": "20260320",
                }
            ],
            "102": [
                {
                    "h_trn_no": "00077",
                    "h_car_tp_nm": "무궁화",
                    "h_trn_gp_nm": "무궁화",
                    "h_dpt_rs_stn_nm": "서울",
                    "h_arv_rs_stn_nm": "부산",
                    "h_dpt_tm_qb": "07:05",
                    "h_arv_tm_qb": "12:10",
                    "h_dpt_dt": "20260320",
                    "h_gen_rsv_nm": "예약가능",
                    "h_gen_rsv_cd": "11",
                    "h_spe_rsv_nm": "없음",
                    "h_spe_rsv_cd": "13",
                    "h_stnd_rsv_nm": "없음",
                    "h_wait_rsv_nm": "가능",
                    "h_wait_rsv_cd": "09",
                    "h_rcvd_amt": "0028000",
                    "h_trn_clsf_cd": "102",
                    "h_trn_gp_cd": "102",
                    "h_dpt_rs_stn_cd": "0001",
                    "h_arv_rs_stn_cd": "0020",
                    "h_run_dt": "20260320",
                }
            ],
        }[train_group]
        return {"trn_infos": {"trn_info": rows}}

    api._api_call = fake_api_call  # type: ignore[method-assign]

    trains = api.search(
        "서울",
        "부산",
        "20260320",
        "07",
        adults=1,
        train_types=("ktx", "nuriro", "mugunghwa"),
    )

    assert calls == [
        (API_SCHEDULE, "100", "100"),
        (API_SCHEDULE, "102", "102"),
    ]
    assert [train.train_no for train in trains] == ["00077", "00123"]


def test_search_filters_itx_maeum_from_shared_101_group() -> None:
    api = KorailAPI.__new__(KorailAPI)
    calls: list[tuple[str, str]] = []

    def fake_api_call(endpoint: str, params: dict[str, str]) -> dict[str, object]:
        del endpoint
        calls.append((params["selGoTrain"], params["txtTrnGpCd"]))
        assert params["txtTrnGpCd"] == "101"
        return {
            "trn_infos": {
                "trn_info": [
                    {
                        "h_trn_no": "00011",
                        "h_car_tp_nm": "",
                        "h_trn_clsf_nm": "ITX-새마을",
                        "h_trn_gp_nm": "ITX-새마을",
                        "h_dpt_rs_stn_nm": "서울",
                        "h_arv_rs_stn_nm": "부산",
                        "h_dpt_tm_qb": "07:10",
                        "h_arv_tm_qb": "10:20",
                        "h_dpt_dt": "20260320",
                        "h_gen_rsv_nm": "예약가능",
                        "h_gen_rsv_cd": "11",
                        "h_spe_rsv_nm": "매진",
                        "h_spe_rsv_cd": "13",
                        "h_stnd_rsv_nm": "없음",
                        "h_wait_rsv_nm": "가능",
                        "h_wait_rsv_cd": "09",
                        "h_rcvd_amt": "0045000",
                        "h_trn_clsf_cd": "101",
                        "h_trn_gp_cd": "101",
                        "h_dpt_rs_stn_cd": "0001",
                        "h_arv_rs_stn_cd": "0020",
                        "h_run_dt": "20260320",
                    },
                    {
                        "h_trn_no": "00021",
                        "h_car_tp_nm": "",
                        "h_trn_clsf_nm": "ITX-마음",
                        "h_trn_gp_nm": "ITX-새마을",
                        "h_dpt_rs_stn_nm": "서울",
                        "h_arv_rs_stn_nm": "부산",
                        "h_dpt_tm_qb": "07:30",
                        "h_arv_tm_qb": "10:40",
                        "h_dpt_dt": "20260320",
                        "h_gen_rsv_nm": "예약가능",
                        "h_gen_rsv_cd": "11",
                        "h_spe_rsv_nm": "매진",
                        "h_spe_rsv_cd": "13",
                        "h_stnd_rsv_nm": "없음",
                        "h_wait_rsv_nm": "가능",
                        "h_wait_rsv_cd": "09",
                        "h_rcvd_amt": "0049000",
                        "h_trn_clsf_cd": "101",
                        "h_trn_gp_cd": "101",
                        "h_dpt_rs_stn_cd": "0001",
                        "h_arv_rs_stn_cd": "0020",
                        "h_run_dt": "20260320",
                    },
                ]
            }
        }

    api._api_call = fake_api_call  # type: ignore[method-assign]

    maeum_only = api.search(
        "서울", "부산", "20260320", "07", adults=1, train_types=("itx-maeum",)
    )
    saemaeul_only = api.search(
        "서울", "부산", "20260320", "07", adults=1, train_types=("itx-saemaeul",)
    )
    both = api.search(
        "서울",
        "부산",
        "20260320",
        "07",
        adults=1,
        train_types=("itx-saemaeul", "itx-maeum"),
    )

    assert calls == [("101", "101"), ("101", "101"), ("101", "101")]
    assert [train.raw["h_trn_clsf_nm"] for train in maeum_only] == ["ITX-마음"]
    assert [train.raw["h_trn_clsf_nm"] for train in saemaeul_only] == ["ITX-새마을"]
    assert [train.raw["h_trn_clsf_nm"] for train in both] == ["ITX-새마을", "ITX-마음"]


def test_reserve_uses_train_codes_from_response() -> None:
    api = KorailAPI.__new__(KorailAPI)
    captured: dict[str, str] = {}

    def fake_api_call(endpoint: str, params: dict[str, str]) -> dict[str, object]:
        del endpoint
        captured.update(params)
        return {"strResult": "SUCC"}

    api._api_call = fake_api_call  # type: ignore[method-assign]
    train = Train.from_schedule(
        {
            "h_trn_no": "00077",
            "h_car_tp_nm": "무궁화",
            "h_trn_gp_nm": "무궁화",
            "h_dpt_rs_stn_nm": "서울",
            "h_arv_rs_stn_nm": "부산",
            "h_dpt_tm_qb": "07:05",
            "h_dpt_dt": "20260320",
            "h_gen_rsv_nm": "예약가능",
            "h_gen_rsv_cd": "11",
            "h_spe_rsv_nm": "없음",
            "h_spe_rsv_cd": "13",
            "h_stnd_rsv_nm": "없음",
            "h_wait_rsv_nm": "가능",
            "h_wait_rsv_cd": "09",
            "h_rcvd_amt": "0028000",
            "h_trn_clsf_cd": "102",
            "h_trn_gp_cd": "102",
            "h_dpt_rs_stn_cd": "0001",
            "h_arv_rs_stn_cd": "0020",
            "h_run_dt": "20260320",
        }
    )

    api.reserve(train, seat_type="general", adults=1)

    assert captured["txtTrnClsfCd1"] == "102"
    assert captured["txtTrnGpCd1"] == "102"
