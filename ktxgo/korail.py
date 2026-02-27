from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import cast

from playwright.sync_api import Page

from .config import (
    API_LOGIN_CHECK,
    API_PAY,
    API_RESERVATION_LIST,
    API_RESERVATION_VIEW,
    API_RESERVE,
    API_SCHEDULE,
    LOGIN_URL,
    MOBILE_DEVICE,
    MOBILE_KEY,
    MOBILE_VERSION,
    RSV_AVAILABLE,
    SEARCH_URL,
    TRAIN_GROUP_ALL,
    TRAIN_GROUP_KTX,
)


class KorailError(RuntimeError):
    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code: str | None = code


@dataclass(slots=True)
class Train:
    train_no: str
    train_type: str
    train_group: str
    departure: str
    arrival: str
    dep_time: str
    arr_time: str
    dep_date: str
    general_seat: str
    general_code: str
    special_seat: str
    special_code: str
    standing_seat: str
    price: str
    raw: dict[str, str]

    @property
    def has_general(self) -> bool:
        return self.general_code == RSV_AVAILABLE

    @property
    def has_special(self) -> bool:
        return self.special_code == RSV_AVAILABLE

    @property
    def has_any_seat(self) -> bool:
        return self.has_general or self.has_special

    @property
    def has_standing(self) -> bool:
        return self.raw.get("h_stnd_rsv_cd", "") == RSV_AVAILABLE

    @classmethod
    def from_schedule(cls, row: dict[str, object]) -> Train:
        normalized = {
            str(key): "" if value is None else str(value) for key, value in row.items()
        }
        return cls(
            train_no=normalized.get("h_trn_no", ""),
            train_type=normalized.get("h_car_tp_nm", ""),
            train_group=normalized.get("h_trn_gp_nm", ""),
            departure=normalized.get("h_dpt_rs_stn_nm", ""),
            arrival=normalized.get("h_arv_rs_stn_nm", ""),
            dep_time=normalized.get("h_dpt_tm_qb", ""),
            arr_time=normalized.get("h_arv_tm_qb", ""),
            dep_date=normalized.get("h_dpt_dt", ""),
            general_seat=normalized.get("h_gen_rsv_nm", ""),
            general_code=normalized.get("h_gen_rsv_cd", ""),
            special_seat=normalized.get("h_spe_rsv_nm", ""),
            special_code=normalized.get("h_spe_rsv_cd", ""),
            standing_seat=normalized.get("h_stnd_rsv_nm", ""),
            price=normalized.get("h_rcvd_amt", ""),
            raw=normalized,
        )


class KorailAPI:
    def __init__(self, page: Page):
        self.page: Page = page

    def _api_call(self, endpoint: str, params: dict[str, str]) -> dict[str, object]:
        payload = cast(
            dict[str, object],
            self.page.evaluate(
                """async ({ endpoint, params }) => {
                const form = new FormData();
                for (const [key, value] of Object.entries(params)) {
                    form.append(key, value == null ? "" : String(value));
                }

                const response = await fetch(endpoint, {
                    method: "POST",
                    body: form,
                    credentials: "include"
                });

                const text = await response.text();
                return { ok: response.ok, status: response.status, text };
            }""",
                {"endpoint": endpoint, "params": params},
            ),
        )

        text_obj = payload.get("text", "")
        text = str(text_obj).strip()
        if not text:
            raise KorailError(f"Empty response from {endpoint}")

        raw_data: object
        try:
            raw_data = cast(object, json.loads(text))
        except json.JSONDecodeError as exc:
            raise KorailError(f"Invalid JSON from {endpoint}") from exc

        if not isinstance(raw_data, dict):
            raise KorailError(f"Unexpected JSON payload from {endpoint}")

        data = cast(dict[str, object], raw_data)

        if str(data.get("strResult", "")) == "FAIL":
            raise KorailError(
                str(
                    data.get("h_msg_txt") or data.get("message") or "Korail API failed"
                ),
                str(data.get("h_msg_cd") or data.get("code") or ""),
            )

        return data

    def login_manual(self, timeout_s: int = 300) -> bool:
        """Navigate to login page and wait for user to log in manually.

        DynaPath blocks programmatic login API calls, so the user must
        log in through the real web form.  After login, cookies are saved
        by the caller (BrowserManager.save_cookies) for future reuse.
        """
        _ = self.page.goto(LOGIN_URL, wait_until="networkidle")
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.is_logged_in():
                _ = self.page.goto(SEARCH_URL, wait_until="networkidle")
                return True
            time.sleep(2.0)
        return False

    def search(
        self,
        departure: str,
        arrival: str,
        date: str,
        time_str: str,
        adults: int = 1,
    ) -> list[Train]:
        hh = time_str.zfill(2)
        params = {
            "Device": "BH",
            "Version": "999999999",
            "radJobId": "1",
            "selGoTrain": TRAIN_GROUP_ALL,
            "txtCardPsgCnt": "0",
            "txtGdNo": "",
            "txtGoAbrdDt": date,
            "txtGoEnd": arrival,
            "txtGoHour": f"{hh}0000",
            "txtGoStart": departure,
            "txtJobDv": "",
            "txtMenuId": "11",
            "txtPsgFlg_1": str(adults),
            "txtPsgFlg_2": "0",
            "txtPsgFlg_3": "0",
            "txtPsgFlg_4": "0",
            "txtPsgFlg_5": "0",
            "txtSeatAttCd_2": "000",
            "txtSeatAttCd_3": "000",
            "txtSeatAttCd_4": "015",
            "txtTrnGpCd": TRAIN_GROUP_KTX,
            "searchType": "GENERAL",
        }
        data = self._api_call(API_SCHEDULE, params)
        trn_infos_obj = data.get("trn_infos")
        if not isinstance(trn_infos_obj, dict):
            return []
        trn_infos = cast(dict[str, object], trn_infos_obj)

        trn_info_obj = trn_infos.get("trn_info")
        if isinstance(trn_info_obj, dict):
            return [Train.from_schedule(cast(dict[str, object], trn_info_obj))]
        if not isinstance(trn_info_obj, list):
            return []

        trains: list[Train] = []
        for row in cast(list[object], trn_info_obj):
            if isinstance(row, dict):
                trains.append(Train.from_schedule(cast(dict[str, object], row)))
        return trains

    def reserve(
        self,
        train: Train,
        seat_type: str = "general",
        adults: int = 1,
    ) -> dict[str, object]:
        seat_code = "1" if seat_type == "general" else "2"
        dep_time = str(
            train.raw.get("h_dpt_tm", "") or train.raw.get("h_dpt_tm_qb", "")
        )
        dep_time = dep_time.replace(":", "")
        if len(dep_time) == 4:
            dep_time = f"{dep_time}00"

        params = {
            "Device": "BH",
            "Version": "999999999",
            "txtMenuId": "11",
            "txtJobId": "1101",
            "txtGdNo": "",
            "hidFreeFlg": "N",
            "txtTotPsgCnt": str(adults),
            "txtSeatAttCd1": "000",
            "txtSeatAttCd2": "000",
            "txtSeatAttCd3": "000",
            "txtSeatAttCd4": "015",
            "txtSeatAttCd5": "000",
            "txtStndFlg": "N",
            "txtSrcarCnt": "0",
            "txtJrnyCnt": "1",
            "txtJrnySqno1": "001",
            "txtJrnyTpCd1": "11",
            "txtDptDt1": train.dep_date,
            "txtDptRsStnCd1": str(train.raw.get("h_dpt_rs_stn_cd", "")),
            "txtDptTm1": dep_time,
            "txtArvRsStnCd1": str(train.raw.get("h_arv_rs_stn_cd", "")),
            "txtTrnNo1": train.train_no,
            "txtRunDt1": str(train.raw.get("h_run_dt", train.dep_date)),
            "txtTrnClsfCd1": str(train.raw.get("h_trn_clsf_cd", "100")),
            "txtTrnGpCd1": str(train.raw.get("h_trn_gp_cd", TRAIN_GROUP_KTX)),
            "txtPsrmClCd1": seat_code,
            "txtChgFlg1": "",
            # Passenger 1: adult
            "txtPsgTpCd1": "1",
            "txtDiscKndCd1": "000",
            "txtCompaCnt1": str(adults),
            "txtCardCode_1": "",
            "txtCardNo_1": "",
            "txtCardPw_1": "",
        }
        return self._api_call(API_RESERVE, params)

    def is_logged_in(self) -> bool:
        try:
            data = self._api_call(API_LOGIN_CHECK, {})
        except KorailError:
            return False

        # loginCheck returns strResult=SUCC even when NOT logged in.
        # Must check h_msg_txt to distinguish.
        msg = str(data.get("h_msg_txt", "")).strip()
        if "로그인 정보가 없습니다" in msg or "로그인" in msg and "없" in msg:
            return False

        # Positive indicators
        if data.get("strResult") in {"SUCC", "SUCCESS", "Y"}:
            # Double-check: presence of member credentials confirms login
            for key in ("strMbCrdNo", "strCustNm", "mbCrdNo"):
                value = str(data.get(key, "")).strip()
                if value and value not in {"N", "FALSE", "0"}:
                    return True
            # strResult=SUCC without negative msg — likely logged in
            return True

        for key in ("loginYn", "isLogin"):
            value = str(data.get(key, "")).strip().upper()
            if value and value not in {"N", "FALSE", "0"}:
                return True
        return False

    def reservations(self) -> list[dict[str, object]]:
        data = self._api_call(
            API_RESERVATION_LIST,
            {
                "Device": "BH",
                "Version": "999999999",
            },
        )

        jrny_infos_obj = data.get("jrny_infos")
        if not isinstance(jrny_infos_obj, dict):
            return []
        jrny_infos = cast(dict[str, object], jrny_infos_obj)

        items_obj = jrny_infos.get("jrny_info")
        if isinstance(items_obj, dict):
            return [cast(dict[str, object], items_obj)]
        if not isinstance(items_obj, list):
            return []

        reservations: list[dict[str, object]] = []
        for item in cast(list[object], items_obj):
            if isinstance(item, dict):
                reservations.append(cast(dict[str, object], item))
        return reservations

    def pay(
        self,
        reserve_result: dict[str, object],
        card_number: str,
        card_password: str,
        birthday: str,
        card_expire: str,
        smart_ticket: bool = True,
        installment: int = 0,
        card_type: str | None = None,
    ) -> dict[str, object]:
        """Pay for a reservation using a credit card.

        Args:
            reserve_result: The dict returned by reserve().
            card_number: Full card number (no hyphens).
            card_password: First 2 digits of card password.
            birthday: YYMMDD (individual) or 10-digit biz registration number.
            card_expire: Card expiry in YYMM format.
            smart_ticket: True = issue as smart ticket (KorailTalk), False = non-smart.
            installment: 0 = lump sum, N = N-month installment.
            card_type: 'J' for individual, 'S' for business. Auto-detected from birthday length.
        """
        pnr_no = str(reserve_result.get("h_pnr_no", ""))
        if not pnr_no:
            raise KorailError("Missing reservation number (h_pnr_no).")

        wct_no = str(reserve_result.get("h_wct_no", ""))
        rsv_chg_no = str(
            reserve_result.get("h_rsv_chg_no", reserve_result.get("hidRsvChgNo", ""))
        ).strip()
        tmp_job_sqno1 = str(reserve_result.get("h_tmp_job_sqno1", "000000"))
        tmp_job_sqno2 = str(reserve_result.get("h_tmp_job_sqno2", "000000"))
        price = ""

        def _digit_only(value: object) -> str:
            return "".join(ch for ch in str(value) if ch.isdigit())

        def _dict_items(value: object) -> list[dict[str, object]]:
            if isinstance(value, dict):
                return [cast(dict[str, object], value)]
            if not isinstance(value, list):
                return []
            return [
                cast(dict[str, object], item)
                for item in cast(list[object], value)
                if isinstance(item, dict)
            ]

        def _hydrate_from_item(item: dict[str, object], *, require_pnr: bool) -> None:
            nonlocal price, wct_no, tmp_job_sqno1, tmp_job_sqno2, rsv_chg_no
            if require_pnr:
                item_pnr = str(item.get("h_pnr_no", item.get("hidPnrNo", ""))).strip()
                if item_pnr and item_pnr != pnr_no:
                    return

            if (not price or price == "0"):
                for key in ("h_rsv_amt", "h_rcvd_amt", "hidPayAmount"):
                    candidate = _digit_only(item.get(key, ""))
                    if candidate and candidate != "0":
                        price = candidate
                        break

            if not wct_no:
                for key in ("h_wct_no", "hidWctNo"):
                    candidate = str(item.get(key, "")).strip()
                    if candidate:
                        wct_no = candidate
                        break

            if not rsv_chg_no:
                for key in ("h_rsv_chg_no", "hidRsvChgNo"):
                    candidate = str(item.get(key, "")).strip()
                    if candidate:
                        rsv_chg_no = candidate
                        break

            if tmp_job_sqno1 in {"", "000000"}:
                candidate = str(item.get("h_tmp_job_sqno1", "")).strip()
                if candidate:
                    tmp_job_sqno1 = candidate

            if tmp_job_sqno2 in {"", "000000"}:
                candidate = str(item.get("h_tmp_job_sqno2", "")).strip()
                if candidate:
                    tmp_job_sqno2 = candidate

        def _hydrate_from_payload(
            data: dict[str, object], *, include_top_level: bool
        ) -> None:
            if include_top_level:
                _hydrate_from_item(data, require_pnr=False)
            jrny_infos_obj = data.get("jrny_infos")
            if not isinstance(jrny_infos_obj, dict):
                return
            jrny_items = _dict_items(jrny_infos_obj.get("jrny_info"))
            for jrny in jrny_items:
                _hydrate_from_item(jrny, require_pnr=False)

                train_infos_obj = jrny.get("train_infos")
                if not isinstance(train_infos_obj, dict):
                    continue
                for train in _dict_items(train_infos_obj.get("train_info")):
                    _hydrate_from_item(train, require_pnr=True)

        _hydrate_from_payload(reserve_result, include_top_level=True)

        # Some reserve responses omit payment context.
        # Recover from reservation APIs using the same defaults as KorailTalk.
        mobile_base = {
            "Device": MOBILE_DEVICE,
            "Version": MOBILE_VERSION,
            "Key": MOBILE_KEY,
        }
        need_context = (
            (not price or price == "0")
            or not wct_no
            or not rsv_chg_no
            or tmp_job_sqno1 in {"", "000000"}
            or tmp_job_sqno2 in {"", "000000"}
        )
        if need_context:
            detail = self._api_call(
                API_RESERVATION_LIST,
                {
                    **mobile_base,
                    "hidPnrNo": pnr_no,
                },
            )
            _hydrate_from_payload(detail, include_top_level=True)

        if (
            (not price or price == "0")
            or not wct_no
            or not rsv_chg_no
            or tmp_job_sqno1 in {"", "000000"}
            or tmp_job_sqno2 in {"", "000000"}
        ):
            view = self._api_call(API_RESERVATION_VIEW, mobile_base)
            _hydrate_from_payload(view, include_top_level=False)

        if not price or price == "0":
            raise KorailError("Unable to determine payment amount.")
        if not wct_no:
            raise KorailError("Unable to determine payment key (h_wct_no).")

        if card_type is None:
            card_type = "J" if len(birthday) <= 6 else "S"

        params = {
            "Device": MOBILE_DEVICE,
            "Version": MOBILE_VERSION,
            "Key": MOBILE_KEY,
            "hidPnrNo": pnr_no,
            "hidWctNo": wct_no,
            "hidTmpJobSqno1": tmp_job_sqno1,
            "hidTmpJobSqno2": tmp_job_sqno2,
            "hidRsvChgNo": rsv_chg_no or "000",
            "hidInrecmnsGridcnt": "1",
            "hidStlMnsSqno1": "1",
            "hidStlMnsCd1": "02",
            "hidMnsStlAmt1": price,
            "hidCrdInpWayCd1": "@",
            "hidStlCrCrdNo1": card_number,
            "hidVanPwd1": card_password,
            "hidCrdVlidTrm1": card_expire,
            "hidIsmtMnthNum1": str(installment),
            "hidAthnDvCd1": card_type,
            "hidAthnVal1": birthday,
            "hiduserYn": "Y" if smart_ticket else "N",
        }
        return self._api_call(API_PAY, params)
