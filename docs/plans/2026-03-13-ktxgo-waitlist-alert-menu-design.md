# KTXgo Waitlist Alert Menu Design

## Goal

Add an interactive menu entry that lets the user register or update the phone number used for waitlist seat-assignment SMS alerts.

## Scope

- Add a new top-level interactive menu item beside login/station/card settings
- Store the phone number in keyring as `KTX / waitlist_alert_phone`
- Reuse the existing waitlist alert flow after reservation success
- Do not add KakaoTalk support or a separate alert channel selector in this change

## Design

The current interactive menu already exposes persistent settings through dedicated flows:

- login settings
- station settings
- card registration

The new SMS alert setting should follow the same pattern:

1. Add `예약대기 SMS 알림 번호 등록/수정` to `_prompt_main_menu()`
2. Implement `_set_waitlist_alert_phone_interactive()` using `inquirer`
3. Pre-fill the prompt with the current keyring value if one exists
4. Normalize the value to digits only and store it in keyring
5. Return to the main menu after save/cancel, like other settings flows

## Validation

- Reject empty or non-numeric values
- Accept either plain digits or hyphenated input and normalize before saving
- Keep existing non-interactive CLI behavior unchanged

## Testing

- verify the interactive setter saves the normalized number
- verify the new menu action dispatches to the setter
- verify existing waitlist alert tests still pass
