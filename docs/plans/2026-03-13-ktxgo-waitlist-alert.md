# KTXgo Waitlist Alert Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** After a successful KTX waitlist booking, automatically register the Korail seat-assignment alert with the requested channel and phone number.

**Architecture:** Keep the existing waitlist booking flow in `ktxgo.cli`, then add one post-success follow-up call in `ktxgo.korail`. Prefer reusing the current browser session and `_api_call()` for the alert request; only add a small mobile-domain helper if the captured upstream request proves the alert action cannot run from the current session/origin.

**Tech Stack:** Python 3.10+, Click CLI, Playwright browser session, keyring, pytest

---

## Blocking Prerequisite

Do not start the code changes below until the real Korail request contract is captured from the reservation-history "좌석 배정 알림 신청" flow.

Capture checklist:

- endpoint path and origin
- HTTP method
- exact form field names
- which identifier is used: `h_pnr_no`, `hidPnrNo`, `h_rsv_chg_no`, another token, or a combination
- channel field values for SMS, KakaoTalk, and both if present
- phone-number field name and accepted format
- success markers in the JSON response
- failure markers in the JSON response

If the captured request targets `smart.letskorail.com` or otherwise fails from the current browser session, use the fallback branch in Task 3.

### Task 1: Add Failing Tests For Waitlist Alert Orchestration

**Files:**
- Create: `tests/ktxgo/test_waitlist_alert.py`
- Modify: `tests/ktxgo/test_train_types.py`
- Test: `tests/ktxgo/test_waitlist_alert.py`

**Step 1: Write the failing test for alert setting resolution**

```python
def test_resolve_waitlist_alert_settings_prefers_cli_over_keyring(monkeypatch):
    monkeypatch.setattr(cli.keyring, "get_password", lambda service, key: {
        ("KTX", "waitlist_alert_phone"): "01099998888",
        ("KTX", "waitlist_alert_channel"): "sms",
    }.get((service, key)))

    settings = cli._resolve_waitlist_alert_settings(
        phone="01012341234",
        channel="kakao",
    )

    assert settings == ("01012341234", "kakao")
```

**Step 2: Write the failing test for waitlist follow-up**

```python
def test_cli_registers_waitlist_alert_after_waitlist_success(monkeypatch):
    calls = []

    class DummyAPI:
        def __init__(self, page):
            del page

        def search(self, *args, **kwargs):
            return [train_with_waitlist_only()]

        def reserve(self, train, seat_type="general", adults=1, waitlist=False):
            assert waitlist is True
            return {"h_pnr_no": "PNR123", "strResult": "SUCC"}

        def set_waitlist_alert(self, pnr_no, phone, channel):
            calls.append((pnr_no, phone, channel))
            return {"strResult": "SUCC"}
```

Assert `calls == [("PNR123", "01012341234", "sms")]`.

**Step 3: Write the failing test for partial failure handling**

```python
def test_cli_keeps_waitlist_success_when_alert_registration_fails(monkeypatch):
    class DummyAPI:
        ...
        def set_waitlist_alert(self, pnr_no, phone, channel):
            raise KorailError("alert registration failed", "ERR")

    result = runner.invoke(
        cli.main,
        [
            "--no-interactive",
            "--max-attempts", "1",
            "--waitlist-alert-phone", "01012341234",
            "--waitlist-alert-channel", "sms",
        ],
    )

    assert result.exit_code == 0
    assert "예약대기 신청완료" in result.output
    assert "alert registration failed" in result.output
```

**Step 4: Run tests to verify they fail**

Run: `pytest tests/ktxgo/test_waitlist_alert.py -v`

Expected: FAIL with missing `_resolve_waitlist_alert_settings`, missing `set_waitlist_alert`, or missing CLI options.

**Step 5: Commit**

```bash
git add tests/ktxgo/test_waitlist_alert.py tests/ktxgo/test_train_types.py
git commit -m "test: add waitlist alert orchestration coverage"
```

### Task 2: Add Alert Endpoint Constants And API Helper

**Files:**
- Modify: `ktxgo/config.py`
- Modify: `ktxgo/korail.py`
- Test: `tests/ktxgo/test_waitlist_alert.py`

**Step 1: Write the failing unit test for the request builder**

```python
def test_set_waitlist_alert_uses_captured_contract(monkeypatch):
    api = KorailAPI.__new__(KorailAPI)
    captured = {}

    def fake_api_call(endpoint, params):
        captured["endpoint"] = endpoint
        captured["params"] = params
        return {"strResult": "SUCC"}

    api._api_call = fake_api_call
    api.set_waitlist_alert("PNR123", "01012341234", "sms")

    assert captured["endpoint"] == API_WAITLIST_ALERT
    assert captured["params"] == {
        "Device": EXPECTED_DEVICE,
        "Version": EXPECTED_VERSION,
        "Key": EXPECTED_KEY,
        "hidPnrNo": "PNR123",
        "telNo": "01012341234",
        "smsSndFlg": "Y",
    }
```

Replace the placeholder values with the real captured contract before implementation.

**Step 2: Add the constant and helper**

```python
API_WAITLIST_ALERT = "/classes/com.korail.mobile.<DISCOVERED_ALERT_ENDPOINT>"

def set_waitlist_alert(self, pnr_no: str, phone: str, channel: str) -> dict[str, object]:
    params = self._build_waitlist_alert_params(pnr_no, phone, channel)
    return self._api_call(API_WAITLIST_ALERT, params)
```

Keep parameter construction in a dedicated helper so the test can assert the exact payload without needing a live Korail session.

**Step 3: Add the fallback branch only if the capture proves it is required**

If the captured request cannot run through the existing browser session:

- Create: `ktxgo/mobile_waitlist_alert.py`
- implement a minimal helper that logs in with saved KTX credentials and sends the exact captured request to the mobile origin
- keep this helper focused only on alert registration, not generic reservation features

**Step 4: Run tests to verify they pass**

Run: `pytest tests/ktxgo/test_waitlist_alert.py::test_set_waitlist_alert_uses_captured_contract -v`

Expected: PASS

**Step 5: Commit**

```bash
git add ktxgo/config.py ktxgo/korail.py tests/ktxgo/test_waitlist_alert.py
git commit -m "feat: add korail waitlist alert api helper"
```

### Task 3: Add CLI Options And Keyring Resolution

**Files:**
- Modify: `ktxgo/cli.py`
- Test: `tests/ktxgo/test_waitlist_alert.py`

**Step 1: Write the failing tests for CLI parsing and keyring fallback**

```python
def test_cli_accepts_waitlist_alert_options(runner):
    result = runner.invoke(
        cli.main,
        [
            "--no-interactive",
            "--max-attempts", "1",
            "--waitlist-alert-phone", "01012341234",
            "--waitlist-alert-channel", "sms",
        ],
    )

    assert result.exit_code == 0
```

```python
def test_resolve_waitlist_alert_settings_uses_keyring_defaults(monkeypatch):
    monkeypatch.setattr(cli.keyring, "get_password", lambda service, key: {
        ("KTX", "waitlist_alert_phone"): "01077776666",
        ("KTX", "waitlist_alert_channel"): "sms",
    }.get((service, key)))

    assert cli._resolve_waitlist_alert_settings(None, None) == (
        "01077776666",
        "sms",
    )
```

**Step 2: Add the options and helper**

```python
@click.option(
    "--waitlist-alert-phone",
    default=None,
    help="Phone number used for Korail waitlist seat-assignment alerts",
)
@click.option(
    "--waitlist-alert-channel",
    type=click.Choice(["sms", "kakao", "both"]),
    default=None,
    help="Korail waitlist alert channel",
)
```

```python
def _resolve_waitlist_alert_settings(
    phone: str | None,
    channel: str | None,
) -> tuple[str, str] | None:
    resolved_phone = (phone or keyring.get_password("KTX", "waitlist_alert_phone") or "").strip()
    resolved_channel = (channel or keyring.get_password("KTX", "waitlist_alert_channel") or "").strip().lower()
    if not resolved_phone or not resolved_channel:
        return None
    return resolved_phone, resolved_channel
```

**Step 3: Run tests to verify they pass**

Run: `pytest tests/ktxgo/test_waitlist_alert.py -k "resolve_waitlist_alert_settings or accepts_waitlist_alert_options" -v`

Expected: PASS

**Step 4: Commit**

```bash
git add ktxgo/cli.py tests/ktxgo/test_waitlist_alert.py
git commit -m "feat: add waitlist alert cli configuration"
```

### Task 4: Wire The Post-Waitlist Follow-Up And Messaging

**Files:**
- Modify: `ktxgo/cli.py`
- Modify: `ktxgo/korail.py`
- Test: `tests/ktxgo/test_waitlist_alert.py`

**Step 1: Write the failing test for the orchestration branch**

```python
def test_waitlist_success_triggers_follow_up_alert_registration(monkeypatch):
    recorded = []

    class DummyAPI:
        ...
        def reserve(self, train, seat_type="general", adults=1, waitlist=False):
            return {"h_pnr_no": "PNR123", "strResult": "SUCC"}

        def set_waitlist_alert(self, pnr_no, phone, channel):
            recorded.append((pnr_no, phone, channel))
            return {"strResult": "SUCC"}

    result = runner.invoke(
        cli.main,
        [
            "--no-interactive",
            "--max-attempts", "1",
            "--waitlist-alert-phone", "01012341234",
            "--waitlist-alert-channel", "sms",
        ],
    )

    assert result.exit_code == 0
    assert recorded == [("PNR123", "01012341234", "sms")]
    assert "좌석배정 알림 등록완료" in result.output
```

**Step 2: Implement the orchestration**

```python
alert_status = None
if waitlist:
    alert_settings = _resolve_waitlist_alert_settings(
        waitlist_alert_phone,
        waitlist_alert_channel,
    )
    if alert_settings is None:
        click.echo(f"[{_now()}] 예약대기 알림 설정이 없어 좌석배정 알림 신청을 건너뜁니다.")
    else:
        phone, channel = alert_settings
        try:
            api.set_waitlist_alert(result["h_pnr_no"], phone, channel)
            alert_status = "registered"
            click.echo(f"[{_now()}] 좌석배정 알림 등록완료 ({channel})")
        except KorailError as exc:
            alert_status = f"failed:{exc}"
            click.echo(f"[{_now()}] 좌석배정 알림 등록 실패: {exc}")
```

Extend `_send_telegram(...)` so waitlist notifications can include the alert-registration outcome.

**Step 3: Run tests to verify they pass**

Run: `pytest tests/ktxgo/test_waitlist_alert.py -v`

Expected: PASS

**Step 4: Commit**

```bash
git add ktxgo/cli.py ktxgo/korail.py tests/ktxgo/test_waitlist_alert.py
git commit -m "feat: register waitlist alerts after booking"
```

### Task 5: Document The Feature And Run Verification

**Files:**
- Modify: `ktxgo/README.md`
- Modify: `README.md`
- Test: `tests/ktxgo/test_waitlist_alert.py`

**Step 1: Update the docs**

Document:

- new CLI options
- keyring keys:
  - `KTX waitlist_alert_phone`
  - `KTX waitlist_alert_channel`
- waitlist success / alert-registration partial-failure behavior
- whether KakaoTalk support is available or whether the captured upstream flow only supports SMS

**Step 2: Run the test suite**

Run: `pytest tests/ktxgo/test_waitlist_alert.py tests/ktxgo/test_train_types.py -v`

Expected: PASS

**Step 3: Run one manual smoke test**

Run a headed session:

```bash
python3 -m ktxgo --no-headless --max-attempts 1
```

Expected:

- login still works
- a waitlist reservation still succeeds
- the follow-up alert registration call either succeeds or prints the exact Korail failure cleanly

**Step 4: Commit**

```bash
git add README.md ktxgo/README.md tests/ktxgo/test_waitlist_alert.py tests/ktxgo/test_train_types.py ktxgo/cli.py ktxgo/korail.py ktxgo/config.py
git commit -m "docs: document waitlist alert registration"
```
