from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import cast

from playwright.sync_api import Frame, Locator, Page

from .config import (
    API_LOGIN_CHECK,
    API_MYTICKET,
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
    RSV_WAITING,
    SEARCH_URL,
    TRAIN_GROUP_KTX,
    train_type_codes,
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
    waiting_seat: str
    waiting_code: str
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

    @property
    def has_waiting_list(self) -> bool:
        code = self.waiting_code.strip()
        if code.isdigit():
            code = code.zfill(2)
        if code == RSV_WAITING:
            return True

        name = self.waiting_seat.strip()
        if not name:
            return False
        if "가능" in name and not any(
            token in name for token in ("불가", "없", "마감")
        ):
            return True
        return False

    @property
    def waiting_status(self) -> str:
        name = self.waiting_seat.strip()
        if name:
            return name
        return "가능" if self.has_waiting_list else "불가"

    @classmethod
    def from_schedule(cls, row: dict[str, object]) -> Train:
        normalized = {
            str(key): "" if value is None else str(value) for key, value in row.items()
        }
        return cls(
            train_no=normalized.get("h_trn_no", ""),
            train_type=normalized.get("h_car_tp_nm", "")
            or normalized.get("h_trn_clsf_nm", ""),
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
            waiting_seat=normalized.get("h_wait_rsv_nm", ""),
            waiting_code=normalized.get(
                "h_wait_rsv_flg", normalized.get("h_wait_rsv_cd", "")
            ),
            price=normalized.get("h_rcvd_amt", ""),
            raw=normalized,
        )


class KorailAPI:
    def __init__(self, page: Page):
        self.page: Page = page
        self.last_auto_login_error: str | None = None
        self.last_auto_login_detail: str | None = None

    @staticmethod
    def _format_prefill_login_state(snapshot: dict[str, object]) -> str:
        hidden_fields = snapshot.get("hidden_security_fields", [])
        hidden_count = len(hidden_fields) if isinstance(hidden_fields, list) else 0
        return (
            "member_mode={member_mode} id={id_present} pw={password_present} "
            "login_btn={login_button} pw_readonly={pw_readonly} "
            "pw_disabled={pw_disabled} hidden_fields={hidden_count} nshc={nshc}"
        ).format(
            member_mode=snapshot.get("member_mode_present"),
            id_present=snapshot.get("id_present"),
            password_present=snapshot.get("password_present"),
            login_button=snapshot.get("login_button_present"),
            pw_readonly=snapshot.get("password_read_only"),
            pw_disabled=snapshot.get("password_disabled"),
            hidden_count=hidden_count,
            nshc=snapshot.get("nshc_present"),
        )

    def _collect_prefill_login_state(self) -> dict[str, object]:
        try:
            raw = cast(
                dict[str, object],
                self.page.evaluate(
                    """() => {
                    const memberMode = document.querySelector('button#memberNo');
                    const idInput = document.querySelector('input#id');
                    const pwInput = document.querySelector('input#password');
                    const loginButton = document.querySelector('section.loginWrap div.mem_wrap div.btnWrap > button.btn_bn-depblue');
                    const hiddenSecurityFields = pwInput
                        ? Array.from((pwInput.parentElement || document).querySelectorAll('input[type="hidden"]'))
                            .map((el) => el.id || el.name || '')
                            .filter((name) => /password|passwd|pwd|nfilter|secure/i.test(name))
                        : [];

                    return {
                        memberModePresent: !!memberMode,
                        idPresent: !!idInput,
                        passwordPresent: !!pwInput,
                        passwordReadOnly: !!pwInput && !!pwInput.readOnly,
                        passwordDisabled: !!pwInput && !!pwInput.disabled,
                        loginButtonPresent: !!loginButton,
                        hiddenSecurityFields,
                        nshcPresent: typeof window.nshc !== 'undefined',
                    };
                }"""
                ),
            )
        except Exception:
            raw = {}

        hidden_fields_obj = raw.get("hiddenSecurityFields", [])
        hidden_fields = (
            [str(item) for item in hidden_fields_obj if item]
            if isinstance(hidden_fields_obj, list)
            else []
        )
        return {
            "member_mode_present": bool(raw.get("memberModePresent", False)),
            "id_present": bool(raw.get("idPresent", False)),
            "password_present": bool(raw.get("passwordPresent", False)),
            "password_read_only": bool(raw.get("passwordReadOnly", False)),
            "password_disabled": bool(raw.get("passwordDisabled", False)),
            "login_button_present": bool(raw.get("loginButtonPresent", False)),
            "hidden_security_fields": hidden_fields,
            "nshc_present": bool(raw.get("nshcPresent", False)),
        }

    def _wait_for_prefill_login_ready(
        self,
        *,
        max_checks: int = 4,
        poll_ms: int = 200,
        settle_ms: int = 250,
    ) -> tuple[bool, dict[str, object]]:
        snapshot: dict[str, object] = {}
        checks = max(1, max_checks)
        for attempt in range(checks):
            snapshot = self._collect_prefill_login_state()
            ready = (
                bool(snapshot.get("member_mode_present"))
                and bool(snapshot.get("id_present"))
                and bool(snapshot.get("password_present"))
                and bool(snapshot.get("login_button_present"))
                and not bool(snapshot.get("password_disabled"))
            )
            if ready:
                self.page.wait_for_timeout(max(0, settle_ms))
                return True, snapshot
            if attempt + 1 < checks:
                self.page.wait_for_timeout(max(0, poll_ms))
        return False, snapshot

    @staticmethod
    def _type_prefill_value(
        field: Locator,
        value: str,
        *,
        delay: int,
        timeout: int,
    ) -> None:
        field.first.click(timeout=2_000)
        field.first.press("ControlOrMeta+A", timeout=1_000)
        field.first.press("Backspace", timeout=1_000)
        field.first.type(value, delay=delay, timeout=timeout)

    def _stabilize_prefill_password_state(
        self,
        blur_target: Locator,
        *,
        settle_ms: int = 700,
    ) -> None:
        blur_target.first.click(timeout=2_000)
        self.page.wait_for_timeout(settle_ms)

    def _wait_before_prefilled_submit(self, attempt_idx: int) -> int:
        wait_ms = 1200 + (max(0, attempt_idx) * 600)
        self.page.wait_for_timeout(wait_ms)
        return wait_ms

    @staticmethod
    def _pick_visible_locator(
        scope: Frame | Locator,
        selectors: list[str],
    ) -> Locator | None:
        for selector in selectors:
            try:
                items = scope.locator(selector)
                count = min(items.count(), 8)
            except Exception:
                continue
            for idx in range(count):
                try:
                    candidate = items.nth(idx)
                    if candidate.is_visible():
                        return candidate
                except Exception:
                    continue
        return None

    @staticmethod
    def _click_member_mode(frame: Frame) -> bool:
        selector_candidates = [
            "input[type='radio'][value*='2']",
            "input[type='radio'][id*='member' i]",
            "input[type='radio'][name*='member' i]",
            "input[type='radio'][id*='mb' i]",
            "input[type='radio'][name*='mb' i]",
        ]
        radio = KorailAPI._pick_visible_locator(frame, selector_candidates)
        if radio is not None:
            try:
                radio.click(timeout=800)
                return True
            except Exception:
                pass

        text_candidates = [
            "label:has-text('회원번호')",
            "button:has-text('회원번호')",
            "a:has-text('회원번호')",
            "[role='tab']:has-text('회원번호')",
            "span:has-text('회원번호')",
        ]
        text_target = KorailAPI._pick_visible_locator(frame, text_candidates)
        if text_target is not None:
            try:
                text_target.click(timeout=800)
                return True
            except Exception:
                return False
        return False

    @staticmethod
    def _pick_submit_near_password(frame: Frame, pw_input: Locator) -> Locator | None:
        """Pick the login submit control nearest to the password input."""
        try:
            pw_box = pw_input.bounding_box()
        except Exception:
            return None
        if not pw_box:
            return None

        pw_center_x = pw_box["x"] + (pw_box["width"] / 2.0)
        pw_center_y = pw_box["y"] + (pw_box["height"] / 2.0)

        candidate_selectors = [
            "button[name='btnLogin']",
            "input[name='btnLogin']",
            "button[id='btnLogin']",
            "input[id='btnLogin']",
            "button[name*='btnLogin' i]",
            "input[name*='btnLogin' i]",
            "button[id*='btnLogin' i]",
            "input[id*='btnLogin' i]",
            "button:has-text('로그인')",
            "a:has-text('로그인')",
            "[role='button']:has-text('로그인')",
            "button[type='submit']",
            "input[type='submit']",
            "button[id*='login' i]",
            "a[id*='login' i]",
            "button[name*='login' i]",
            "a[name*='login' i]",
            "[role='button'][id*='login' i]",
            "[role='button'][name*='login' i]",
        ]

        best_below: tuple[float, Locator] | None = None
        best_any: tuple[float, Locator] | None = None

        for selector in candidate_selectors:
            try:
                items = frame.locator(selector)
                count = min(items.count(), 20)
            except Exception:
                continue
            for idx in range(count):
                try:
                    candidate = items.nth(idx)
                    if not candidate.is_visible():
                        continue
                    box = candidate.bounding_box()
                except Exception:
                    continue
                if not box:
                    continue

                center_x = box["x"] + (box["width"] / 2.0)
                center_y = box["y"] + (box["height"] / 2.0)
                dx = abs(center_x - pw_center_x)
                dy = center_y - pw_center_y

                # Prefer controls at or below password input.
                score = abs(dy) + (0.4 * dx)
                if dy >= -6:
                    if best_below is None or score < best_below[0]:
                        best_below = (score, candidate)
                    continue

                fallback_score = score + 1200.0
                if best_any is None or fallback_score < best_any[0]:
                    best_any = (fallback_score, candidate)

        if best_below is not None:
            return best_below[1]
        if best_any is not None:
            return best_any[1]
        return None

    @staticmethod
    def _click_submit_via_dom_near_password(pw_input: Locator) -> str | None:
        """DOM-level submit click near password input for pages with tricky handlers."""
        try:
            clicked = cast(
                str,
                pw_input.evaluate(
                    """(pw) => {
                    const isVisible = (el) => {
                        if (!(el instanceof HTMLElement)) return false;
                        const style = window.getComputedStyle(el);
                        if (style.display === "none" || style.visibility === "hidden") return false;
                        if (el.hasAttribute("disabled")) return false;
                        const rect = el.getBoundingClientRect();
                        return rect.width > 0 && rect.height > 0;
                    };
                    const isLoginLike = (el) => {
                        const id = (el.id || "").toLowerCase();
                        const name = (el.getAttribute("name") || "").toLowerCase();
                        const cls = (el.className || "").toString().toLowerCase();
                        const val = (el.getAttribute("value") || "").toLowerCase();
                        const txt = (el.textContent || "").replace(/\\s+/g, "");
                        return (
                            id.includes("login")
                            || id.includes("btnlogin")
                            || name.includes("login")
                            || name.includes("btnlogin")
                            || cls.includes("login")
                            || val.includes("로그인")
                            || txt.includes("로그인")
                        );
                    };
                    const collect = (root) => {
                        const selectors = [
                            "button[name='btnLogin']",
                            "input[name='btnLogin']",
                            "button[id='btnLogin']",
                            "input[id='btnLogin']",
                            "button[name*='btnLogin' i]",
                            "input[name*='btnLogin' i]",
                            "button[id*='btnLogin' i]",
                            "input[id*='btnLogin' i]",
                            "button[type='submit']",
                            "input[type='submit']",
                            "button",
                            "a",
                            "input[type='button']",
                            "[role='button']",
                        ];
                        const out = [];
                        for (const s of selectors) {
                            for (const el of root.querySelectorAll(s)) {
                                if (!isVisible(el)) continue;
                                if (!isLoginLike(el)) continue;
                                out.push(el);
                            }
                        }
                        return out;
                    };

                    const pwRect = pw.getBoundingClientRect();
                    const cx = pwRect.left + pwRect.width / 2;
                    const cy = pwRect.top + pwRect.height / 2;

                    const form = pw.closest("form");
                    const candidates = [];
                    const seen = new Set();
                    for (const root of [form, document]) {
                        if (!root) continue;
                        for (const el of collect(root)) {
                            if (seen.has(el)) continue;
                            seen.add(el);
                            const r = el.getBoundingClientRect();
                            const ex = r.left + r.width / 2;
                            const ey = r.top + r.height / 2;
                            const dx = Math.abs(ex - cx);
                            const dy = ey - cy;
                            let score = Math.abs(dy) + (0.4 * dx);
                            if (dy < -6) score += 1200;
                            candidates.push({ el, score });
                        }
                        if (candidates.length) break;
                    }
                    if (!candidates.length) return "";
                    candidates.sort((a, b) => a.score - b.score);
                    const target = candidates[0].el;
                    if (!(target instanceof HTMLElement)) return "";
                    target.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true }));
                    target.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, cancelable: true }));
                    target.click();
                    const label = (target.textContent || "").replace(/\\s+/g, " ").trim().slice(0, 50);
                    const id = target.id || "";
                    const name = target.getAttribute("name") || "";
                    return `dom-click target=${target.tagName.toLowerCase()}#${id}[name=${name}] text=${label}`;
                }"""
                ),
            )
        except Exception:
            return None
        return clicked or None

    @staticmethod
    def _invoke_login_function(frame: Frame) -> str | None:
        try:
            invoked = cast(
                str,
                frame.evaluate(
                    """() => {
                    const fnNames = [
                        "fn_login",
                        "fnLogin",
                        "goLogin",
                        "loginProc",
                        "loginProcess",
                        "doLogin",
                    ];
                    for (const name of fnNames) {
                        const fn = window[name];
                        if (typeof fn === "function") {
                            try {
                                fn();
                                return name;
                            } catch (_) {
                                // try next
                            }
                        }
                    }
                    return "";
                }"""
                ),
            )
        except Exception:
            return None
        return invoked or None

    def _wait_login_after_submit(self, timeout_s: float) -> bool:
        deadline = time.monotonic() + max(1.0, timeout_s)
        while time.monotonic() < deadline:
            if self.wait_for_login_stable(
                timeout_s=0.8,
                interval_s=0.3,
                stable_checks=2,
            ):
                _ = self.page.goto(SEARCH_URL, wait_until="networkidle")
                self.last_auto_login_error = None
                self.last_auto_login_detail = None
                return True
            time.sleep(0.6)
        return False

    def prefill_login_form(self, member_id: str, password: str) -> bool:
        """Open login page and prefill credentials without submitting."""
        self.last_auto_login_error = None
        self.last_auto_login_detail = None
        member_id = member_id.strip()
        if not member_id or not password:
            return False

        _ = self.page.goto(LOGIN_URL, wait_until="domcontentloaded")
        try:
            self.page.wait_for_load_state("networkidle", timeout=5_000)
        except Exception:
            pass

        ready, ready_snapshot = self._wait_for_prefill_login_ready()
        if not ready:
            self.last_auto_login_error = "prefill_not_ready"
            self.last_auto_login_detail = (
                "prefill readiness failed: "
                f"{self._format_prefill_login_state(ready_snapshot)}"
            )
            return False

        member_mode = self.page.locator("button#memberNo")
        id_input = self.page.locator("input#id")
        pw_input = self.page.locator("input#password")
        login_btn = self.page.locator(
            "section.loginWrap div.mem_wrap div.btnWrap > button.btn_bn-depblue"
        )
        if (
            member_mode.count() == 0
            or id_input.count() == 0
            or pw_input.count() == 0
            or login_btn.count() == 0
        ):
            return False

        try:
            member_mode.first.click(timeout=2_000)
        except Exception:
            pass

        try:
            self._type_prefill_value(id_input, member_id, delay=70, timeout=4_000)

            self._type_prefill_value(pw_input, password, delay=90, timeout=5_000)
            self._stabilize_prefill_password_state(id_input)
        except Exception:
            self.last_auto_login_error = "prefill_failed"
            self.last_auto_login_detail = (
                "prefill interaction failed after readiness: "
                f"{self._format_prefill_login_state(ready_snapshot)}"
            )
            return False
        self.last_auto_login_detail = (
            "prefill ready / pw_transition=blur_to_id: "
            f"{self._format_prefill_login_state(ready_snapshot)}"
        )
        return True

    def submit_prefilled_login(
        self, timeout_s: int = 25, max_attempts: int = 3
    ) -> bool:
        self.last_auto_login_error = None
        self.last_auto_login_detail = None
        dialog_messages: list[str] = []

        def _on_dialog(dialog: object) -> None:
            try:
                msg = str(getattr(dialog, "message", lambda: "")()).strip()
            except Exception:
                msg = ""
            if msg:
                dialog_messages.append(msg)
            try:
                _ = getattr(dialog, "accept")()
            except Exception:
                pass

        self.page.on("dialog", _on_dialog)
        try:
            attempts = max(1, min(max_attempts, 2))
            for attempt_idx in range(attempts):
                pw_input = self.page.locator("input#password")
                login_btn = self.page.locator(
                    "section.loginWrap div.mem_wrap div.btnWrap > button.btn_bn-depblue"
                )
                if pw_input.count() == 0 or login_btn.count() == 0:
                    self.last_auto_login_error = "submit_not_found"
                    self.last_auto_login_detail = (
                        "password/login button not found in prefill flow"
                    )
                    return False

                try:
                    pw_input.first.click(timeout=2_000)
                    wait_ms = self._wait_before_prefilled_submit(attempt_idx)
                    login_btn.first.click(timeout=2_000)
                    self.last_auto_login_detail = (
                        "submit_method=human_like_pw_click_then_login_click"
                        f" wait_ms={wait_ms}"
                    )
                except Exception:
                    self.last_auto_login_error = "submit_click_failed"
                    self.last_auto_login_detail = (
                        "failed to click password or login button"
                    )
                    return False

                if self._wait_login_after_submit(min(12.0, float(timeout_s))):
                    self.last_auto_login_error = None
                    return True

                latest_dialog = dialog_messages[-1] if dialog_messages else ""
                if "통신 중 에러" in latest_dialog:
                    self.last_auto_login_error = "comm_error_after_submit"
                    self.last_auto_login_detail = (
                        f"attempt={attempt_idx + 1} dialog={latest_dialog}"
                    )
                    try:
                        pw_input.first.click(timeout=1_500)
                        self.page.wait_for_timeout(500)
                    except Exception:
                        pass
                    continue

                self.last_auto_login_error = "login_check_failed"
                if not self.last_auto_login_detail:
                    self.last_auto_login_detail = f"attempt={attempt_idx + 1} submitted but login was not confirmed"

            if not self.last_auto_login_detail and dialog_messages:
                self.last_auto_login_detail = f"dialog: {dialog_messages[-1]}"
            if not self.last_auto_login_detail:
                self.last_auto_login_detail = "prefill submit did not confirm login"
            return False
        finally:
            self.page.remove_listener("dialog", _on_dialog)

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

    def login_manual(
        self, timeout_s: int = 300, *, open_login_page: bool = True
    ) -> bool:
        """Navigate to login page and wait for user to log in manually.

        DynaPath blocks programmatic login API calls, so the user must
        log in through the real web form.  After login, cookies are saved
        by the caller (BrowserManager.save_cookies) for future reuse.
        """
        comm_error_seen = False

        def _on_dialog(dialog: object) -> None:
            nonlocal comm_error_seen
            msg = ""
            try:
                msg = str(getattr(dialog, "message", lambda: "")()).strip()
            except Exception:
                msg = ""
            if "통신 중 에러" in msg:
                comm_error_seen = True
            try:
                _ = getattr(dialog, "accept")()
            except Exception:
                pass

        self.page.on("dialog", _on_dialog)
        try:
            if open_login_page:
                _ = self.page.goto(LOGIN_URL, wait_until="networkidle")
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                if comm_error_seen:
                    comm_error_seen = False
                    try:
                        pw_input = self.page.locator("input#password")
                        if pw_input.count() > 0:
                            pw_input.first.click(timeout=1_500)
                            self.page.wait_for_timeout(350)
                    except Exception:
                        pass

                if self.wait_for_login_stable(
                    timeout_s=0.6,
                    interval_s=0.3,
                    stable_checks=2,
                ):
                    _ = self.page.goto(SEARCH_URL, wait_until="networkidle")
                    return True
                time.sleep(1.0)
            return False
        finally:
            self.page.remove_listener("dialog", _on_dialog)

    def login_auto(self, member_id: str, password: str, timeout_s: int = 30) -> bool:
        """Try automatic login through the real Korail web form."""
        self.last_auto_login_error = None
        self.last_auto_login_detail = None
        dialog_messages: list[str] = []
        macro_error_msg: str | None = None

        def _on_dialog(dialog: object) -> None:
            try:
                msg = str(getattr(dialog, "message", lambda: "")()).strip()
            except Exception:
                msg = ""
            if msg:
                dialog_messages.append(msg)
            try:
                _ = getattr(dialog, "accept")()
            except Exception:
                pass

        def _on_response(response: object) -> None:
            nonlocal macro_error_msg
            if macro_error_msg:
                return
            try:
                url = str(getattr(response, "url"))
            except Exception:
                return
            if "/dynaPath/" not in url:
                return
            try:
                text = str(getattr(response, "text")())
            except Exception:
                return
            if "MACRO ERROR" in text:
                macro_error_msg = "dynaPath blocked automated submit with MACRO ERROR."

        self.page.on("dialog", _on_dialog)
        self.page.on("response", _on_response)
        member_id = member_id.strip()
        if not member_id or not password:
            self.last_auto_login_error = "missing_credentials"
            self.last_auto_login_detail = "KTX id/pass is empty."
            self.page.remove_listener("dialog", _on_dialog)
            self.page.remove_listener("response", _on_response)
            return False

        _ = self.page.goto(LOGIN_URL, wait_until="domcontentloaded")
        try:
            self.page.wait_for_load_state("networkidle", timeout=5_000)
        except Exception:
            pass

        id_selectors = [
            "input[name='txtMemberNo']",
            "input[id='txtMemberNo']",
            "input[name*='txtMemberNo' i]",
            "input[id*='txtMemberNo' i]",
            "input[name='txtId']",
            "input[id='txtId']",
            "input[name*='member' i]",
            "input[id*='member' i]",
            "input[name*='mb' i]",
            "input[id*='mb' i]",
            "input[name*='id' i]",
            "input[id*='id' i]",
            "input[type='text']",
            "input[type='tel']",
        ]
        pw_selectors = [
            "input[name='txtPwd']",
            "input[id='txtPwd']",
            "input[name*='txtPwd' i]",
            "input[id*='txtPwd' i]",
            "input[type='password']",
            "input[name*='pwd' i]",
            "input[id*='pwd' i]",
            "input[name*='pass' i]",
            "input[id*='pass' i]",
        ]
        submit_selectors = [
            "button[name='btnLogin']",
            "input[name='btnLogin']",
            "button[id='btnLogin']",
            "input[id='btnLogin']",
            "button[name*='btnLogin' i]",
            "input[name*='btnLogin' i]",
            "button[id*='btnLogin' i]",
            "input[id*='btnLogin' i]",
            "button[type='submit']",
            "input[type='submit']",
            "button[id*='login' i]",
            "a[id*='login' i]",
            "button[name*='login' i]",
            "a[name*='login' i]",
            "[role='button'][id*='login' i]",
            "[role='button'][name*='login' i]",
            "button:has-text('로그인')",
            "a:has-text('로그인')",
            "[role='button']:has-text('로그인')",
        ]

        for frame in self.page.frames:
            try:
                _ = self._click_member_mode(frame)
            except Exception:
                pass

            id_input = self._pick_visible_locator(frame, id_selectors)
            pw_input = self._pick_visible_locator(frame, pw_selectors)
            if id_input is None or pw_input is None:
                continue

            form_scope: Locator | None = None
            try:
                possible_form = pw_input.locator("xpath=ancestor::form[1]").first
                if possible_form.count() > 0:
                    form_scope = possible_form
            except Exception:
                form_scope = None

            if form_scope is not None:
                scoped_id = self._pick_visible_locator(form_scope, id_selectors)
                if scoped_id is not None:
                    id_input = scoped_id
                scoped_submit = self._pick_visible_locator(form_scope, submit_selectors)
            else:
                scoped_submit = None

            id_values = [member_id]
            digits_only = "".join(ch for ch in member_id if ch.isdigit())
            if digits_only and digits_only != member_id:
                id_values.append(digits_only)

            for id_value in id_values:
                try:
                    id_input.click(timeout=1_500)
                    id_input.fill("", timeout=1_500)
                    id_input.type(id_value, delay=45, timeout=3_500)

                    pw_input.click(timeout=1_500)
                    pw_input.fill("", timeout=1_500)
                    pw_input.type(password, delay=55, timeout=4_500)
                    # Korail login can fail when submit is triggered immediately
                    # after password typing. Re-focus and wait briefly first.
                    pw_input.click(timeout=1_500)
                    time.sleep(0.55)
                except Exception:
                    self.last_auto_login_error = "fill_failed"
                    self.last_auto_login_detail = (
                        "Failed while typing into id/password input fields."
                    )
                    continue

                submit_wait_s = min(7.0, float(timeout_s))
                submit_attempted = False
                near_submit = self._pick_submit_near_password(frame, pw_input)
                if near_submit is not None:
                    try:
                        near_submit.click(timeout=1_800)
                        submit_attempted = True
                        self.last_auto_login_detail = (
                            "submit_method=playwright_near_click"
                        )
                    except Exception:
                        pass
                    else:
                        if self._wait_login_after_submit(submit_wait_s):
                            self.page.remove_listener("dialog", _on_dialog)
                            self.page.remove_listener("response", _on_response)
                            return True

                    try:
                        near_submit.click(timeout=1_800, force=True)
                        submit_attempted = True
                        self.last_auto_login_detail = (
                            "submit_method=playwright_near_click_force"
                        )
                    except Exception:
                        pass
                    else:
                        if self._wait_login_after_submit(submit_wait_s):
                            self.page.remove_listener("dialog", _on_dialog)
                            self.page.remove_listener("response", _on_response)
                            return True

                submit_btn = scoped_submit or self._pick_visible_locator(
                    frame, submit_selectors
                )
                if submit_btn is not None:
                    try:
                        submit_btn.click(timeout=1_800)
                        submit_attempted = True
                        self.last_auto_login_detail = (
                            "submit_method=playwright_submit_click"
                        )
                    except Exception:
                        pass
                    else:
                        if self._wait_login_after_submit(submit_wait_s):
                            self.page.remove_listener("dialog", _on_dialog)
                            self.page.remove_listener("response", _on_response)
                            return True

                    try:
                        submit_btn.click(timeout=1_800, force=True)
                        submit_attempted = True
                        self.last_auto_login_detail = (
                            "submit_method=playwright_submit_click_force"
                        )
                    except Exception:
                        pass
                    else:
                        if self._wait_login_after_submit(submit_wait_s):
                            self.page.remove_listener("dialog", _on_dialog)
                            self.page.remove_listener("response", _on_response)
                            return True

                try:
                    pw_input.press("Enter", timeout=1_800)
                    submit_attempted = True
                    self.last_auto_login_detail = "submit_method=password_enter"
                except Exception:
                    pass
                else:
                    if self._wait_login_after_submit(submit_wait_s):
                        self.page.remove_listener("dialog", _on_dialog)
                        self.page.remove_listener("response", _on_response)
                        return True

                dom_submit = self._click_submit_via_dom_near_password(pw_input)
                if dom_submit:
                    submit_attempted = True
                    self.last_auto_login_detail = dom_submit
                    if self._wait_login_after_submit(submit_wait_s):
                        self.page.remove_listener("dialog", _on_dialog)
                        self.page.remove_listener("response", _on_response)
                        return True

                if form_scope is not None:
                    try:
                        _ = form_scope.evaluate(
                            """(form) => {
                            if (form && typeof form.requestSubmit === "function") {
                                form.requestSubmit();
                                return true;
                            }
                            if (form && typeof form.submit === "function") {
                                form.submit();
                                return true;
                            }
                            return false;
                        }"""
                        )
                        submit_attempted = True
                        self.last_auto_login_detail = (
                            "submit_method=form_request_submit"
                        )
                    except Exception:
                        pass
                    else:
                        if self._wait_login_after_submit(submit_wait_s):
                            self.page.remove_listener("dialog", _on_dialog)
                            self.page.remove_listener("response", _on_response)
                            return True

                invoked = self._invoke_login_function(frame)
                if invoked:
                    submit_attempted = True
                    self.last_auto_login_detail = f"Invoked login function: {invoked}"
                    if self._wait_login_after_submit(submit_wait_s):
                        self.page.remove_listener("dialog", _on_dialog)
                        self.page.remove_listener("response", _on_response)
                        return True

                if not submit_attempted:
                    self.last_auto_login_error = "submit_failed"
                    if not self.last_auto_login_detail:
                        self.last_auto_login_detail = "Login submit control not found/clickable and Enter submit failed."
                    continue

                self.last_auto_login_error = "login_check_failed"
                login_msg = ""
                try:
                    data = self._api_call(API_LOGIN_CHECK, {})
                    login_msg = str(data.get("h_msg_txt", "")).strip()
                except KorailError as exc:
                    login_msg = str(exc).strip()
                if login_msg:
                    detail_prefix = self.last_auto_login_detail or ""
                    if detail_prefix:
                        self.last_auto_login_detail = (
                            f"{detail_prefix} / loginCheck message: {login_msg}"
                        )
                    else:
                        self.last_auto_login_detail = f"loginCheck message: {login_msg}"
                else:
                    if not self.last_auto_login_detail:
                        self.last_auto_login_detail = (
                            "Form submitted but login state was not confirmed."
                        )
                if dialog_messages:
                    self.last_auto_login_detail = (
                        f"{self.last_auto_login_detail} / dialog: {dialog_messages[-1]}"
                    )
                if macro_error_msg:
                    self.last_auto_login_error = "macro_blocked"
                    self.last_auto_login_detail = (
                        f"{self.last_auto_login_detail} / {macro_error_msg}"
                    )
        if self.last_auto_login_error is None:
            self.last_auto_login_error = "login_form_not_found"
            self.last_auto_login_detail = (
                "Could not find visible id/password inputs in any frame."
            )
        self.page.remove_listener("dialog", _on_dialog)
        self.page.remove_listener("response", _on_response)
        return False

    def wait_for_login_stable(
        self,
        *,
        timeout_s: float = 3.0,
        interval_s: float = 0.35,
        stable_checks: int = 2,
    ) -> bool:
        """Check login status with short retries to absorb session propagation delay."""
        required = max(1, stable_checks)
        streak = 0
        deadline = time.monotonic() + max(0.0, timeout_s)

        while True:
            if self.is_logged_in():
                streak += 1
                if streak >= required:
                    return True
            else:
                streak = 0

            if time.monotonic() >= deadline:
                return False
            time.sleep(max(0.05, interval_s))

    @staticmethod
    def _train_sort_key(train: Train) -> tuple[str, str, str, str, str, str]:
        return (
            train.dep_date,
            train.dep_time,
            train.arr_time,
            train.train_no,
            train.departure,
            train.arrival,
        )

    @staticmethod
    def _train_identity(train: Train) -> tuple[str, str, str, str, str]:
        return (
            train.dep_date,
            train.train_no,
            train.dep_time,
            train.departure,
            train.arrival,
        )

    @staticmethod
    def _trains_from_schedule_payload(data: dict[str, object]) -> list[Train]:
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

    @staticmethod
    def _matches_requested_train_types(
        train: Train, requested_train_types: tuple[str, ...] | None
    ) -> bool:
        if not requested_train_types:
            return True

        raw_name = train.train_type.strip()
        code = str(
            train.raw.get("h_trn_gp_cd", "") or train.raw.get("h_trn_clsf_cd", "")
        ).strip()

        for train_type in requested_train_types:
            if train_type == "ktx" and code == "100":
                return True
            if train_type == "itx-saemaeul" and raw_name == "ITX-새마을":
                return True
            if train_type == "mugunghwa" and raw_name.startswith("무궁화"):
                return True
            if train_type == "tonggeun" and "통근" in raw_name:
                return True
            if train_type == "itx-cheongchun" and raw_name == "ITX-청춘":
                return True
            if train_type == "itx-maeum" and raw_name == "ITX-마음":
                return True
            if train_type == "airport" and "공항" in raw_name:
                return True

        return False

    def search(
        self,
        departure: str,
        arrival: str,
        date: str,
        time_str: str,
        adults: int = 1,
        train_types: tuple[str, ...] | None = None,
    ) -> list[Train]:
        hh = time_str.zfill(2)
        seen: set[tuple[str, str, str, str, str]] = set()
        trains: list[Train] = []

        for train_code in train_type_codes(train_types):
            params = {
                "Device": "BH",
                "Version": "999999999",
                "radJobId": "1",
                "selGoTrain": train_code,
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
                "txtTrnGpCd": train_code,
                "searchType": "GENERAL",
            }
            data = self._api_call(API_SCHEDULE, params)
            for train in self._trains_from_schedule_payload(data):
                if not self._matches_requested_train_types(train, train_types):
                    continue
                identity = self._train_identity(train)
                if identity in seen:
                    continue
                seen.add(identity)
                trains.append(train)

        return sorted(trains, key=self._train_sort_key)

    def reserve(
        self,
        train: Train,
        seat_type: str = "general",
        adults: int = 1,
        waitlist: bool = False,
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
            "txtJobId": "1102" if waitlist else "1101",
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

    def login_profile(self) -> dict[str, str] | None:
        """Return current login profile from loginCheck response, or None."""
        try:
            data = self._api_call(API_LOGIN_CHECK, {})
        except KorailError:
            return None

        msg = str(data.get("h_msg_txt", "")).strip()
        if "로그인 정보가 없습니다" in msg or ("로그인" in msg and "없" in msg):
            return None

        member_no = ""
        for key in ("strMbCrdNo", "mbCrdNo", "strCustNo", "custNo"):
            value = str(data.get(key, "")).strip()
            if value and value not in {"N", "FALSE", "0"}:
                member_no = value
                break

        name = ""
        for key in ("strCustNm", "custNm", "h_cust_nm", "strUserNm"):
            value = str(data.get(key, "")).strip()
            if value and value not in {"N", "FALSE", "0"}:
                name = value
                break

        login_id = ""
        for key in ("strCustId", "custId", "userId"):
            value = str(data.get(key, "")).strip()
            if value and value not in {"N", "FALSE", "0"}:
                login_id = value
                break

        if not any((member_no, name, login_id)):
            if data.get("strResult") in {"SUCC", "SUCCESS", "Y"}:
                return {"member_no": "", "name": "", "login_id": ""}
            return None

        return {
            "member_no": member_no,
            "name": name,
            "login_id": login_id,
        }

    def reservations(self) -> list[dict[str, object]]:
        mobile_base = {
            "Device": MOBILE_DEVICE,
            "Version": MOBILE_VERSION,
            "Key": MOBILE_KEY,
        }
        try:
            data = self._api_call(API_RESERVATION_VIEW, mobile_base)
        except KorailError as exc:
            code = (exc.code or "").strip()
            msg = str(exc)
            no_result_codes = {"P100", "WRG000000", "WRD000061", "WRT300005"}
            if code in no_result_codes or ("예약" in msg and "없" in msg):
                return []
            raise

        jrny_infos_obj = data.get("jrny_infos")
        if not isinstance(jrny_infos_obj, dict):
            return []
        jrny_infos = cast(dict[str, object], jrny_infos_obj)

        jrny_info_obj = jrny_infos.get("jrny_info")
        jrny_items: list[dict[str, object]] = []
        if isinstance(jrny_info_obj, dict):
            jrny_items = [cast(dict[str, object], jrny_info_obj)]
        elif isinstance(jrny_info_obj, list):
            jrny_items = [
                cast(dict[str, object], item)
                for item in cast(list[object], jrny_info_obj)
                if isinstance(item, dict)
            ]
        if not jrny_items:
            return []

        reservations: list[dict[str, object]] = []
        inherit_keys = (
            "h_pnr_no",
            "h_rsv_amt",
            "h_ntisu_lmt_dt",
            "h_ntisu_lmt_tm",
            "h_run_dt",
            "h_dpt_dt",
            "h_dpt_tm",
            "h_dpt_rs_stn_nm",
            "h_arv_rs_stn_nm",
            "h_trn_no",
            "h_rsv_chg_no",
            "hidRsvChgNo",
            "h_wct_no",
        )
        for jrny in jrny_items:
            train_infos_obj = jrny.get("train_infos")
            if not isinstance(train_infos_obj, dict):
                reservations.append(jrny)
                continue
            train_info_obj = train_infos_obj.get("train_info")
            train_items: list[dict[str, object]] = []
            if isinstance(train_info_obj, dict):
                train_items = [cast(dict[str, object], train_info_obj)]
            elif isinstance(train_info_obj, list):
                train_items = [
                    cast(dict[str, object], item)
                    for item in cast(list[object], train_info_obj)
                    if isinstance(item, dict)
                ]

            if not train_items:
                reservations.append(jrny)
                continue

            for train in train_items:
                merged = dict(train)
                for key in inherit_keys:
                    if str(merged.get(key, "")).strip():
                        continue
                    if key in jrny:
                        merged[key] = jrny[key]
                reservations.append(cast(dict[str, object], merged))
        return reservations

    def tickets(self) -> list[dict[str, object]]:
        """Return issued ticket list (paid bookings)."""
        no_result_codes = {"P100", "WRG000000", "WRD000061", "WRT300005"}
        param_candidates = [
            {
                "Device": MOBILE_DEVICE,
                "Version": MOBILE_VERSION,
                "Key": MOBILE_KEY,
                "txtDeviceId": "",
                "txtIndex": "1",
                "h_page_no": "1",
                "h_abrd_dt_from": "",
                "h_abrd_dt_to": "",
                "hiduserYn": "Y",
            },
            {
                "Device": "BH",
                "Version": "999999999",
                "txtDeviceId": "",
                "txtIndex": "1",
                "h_page_no": "1",
                "h_abrd_dt_from": "",
                "h_abrd_dt_to": "",
                "hiduserYn": "Y",
            },
        ]

        data: dict[str, object] | None = None
        for params in param_candidates:
            try:
                data = self._api_call(API_MYTICKET, params)
                break
            except KorailError as exc:
                code = (exc.code or "").strip()
                msg = str(exc)
                if code in no_result_codes or ("예약" in msg and "없" in msg):
                    return []
                continue

        if data is None:
            return []

        reservation_list_obj = data.get("reservation_list")
        entries: list[dict[str, object]] = []
        if isinstance(reservation_list_obj, dict):
            entries = [cast(dict[str, object], reservation_list_obj)]
        elif isinstance(reservation_list_obj, list):
            entries = [
                cast(dict[str, object], item)
                for item in cast(list[object], reservation_list_obj)
                if isinstance(item, dict)
            ]
        if not entries:
            return []

        tickets: list[dict[str, object]] = []
        inherit_keys = (
            "h_pnr_no",
            "h_orgtk_sale_dt",
            "h_orgtk_wct_no",
            "h_orgtk_ret_sale_dt",
            "h_orgtk_sale_sqno",
            "h_orgtk_ret_pwd",
            "h_rcvd_amt",
            "h_buy_ps_nm",
        )
        for entry in entries:
            ticket_list_obj = entry.get("ticket_list")
            ticket_items: list[dict[str, object]] = []
            if isinstance(ticket_list_obj, dict):
                ticket_items = [cast(dict[str, object], ticket_list_obj)]
            elif isinstance(ticket_list_obj, list):
                ticket_items = [
                    cast(dict[str, object], item)
                    for item in cast(list[object], ticket_list_obj)
                    if isinstance(item, dict)
                ]

            if not ticket_items:
                tickets.append(entry)
                continue

            for ticket in ticket_items:
                train_info_obj = ticket.get("train_info")
                train_items: list[dict[str, object]] = []
                if isinstance(train_info_obj, dict):
                    train_items = [cast(dict[str, object], train_info_obj)]
                elif isinstance(train_info_obj, list):
                    train_items = [
                        cast(dict[str, object], item)
                        for item in cast(list[object], train_info_obj)
                        if isinstance(item, dict)
                    ]

                if not train_items:
                    merged_ticket = dict(ticket)
                    for key in inherit_keys:
                        if str(merged_ticket.get(key, "")).strip():
                            continue
                        if key in entry:
                            merged_ticket[key] = entry[key]
                    tickets.append(cast(dict[str, object], merged_ticket))
                    continue

                for train in train_items:
                    merged = dict(train)
                    for key in inherit_keys:
                        if str(merged.get(key, "")).strip():
                            continue
                        if key in ticket:
                            merged[key] = ticket[key]
                        elif key in entry:
                            merged[key] = entry[key]
                    tickets.append(cast(dict[str, object], merged))

        return tickets

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

            if not price or price == "0":
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
