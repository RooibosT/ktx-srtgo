# KTXgo Waitlist Alert Menu Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add an interactive menu entry that lets the user register or update the phone number used for KTX waitlist seat-assignment SMS alerts.

**Architecture:** Extend the existing top-level interactive menu in `ktxgo.cli` with one more persistent settings flow. Reuse keyring storage under `KTX / waitlist_alert_phone`, normalize the input to digits only, and leave the existing non-interactive waitlist alert follow-up logic unchanged.

**Tech Stack:** Python 3.10+, Click CLI, inquirer, keyring, pytest

---

### Task 1: Add failing tests for the interactive waitlist phone setting

**Files:**
- Modify: `tests/ktxgo/test_waitlist_alert.py`
- Test: `tests/ktxgo/test_waitlist_alert.py`

**Step 1: Write the failing test for the interactive setter**

```python
def test_set_waitlist_alert_phone_interactive_saves_normalized_phone(monkeypatch):
    stored = {}

    monkeypatch.setattr(
        cli,
        "_prompt_guarded",
        lambda questions: {"phone": "010-1234-5678"},
    )
    monkeypatch.setattr(
        cli.keyring,
        "set_password",
        lambda service, key, value: stored.__setitem__((service, key), value),
    )
    monkeypatch.setattr(cli.keyring, "get_password", lambda service, key: None)

    assert cli._set_waitlist_alert_phone_interactive() is True
    assert stored[("KTX", "waitlist_alert_phone")] == "01012345678"
```

**Step 2: Write the failing test for menu dispatch**

```python
def test_interactive_menu_dispatches_waitlist_alert_setting(monkeypatch):
    calls = []

    monkeypatch.setattr(cli, "_prompt_main_menu", lambda: "waitlist-alert")
    monkeypatch.setattr(cli, "_set_waitlist_alert_phone_interactive", lambda: calls.append("called") or True)

    result = CliRunner().invoke(cli.main, ["--interactive", "--max-attempts", "1"])

    assert result.exit_code == 0
    assert calls == ["called"]
```

Patch other interactive dependencies in the same style as the existing CLI tests so the command exits before opening a browser session.

**Step 3: Run tests to verify they fail**

Run: `pytest tests/ktxgo/test_waitlist_alert.py -k "interactive or waitlist_alert_phone" -v`

Expected: FAIL with missing `_set_waitlist_alert_phone_interactive` or missing menu dispatch.

**Step 4: Commit**

```bash
git add tests/ktxgo/test_waitlist_alert.py docs/plans/2026-03-13-ktxgo-waitlist-alert-menu.md
git commit -m "test: add interactive waitlist alert setting coverage"
```

### Task 2: Implement the interactive waitlist phone menu flow

**Files:**
- Modify: `ktxgo/cli.py`
- Test: `tests/ktxgo/test_waitlist_alert.py`

**Step 1: Add a dedicated interactive setter**

```python
def _set_waitlist_alert_phone_interactive() -> bool:
    defaults = {
        "phone": keyring.get_password("KTX", "waitlist_alert_phone") or "",
    }
    waitlist_info = _prompt_guarded(
        [
            inquirer.Text(
                "phone",
                message="예약대기 좌석배정 SMS 알림 번호 (Enter: 완료, Ctrl-C: 취소)",
                default=defaults["phone"],
            )
        ]
    )
```

Normalize to digits, reject empty input, then save with:

```python
keyring.set_password("KTX", "waitlist_alert_phone", digits_only)
```

**Step 2: Add a new top-level menu item**

Add `("예약대기 SMS 알림 번호 등록/수정", "waitlist-alert")` to `_prompt_main_menu()`.

**Step 3: Add the main-loop branch**

Inside the interactive menu loop:

```python
if action == "waitlist-alert":
    _set_waitlist_alert_phone_interactive()
    continue
```

**Step 4: Run the targeted tests to verify they pass**

Run: `pytest tests/ktxgo/test_waitlist_alert.py -k "interactive or waitlist_alert_phone" -v`

Expected: PASS

**Step 5: Commit**

```bash
git add ktxgo/cli.py tests/ktxgo/test_waitlist_alert.py
git commit -m "feat: add interactive waitlist alert phone setting"
```

### Task 3: Verify the existing waitlist alert flow still works

**Files:**
- Modify: `ktxgo/README.md`
- Modify: `README.md`
- Test: `tests/ktxgo/test_waitlist_alert.py`

**Step 1: Update docs if the new interactive setting should be mentioned**

Add one short note that the waitlist alert phone can now be configured from the interactive menu as well as via CLI/keyring.

**Step 2: Run the full related test set**

Run: `pytest tests/ktxgo/test_waitlist_alert.py tests/ktxgo/test_train_types.py -v`

Expected: PASS

**Step 3: Commit**

```bash
git add README.md ktxgo/README.md tests/ktxgo/test_waitlist_alert.py
git commit -m "docs: mention interactive waitlist alert phone setting"
```
