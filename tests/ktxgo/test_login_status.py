from __future__ import annotations

from ktxgo.config import API_LOGIN_CHECK
from ktxgo.korail import KorailAPI


def _api_with_login_check_payload(payload: dict[str, object]) -> KorailAPI:
    api = KorailAPI.__new__(KorailAPI)

    def fake_api_call(endpoint: str, params: dict[str, str]) -> dict[str, object]:
        assert endpoint == API_LOGIN_CHECK
        assert params == {}
        return payload

    api._api_call = fake_api_call  # type: ignore[attr-defined]
    return api


def test_is_logged_in_rejects_bare_success_without_member_identity() -> None:
    api = _api_with_login_check_payload({"strResult": "SUCC", "h_msg_txt": ""})

    assert api.is_logged_in() is False


def test_is_logged_in_accepts_member_identity() -> None:
    api = _api_with_login_check_payload(
        {
            "strResult": "SUCC",
            "strMbCrdNo": "1234567890",
            "strCustNm": "홍길동",
        }
    )

    assert api.is_logged_in() is True


def test_login_profile_rejects_bare_success_without_member_identity() -> None:
    api = _api_with_login_check_payload({"strResult": "SUCC", "h_msg_txt": ""})

    assert api.login_profile() is None
