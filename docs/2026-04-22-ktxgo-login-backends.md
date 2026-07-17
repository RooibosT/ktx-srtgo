# KTXGO Login Backend Notes

## Current supported path

The supported KTX reservation path is the Chromium extension backend:

1. Start a normal, non-Playwright-controlled Chromium process with the KTXGO extension.
2. Let the user complete Korail login in the visible window when needed.
3. If a cached/profile login is confirmed, run headlessly. If visible login is required, keep that logged-in visible Chromium session alive and minimize it.
4. Keep using that Chromium profile/session for page-context `XMLHttpRequest` API calls.
5. Run the polling/reservation loop through `ExtensionKorailAPI`.
6. During idle polling attempts, periodically call `loginCheck` as a keepalive so the member login session is refreshed before a reservation opportunity appears; a single unconfirmed keepalive does not immediately force visible login.
7. Save `~/.ktxgo/extension_cookies.json` only as a fast-path hint that a recent extension login happened.

The cookie cache has no app-side fixed TTL. Korail owns the real session lifetime. KTXGO treats the cache as a hint, then confirms member login with `loginCheck` before trusting a headless cached session. Login confirmation is strict: a bare `strResult=SUCC` is not enough unless member identity or an explicit login flag is present. If visible login is required during a headless run, KTXGO keeps that logged-in browser session instead of closing it for a headless handoff, because session cookies may not survive an immediate process switch. The polling loop keeps the member login session warm with periodic login checks when no reservation candidate is being submitted. If repeated keepalive checks fail or a cached/profile session later expires, the reservation loop reopens visible Chromium login.

## Retired or diagnostic-only approaches

These approaches were tried while investigating Korail login/search failures. They are kept here as history so they are not reintroduced as the default reservation flow.

| Approach | Result | Reason it is not the active path |
| --- | --- | --- |
| Mobile/API auto-login with saved credentials | Blocked or unreliable | Korail login and later search/reserve calls can fail with automation/security errors. |
| Playwright Firefox login/search | Blocked | Login could be completed in some variants, but Playwright-controlled API/search calls still produced `MACRO ERROR`. |
| Playwright Chromium or system Chrome through Playwright | Blocked | Korail/DynaPath detected the controlled browser more aggressively, including developer-tools/reject-service style failures. |
| External Firefox login plus cookie import | Insufficient | Imported cookies could pass some login checks, but direct API search still hit `MACRO ERROR`; cookie replay is not enough for DynaPath. |
| Pure login window / stealth / webdriver flag variants | Diagnostic only | These helped isolate fingerprint issues but did not provide a stable end-to-end polling/reservation backend. |
| Direct `requests`/`curl_cffi` cookie replay | Blocked | Replayed cookies and captured DynaPath URLs did not reproduce a valid browser-bound session. |
| Selenium/Marionette Firefox | Blocked | Automation context still produced the same Korail macro/app-update style failure. |
| Extension cookie restore into a fresh profile | Removed | The current backend reuses the persistent Chromium profile. A JSON cookie file is only a hint; restoring cookies into another profile is not enough to guarantee a valid Korail/DynaPath session. |

## Practical implication

Do not route normal KTX reservation polling back through Playwright, external Firefox cookie import, or direct HTTP replay. If the extension profile is not logged in, open visible Chromium for manual login, then prefer a measured handoff back to headless Chromium for reservation polling; use the visible session path only when that handoff is not confirmed quickly.
