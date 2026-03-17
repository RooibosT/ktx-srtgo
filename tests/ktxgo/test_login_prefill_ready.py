from __future__ import annotations

from ktxgo.korail import KorailAPI


class _PageForState:
    def __init__(self, payload: dict[str, object]):
        self.payload = payload

    def evaluate(self, script: str) -> dict[str, object]:
        assert "memberModePresent" in script
        return self.payload


class _PageForReady:
    def __init__(self):
        self.waits: list[int] = []

    def wait_for_timeout(self, value: int) -> None:
        self.waits.append(value)


class _PageForBlur(_PageForReady):
    pass


class _PageForSubmitWait(_PageForReady):
    pass


class _FieldRecorder:
    def __init__(self):
        self.calls: list[tuple[str, object]] = []

    @property
    def first(self) -> _FieldRecorder:
        return self

    def click(self, timeout: int) -> None:
        self.calls.append(("click", timeout))

    def press(self, key: str, timeout: int) -> None:
        self.calls.append(("press", (key, timeout)))

    def type(self, value: str, delay: int, timeout: int) -> None:
        self.calls.append(("type", (value, delay, timeout)))


def test_collect_prefill_login_state_returns_normalized_snapshot() -> None:
    api = KorailAPI.__new__(KorailAPI)
    api.page = _PageForState(
        {
            "memberModePresent": True,
            "idPresent": True,
            "passwordPresent": True,
            "passwordReadOnly": False,
            "passwordDisabled": False,
            "loginButtonPresent": True,
            "hiddenSecurityFields": ["password_nfilter_sec"],
            "nshcPresent": True,
        }
    )

    snapshot = api._collect_prefill_login_state()

    assert snapshot["member_mode_present"] is True
    assert snapshot["id_present"] is True
    assert snapshot["password_present"] is True
    assert snapshot["login_button_present"] is True
    assert snapshot["password_read_only"] is False
    assert snapshot["password_disabled"] is False
    assert snapshot["hidden_security_fields"] == ["password_nfilter_sec"]
    assert snapshot["nshc_present"] is True


def test_wait_for_prefill_login_ready_retries_until_controls_exist(monkeypatch) -> None:
    api = KorailAPI.__new__(KorailAPI)
    api.page = _PageForReady()

    snapshots = iter(
        [
            {
                "member_mode_present": False,
                "id_present": True,
                "password_present": True,
                "login_button_present": True,
                "password_read_only": False,
                "password_disabled": False,
                "hidden_security_fields": [],
                "nshc_present": False,
            },
            {
                "member_mode_present": True,
                "id_present": True,
                "password_present": True,
                "login_button_present": True,
                "password_read_only": False,
                "password_disabled": False,
                "hidden_security_fields": ["password_nfilter_sec"],
                "nshc_present": True,
            },
        ]
    )

    monkeypatch.setattr(api, "_collect_prefill_login_state", lambda: next(snapshots))

    ready, snapshot = api._wait_for_prefill_login_ready(max_checks=2, settle_ms=250)

    assert ready is True
    assert snapshot["member_mode_present"] is True
    assert snapshot["hidden_security_fields"] == ["password_nfilter_sec"]
    assert api.page.waits == [200, 250]


def test_type_prefill_value_uses_press_instead_of_fill() -> None:
    api = KorailAPI.__new__(KorailAPI)
    field = _FieldRecorder()

    api._type_prefill_value(field, "123456", delay=70, timeout=4000)

    assert field.calls == [
        ("click", 2000),
        ("press", ("ControlOrMeta+A", 1000)),
        ("press", ("Backspace", 1000)),
        ("type", ("123456", 70, 4000)),
    ]


def test_stabilize_prefill_password_state_blurs_to_safe_target() -> None:
    api = KorailAPI.__new__(KorailAPI)
    api.page = _PageForBlur()
    blur_target = _FieldRecorder()

    api._stabilize_prefill_password_state(blur_target, settle_ms=700)

    assert blur_target.calls == [("click", 2000)]
    assert api.page.waits == [700]


def test_wait_before_prefilled_submit_uses_longer_second_attempt() -> None:
    api = KorailAPI.__new__(KorailAPI)
    api.page = _PageForSubmitWait()

    api._wait_before_prefilled_submit(0)
    api._wait_before_prefilled_submit(1)

    assert api.page.waits == [1200, 1800]
