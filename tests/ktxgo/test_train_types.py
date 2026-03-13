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


def _make_train(
    *,
    train_no: str = "00123",
    train_type: str = "KTX",
    train_group: str = "100",
) -> Train:
    return Train.from_schedule(
        {
            "h_trn_no": train_no,
            "h_car_tp_nm": train_type,
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
    monkeypatch.setattr(cli, "_ensure_login", lambda api, manager, headless: api)
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
    monkeypatch.setattr(cli, "_ensure_login", lambda api, manager, headless: api)
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


def test_normalize_train_types_expands_aliases() -> None:
    assert cli._normalize_train_types(("legacy-all", "nuriro", "ktx")) == (
        "ktx",
        "itx-saemaeul",
        "mugunghwa",
        "tonggeun",
        "itx-cheongchun",
        "airport",
    )


def test_format_train_type_normalizes_display_names() -> None:
    assert cli._format_train_type(_make_train(train_type="KTX")) == "KTX"
    assert cli._format_train_type(_make_train(train_type="ITX-새마을", train_group="101")) == "ITX-새마을"
    assert cli._format_train_type(_make_train(train_type="무궁화호", train_group="102")) == "무궁화"
    assert cli._format_train_type(_make_train(train_type="ITX-청춘", train_group="104")) == "ITX-청춘"


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
