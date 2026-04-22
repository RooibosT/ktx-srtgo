# KTXGO Login Backend Notes

## Current supported path

The supported KTX reservation path is the Chromium extension backend:

1. Start a normal, non-Playwright-controlled Chromium process with the KTXGO extension.
2. Let the user complete Korail login in the visible window when needed.
3. After a required visible login in `--headless` mode, close the login window and reopen the same Chromium profile headlessly.
4. Keep using that Chromium profile/session for page-context `XMLHttpRequest` API calls.
5. Run the polling/reservation loop through `ExtensionKorailAPI`.
6. Save `~/.ktxgo/extension_cookies.json` only as a fast-path hint that a recent extension login happened.

The cookie cache has no app-side fixed TTL. Korail owns the real session lifetime. If no cache hint exists, KTXGO probes the saved Chromium profile before asking the user to log in again. If visible login is required during a headless run, KTXGO measures a headless handoff attempt and falls back to the visible session path if the handoff is not confirmed quickly. If a cached/profile session later expires, the reservation loop reopens visible Chromium login.

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
