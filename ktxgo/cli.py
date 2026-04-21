from __future__ import annotations

import asyncio
import json
import shutil
import signal
import subprocess
import sys
import time
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, cast
from urllib.parse import urlencode

import click
import inquirer
import keyring
from click.core import ParameterSource
from termcolor import colored

from srtgo.keyring_bootstrap import configure_keyring_backend

from .browser import BrowserManager
from .config import (
    BASE_URL,
    COOKIE_PATH,
    DATA_DIR,
    DEFAULT_ARRIVAL,
    DEFAULT_DEPARTURE,
    DEFAULT_TRAIN_TYPES,
    DEFAULT_VISIBLE_STATIONS,
    LOGIN_URL,
    POLL_INTERVAL_S,
    STORAGE_STATE_PATH,
    STATIONS,
    STATION_CODE_BY_NAME,
    TRAIN_TYPE_LABEL_BY_NAME,
    TRAIN_TYPE_OPTION_CHOICES,
    normalize_train_types,
    TRAIN_TYPE_CODE_BY_NAME,
    train_type_codes,
)
from .cookie_import import (
    import_firefox_korail_cookies,
    import_korail_cookies,
)
from .extension_backend import ExtensionBrowserRunner, ExtensionKorailAPI
from .korail import KorailAPI, KorailError, Train

try:
    import termios
except ImportError:  # pragma: no cover - non-POSIX platforms
    termios = None

# Session-expired error codes returned by Korail.
_SESSION_EXPIRED_CODES = {"P058", "WRT300004", "WRD000003"}
PLAYWRIGHT_BROWSER_CHOICES = ("firefox", "chromium", "webkit")
WEBDRIVER_MODE_CHOICES = ("default", "hidden", "false")
_INTERACTIVE_SCOPE_KTX_ONLY = "ktx_only"
_INTERACTIVE_SCOPE_KTX_PLUS_GENERAL = "ktx_plus_general"
_INTERACTIVE_TRAIN_SCOPE_CHOICES = [
    ("KTX만", _INTERACTIVE_SCOPE_KTX_ONLY),
    ("KTX + ITX/무궁화 등", _INTERACTIVE_SCOPE_KTX_PLUS_GENERAL),
]

TrainKey = tuple[str, str, str, str, str]
ReservationPlan = tuple[str, bool]  # (seat_type, waitlist)
_PROMPT_INPUT_GUARD_S = 0.18
_LAST_PROMPT_FINISHED_AT: float | None = None
_INTERACTIVE_DEFAULT_SERVICE = "KTX"
_INTERACTIVE_BOOL_TRUE_VALUES = {"1", "true", "yes", "y", "on"}
_INTERACTIVE_BOOL_FALSE_VALUES = {"0", "false", "no", "n", "off"}
_INTERACTIVE_SEAT_CHOICES = {"general", "special", "any", "standing"}


def _fmt_date() -> str:
    return (datetime.now() + timedelta(minutes=10)).strftime("%Y%m%d")


def _fmt_hour() -> str:
    return (datetime.now() + timedelta(minutes=10)).strftime("%H")


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _print_success_banner(
    title: str,
    *,
    color: str = "red",
    on_color: str = "on_green",
) -> None:
    line = "=" * 50
    click.echo()
    click.echo(colored(line, color, on_color))
    click.echo(colored(title.center(50), color, on_color, attrs=["bold"]))
    click.echo(colored(line, color, on_color))


def _normalize_station(name: str) -> str:
    if name in STATIONS:
        return name
    raise click.BadParameter(f"Unknown station: {name}")


def _validate_date(value: str) -> str:
    if len(value) != 8 or not value.isdigit():
        raise click.BadParameter("date must be YYYYMMDD")
    try:
        datetime.strptime(value, "%Y%m%d")
    except ValueError as exc:
        raise click.BadParameter("date must be YYYYMMDD") from exc
    return value


def _validate_hour(value: str) -> str:
    if not value.isdigit():
        raise click.BadParameter("time must be HH (00-23)")
    hour = int(value)
    if hour < 0 or hour > 23:
        raise click.BadParameter("time must be HH (00-23)")
    return f"{hour:02d}"


def _validate_adults(value: int) -> int:
    if value < 1 or value > 9:
        raise click.BadParameter("adults must be between 1 and 9")
    return value


def _load_interactive_default(key: str) -> str | None:
    return keyring.get_password(_INTERACTIVE_DEFAULT_SERVICE, key)


def _save_interactive_default(key: str, value: object) -> None:
    if key == "train_types":
        serialized = ",".join(
            _normalize_train_types(cast(tuple[str, ...] | list[str] | None, value))
        )
    elif key in {"auto_pay", "smart_ticket"}:
        serialized = "1" if bool(value) else "0"
    else:
        serialized = str(value)
    keyring.set_password(_INTERACTIVE_DEFAULT_SERVICE, key, serialized)


def _sanitize_saved_station(
    value: str | None, stations: list[str], fallback: str
) -> str:
    candidate = str(value or "").strip()
    if candidate in stations:
        return candidate
    return fallback


def _sanitize_saved_date(value: str | None, fallback: str) -> str:
    candidate = str(value or "").strip()
    if not candidate:
        return fallback
    try:
        return _validate_date(candidate)
    except click.BadParameter:
        return fallback


def _sanitize_saved_time(value: str | None, fallback: str) -> str:
    candidate = str(value or "").strip()
    if not candidate:
        return fallback
    try:
        return _validate_hour(candidate)
    except click.BadParameter:
        return fallback


def _sanitize_saved_adults(value: str | None, fallback: int) -> int:
    candidate = str(value or "").strip()
    if not candidate:
        return fallback
    try:
        return _validate_adults(int(candidate))
    except (TypeError, ValueError, click.BadParameter):
        return fallback


def _sanitize_saved_train_types(
    value: str | None, fallback: tuple[str, ...]
) -> tuple[str, ...]:
    candidate = str(value or "").strip()
    if not candidate:
        return fallback
    try:
        return _normalize_train_types(
            tuple(part.strip() for part in candidate.split(",") if part.strip())
        )
    except ValueError:
        return fallback


def _sanitize_saved_seat(value: str | None, fallback: str) -> str:
    candidate = str(value or "").strip().lower()
    if candidate in _INTERACTIVE_SEAT_CHOICES:
        return candidate
    return fallback


def _sanitize_saved_bool(value: str | None, fallback: bool) -> bool:
    candidate = str(value or "").strip().lower()
    if candidate in _INTERACTIVE_BOOL_TRUE_VALUES:
        return True
    if candidate in _INTERACTIVE_BOOL_FALSE_VALUES:
        return False
    return fallback


def _load_saved_interactive_reservation_defaults(
    *,
    stations: list[str],
    departure: str,
    arrival: str,
    date: str,
    time_str: str,
    adults: int,
    train_types: tuple[str, ...],
    seat: str,
    auto_pay: bool,
    smart_ticket: bool,
) -> tuple[str, str, str, str, int, tuple[str, ...], str, bool, bool]:
    saved_departure = _sanitize_saved_station(
        _load_interactive_default("departure"), stations, departure
    )
    saved_arrival = _sanitize_saved_station(
        _load_interactive_default("arrival"), stations, arrival
    )
    if saved_departure == saved_arrival and len(stations) > 1:
        saved_arrival = next(
            (station for station in stations if station != saved_departure), arrival
        )

    return (
        saved_departure,
        saved_arrival,
        _sanitize_saved_date(_load_interactive_default("date"), date),
        _sanitize_saved_time(_load_interactive_default("time"), time_str),
        _sanitize_saved_adults(_load_interactive_default("adults"), adults),
        _sanitize_saved_train_types(
            _load_interactive_default("train_types"), train_types
        ),
        _sanitize_saved_seat(_load_interactive_default("seat"), seat),
        _sanitize_saved_bool(_load_interactive_default("auto_pay"), auto_pay),
        smart_ticket,
    )


def _should_apply_saved_interactive_default(
    ctx: click.Context | None, param_name: str
) -> bool:
    if ctx is None:
        return False
    source = ctx.get_parameter_source(param_name)
    return source is ParameterSource.DEFAULT


def _apply_saved_interactive_reservation_defaults(
    ctx: click.Context | None,
    *,
    stations: list[str],
    departure: str,
    arrival: str,
    date: str,
    time_str: str,
    adults: int,
    train_types: tuple[str, ...],
    seat: str,
    auto_pay: bool,
    smart_ticket: bool,
) -> tuple[str, str, str, str, int, tuple[str, ...], str, bool, bool]:
    saved_defaults = _load_saved_interactive_reservation_defaults(
        stations=stations,
        departure=departure,
        arrival=arrival,
        date=date,
        time_str=time_str,
        adults=adults,
        train_types=train_types,
        seat=seat,
        auto_pay=auto_pay,
        smart_ticket=smart_ticket,
    )
    merged = {
        "departure": departure,
        "arrival": arrival,
        "date": date,
        "time_str": time_str,
        "adults": adults,
        "train_types": train_types,
        "seat": seat,
        "auto_pay": auto_pay,
        "smart_ticket": smart_ticket,
    }
    for key, saved_value in zip(merged.keys(), saved_defaults):
        if _should_apply_saved_interactive_default(ctx, key):
            merged[key] = saved_value

    return (
        cast(str, merged["departure"]),
        cast(str, merged["arrival"]),
        cast(str, merged["date"]),
        cast(str, merged["time_str"]),
        cast(int, merged["adults"]),
        cast(tuple[str, ...], merged["train_types"]),
        cast(str, merged["seat"]),
        cast(bool, merged["auto_pay"]),
        cast(bool, merged["smart_ticket"]),
    )


def _normalize_train_types(
    train_types: tuple[str, ...] | list[str] | None,
) -> tuple[str, ...]:
    return normalize_train_types(train_types)


def _train_types_from_interactive_scope(scope: str) -> tuple[str, ...]:
    if scope == _INTERACTIVE_SCOPE_KTX_ONLY:
        return (DEFAULT_TRAIN_TYPES[0],)
    return _normalize_train_types(("legacy-all",))


def _interactive_train_scope_from_types(
    train_types: tuple[str, ...] | list[str] | None,
) -> str:
    normalized = _normalize_train_types(train_types)
    if normalized == DEFAULT_TRAIN_TYPES:
        return _INTERACTIVE_SCOPE_KTX_ONLY
    return _INTERACTIVE_SCOPE_KTX_PLUS_GENERAL


def _format_train_type(train: Train) -> str:
    raw_name = train.train_type.strip()
    if raw_name == "ITX-마음":
        return raw_name
    if raw_name == "ITX-새마을":
        return raw_name
    if raw_name == "ITX-청춘":
        return raw_name
    if raw_name.startswith("무궁화"):
        return "무궁화"

    code = str(
        train.raw.get("h_trn_gp_cd", "") or train.raw.get("h_trn_clsf_cd", "")
    ).strip()
    for train_type, train_code in TRAIN_TYPE_CODE_BY_NAME.items():
        if train_code == code:
            label = TRAIN_TYPE_LABEL_BY_NAME[train_type]
            return "무궁화" if label == "무궁화/누리로" else label

    return raw_name or "-"


def _train_key(train: Train) -> TrainKey:
    return (
        train.dep_date,
        train.train_no,
        train.dep_time,
        train.departure,
        train.arrival,
    )


def _train_brief(train: Train) -> str:
    return (
        f"{train.train_no} {train.dep_time}-{train.arr_time} "
        f"{train.departure}->{train.arrival}"
    )


def _display_width(text: str) -> int:
    width = 0
    for ch in text:
        width += 2 if unicodedata.east_asian_width(ch) in {"W", "F"} else 1
    return width


def _fit_display(text: str, width: int) -> str:
    if width <= 0:
        return ""
    out: list[str] = []
    current = 0
    for ch in text:
        ch_w = 2 if unicodedata.east_asian_width(ch) in {"W", "F"} else 1
        if current + ch_w > width:
            break
        out.append(ch)
        current += ch_w
    return "".join(out)


def _pad_display(text: str, width: int, *, align: str = "left") -> str:
    trimmed = _fit_display(text, width)
    pad = max(0, width - _display_width(trimmed))
    if align == "right":
        return (" " * pad) + trimmed
    return trimmed + (" " * pad)


def _format_row(columns: list[tuple[str, int, str]]) -> str:
    return " ".join(
        _pad_display(text, width, align=align) for text, width, align in columns
    )


def _flush_tty_input_buffer() -> None:
    if termios is None or not sys.stdin.isatty():
        return
    try:
        termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    except Exception:
        pass


def _prepare_tty_prompt() -> None:
    global _LAST_PROMPT_FINISHED_AT
    if not sys.stdin.isatty():
        return
    if _LAST_PROMPT_FINISHED_AT is not None:
        elapsed = time.monotonic() - _LAST_PROMPT_FINISHED_AT
        if elapsed < _PROMPT_INPUT_GUARD_S:
            time.sleep(_PROMPT_INPUT_GUARD_S - elapsed)
    _flush_tty_input_buffer()


def _finish_tty_prompt() -> None:
    global _LAST_PROMPT_FINISHED_AT
    if sys.stdin.isatty():
        _LAST_PROMPT_FINISHED_AT = time.monotonic()


def _list_input_guarded(
    *, message: str, choices: list[object], **kwargs: object
) -> object:
    _prepare_tty_prompt()
    try:
        return inquirer.list_input(message=message, choices=choices, **kwargs)
    finally:
        _finish_tty_prompt()


def _prompt_guarded(questions: list[object]) -> dict[str, object] | None:
    _prepare_tty_prompt()
    try:
        return inquirer.prompt(questions)
    finally:
        _finish_tty_prompt()


def _prompt_required_value(
    key: str, question: object, *, cancel_message: str
) -> object:
    answer = _prompt_guarded([question])
    if answer is None:
        click.echo(cancel_message)
        sys.exit(0)
    return answer.get(key)


def _train_choice_label(idx: int, train: Train) -> str:
    return _format_row(
        [
            (f"[{idx}]", 4, "right"),
            (train.train_no, 6, "right"),
            (_format_train_type(train), 10, "left"),
            (f"{train.dep_time}-{train.arr_time}", 12, "left"),
            (f"{train.departure}->{train.arrival}", 15, "left"),
            (f"일반:{train.general_seat}", 14, "left"),
            (f"특석:{train.special_seat}", 14, "left"),
            (f"입석:{train.standing_seat}", 14, "left"),
            (f"예약대기:{train.waiting_status}", 14, "left"),
        ]
    )


def _prompt_main_menu() -> str:
    choice = _list_input_guarded(
        message="메뉴 선택 (↕:이동, Enter: 선택, Ctrl-C: 취소)",
        choices=[
            ("예매 시작", "reserve"),
            ("예매 정보 확인", "reservation"),
            ("로그인 설정", "login"),
            ("역 설정", "station"),
            ("예약대기 SMS 알림 번호 등록/수정", "waitlist-alert"),
            ("카드 등록/수정", "card"),
            ("나가기", "exit"),
        ],
    )
    if choice is None:
        return "exit"
    return str(choice)


def _format_login_profile(profile: dict[str, str]) -> str:
    name = profile.get("name", "").strip() or "이름 미확인"
    member_no = profile.get("member_no", "").strip() or "회원번호 미확인"
    login_id = profile.get("login_id", "").strip()
    if login_id:
        return f"{name} / 회원번호:{member_no} / ID:{login_id}"
    return f"{name} / 회원번호:{member_no}"


def _cached_login_profile() -> dict[str, str] | None:
    if not COOKIE_PATH.is_file():
        return None
    try:
        with BrowserManager(headless=True) as manager:
            api = KorailAPI(manager.page)
            return api.login_profile()
    except Exception:
        return None


def _mask_login_id(login_id: str) -> str:
    value = login_id.strip()
    if not value:
        return "(없음)"
    if len(value) <= 4:
        return "*" * len(value)
    return ("*" * (len(value) - 4)) + value[-4:]


def _load_login_credentials() -> tuple[str, str] | None:
    login_id = (keyring.get_password("KTX", "id") or "").strip()
    login_pass = (keyring.get_password("KTX", "pass") or "").strip()
    if not login_id or not login_pass:
        return None
    return login_id, login_pass


def _set_login_credentials_interactive() -> bool:
    defaults = {
        "id": keyring.get_password("KTX", "id") or "",
        "pass": keyring.get_password("KTX", "pass") or "",
    }
    login_info = _prompt_guarded(
        [
            inquirer.Text(
                "id",
                message="KTX 회원번호 (Enter: 완료, Ctrl-C: 취소)",
                default=defaults["id"],
            ),
            inquirer.Password(
                "pass",
                message="KTX 비밀번호 (Enter: 완료, Ctrl-C: 취소)",
                default=defaults["pass"],
            ),
        ]
    )
    if not login_info:
        click.echo("자동로그인 계정 설정이 취소되었습니다.")
        return False

    login_id = str(login_info.get("id", "")).strip()
    login_pass = str(login_info.get("pass", "")).strip()
    if not login_id or not login_pass:
        click.echo("입력 오류: 회원번호와 비밀번호를 모두 입력하세요.")
        return False

    keyring.set_password("KTX", "id", login_id)
    keyring.set_password("KTX", "pass", login_pass)
    click.echo(
        f"자동로그인 계정이 저장되었습니다. (회원번호: {_mask_login_id(login_id)})"
    )
    return True


def _login_and_save_session(force_relogin: bool = False) -> bool:
    backup_cookie: str | None = None
    backup_storage_state: str | None = None
    if force_relogin:
        if COOKIE_PATH.is_file():
            try:
                backup_cookie = COOKIE_PATH.read_text()
            except OSError:
                backup_cookie = None
            try:
                COOKIE_PATH.unlink()
            except OSError:
                pass
        if STORAGE_STATE_PATH.is_file():
            try:
                backup_storage_state = STORAGE_STATE_PATH.read_text()
            except OSError:
                backup_storage_state = None
            try:
                STORAGE_STATE_PATH.unlink()
            except OSError:
                pass

    manager = BrowserManager(headless=False)
    try:
        with manager:
            api = KorailAPI(manager.page)
            click.echo(f"[{_now()}] 브라우저에서 로그인하세요. (5분 제한)")
            if not api.login_manual(timeout_s=300):
                click.echo("로그인 시간이 초과되었습니다.")
                if (
                    force_relogin
                    and backup_cookie is not None
                    and not COOKIE_PATH.is_file()
                ):
                    COOKIE_PATH.parent.mkdir(parents=True, exist_ok=True)
                    COOKIE_PATH.write_text(backup_cookie)
                if (
                    force_relogin
                    and backup_storage_state is not None
                    and not STORAGE_STATE_PATH.is_file()
                ):
                    STORAGE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
                    STORAGE_STATE_PATH.write_text(backup_storage_state)
                return False

            manager.save_cookies()
            profile = api.login_profile()
            if profile is None:
                click.echo(f"[{_now()}] 로그인 성공 — 세션 저장 완료.")
            else:
                click.echo(
                    f"[{_now()}] 로그인 성공 — 세션 저장 완료. "
                    f"({_format_login_profile(profile)})"
                )
            return True
    except Exception as exc:
        if force_relogin and backup_cookie is not None and not COOKIE_PATH.is_file():
            try:
                COOKIE_PATH.parent.mkdir(parents=True, exist_ok=True)
                COOKIE_PATH.write_text(backup_cookie)
            except OSError:
                pass
        if (
            force_relogin
            and backup_storage_state is not None
            and not STORAGE_STATE_PATH.is_file()
        ):
            try:
                STORAGE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
                STORAGE_STATE_PATH.write_text(backup_storage_state)
            except OSError:
                pass
        click.echo(f"[{_now()}] 로그인 설정 실패: {exc}")
        return False


def _configure_login_interactive() -> None:
    click.echo("\n로그인 설정")
    creds = _load_login_credentials()
    if creds is None:
        click.echo("자동로그인 계정: 미설정")
    else:
        click.echo(f"자동로그인 계정: {_mask_login_id(creds[0])}")

    if not COOKIE_PATH.is_file():
        click.echo("저장된 로그인 세션이 없습니다.")
        choice = _list_input_guarded(
            message="로그인 설정",
            choices=[
                ("자동로그인 계정 등록/수정", "credentials"),
                ("지금 로그인 창 열기 (수동 로그인)", "login"),
                ("취소", "cancel"),
            ],
        )
        if choice == "credentials":
            _set_login_credentials_interactive()
        elif choice == "login":
            _login_and_save_session(force_relogin=False)
        else:
            click.echo("로그인 설정을 취소했습니다.")
        return

    profile = _cached_login_profile()
    if profile is None:
        click.echo("저장된 세션이 만료되었거나 유효하지 않습니다.")
        choice = _list_input_guarded(
            message="로그인 정보 처리",
            choices=[
                ("로그인 정보 변경 (다시 로그인)", "change"),
                ("자동로그인 계정 등록/수정", "credentials"),
                ("취소", "cancel"),
            ],
        )
        if choice == "change":
            _login_and_save_session(force_relogin=True)
        elif choice == "credentials":
            _set_login_credentials_interactive()
        else:
            click.echo("로그인 설정을 취소했습니다.")
        return

    click.echo(f"현재 로그인 정보: {_format_login_profile(profile)}")
    choice = _list_input_guarded(
        message="로그인 정보 처리",
        choices=[
            ("현재 로그인 정보 유지", "keep"),
            ("로그인 정보 변경 (다시 로그인)", "change"),
            ("자동로그인 계정 등록/수정", "credentials"),
            ("취소", "cancel"),
        ],
    )
    if choice == "change":
        _login_and_save_session(force_relogin=True)
    elif choice == "credentials":
        _set_login_credentials_interactive()
    elif choice == "keep":
        click.echo("현재 로그인 정보를 유지합니다.")
    else:
        click.echo("로그인 설정을 취소했습니다.")


def _load_visible_stations() -> list[str]:
    station_key = keyring.get_password("KTX", "station")
    if not station_key:
        return [station for station in STATIONS if station in DEFAULT_VISIBLE_STATIONS]

    selected = {
        station.strip() for station in station_key.split(",") if station.strip()
    }
    ordered = [station for station in STATIONS if station in selected]
    return ordered if ordered else list(STATIONS)


def _set_visible_stations_interactive() -> bool:
    defaults = _load_visible_stations()
    station_info = _prompt_guarded(
        [
            inquirer.Checkbox(
                "stations",
                message=(
                    "표시할 역 선택 "
                    "(↕:이동, Space:선택, Enter:완료, Ctrl-A:전체선택, Ctrl-R:선택해제, Ctrl-C:취소)"
                ),
                choices=STATIONS,
                default=defaults,
            )
        ]
    )
    if not station_info:
        click.echo("역 설정이 취소되었습니다.")
        return False

    selected_obj = station_info.get("stations", [])
    selected: list[str] = (
        [str(station) for station in selected_obj]
        if isinstance(selected_obj, list)
        else []
    )
    if not selected:
        click.echo("선택된 역이 없습니다.")
        return False

    selected_set = set(selected)
    ordered_selected = [station for station in STATIONS if station in selected_set]
    selected_stations = ",".join(ordered_selected)
    keyring.set_password("KTX", "station", selected_stations)
    click.echo(f"선택된 역: {selected_stations}")
    return True


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
    if not waitlist_info:
        click.echo("예약대기 SMS 알림 번호 설정이 취소되었습니다.")
        return False

    raw_phone = str(waitlist_info.get("phone", "")).strip()
    digits_only = "".join(ch for ch in raw_phone if ch.isdigit())
    if not digits_only:
        click.echo("입력 오류: 전화번호를 숫자로 입력하세요.")
        return False

    keyring.set_password("KTX", "waitlist_alert_phone", digits_only)
    click.echo(f"예약대기 SMS 알림 번호가 저장되었습니다. ({digits_only})")
    return True


def _prompt_conditions(
    departure: str,
    arrival: str,
    date: str,
    time_str: str,
    adults: int,
    stations: list[str],
    train_types: tuple[str, ...],
) -> tuple[str, str, str, str, int, tuple[str, ...]]:
    click.echo("\n대화형 모드: 화살표(↑/↓)로 조회 조건을 선택하세요.")
    if len(stations) < 2:
        click.echo("역 설정에서 최소 2개 역을 선택하세요.")
        sys.exit(1)

    if departure not in stations:
        departure = stations[0]
    if arrival not in stations:
        arrival = stations[1] if len(stations) > 1 else stations[0]
    if departure == arrival and len(stations) > 1:
        arrival = next(
            (station for station in stations if station != departure), stations[0]
        )

    now = datetime.now() + timedelta(minutes=10)
    max_days = 31 if now.hour >= 7 else 30

    date_choices: list[tuple[str, str]] = [
        (
            (now + timedelta(days=i)).strftime("%Y/%m/%d %a"),
            (now + timedelta(days=i)).strftime("%Y%m%d"),
        )
        for i in range(max_days + 1)
    ]
    date_values = {value for _, value in date_choices}
    if date not in date_values:
        date_dt = datetime.strptime(date, "%Y%m%d")
        date_choices.insert(0, (date_dt.strftime("%Y/%m/%d %a (직접지정)"), date))

    time_choices = [(f"{hour:02d}시", f"{hour:02d}") for hour in range(24)]
    adult_choices = [(f"{count}명", count) for count in range(1, 10)]

    departure = _normalize_station(
        str(
            _prompt_required_value(
                "departure",
                inquirer.List(
                    "departure",
                    message="출발역 선택 (↕:이동, Enter: 선택, Ctrl-C: 취소)",
                    choices=stations,
                    default=departure,
                ),
                cancel_message="예매 정보 입력 중 취소되었습니다.",
            )
        )
    )
    _save_interactive_default("departure", departure)

    while True:
        arrival_default = arrival
        if arrival_default == departure and len(stations) > 1:
            arrival_default = next(
                (station for station in stations if station != departure), stations[0]
            )
        arrival = _normalize_station(
            str(
                _prompt_required_value(
                    "arrival",
                    inquirer.List(
                        "arrival",
                        message="도착역 선택 (↕:이동, Enter: 선택, Ctrl-C: 취소)",
                        choices=stations,
                        default=arrival_default,
                    ),
                    cancel_message="예매 정보 입력 중 취소되었습니다.",
                )
            )
        )
        if departure == arrival:
            click.echo("입력 오류: 출발역과 도착역은 달라야 합니다.")
            continue
        _save_interactive_default("arrival", arrival)
        break

    date = _validate_date(
        str(
            _prompt_required_value(
                "date",
                inquirer.List(
                    "date",
                    message="출발일 선택 (↕:이동, Enter: 선택, Ctrl-C: 취소)",
                    choices=date_choices,
                    default=date,
                ),
                cancel_message="예매 정보 입력 중 취소되었습니다.",
            )
        )
    )
    _save_interactive_default("date", date)

    time_str = _validate_hour(
        str(
            _prompt_required_value(
                "time",
                inquirer.List(
                    "time",
                    message="출발 시각 선택 (↕:이동, Enter: 선택, Ctrl-C: 취소)",
                    choices=time_choices,
                    default=time_str,
                ),
                cancel_message="예매 정보 입력 중 취소되었습니다.",
            )
        )
    )
    _save_interactive_default("time", time_str)

    adults = _validate_adults(
        int(
            str(
                _prompt_required_value(
                    "adults",
                    inquirer.List(
                        "adults",
                        message="인원수 선택 (성인, ↕:이동, Enter: 선택, Ctrl-C: 취소)",
                        choices=adult_choices,
                        default=adults,
                    ),
                    cancel_message="예매 정보 입력 중 취소되었습니다.",
                )
            )
        )
    )
    _save_interactive_default("adults", adults)

    selected_train_types = _train_types_from_interactive_scope(
        str(
            _prompt_required_value(
                "train_scope",
                inquirer.List(
                    "train_scope",
                    message="조회 열차 범위 선택 (↕:이동, Enter: 선택, Ctrl-C: 취소)",
                    choices=_INTERACTIVE_TRAIN_SCOPE_CHOICES,
                    default=_interactive_train_scope_from_types(train_types),
                ),
                cancel_message="예매 정보 입력 중 취소되었습니다.",
            )
        )
    )
    _save_interactive_default("train_types", selected_train_types)
    return departure, arrival, date, time_str, adults, selected_train_types


def _prompt_target_trains(
    api: KorailAPI,
    departure: str,
    arrival: str,
    date: str,
    time_str: str,
    adults: int,
    train_types: tuple[str, ...],
) -> list[TrainKey]:
    click.echo("\n예약 시도할 열차를 선택하세요.")
    while True:
        trains = api.search(
            departure,
            arrival,
            date,
            time_str,
            adults=adults,
            train_types=train_types,
        )
        if not trains:
            click.echo(f"[{_now()}] 초기 조회 결과가 없습니다.")
            if not click.confirm("같은 조건으로 다시 조회할까요?", default=True):
                sys.exit(0)
            continue

        choice = _prompt_guarded(
            [
                inquirer.Checkbox(
                    "trains",
                    message=(
                        "예약 대상 열차 선택 "
                        "(↕:이동, Space:선택, Enter:완료, Ctrl-A:전체선택, Ctrl-R:선택해제, Ctrl-C:취소)"
                    ),
                    choices=[
                        (_train_choice_label(idx, train), idx)
                        for idx, train in enumerate(trains)
                    ],
                    default=None,
                )
            ]
        )
        if choice is None:
            click.echo("열차 선택이 취소되었습니다.")
            sys.exit(0)

        selected_indices_obj = choice.get("trains", [])
        selected_indices: list[int] = (
            [int(str(idx)) for idx in cast(list[object], selected_indices_obj)]
            if isinstance(selected_indices_obj, list)
            else []
        )
        if not selected_indices:
            click.echo("선택한 열차가 없습니다.")
            if not click.confirm("다시 선택할까요?", default=True):
                sys.exit(0)
            continue

        selected = [trains[idx] for idx in selected_indices]
        click.echo(
            "선택한 열차: " + ", ".join(_train_brief(train) for train in selected)
        )
        return [_train_key(train) for train in selected]


def _prompt_reservation_options(
    default_seat: str, default_auto_pay: bool, default_smart_ticket: bool
) -> tuple[str, bool, bool]:
    seat_choices = [
        ("일반석", "general"),
        ("특석", "special"),
        ("모두 (일반석/특석)", "any"),
        ("입석/자유석", "standing"),
    ]

    seat = _sanitize_saved_seat(
        str(
            _prompt_required_value(
                "seat",
                inquirer.List(
                    "seat",
                    message="좌석 선호 선택 (↕:이동, Enter: 선택, Ctrl-C: 취소)",
                    choices=seat_choices,
                    default=default_seat,
                ),
                cancel_message="예매 옵션 입력 중 취소되었습니다.",
            )
        ),
        default_seat,
    )
    _save_interactive_default("seat", seat)

    auto_pay = bool(
        _prompt_required_value(
            "auto_pay",
            inquirer.Confirm(
                "auto_pay",
                message="예매 성공 시 카드 자동결제",
                default=default_auto_pay,
            ),
            cancel_message="예매 옵션 입력 중 취소되었습니다.",
        )
    )
    _save_interactive_default("auto_pay", auto_pay)

    # Keep smart-ticket behavior as a default/CLI setting without asking in TTY.
    return seat, auto_pay, default_smart_ticket


def _resolve_targets(
    trains: list[Train], targets: list[TrainKey]
) -> tuple[list[Train], int]:
    train_by_key = {_train_key(train): train for train in trains}
    found: list[Train] = []
    missing = 0
    for key in targets:
        train = train_by_key.get(key)
        if train is None:
            missing += 1
            continue
        found.append(train)
    return found, missing


def _target_summary(targets: list[TrainKey] | None) -> str | None:
    if not targets:
        return None
    return "대상 열차: " + ", ".join(
        f"{train_no}({dep_time})" for _, train_no, dep_time, _, _ in targets
    )


def _render_screen(
    status_line: str, target_line: str | None, clear_screen: bool
) -> None:
    if clear_screen:
        click.clear()
    click.echo(status_line)
    if target_line:
        click.echo(target_line)


def _first_non_empty(row: dict[str, object], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = str(row.get(key, "")).strip()
        if value:
            return value
    return ""


def _digits_only(value: object) -> str:
    return "".join(ch for ch in str(value) if ch.isdigit())


def _fmt_yyyymmdd(value: object) -> str:
    digits = _digits_only(value)
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return str(value).strip()


def _fmt_hhmm(value: object) -> str:
    digits = _digits_only(value)
    if len(digits) >= 4:
        return f"{digits[:2]}:{digits[2:4]}"
    return str(value).strip()


def _fmt_datetime(date_value: object, time_value: object) -> str:
    date_part = _fmt_yyyymmdd(date_value)
    time_part = _fmt_hhmm(time_value)
    if date_part and time_part:
        return f"{date_part} {time_part}"
    if date_part:
        return date_part
    if time_part:
        return time_part
    return "-"


def _fmt_amount(value: object) -> str:
    digits = _digits_only(value)
    if not digits:
        return "-"
    return f"{int(digits):,}"


def _print_reservations(
    reservations: list[dict[str, object]],
    *,
    record_kind: str = "reservation",
) -> None:
    header = _format_row(
        [
            ("idx", 3, "right"),
            ("pnr", 15, "right"),
            ("train", 6, "right"),
            ("route", 15, "left"),
            ("depart", 16, "left"),
            ("limit", 16, "left"),
            ("amount", 10, "right"),
        ]
    )
    click.echo(header)
    click.echo("-" * len(header))

    for idx, row in enumerate(reservations):
        pnr = _first_non_empty(row, ("h_pnr_no", "pnrNo"))
        train_no = _first_non_empty(row, ("h_trn_no", "trnNo"))
        dep_stn = _first_non_empty(row, ("h_dpt_rs_stn_nm", "dptRsStnNm"))
        arv_stn = _first_non_empty(row, ("h_arv_rs_stn_nm", "arvRsStnNm"))
        route = f"{dep_stn}->{arv_stn}" if dep_stn or arv_stn else "-"

        dep_date = _first_non_empty(row, ("h_run_dt", "h_dpt_dt", "dptDt"))
        dep_time = _first_non_empty(row, ("h_dpt_tm", "h_dpt_tm_qb", "dptTm"))
        depart = _fmt_datetime(dep_date, dep_time)

        if record_kind == "ticket":
            limit = "발권완료"
        else:
            limit_date = _first_non_empty(row, ("h_ntisu_lmt_dt", "ntisuLmtDt"))
            limit_time = _first_non_empty(row, ("h_ntisu_lmt_tm", "ntisuLmtTm"))
            is_waiting = limit_date in {"", "00000000"} or limit_time in {"", "235959"}
            limit = "예약대기" if is_waiting else _fmt_datetime(limit_date, limit_time)

        amount = _fmt_amount(
            _first_non_empty(row, ("h_rsv_amt", "h_rcvd_amt", "rsvAmt"))
        )

        line = _format_row(
            [
                (str(idx), 3, "right"),
                (pnr or "-", 15, "right"),
                (train_no or "-", 6, "right"),
                (route, 15, "left"),
                (depart, 16, "left"),
                (limit, 16, "left"),
                (amount, 10, "right"),
            ]
        )
        click.echo(line)


def _show_reservations_interactive() -> None:
    click.echo("\n예매 정보 조회")
    with BrowserManager(headless=True) as manager:
        api = KorailAPI(manager.page)
        try:
            api = _ensure_login(api, manager, headless=True)
        except SystemExit:
            return

        profile = api.login_profile()
        if profile:
            click.echo(f"조회 계정: {_format_login_profile(profile)}")

        try:
            reservations = api.reservations()
        except KorailError as exc:
            click.echo(f"[{_now()}] 예매 정보 조회 실패: {exc}")
            return

        try:
            tickets = api.tickets()
        except KorailError:
            tickets = []

    if not reservations and not tickets:
        click.echo("현재 예약/발권 내역이 없습니다.")
        return

    if reservations:
        click.echo(f"\n예약 내역 {len(reservations)}건")
        _print_reservations(reservations, record_kind="reservation")
    else:
        click.echo("\n예약 내역 0건")

    if tickets:
        click.echo(f"\n발권 내역 {len(tickets)}건")
        _print_reservations(tickets, record_kind="ticket")
    else:
        click.echo("\n발권 내역 0건")


def _reservation_plan(train: Train, seat: str) -> ReservationPlan | None:
    if seat == "standing":
        if train.has_standing:
            return "general", False
        return None

    if seat == "general":
        if train.has_general:
            return "general", False
        if train.has_waiting_list:
            return "general", True
        return None

    if seat == "special":
        if train.has_special:
            return "special", False
        if train.has_waiting_list:
            return "special", True
        return None

    if train.has_general:
        return "general", False
    if train.has_special:
        return "special", False
    if train.has_waiting_list:
        return "general", True
    return None


def _print_results(trains: list[Train]) -> None:
    header = _format_row(
        [
            ("idx", 3, "right"),
            ("train", 6, "right"),
            ("type", 10, "left"),
            ("dep->arr", 15, "left"),
            ("time", 12, "left"),
            ("gen", 9, "left"),
            ("spe", 9, "left"),
            ("stnd", 9, "left"),
            ("예약대기", 9, "left"),
            ("price", 7, "right"),
        ]
    )
    click.echo(header)
    click.echo("-" * len(header))
    for idx, train in enumerate(trains):
        route = f"{train.departure}->{train.arrival}"
        tm = f"{train.dep_time}-{train.arr_time}"
        price = train.price.lstrip("0") or "0"
        row = _format_row(
            [
                (str(idx), 3, "right"),
                (train.train_no, 6, "right"),
                (_format_train_type(train), 10, "left"),
                (route, 15, "left"),
                (tm, 12, "left"),
                (train.general_seat, 9, "left"),
                (train.special_seat, 9, "left"),
                (train.standing_seat, 9, "left"),
                (train.waiting_status, 9, "left"),
                (price, 7, "right"),
            ]
        )
        click.echo(row)


def _ensure_login(
    api: KorailAPI,
    manager: BrowserManager,
    headless: bool,
    *,
    manual_login_only: bool = False,
    force_relogin: bool = False,
    use_external_firefox_login: bool = True,
    external_firefox: str = "firefox",
    external_firefox_profile: Path | None = None,
) -> KorailAPI:
    """Ensure the session is authenticated. Returns (possibly new) KorailAPI instance."""
    if (
        not force_relogin
        and api.wait_for_login_stable(timeout_s=0.8, interval_s=0.25, stable_checks=1)
    ):
        click.echo(f"[{_now()}] Logged in via saved session.")
        return api
    if force_relogin:
        click.echo(f"[{_now()}] Force relogin requested. Skipping saved session reuse.")

    def _restart_browser(*, headed: bool) -> KorailAPI:
        manager.close()
        manager._headless = not headed
        manager.start()
        return KorailAPI(manager.page)

    def _reload_headless_after_login() -> KorailAPI:
        if not headless:
            return KorailAPI(manager.page)
        manager._use_saved_session = True
        api_local = _restart_browser(headed=False)
        if not api_local.wait_for_login_stable(
            timeout_s=3.0,
            interval_s=0.35,
            stable_checks=2,
        ):
            click.echo("Saved session not ready. Try --no-headless.")
            sys.exit(1)
        return api_local

    def _reload_saved_session_after_external_login() -> KorailAPI:
        manager._use_saved_session = True
        api_local = _restart_browser(headed=not headless)
        if not api_local.wait_for_login_stable(
            timeout_s=3.0,
            interval_s=0.35,
            stable_checks=2,
        ):
            click.echo("Imported Firefox session is not logged in.")
            sys.exit(1)
        return api_local

    if manual_login_only:
        click.echo(
            f"[{_now()}] Manual-login-only mode enabled. "
            "No saved credentials will be auto-filled."
        )
        if manager._headless:
            click.echo(f"[{_now()}] Restarting browser for manual login...")
            api = _restart_browser(headed=True)

        click.echo(
            f"[{_now()}] Please enter credentials manually in the browser window "
            "(5 min timeout)."
        )
        if not api.login_manual(
            timeout_s=300,
            touch_password_on_comm_error=False,
        ):
            click.echo("Login timed out.")
            sys.exit(1)

        manager.save_cookies()
        click.echo(f"[{_now()}] Login successful — session saved.")

        if headless:
            return _reload_headless_after_login()
        return api

    if use_external_firefox_login:
        click.echo(
            f"[{_now()}] Saved session is invalid. "
            "Using external Firefox login..."
        )
        _run_external_firefox_login(
            firefox_executable=external_firefox,
            profile_dir=external_firefox_profile or _default_external_firefox_profile_dir(),
        )
        return _reload_saved_session_after_external_login()

    creds = _load_login_credentials()
    if creds is not None:
        login_id, login_pass = creds
        masked_id = _mask_login_id(login_id)
        if manager._headless:
            click.echo(
                f"[{_now()}] Saved session is invalid. "
                f"Using visible assisted auto-login ({masked_id})..."
            )
            api = _restart_browser(headed=True)
        else:
            click.echo(
                f"[{_now()}] Saved session is invalid. Preparing assisted login ({masked_id})..."
            )

        prefilled = api.prefill_login_form(login_id, login_pass)
        if prefilled:
            click.echo(f"[{_now()}] 로그인 정보 자동입력 완료 ({masked_id}).")
            click.echo(
                colored(
                    "[로그인 필요] 자동으로 접속된 브라우저에서 로그인 버튼을 직접 눌러주세요",
                    "white",
                    "on_red",
                    attrs=["bold"],
                )
            )
            if not api.login_manual(timeout_s=300, open_login_page=False):
                click.echo("Login timed out.")
                sys.exit(1)
            manager.save_cookies()
            click.echo(f"[{_now()}] Login successful — session saved.")
            if headless:
                return _reload_headless_after_login()
            return api
        click.echo(f"[{_now()}] Assisted prefill failed. Falling back to manual login.")
    else:
        click.echo(f"[{_now()}] No saved auto-login credentials. Skipping auto-login.")

    # Fallback to manual login — must open visible browser
    if manager._headless:
        click.echo(f"[{_now()}] Restarting browser for manual login...")
        api = _restart_browser(headed=True)

    click.echo(f"[{_now()}] Please log in through the browser window (5 min timeout).")
    if not api.login_manual(timeout_s=300):
        click.echo("Login timed out.")
        sys.exit(1)

    manager.save_cookies()
    click.echo(f"[{_now()}] Login successful — session saved.")

    if headless:
        return _reload_headless_after_login()
    return api


_LOGIN_FINGERPRINT_SCRIPT = """async () => {
    const webgl = (() => {
        try {
            const canvas = document.createElement("canvas");
            const gl = canvas.getContext("webgl") || canvas.getContext("experimental-webgl");
            if (!gl) return null;
            const ext = gl.getExtension("WEBGL_debug_renderer_info");
            return {
                vendor: ext ? gl.getParameter(ext.UNMASKED_VENDOR_WEBGL) : gl.getParameter(gl.VENDOR),
                renderer: ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER),
            };
        } catch (error) {
            return { error: String(error) };
        }
    })();

    const permissions = {};
    if (navigator.permissions && navigator.permissions.query) {
        for (const name of ["notifications", "geolocation", "camera", "microphone"]) {
            try {
                permissions[name] = (await navigator.permissions.query({ name })).state;
            } catch (error) {
                permissions[name] = `error:${String(error)}`;
            }
        }
    }

    return {
        url: location.href,
        title: document.title,
        webdriver: navigator.webdriver,
        userAgent: navigator.userAgent,
        platform: navigator.platform,
        languages: navigator.languages,
        language: navigator.language,
        cookieEnabled: navigator.cookieEnabled,
        hardwareConcurrency: navigator.hardwareConcurrency,
        deviceMemory: navigator.deviceMemory ?? null,
        maxTouchPoints: navigator.maxTouchPoints,
        pluginsLength: navigator.plugins ? navigator.plugins.length : null,
        mimeTypesLength: navigator.mimeTypes ? navigator.mimeTypes.length : null,
        screen: {
            width: screen.width,
            height: screen.height,
            availWidth: screen.availWidth,
            availHeight: screen.availHeight,
            colorDepth: screen.colorDepth,
            pixelDepth: screen.pixelDepth,
        },
        viewport: {
            innerWidth,
            innerHeight,
            outerWidth,
            outerHeight,
            devicePixelRatio,
        },
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
        permissions,
        webgl,
    };
}"""


def _json_default(value: object) -> str:
    return str(value)


class _LoginDebugRecorder:
    def __init__(self, page: Any, output_dir: Path):
        self._page = page
        self._output_dir = output_dir
        self._events_path = output_dir / "events.jsonl"

    def start(self) -> None:
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._events_path.touch()
        self._page.on("console", self._on_console)
        self._page.on("pageerror", self._on_pageerror)
        self._page.on("request", self._on_request)
        self._page.on("response", self._on_response)
        self.snapshot("before-login")

    def snapshot(self, label: str) -> None:
        try:
            data = self._page.evaluate(_LOGIN_FINGERPRINT_SCRIPT)
        except Exception as exc:
            data = {"error": str(exc)}
        self._write_json(f"fingerprint-{label}.json", data)

    def _write_json(self, filename: str, data: object) -> None:
        path = self._output_dir / filename
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, default=_json_default),
            encoding="utf-8",
        )

    def _append_event(self, kind: str, data: dict[str, object]) -> None:
        event = {"ts": datetime.now().isoformat(timespec="milliseconds"), "kind": kind}
        event.update(data)
        with self._events_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False, default=_json_default))
            fh.write("\n")

    @staticmethod
    def _value(obj: object, name: str, default: object = "") -> object:
        value = getattr(obj, name, default)
        if callable(value):
            try:
                return value()
            except Exception as exc:
                return f"error:{exc}"
        return value

    def _on_console(self, msg: object) -> None:
        self._append_event(
            "console",
            {
                "type": self._value(msg, "type"),
                "text": self._value(msg, "text"),
                "location": self._value(msg, "location", {}),
            },
        )

    def _on_pageerror(self, error: object) -> None:
        self._append_event("pageerror", {"error": str(error)})

    def _on_request(self, request: object) -> None:
        self._append_event(
            "request",
            {
                "method": self._value(request, "method"),
                "url": self._value(request, "url"),
                "resource_type": self._value(request, "resource_type"),
            },
        )

    def _on_response(self, response: object) -> None:
        self._append_event(
            "response",
            {
                "status": self._value(response, "status"),
                "url": self._value(response, "url"),
            },
        )


def _confirm_pure_login_window(
    api: KorailAPI,
    manager: BrowserManager,
    *,
    login_debug_dir: Path | None = None,
) -> KorailAPI:
    """Wait for the user to finish a login attempt in an untouched login page."""
    recorder = (
        _LoginDebugRecorder(manager.page, login_debug_dir)
        if login_debug_dir is not None
        else None
    )
    if recorder is not None:
        recorder.start()
        click.echo(f"[{_now()}] Login debug artifacts: {login_debug_dir}")

    click.echo(
        f"[{_now()}] Pure login-window mode: opened Korail login page directly."
    )
    click.echo(
        "No saved session, stealth script, search-page navigation, "
        "credential prefill, dialog handler, or loginCheck polling is used before Enter."
    )
    click.pause("브라우저에서 직접 로그인한 뒤 터미널로 돌아와 Enter를 누르세요.")
    if recorder is not None:
        recorder.snapshot("after-enter-before-login-check")
    if not api.wait_for_login_stable(
        timeout_s=3.0,
        interval_s=0.35,
        stable_checks=2,
    ):
        click.echo("Login was not confirmed after Enter.")
        sys.exit(1)

    manager.save_cookies()
    click.echo(f"[{_now()}] Login successful — session saved.")
    return api


def _load_card() -> dict[str, str] | None:
    """Load card info from keyring. Returns dict or None if not configured."""
    card_number = keyring.get_password("KTX", "card_number")
    card_password = keyring.get_password("KTX", "card_password")
    birthday = keyring.get_password("KTX", "birthday")
    card_expire = keyring.get_password("KTX", "card_expire")
    if not all([card_number, card_password, birthday, card_expire]):
        return None
    return {
        "card_number": card_number or "",
        "card_password": card_password or "",
        "birthday": birthday or "",
        "card_expire": card_expire or "",
    }


def _set_card_interactive() -> bool:
    """Set card info using TTY prompts and save to keyring."""
    defaults = {
        "card_number": keyring.get_password("KTX", "card_number") or "",
        "card_password": keyring.get_password("KTX", "card_password") or "",
        "birthday": keyring.get_password("KTX", "birthday") or "",
        "card_expire": keyring.get_password("KTX", "card_expire") or "",
    }

    card_info = _prompt_guarded(
        [
            inquirer.Password(
                "card_number",
                message="카드번호 (하이픈 제외, Enter: 완료, Ctrl-C: 취소)",
                default=defaults["card_number"],
            ),
            inquirer.Password(
                "card_password",
                message="카드 비밀번호 앞 2자리 (Enter: 완료, Ctrl-C: 취소)",
                default=defaults["card_password"],
            ),
            inquirer.Password(
                "birthday",
                message="생년월일 YYMMDD / 사업자번호 10자리 (Enter: 완료, Ctrl-C: 취소)",
                default=defaults["birthday"],
            ),
            inquirer.Password(
                "card_expire",
                message="유효기간 YYMM (Enter: 완료, Ctrl-C: 취소)",
                default=defaults["card_expire"],
            ),
        ]
    )

    if not card_info:
        click.echo("카드 등록이 취소되었습니다.")
        return False

    card_number = str(card_info["card_number"]).replace("-", "").replace(" ", "")
    card_password = str(card_info["card_password"]).strip()
    birthday = str(card_info["birthday"]).strip()
    card_expire = str(card_info["card_expire"]).strip()

    if not card_number.isdigit():
        click.echo("입력 오류: 카드번호는 숫자만 입력하세요.")
        return False
    if len(card_password) != 2 or not card_password.isdigit():
        click.echo("입력 오류: 카드 비밀번호 앞 2자리를 숫자로 입력하세요.")
        return False
    if len(birthday) not in (6, 10) or not birthday.isdigit():
        click.echo("입력 오류: 생년월일 6자리 또는 사업자번호 10자리를 입력하세요.")
        return False
    if len(card_expire) != 4 or not card_expire.isdigit():
        click.echo("입력 오류: 유효기간은 YYMM 4자리 숫자입니다.")
        return False

    keyring.set_password("KTX", "card_number", card_number)
    keyring.set_password("KTX", "card_password", card_password)
    keyring.set_password("KTX", "birthday", birthday)
    keyring.set_password("KTX", "card_expire", card_expire)
    click.echo("카드 정보가 저장되었습니다.")
    return True


def _ensure_card_for_auto_pay() -> bool:
    """Ensure card exists for auto-pay. Returns True if auto-pay can proceed."""
    if _load_card() is not None:
        return True

    click.echo("자동결제를 선택했지만 카드 정보가 등록되어 있지 않습니다.")
    if click.confirm("지금 카드 정보를 등록할까요?", default=True):
        if _set_card_interactive() and _load_card() is not None:
            return True

    click.echo(
        "  설정 방법:\n"
        "    keyring set KTX card_number\n"
        "    keyring set KTX card_password\n"
        "    keyring set KTX birthday\n"
        "    keyring set KTX card_expire"
    )
    return False


def _do_pay(
    api: KorailAPI, reserve_result: dict[str, object], smart_ticket: bool
) -> bool:
    """Attempt auto-payment. Returns True on success."""
    card = _load_card()
    if card is None:
        click.echo(
            f"[{_now()}] Auto-pay skipped: card not configured.\n"
            "  Set card info with:\n"
            "    keyring set KTX card_number\n"
            "    keyring set KTX card_password\n"
            "    keyring set KTX birthday\n"
            "    keyring set KTX card_expire"
        )
        return False

    ticket_mode = "스마트티켓" if smart_ticket else "일반발권"
    click.echo(
        f"[{_now()}] Paying with card ending ...{card['card_number'][-4:]} "
        f"({ticket_mode})"
    )
    try:
        pay_result = api.pay(
            reserve_result,
            card_number=card["card_number"],
            card_password=card["card_password"],
            birthday=card["birthday"],
            card_expire=card["card_expire"],
            smart_ticket=smart_ticket,
        )
        pay_msg = str(pay_result.get("h_msg_txt", "")).strip()
        # Korail may return strResult=SUCC with an error message.
        if pay_msg and any(
            token in pay_msg for token in ("오류", "실패", "불가", "invalid", "error")
        ):
            click.echo(f"[{_now()}] Payment failed: {pay_msg}")
            click.echo("  Reservation is kept. Pay manually before the deadline.")
            return False

        _print_success_banner(
            "Payment successful!",
            color="green",
            on_color="on_red",
        )
        for key in ("strResult", "h_msg_txt", "h_pnr_no"):
            if key in pay_result:
                click.echo(f"  {key}: {pay_result[key]}")
        return True
    except KorailError as exc:
        click.echo(f"[{_now()}] Payment failed: {exc}")
        click.echo("  Reservation is kept. Pay manually before the deadline.")
        return False


def _send_telegram(
    train: Train,
    reserve_result: dict[str, object],
    paid: bool,
    *,
    waitlist: bool = False,
    waitlist_alert_status: str | None = None,
) -> None:
    """Send reservation/payment notification via Telegram."""
    token = keyring.get_password("telegram", "token")
    chat_id = keyring.get_password("telegram", "chat_id")
    if not token or not chat_id:
        click.echo(f"[{_now()}] Telegram skipped: token/chat_id not configured.")
        return

    pnr = reserve_result.get("h_pnr_no", "?")
    if waitlist:
        status = "예약대기 신청완료"
    else:
        status = "예약+결제 완료" if paid else "예약 완료 (미결제)"
    dep_date = train.dep_date
    formatted_date = (
        f"{dep_date[:4]}-{dep_date[4:6]}-{dep_date[6:]}"
        if len(dep_date) == 8
        else dep_date
    )
    dep_time = train.dep_time
    formatted_time = (
        f"{dep_time[:2]}:{dep_time[2:4]}" if len(dep_time) >= 4 else dep_time
    )

    text = (
        f"[KTXgo] {status}\n"
        f"{train.train_type} {train.train_no}\n"
        f"{train.departure} → {train.arrival}\n"
        f"{formatted_date} {formatted_time}\n"
        f"PNR: {pnr}"
    )
    if waitlist and waitlist_alert_status:
        text += f"\n좌석배정 알림: {waitlist_alert_status}"

    try:
        import telegram

        async def _send() -> None:
            bot = telegram.Bot(token=token)
            async with bot:
                await bot.send_message(chat_id=chat_id, text=text)

        asyncio.run(_send())
        click.echo(f"[{_now()}] Telegram notification sent.")
    except Exception as exc:
        click.echo(f"[{_now()}] Telegram failed: {exc}")


def _resolve_waitlist_alert_phone(phone: str | None) -> str | None:
    resolved = (
        phone or keyring.get_password("KTX", "waitlist_alert_phone") or ""
    ).strip()
    digits_only = "".join(ch for ch in resolved if ch.isdigit())
    return digits_only or None


def _parse_dimension(value: str, option_name: str) -> dict[str, int]:
    normalized = value.lower().replace("×", "x").strip()
    parts = normalized.split("x")
    if len(parts) != 2:
        raise click.BadParameter("expected WIDTHxHEIGHT", param_hint=option_name)
    try:
        width = int(parts[0])
        height = int(parts[1])
    except ValueError as exc:
        raise click.BadParameter("expected WIDTHxHEIGHT", param_hint=option_name) from exc
    if width <= 0 or height <= 0:
        raise click.BadParameter("width and height must be positive", param_hint=option_name)
    return {"width": width, "height": height}


def _check_saved_login_session(browser_kwargs: dict[str, object]) -> bool:
    with BrowserManager(**browser_kwargs) as manager:
        api = KorailAPI(manager.page)
        profile = api.login_profile()
        if profile is None:
            click.echo(f"[{_now()}] Saved Korail session is not logged in.")
            return False
        member_no = profile.get("member_no") or ""
        name = profile.get("name") or ""
        label = name or member_no or "authenticated user"
        click.echo(f"[{_now()}] Saved Korail session is logged in: {label}")
        return True


def _default_external_firefox_profile_dir() -> Path:
    snap_firefox_home = Path.home() / "snap" / "firefox" / "common" / ".mozilla" / "firefox"
    if snap_firefox_home.is_dir():
        return snap_firefox_home / "ktxgo-login-profile"
    return DATA_DIR / "firefox-login-profile"


def _run_external_firefox_login(
    *,
    firefox_executable: str,
    profile_dir: Path,
) -> int:
    profile_dir = profile_dir.expanduser()
    profile_dir.mkdir(parents=True, exist_ok=True)
    command = [
        firefox_executable,
        "--no-remote",
        "--profile",
        str(profile_dir),
        LOGIN_URL,
    ]
    click.echo(f"[{_now()}] Opening external Firefox for Korail login...")
    click.echo(f"[{_now()}] Firefox profile: {profile_dir}")
    subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    click.pause(
        colored(
            "Korail 로그인을 완료한 뒤 창을 닫고, 이 터미널에서 Enter를 누르세요.",
            "white",
            "on_blue",
            attrs=["bold"],
        )
    )
    imported_count = import_firefox_korail_cookies(profile_dir)
    if imported_count <= 0:
        raise click.ClickException(
            "No Korail cookies found in the external Firefox profile. "
            "Make sure login completed in the opened Firefox window."
        )
    suffix = "" if imported_count == 1 else "s"
    click.echo(
        f"[{_now()}] Imported {imported_count} Korail cookie{suffix} "
        f"from Firefox profile into {COOKIE_PATH}."
    )
    return imported_count


def _build_external_firefox_search_url(
    *,
    departure: str,
    arrival: str,
    date: str,
    time_str: str,
    adults: int,
    train_types: tuple[str, ...],
) -> str:
    train_code = train_type_codes(train_types)[0]
    hh = time_str.zfill(2)
    params = {
        "searchType": "GENERAL",
        "txtGoStart": departure,
        "txtGoEnd": arrival,
        "txtGoAbrdDt": date,
        "txtGoHour": f"{hh}0000",
        "txtPsgFlg_1": str(adults),
        "txtPsgFlg_2": "0",
        "txtPsgFlg_3": "0",
        "txtPsgFlg_4": "0",
        "txtPsgFlg_5": "0",
        "txtSeatAttCd_4": "015",
        "txtTrnGpCd": train_code,
        "selGoTrain": train_code,
        "txtMenuId": "11",
        "radJobId": "1",
        "srtCheckYn": "N",
        "ebizCrossCheck": "N",
        "adjStnScdlOfrFlg": "N",
        "rtYn": "N",
    }
    dep_code = STATION_CODE_BY_NAME.get(departure)
    arr_code = STATION_CODE_BY_NAME.get(arrival)
    if dep_code:
        params["txtGoStartCode"] = dep_code
    if arr_code:
        params["txtGoEndCode"] = arr_code
    return f"{BASE_URL}/ticket/search/list?{urlencode(params)}"


def _run_external_firefox_search(
    *,
    firefox_executable: str,
    profile_dir: Path,
    departure: str,
    arrival: str,
    date: str,
    time_str: str,
    adults: int,
    train_types: tuple[str, ...],
) -> None:
    profile_dir = profile_dir.expanduser()
    profile_dir.mkdir(parents=True, exist_ok=True)
    url = _build_external_firefox_search_url(
        departure=departure,
        arrival=arrival,
        date=date,
        time_str=time_str,
        adults=adults,
        train_types=train_types,
    )
    command = [
        firefox_executable,
        "--no-remote",
        "--profile",
        str(profile_dir),
        url,
    ]
    click.echo(f"[{_now()}] Opening external Firefox search page...")
    click.echo(f"[{_now()}] Firefox profile: {profile_dir}")
    click.echo(f"[{_now()}] Search: {departure} → {arrival} {date} {time_str}:00")
    subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    click.pause(
        colored(
            "외부 Firefox에서 Korail 조회/예매를 진행한 뒤, 이 터미널에서 Enter를 누르세요.",
            "white",
            "on_blue",
            attrs=["bold"],
        )
    )


def _default_extension_chromium_executable() -> Path | None:
    candidates = [
        Path.home() / ".cache/ms-playwright/chromium-1105/chrome-linux/chrome",
    ]
    playwright_cache = Path.home() / ".cache/ms-playwright"
    if playwright_cache.is_dir():
        candidates.extend(
            sorted(playwright_cache.glob("chromium-*/chrome-linux/chrome"))
        )
        candidates.extend(
            sorted(playwright_cache.glob("chromium-*/chrome-linux64/chrome"))
        )

    for path in candidates:
        if path.is_file():
            return path

    for executable in ("chromium", "chromium-browser"):
        resolved = shutil.which(executable)
        if resolved:
            return Path(resolved)
    return None


def _ensure_extension_login(
    api: ExtensionKorailAPI,
    runner: ExtensionBrowserRunner,
    *,
    force_relogin: bool = False,
) -> ExtensionKorailAPI:
    if (
        not force_relogin
        and api.wait_for_login_stable(timeout_s=0.8, interval_s=0.25, stable_checks=1)
    ):
        click.echo(f"[{_now()}] Logged in via extension browser profile.")
        return api

    if not sys.stdin.isatty():
        raise click.UsageError(
            "--api-backend=extension requires a TTY for first Korail login"
        )
    if force_relogin:
        click.echo(f"[{_now()}] Force relogin requested for extension backend.")
    click.echo(f"[{_now()}] Opening Korail login in extension Chromium...")
    runner.navigate(LOGIN_URL)
    click.pause(
        colored(
            "열린 창에서 Korail 로그인을 완료한 뒤, 이 터미널에서 Enter를 누르세요.\n"
            "예매 중 작업표시줄의 chromium-browser를 닫지마세요",
            "white",
            "on_blue",
            attrs=["bold"],
        )
    )
    if not api.wait_for_login_stable(timeout_s=10, interval_s=0.5, stable_checks=2):
        raise click.ClickException("Extension Chromium login was not confirmed.")
    click.echo(f"[{_now()}] Login successful in extension Chromium profile.")
    return api


def _run_reservation_loop(
    api: KorailAPI,
    *,
    reauthenticate: Callable[[KorailAPI, str], KorailAPI],
    interactive_mode: bool,
    departure: str,
    arrival: str,
    date: str,
    time_str: str,
    adults: int,
    train_types: tuple[str, ...],
    seat: str,
    auto_pay: bool,
    smart_ticket: bool,
    telegram: bool,
    waitlist_alert_phone: str | None,
    max_attempts: int,
) -> None:
    target_trains: list[TrainKey] | None = None
    target_line: str | None = None
    clear_each_attempt = sys.stdout.isatty()

    if interactive_mode:
        while True:
            try:
                target_trains = _prompt_target_trains(
                    api,
                    departure,
                    arrival,
                    date,
                    time_str,
                    adults,
                    train_types,
                )
                break
            except KorailError as exc:
                code = exc.code or ""
                if code in _SESSION_EXPIRED_CODES:
                    click.echo(
                        f"[{_now()}] Session expired before selection. Re-authenticating..."
                    )
                    api = reauthenticate(api, "selection")
                    continue
                click.echo(f"[{_now()}] Initial search error: {exc}")
                sys.exit(1)
        target_line = _target_summary(target_trains)
        seat, auto_pay, smart_ticket = _prompt_reservation_options(
            seat, auto_pay, smart_ticket
        )
        if auto_pay and not _ensure_card_for_auto_pay():
            if click.confirm("자동결제 없이 계속 진행할까요?", default=True):
                auto_pay = False
                _save_interactive_default("auto_pay", auto_pay)
            else:
                sys.exit(0)

    status_line = (
        f"KTXgo — {departure} → {arrival}  {date} {time_str}:00  "
        f"adults={adults} seat={seat} train-types={','.join(train_types)}"
        f"{' auto-pay' if auto_pay else ''}{' telegram' if telegram else ''}"
    )

    if not clear_each_attempt:
        _render_screen(status_line, target_line, clear_screen=False)

    attempt = 0
    consecutive_errors = 0
    while max_attempts == 0 or attempt < max_attempts:
        attempt += 1

        if clear_each_attempt:
            _render_screen(status_line, target_line, clear_screen=True)

        try:
            trains = api.search(
                departure,
                arrival,
                date,
                time_str,
                adults=adults,
                train_types=train_types,
            )
            consecutive_errors = 0
        except KorailError as exc:
            consecutive_errors += 1
            code = exc.code or ""
            if code in _SESSION_EXPIRED_CODES:
                click.echo(f"[{_now()}] Session expired. Re-authenticating...")
                api = reauthenticate(api, "search")
                continue
            click.echo(f"[{_now()}] Search error: {exc}")
            if code == "MACRO ERROR":
                sys.exit(1)
            if consecutive_errors >= 5:
                click.echo("Too many consecutive errors. Exiting.")
                sys.exit(1)
            time.sleep(POLL_INTERVAL_S * 2)
            continue

        click.echo(f"[{_now()}] Attempt {attempt}  ({departure}→{arrival})")
        if not trains:
            click.echo("No trains returned")
            time.sleep(POLL_INTERVAL_S)
            continue

        _print_results(trains)

        candidate_trains = trains
        if target_trains is not None:
            candidate_trains, missing_count = _resolve_targets(
                trains, target_trains
            )
            if missing_count:
                click.echo(
                    f"Selected trains not present now: {missing_count}/{len(target_trains)}"
                )
            if not candidate_trains:
                time.sleep(POLL_INTERVAL_S)
                continue

        for train in candidate_trains:
            plan = _reservation_plan(train, seat)
            if plan is None:
                continue
            seat_type, waitlist = plan
            if waitlist:
                click.echo(
                    f"\n[{_now()}] 예약대기 가능: {train.train_no} "
                    f"({train.dep_time}). 예약대기 신청 ({seat_type})..."
                )
            else:
                click.echo(
                    f"\n[{_now()}] Seat found: {train.train_no} "
                    f"({train.dep_time}). Reserving ({seat_type})..."
                )
            try:
                result = api.reserve(
                    train,
                    seat_type=seat_type,
                    adults=adults,
                    waitlist=waitlist,
                )
            except KorailError as exc:
                code = exc.code or ""
                if code in _SESSION_EXPIRED_CODES:
                    click.echo(
                        f"[{_now()}] Session expired during reserve. Re-authenticating..."
                    )
                    api = reauthenticate(api, "reserve")
                    break  # Restart search loop
                click.echo(f"  → Reserve failed: {exc}")
                continue

            if waitlist:
                _print_success_banner("예약대기 신청완료")
            else:
                _print_success_banner("Reservation successful!")
            for key in ("h_pnr_no", "h_rsv_no", "strResult", "h_msg_txt"):
                if key in result:
                    click.echo(f"  {key}: {result[key]}")

            waitlist_alert_status: str | None = None
            if waitlist:
                resolved_waitlist_alert_phone = _resolve_waitlist_alert_phone(
                    waitlist_alert_phone
                )
                if resolved_waitlist_alert_phone is None:
                    waitlist_alert_status = "미등록"
                    click.echo(
                        f"[{_now()}] 예약대기 알림 전화번호가 없어 좌석배정 알림 신청을 건너뜁니다."
                    )
                else:
                    try:
                        api.set_waitlist_alert(
                            str(result.get("h_pnr_no", "")),
                            resolved_waitlist_alert_phone,
                        )
                        waitlist_alert_status = "등록완료"
                        click.echo(
                            f"[{_now()}] 좌석배정 알림 등록완료 ({resolved_waitlist_alert_phone})"
                        )
                    except KorailError as exc:
                        waitlist_alert_status = f"등록실패: {exc}"
                        click.echo(f"[{_now()}] 좌석배정 알림 등록 실패: {exc}")

            paid = False
            if auto_pay and not waitlist:
                paid = _do_pay(api, result, smart_ticket)
            elif auto_pay and waitlist:
                click.echo(f"[{_now()}] 예약대기는 자동결제를 지원하지 않습니다.")

            if telegram:
                _send_telegram(
                    train,
                    result,
                    paid,
                    waitlist=waitlist,
                    waitlist_alert_status=waitlist_alert_status,
                )

            return

        time.sleep(POLL_INTERVAL_S)


@click.command()
@click.option("--departure", default=DEFAULT_DEPARTURE, show_default=True)
@click.option("--arrival", default=DEFAULT_ARRIVAL, show_default=True)
@click.option("--date", default=None, help="YYYYMMDD")
@click.option("--time", "time_str", default=None, help="HH")
@click.option(
    "--adults",
    default=1,
    show_default=True,
    type=click.IntRange(1, 9),
    help="Number of adult passengers",
)
@click.option("--headless/--no-headless", default=True, show_default=True)
@click.option(
    "--manual-login-only",
    is_flag=True,
    default=False,
    help="Open Korail login page without assisted credential prefill",
)
@click.option(
    "--force-relogin",
    is_flag=True,
    default=False,
    help="Ignore saved KTX browser session for this run",
)
@click.option(
    "--import-cookies",
    "import_cookies_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Import Korail cookies from cookies.txt or JSON export and exit",
)
@click.option(
    "--check-login-session",
    is_flag=True,
    default=False,
    help="Check whether the saved/imported Korail cookie session is logged in and exit",
)
@click.option(
    "--external-firefox-login",
    is_flag=True,
    default=False,
    help="Open normal Firefox for manual Korail login, then import its cookies",
)
@click.option(
    "--external-firefox-search",
    is_flag=True,
    default=False,
    help="Open the official Korail search page in normal Firefox and exit",
)
@click.option(
    "--external-firefox",
    default="firefox",
    show_default=True,
    help="Firefox executable for external Firefox modes",
)
@click.option(
    "--external-firefox-profile",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    help="Firefox profile directory for external Firefox modes",
)
@click.option(
    "--pure-login-window",
    is_flag=True,
    default=False,
    help="Open only Korail login page and wait for Enter before login checks",
)
@click.option(
    "--pure-login-stealth",
    is_flag=True,
    default=False,
    help="Use the webdriver-hiding init script in pure login-window mode",
)
@click.option(
    "--webdriver-mode",
    type=click.Choice(WEBDRIVER_MODE_CHOICES),
    default="default",
    show_default=True,
    help="navigator.webdriver init script mode",
)
@click.option(
    "--api-backend",
    type=click.Choice(("playwright", "extension")),
    default="extension",
    show_default=True,
    help="Korail API execution backend (extension avoids Korail macro checks)",
)
@click.option(
    "--extension-chromium",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Chromium executable for --api-backend=extension",
)
@click.option(
    "--extension-profile",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    help="Persistent Chromium profile for --api-backend=extension",
)
@click.option(
    "--browser",
    "browser_name",
    type=click.Choice(PLAYWRIGHT_BROWSER_CHOICES),
    default="firefox",
    show_default=True,
    help="Playwright browser engine to launch",
)
@click.option(
    "--browser-channel",
    default=None,
    help="Optional Playwright browser channel, e.g. chrome or msedge for chromium",
)
@click.option(
    "--browser-executable",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Optional browser executable path for Playwright launch",
)
@click.option(
    "--browser-profile-dir",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    help="Use a persistent browser profile directory for this run",
)
@click.option(
    "--browser-locale",
    default="ko-KR",
    show_default=True,
    help="Browser context locale",
)
@click.option(
    "--browser-user-agent",
    default=None,
    help="Override browser context user agent",
)
@click.option(
    "--viewport-size",
    default=None,
    help="Browser viewport size as WIDTHxHEIGHT",
)
@click.option(
    "--screen-size",
    default=None,
    help="Browser screen size as WIDTHxHEIGHT",
)
@click.option(
    "--device-scale-factor",
    type=float,
    default=None,
    help="Browser context device scale factor",
)
@click.option(
    "--login-debug-dir",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    help="Directory for pure-login-window network/fingerprint debug artifacts",
)
@click.option(
    "--interactive/--no-interactive",
    default=None,
    help="Prompt for date/time/train selection (default: on for TTY)",
)
@click.option("--max-attempts", default=0, show_default=True, help="0 means infinite")
@click.option(
    "--train-type",
    "train_types",
    multiple=True,
    type=click.Choice(TRAIN_TYPE_OPTION_CHOICES),
    default=DEFAULT_TRAIN_TYPES,
    show_default=True,
    help="Train classes to search and reserve",
)
@click.option(
    "--seat",
    type=click.Choice(["general", "special", "any", "standing"]),
    default="any",
    show_default=True,
)
@click.option(
    "--set-card",
    "set_card_mode",
    is_flag=True,
    default=False,
    help="Configure saved card info in keyring and exit",
)
@click.option(
    "--auto-pay", is_flag=True, default=False, help="Auto-pay after reservation"
)
@click.option(
    "--smart-ticket/--no-smart-ticket",
    default=True,
    show_default=True,
    help="Smart-ticket issuance option for auto-pay",
)
@click.option(
    "--telegram", is_flag=True, default=False, help="Send Telegram notification"
)
@click.option(
    "--waitlist-alert-phone",
    default=None,
    help="Phone number used for Korail waitlist seat-assignment SMS alerts",
)
def main(
    departure: str,
    arrival: str,
    date: str | None,
    time_str: str | None,
    adults: int,
    headless: bool,
    manual_login_only: bool,
    force_relogin: bool,
    pure_login_window: bool,
    pure_login_stealth: bool,
    webdriver_mode: str,
    browser_name: str,
    browser_channel: str | None,
    browser_executable: Path | None,
    browser_profile_dir: Path | None,
    browser_locale: str,
    browser_user_agent: str | None,
    viewport_size: str | None,
    screen_size: str | None,
    device_scale_factor: float | None,
    login_debug_dir: Path | None,
    interactive: bool | None,
    max_attempts: int,
    train_types: tuple[str, ...],
    seat: str,
    set_card_mode: bool,
    auto_pay: bool,
    smart_ticket: bool,
    telegram: bool,
    waitlist_alert_phone: str | None,
    import_cookies_path: Path | None = None,
    check_login_session: bool = False,
    external_firefox_login: bool = False,
    external_firefox_search: bool = False,
    external_firefox: str = "firefox",
    external_firefox_profile: Path | None = None,
    api_backend: str = "extension",
    extension_chromium: Path | None = None,
    extension_profile: Path | None = None,
) -> None:
    configure_keyring_backend()

    if login_debug_dir is not None and not pure_login_window:
        raise click.UsageError("--login-debug-dir requires --pure-login-window")
    if pure_login_stealth and not pure_login_window:
        raise click.UsageError("--pure-login-stealth requires --pure-login-window")
    if check_login_session and pure_login_window:
        raise click.UsageError("--check-login-session cannot be used with --pure-login-window")
    if external_firefox_login and pure_login_window:
        raise click.UsageError("--external-firefox-login cannot be used with --pure-login-window")
    if external_firefox_login and import_cookies_path is not None:
        raise click.UsageError("Use only one of --external-firefox-login or --import-cookies")
    if external_firefox_profile is not None and not (
        external_firefox_login or external_firefox_search
    ):
        raise click.UsageError(
            "--external-firefox-profile requires an external Firefox mode"
        )
    if login_debug_dir is not None:
        login_debug_dir.mkdir(parents=True, exist_ok=True)
    viewport = (
        _parse_dimension(viewport_size, "--viewport-size")
        if viewport_size is not None
        else None
    )
    screen = (
        _parse_dimension(screen_size, "--screen-size")
        if screen_size is not None
        else None
    )

    if set_card_mode:
        if not sys.stdin.isatty():
            raise click.UsageError("--set-card requires a TTY")
        if not _set_card_interactive():
            sys.exit(0)
        sys.exit(0)

    session_check_browser_kwargs: dict[str, object] = {
        "headless": headless,
        "use_saved_session": True,
        "browser_name": browser_name,
        "browser_channel": browser_channel,
        "browser_executable": browser_executable,
        "browser_profile_dir": browser_profile_dir,
        "locale": browser_locale,
        "user_agent": browser_user_agent,
        "viewport": viewport,
        "screen": screen,
        "device_scale_factor": device_scale_factor,
        "webdriver_mode": webdriver_mode,
    }
    if external_firefox_login:
        profile_dir = external_firefox_profile or _default_external_firefox_profile_dir()
        _run_external_firefox_login(
            firefox_executable=external_firefox,
            profile_dir=profile_dir,
        )
        if not check_login_session:
            sys.exit(0)
    if import_cookies_path is not None:
        imported_count = import_korail_cookies(import_cookies_path)
        if imported_count <= 0:
            raise click.ClickException("No Korail cookies found in the import file.")
        suffix = "" if imported_count == 1 else "s"
        click.echo(
            f"[{_now()}] Imported {imported_count} Korail cookie{suffix} into {COOKIE_PATH}."
        )
        if not check_login_session:
            sys.exit(0)
    if check_login_session:
        if not _check_saved_login_session(session_check_browser_kwargs):
            sys.exit(1)
        sys.exit(0)

    departure = _normalize_station(departure)
    arrival = _normalize_station(arrival)
    if departure == arrival:
        raise click.BadParameter("departure and arrival must be different")

    if date is not None:
        date = _validate_date(date)
    if time_str is not None:
        time_str = _validate_hour(time_str)
    adults = _validate_adults(adults)
    date = date or _fmt_date()
    time_str = time_str or _fmt_hour()
    train_types = _normalize_train_types(train_types)

    interactive_mode = sys.stdin.isatty() if interactive is None else interactive
    if interactive_mode and not sys.stdin.isatty():
        raise click.UsageError("--interactive requires a TTY")
    visible_stations = _load_visible_stations()
    if interactive_mode:
        ctx = click.get_current_context(silent=True)
        (
            departure,
            arrival,
            date,
            time_str,
            adults,
            train_types,
            seat,
            auto_pay,
            smart_ticket,
        ) = _apply_saved_interactive_reservation_defaults(
            ctx,
            stations=visible_stations,
            departure=departure,
            arrival=arrival,
            date=date,
            time_str=time_str,
            adults=adults,
            train_types=train_types,
            seat=seat,
            auto_pay=auto_pay,
            smart_ticket=smart_ticket,
        )
        while True:
            action = _prompt_main_menu()
            if action == "reserve":
                break
            if action == "reservation":
                _show_reservations_interactive()
                continue
            if action == "login":
                _configure_login_interactive()
                continue
            if action == "station":
                _set_visible_stations_interactive()
                visible_stations = _load_visible_stations()
                continue
            if action == "waitlist-alert":
                _set_waitlist_alert_phone_interactive()
                continue
            if action == "card":
                _set_card_interactive()
                continue
            sys.exit(0)
        departure, arrival, date, time_str, adults, train_types = _prompt_conditions(
            departure,
            arrival,
            date,
            time_str,
            adults,
            visible_stations,
            train_types,
        )

    if external_firefox_search:
        _run_external_firefox_search(
            firefox_executable=external_firefox,
            profile_dir=external_firefox_profile or _default_external_firefox_profile_dir(),
            departure=departure,
            arrival=arrival,
            date=date,
            time_str=time_str,
            adults=adults,
            train_types=train_types,
        )
        sys.exit(0)

    # Graceful Ctrl+C
    def _sigint(_sig: int, _frame: object) -> None:
        click.echo("\nInterrupted. Exiting.")
        sys.exit(0)

    signal.signal(signal.SIGINT, _sigint)

    if api_backend == "extension":
        if pure_login_window:
            raise click.UsageError("--pure-login-window requires --api-backend=playwright")
        chromium_executable = extension_chromium or _default_extension_chromium_executable()
        if chromium_executable is None:
            raise click.UsageError(
                "No Chromium executable found for --api-backend=extension. "
                "Install Chromium or pass --extension-chromium."
            )
        initial_url = _build_external_firefox_search_url(
            departure=departure,
            arrival=arrival,
            date=date,
            time_str=time_str,
            adults=adults,
            train_types=train_types,
        )

        def _make_extension_runner(*, runner_headless: bool) -> ExtensionBrowserRunner:
            return ExtensionBrowserRunner(
                chromium_executable=chromium_executable,
                profile_dir=extension_profile,
                initial_url=initial_url,
                headless=runner_headless,
            )

        def _run_loop_with_extension_api(
            extension_api: ExtensionKorailAPI,
            *,
            reauthenticate: Callable[[KorailAPI, str], KorailAPI],
        ) -> None:
            _run_reservation_loop(
                extension_api,
                reauthenticate=reauthenticate,
                interactive_mode=interactive_mode,
                departure=departure,
                arrival=arrival,
                date=date,
                time_str=time_str,
                adults=adults,
                train_types=train_types,
                seat=seat,
                auto_pay=auto_pay,
                smart_ticket=smart_ticket,
                telegram=telegram,
                waitlist_alert_phone=waitlist_alert_phone,
                max_attempts=max_attempts,
            )

        def _minimize_extension_runner(runner: ExtensionBrowserRunner) -> None:
            if runner.minimize():
                click.echo(
                    f"[{_now()}] Extension Chromium window minimized; "
                    "reservation loop continues in that logged-in browser."
                )
            else:
                click.echo(
                    f"[{_now()}] Could not minimize extension Chromium window automatically. "
                    "You may minimize it manually; do not close it during reservation."
                )

        def _run_visible_extension_session(
            *,
            minimize_after_login: bool,
            force_login: bool,
        ) -> None:
            with _make_extension_runner(runner_headless=False) as visible_runner:
                visible_api = ExtensionKorailAPI(visible_runner)
                visible_api = _ensure_extension_login(
                    visible_api,
                    visible_runner,
                    force_relogin=force_login,
                )
                if minimize_after_login:
                    _minimize_extension_runner(visible_runner)

                def _visible_extension_reauthenticate(
                    current_api: KorailAPI,
                    _stage: str,
                ) -> KorailAPI:
                    del current_api
                    refreshed_api = _ensure_extension_login(
                        visible_api,
                        visible_runner,
                        force_relogin=True,
                    )
                    if minimize_after_login:
                        _minimize_extension_runner(visible_runner)
                    return refreshed_api

                _run_loop_with_extension_api(
                    visible_api,
                    reauthenticate=_visible_extension_reauthenticate,
                )

        if headless and force_relogin:
            _run_visible_extension_session(
                minimize_after_login=True,
                force_login=True,
            )
            return

        if headless:
            runner = _make_extension_runner(runner_headless=True)
            visible_fallback_runner: ExtensionBrowserRunner | None = None
            runner_closed = False
            runner.start()
            try:
                extension_api = ExtensionKorailAPI(runner)
                if not extension_api.wait_for_login_stable(
                    timeout_s=0.8,
                    interval_s=0.25,
                    stable_checks=1,
                ):
                    click.echo(
                        f"[{_now()}] Saved extension session is invalid. "
                        "Opening visible Chromium login..."
                    )
                    runner.close()
                    runner_closed = True
                    _run_visible_extension_session(
                        minimize_after_login=True,
                        force_login=True,
                    )
                    return
                click.echo(f"[{_now()}] Logged in via headless extension Chromium.")

                def _headless_extension_reauthenticate(
                    current_api: KorailAPI,
                    _stage: str,
                ) -> KorailAPI:
                    nonlocal runner, visible_fallback_runner
                    del current_api
                    click.echo(
                        f"[{_now()}] Headless session expired. "
                        "Opening visible Chromium login..."
                    )
                    runner.close()
                    runner_closed = True
                    if visible_fallback_runner is not None:
                        visible_fallback_runner.close()
                    visible_fallback_runner = _make_extension_runner(
                        runner_headless=False
                    )
                    visible_fallback_runner.start()
                    new_api = ExtensionKorailAPI(visible_fallback_runner)
                    new_api = _ensure_extension_login(
                        new_api,
                        visible_fallback_runner,
                        force_relogin=True,
                    )
                    _minimize_extension_runner(visible_fallback_runner)
                    return new_api

                _run_loop_with_extension_api(
                    extension_api,
                    reauthenticate=_headless_extension_reauthenticate,
                )
            finally:
                if not runner_closed:
                    runner.close()
                if visible_fallback_runner is not None:
                    visible_fallback_runner.close()
            return

        _run_visible_extension_session(
            minimize_after_login=False,
            force_login=force_relogin,
        )
        return

    browser_kwargs: dict[str, object] = {
        "headless": False if pure_login_window else headless,
        "use_saved_session": not (force_relogin or pure_login_window),
        "browser_name": browser_name,
        "browser_channel": browser_channel,
        "browser_executable": browser_executable,
        "browser_profile_dir": browser_profile_dir,
        "locale": browser_locale,
        "user_agent": browser_user_agent,
        "viewport": viewport,
        "screen": screen,
        "device_scale_factor": device_scale_factor,
        "webdriver_mode": webdriver_mode,
    }
    if pure_login_window:
        browser_kwargs["initial_url"] = LOGIN_URL
        browser_kwargs["use_stealth"] = pure_login_stealth
        if login_debug_dir is not None:
            browser_kwargs["record_har_path"] = login_debug_dir / "browser.har"

    with BrowserManager(**browser_kwargs) as manager:
        api = KorailAPI(manager.page)
        ensure_login_kwargs: dict[str, object] = {
            "manual_login_only": manual_login_only,
            "force_relogin": force_relogin,
            "external_firefox": external_firefox,
            "external_firefox_profile": external_firefox_profile,
        }
        if pure_login_window:
            api = _confirm_pure_login_window(
                api,
                manager,
                login_debug_dir=login_debug_dir,
            )
        else:
            api = _ensure_login(
                api,
                manager,
                headless,
                **ensure_login_kwargs,
            )
        target_trains: list[TrainKey] | None = None
        target_line: str | None = None
        clear_each_attempt = sys.stdout.isatty()

        if interactive_mode:
            while True:
                try:
                    target_trains = _prompt_target_trains(
                        api,
                        departure,
                        arrival,
                        date,
                        time_str,
                        adults,
                        train_types,
                    )
                    break
                except KorailError as exc:
                    code = exc.code or ""
                    if code in _SESSION_EXPIRED_CODES:
                        click.echo(
                            f"[{_now()}] Session expired before selection. Re-authenticating..."
                        )
                        api = _ensure_login(
                            api,
                            manager,
                            headless,
                            **{
                                **ensure_login_kwargs,
                                "manual_login_only": manual_login_only or pure_login_window,
                                "force_relogin": False,
                            },
                        )
                        continue
                    click.echo(f"[{_now()}] Initial search error: {exc}")
                    sys.exit(1)
            target_line = _target_summary(target_trains)
            seat, auto_pay, smart_ticket = _prompt_reservation_options(
                seat, auto_pay, smart_ticket
            )
            if auto_pay and not _ensure_card_for_auto_pay():
                if click.confirm("자동결제 없이 계속 진행할까요?", default=True):
                    auto_pay = False
                    _save_interactive_default("auto_pay", auto_pay)
                else:
                    sys.exit(0)

        status_line = (
            f"KTXgo — {departure} → {arrival}  {date} {time_str}:00  "
            f"adults={adults} seat={seat} train-types={','.join(train_types)}"
            f"{' auto-pay' if auto_pay else ''}{' telegram' if telegram else ''}"
        )

        if not clear_each_attempt:
            _render_screen(status_line, target_line, clear_screen=False)

        attempt = 0
        consecutive_errors = 0
        while max_attempts == 0 or attempt < max_attempts:
            attempt += 1

            if clear_each_attempt:
                _render_screen(status_line, target_line, clear_screen=True)

            try:
                trains = api.search(
                    departure,
                    arrival,
                    date,
                    time_str,
                    adults=adults,
                    train_types=train_types,
                )
                consecutive_errors = 0
            except KorailError as exc:
                consecutive_errors += 1
                code = exc.code or ""
                if code in _SESSION_EXPIRED_CODES:
                    click.echo(f"[{_now()}] Session expired. Re-authenticating...")
                    api = _ensure_login(
                        api,
                        manager,
                        headless,
                        **{
                            **ensure_login_kwargs,
                            "manual_login_only": manual_login_only or pure_login_window,
                            "force_relogin": False,
                        },
                    )
                    continue
                click.echo(f"[{_now()}] Search error: {exc}")
                if code == "MACRO ERROR":
                    sys.exit(1)
                if consecutive_errors >= 5:
                    click.echo("Too many consecutive errors. Exiting.")
                    sys.exit(1)
                time.sleep(POLL_INTERVAL_S * 2)
                continue

            click.echo(f"[{_now()}] Attempt {attempt}  ({departure}→{arrival})")
            if not trains:
                click.echo("No trains returned")
                time.sleep(POLL_INTERVAL_S)
                continue

            _print_results(trains)

            candidate_trains = trains
            if target_trains is not None:
                candidate_trains, missing_count = _resolve_targets(
                    trains, target_trains
                )
                if missing_count:
                    click.echo(
                        f"Selected trains not present now: {missing_count}/{len(target_trains)}"
                    )
                if not candidate_trains:
                    time.sleep(POLL_INTERVAL_S)
                    continue

            for train in candidate_trains:
                plan = _reservation_plan(train, seat)
                if plan is None:
                    continue
                seat_type, waitlist = plan
                if waitlist:
                    click.echo(
                        f"\n[{_now()}] 예약대기 가능: {train.train_no} "
                        f"({train.dep_time}). 예약대기 신청 ({seat_type})..."
                    )
                else:
                    click.echo(
                        f"\n[{_now()}] Seat found: {train.train_no} "
                        f"({train.dep_time}). Reserving ({seat_type})..."
                    )
                try:
                    result = api.reserve(
                        train,
                        seat_type=seat_type,
                        adults=adults,
                        waitlist=waitlist,
                    )
                except KorailError as exc:
                    code = exc.code or ""
                    if code in _SESSION_EXPIRED_CODES:
                        click.echo(
                            f"[{_now()}] Session expired during reserve. Re-authenticating..."
                        )
                        api = _ensure_login(
                            api,
                            manager,
                            headless,
                            **{
                                **ensure_login_kwargs,
                                "manual_login_only": manual_login_only or pure_login_window,
                                "force_relogin": False,
                            },
                        )
                        break  # Restart search loop
                    click.echo(f"  → Reserve failed: {exc}")
                    continue

                if waitlist:
                    _print_success_banner("예약대기 신청완료")
                else:
                    _print_success_banner("Reservation successful!")
                for key in ("h_pnr_no", "h_rsv_no", "strResult", "h_msg_txt"):
                    if key in result:
                        click.echo(f"  {key}: {result[key]}")

                waitlist_alert_status: str | None = None
                if waitlist:
                    resolved_waitlist_alert_phone = _resolve_waitlist_alert_phone(
                        waitlist_alert_phone
                    )
                    if resolved_waitlist_alert_phone is None:
                        waitlist_alert_status = "미등록"
                        click.echo(
                            f"[{_now()}] 예약대기 알림 전화번호가 없어 좌석배정 알림 신청을 건너뜁니다."
                        )
                    else:
                        try:
                            api.set_waitlist_alert(
                                str(result.get("h_pnr_no", "")),
                                resolved_waitlist_alert_phone,
                            )
                            waitlist_alert_status = "등록완료"
                            click.echo(
                                f"[{_now()}] 좌석배정 알림 등록완료 ({resolved_waitlist_alert_phone})"
                            )
                        except KorailError as exc:
                            waitlist_alert_status = f"등록실패: {exc}"
                            click.echo(f"[{_now()}] 좌석배정 알림 등록 실패: {exc}")

                # Auto-pay
                paid = False
                if auto_pay and not waitlist:
                    paid = _do_pay(api, result, smart_ticket)
                elif auto_pay and waitlist:
                    click.echo(f"[{_now()}] 예약대기는 자동결제를 지원하지 않습니다.")

                # Telegram notification
                if telegram:
                    _send_telegram(
                        train,
                        result,
                        paid,
                        waitlist=waitlist,
                        waitlist_alert_status=waitlist_alert_status,
                    )

                return

            time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    main()
