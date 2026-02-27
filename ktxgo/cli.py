from __future__ import annotations

import asyncio
import signal
import sys
import time
import unicodedata
from datetime import datetime, timedelta

import click
import inquirer
import keyring
from termcolor import colored

from srtgo.keyring_bootstrap import configure_keyring_backend

from .browser import BrowserManager
from .config import COOKIE_PATH, DEFAULT_ARRIVAL, DEFAULT_DEPARTURE, POLL_INTERVAL_S, STATIONS
from .korail import KorailAPI, KorailError, Train

# Session-expired error codes returned by Korail.
_SESSION_EXPIRED_CODES = {"P058", "WRT300004", "WRD000003"}

TrainKey = tuple[str, str, str, str, str]


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
    return " ".join(_pad_display(text, width, align=align) for text, width, align in columns)


def _train_choice_label(idx: int, train: Train) -> str:
    return _format_row(
        [
            (f"[{idx}]", 4, "right"),
            (train.train_no, 6, "right"),
            (f"{train.dep_time}-{train.arr_time}", 12, "left"),
            (f"{train.departure}->{train.arrival}", 15, "left"),
            (f"일반:{train.general_seat}", 14, "left"),
            (f"특석:{train.special_seat}", 14, "left"),
            (f"입석:{train.standing_seat}", 14, "left"),
        ]
    )


def _prompt_main_menu() -> str:
    choice = inquirer.list_input(
        message="메뉴 선택 (↕:이동, Enter: 선택, Ctrl-C: 취소)",
        choices=[
            ("예매 시작", "reserve"),
            ("예매 정보 확인", "reservation"),
            ("로그인 설정", "login"),
            ("역 설정", "station"),
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


def _login_and_save_session(force_relogin: bool = False) -> bool:
    backup_cookie: str | None = None
    if force_relogin and COOKIE_PATH.is_file():
        try:
            backup_cookie = COOKIE_PATH.read_text()
        except OSError:
            backup_cookie = None
        try:
            COOKIE_PATH.unlink()
        except OSError:
            pass

    manager = BrowserManager(headless=False)
    try:
        with manager:
            api = KorailAPI(manager.page)
            click.echo(f"[{_now()}] 브라우저에서 로그인하세요. (5분 제한)")
            if not api.login_manual(timeout_s=300):
                click.echo("로그인 시간이 초과되었습니다.")
                if force_relogin and backup_cookie is not None:
                    COOKIE_PATH.parent.mkdir(parents=True, exist_ok=True)
                    COOKIE_PATH.write_text(backup_cookie)
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
        click.echo(f"[{_now()}] 로그인 설정 실패: {exc}")
        return False


def _configure_login_interactive() -> None:
    click.echo("\n로그인 설정")

    if not COOKIE_PATH.is_file():
        click.echo("저장된 로그인 세션이 없습니다.")
        if click.confirm("지금 로그인 창을 열까요?", default=True):
            _login_and_save_session(force_relogin=False)
        return

    profile = _cached_login_profile()
    if profile is None:
        click.echo("저장된 세션이 만료되었거나 유효하지 않습니다.")
        if click.confirm("다시 로그인할까요?", default=True):
            _login_and_save_session(force_relogin=True)
        return

    click.echo(f"현재 로그인 정보: {_format_login_profile(profile)}")
    choice = inquirer.list_input(
        message="로그인 정보 처리",
        choices=[
            ("현재 로그인 정보 유지", "keep"),
            ("로그인 정보 변경 (다시 로그인)", "change"),
            ("취소", "cancel"),
        ],
    )
    if choice == "change":
        _login_and_save_session(force_relogin=True)
    elif choice == "keep":
        click.echo("현재 로그인 정보를 유지합니다.")
    else:
        click.echo("로그인 설정을 취소했습니다.")


def _load_visible_stations() -> list[str]:
    station_key = keyring.get_password("KTX", "station")
    if not station_key:
        return list(STATIONS)

    selected = {station.strip() for station in station_key.split(",") if station.strip()}
    ordered = [station for station in STATIONS if station in selected]
    return ordered if ordered else list(STATIONS)


def _set_visible_stations_interactive() -> bool:
    defaults = _load_visible_stations()
    station_info = inquirer.prompt(
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

    selected: list[str] = station_info.get("stations", [])
    if not selected:
        click.echo("선택된 역이 없습니다.")
        return False

    selected_set = set(selected)
    ordered_selected = [station for station in STATIONS if station in selected_set]
    selected_stations = ",".join(ordered_selected)
    keyring.set_password("KTX", "station", selected_stations)
    click.echo(f"선택된 역: {selected_stations}")
    return True


def _prompt_conditions(
    departure: str,
    arrival: str,
    date: str,
    time_str: str,
    adults: int,
    stations: list[str],
) -> tuple[str, str, str, str, int]:
    click.echo("\n대화형 모드: 화살표(↑/↓)로 조회 조건을 선택하세요.")
    if len(stations) < 2:
        click.echo("역 설정에서 최소 2개 역을 선택하세요.")
        sys.exit(1)

    if departure not in stations:
        departure = stations[0]
    if arrival not in stations:
        arrival = stations[1] if len(stations) > 1 else stations[0]
    if departure == arrival and len(stations) > 1:
        arrival = next((station for station in stations if station != departure), stations[0])

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
    while True:
        info = inquirer.prompt(
            [
                inquirer.List(
                    "departure",
                    message="출발역 선택 (↕:이동, Enter: 선택, Ctrl-C: 취소)",
                    choices=stations,
                    default=departure,
                ),
                inquirer.List(
                    "arrival",
                    message="도착역 선택 (↕:이동, Enter: 선택, Ctrl-C: 취소)",
                    choices=stations,
                    default=arrival,
                ),
                inquirer.List(
                    "date",
                    message="출발일 선택 (↕:이동, Enter: 선택, Ctrl-C: 취소)",
                    choices=date_choices,
                    default=date,
                ),
                inquirer.List(
                    "time",
                    message="출발 시각 선택 (↕:이동, Enter: 선택, Ctrl-C: 취소)",
                    choices=time_choices,
                    default=time_str,
                ),
                inquirer.List(
                    "adults",
                    message="인원수 선택 (성인, ↕:이동, Enter: 선택, Ctrl-C: 취소)",
                    choices=adult_choices,
                    default=adults,
                ),
            ]
        )
        if not info:
            click.echo("예매 정보 입력 중 취소되었습니다.")
            sys.exit(0)

        departure = _normalize_station(str(info["departure"]))
        arrival = _normalize_station(str(info["arrival"]))
        if departure == arrival:
            click.echo("입력 오류: 출발역과 도착역은 달라야 합니다.")
            continue

        date = _validate_date(str(info["date"]))
        time_str = _validate_hour(str(info["time"]))
        adults = _validate_adults(int(info["adults"]))
        return departure, arrival, date, time_str, adults


def _prompt_target_trains(
    api: KorailAPI,
    departure: str,
    arrival: str,
    date: str,
    time_str: str,
    adults: int,
) -> list[TrainKey]:
    click.echo("\n예약 시도할 열차를 선택하세요.")
    while True:
        trains = api.search(departure, arrival, date, time_str, adults=adults)
        if not trains:
            click.echo(f"[{_now()}] 초기 조회 결과가 없습니다.")
            if not click.confirm("같은 조건으로 다시 조회할까요?", default=True):
                sys.exit(0)
            continue

        choice = inquirer.prompt(
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

        selected_indices: list[int] = choice.get("trains", [])
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

    choice = inquirer.prompt(
        [
            inquirer.List(
                "seat",
                message="좌석 선호 선택 (↕:이동, Enter: 선택, Ctrl-C: 취소)",
                choices=seat_choices,
                default=default_seat,
            ),
            inquirer.Confirm(
                "auto_pay",
                message="예매 성공 시 카드 자동결제",
                default=default_auto_pay,
            ),
        ]
    )
    if choice is None:
        click.echo("예매 옵션 입력 중 취소되었습니다.")
        sys.exit(0)

    seat = str(choice.get("seat", default_seat))
    auto_pay = bool(choice.get("auto_pay", False))
    if not auto_pay:
        return seat, False, default_smart_ticket

    # Keep smart-ticket behavior as a default/CLI setting without asking in TTY.
    return seat, True, default_smart_ticket


def _resolve_targets(trains: list[Train], targets: list[TrainKey]) -> tuple[list[Train], int]:
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


def _render_screen(status_line: str, target_line: str | None, clear_screen: bool) -> None:
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
            is_waiting = (
                limit_date in {"", "00000000"} or limit_time in {"", "235959"}
            )
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


def _seat_available(train: Train, seat: str) -> bool:
    if seat == "general":
        return train.has_general
    if seat == "special":
        return train.has_special
    if seat == "standing":
        return train.has_standing
    return train.has_any_seat


def _pick_seat(train: Train, seat: str) -> str:
    if seat == "general":
        return "general"
    if seat == "special":
        return "special"
    if seat == "standing":
        return "general"
    if train.has_general:
        return "general"
    return "special"


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
                (train.train_type, 10, "left"),
                (route, 15, "left"),
                (tm, 12, "left"),
                (train.general_seat, 9, "left"),
                (train.special_seat, 9, "left"),
                (train.standing_seat, 9, "left"),
                (price, 7, "right"),
            ]
        )
        click.echo(row)


def _ensure_login(api: KorailAPI, manager: BrowserManager, headless: bool) -> KorailAPI:
    """Ensure the session is authenticated. Returns (possibly new) KorailAPI instance."""
    if api.is_logged_in():
        click.echo(f"[{_now()}] Logged in via saved session.")
        return api

    # Need manual login — must open visible browser
    if headless:
        click.echo(
            f"[{_now()}] No saved session. Restarting browser for manual login..."
        )
        manager.close()
        manager._headless = False
        manager.start()
        api = KorailAPI(manager.page)

    click.echo(f"[{_now()}] Please log in through the browser window (5 min timeout).")
    if not api.login_manual(timeout_s=300):
        click.echo("Login timed out.")
        sys.exit(1)

    manager.save_cookies()
    click.echo(f"[{_now()}] Login successful — session saved.")

    # If we restarted in headed mode but user wanted headless,
    # re-launch headless now that we have cookies.
    if headless:
        manager.close()
        manager._headless = True
        manager.start()
        api = KorailAPI(manager.page)
        if not api.is_logged_in():
            click.echo("Saved session expired immediately. Try --no-headless.")
            sys.exit(1)

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

    card_info = inquirer.prompt(
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

    click.echo(
        "자동결제를 선택했지만 카드 정보가 등록되어 있지 않습니다."
    )
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


def _do_pay(api: KorailAPI, reserve_result: dict[str, object], smart_ticket: bool) -> bool:
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
        if pay_msg and any(token in pay_msg for token in ("오류", "실패", "불가", "invalid", "error")):
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


def _send_telegram(train: Train, reserve_result: dict[str, object], paid: bool) -> None:
    """Send reservation/payment notification via Telegram."""
    token = keyring.get_password("telegram", "token")
    chat_id = keyring.get_password("telegram", "chat_id")
    if not token or not chat_id:
        click.echo(f"[{_now()}] Telegram skipped: token/chat_id not configured.")
        return

    pnr = reserve_result.get("h_pnr_no", "?")
    status = "예약+결제 완료" if paid else "예약 완료 (미결제)"
    dep_date = train.dep_date
    formatted_date = f"{dep_date[:4]}-{dep_date[4:6]}-{dep_date[6:]}" if len(dep_date) == 8 else dep_date
    dep_time = train.dep_time
    formatted_time = f"{dep_time[:2]}:{dep_time[2:4]}" if len(dep_time) >= 4 else dep_time

    text = (
        f"[KTXgo] {status}\n"
        f"{train.train_type} {train.train_no}\n"
        f"{train.departure} → {train.arrival}\n"
        f"{formatted_date} {formatted_time}\n"
        f"PNR: {pnr}"
    )

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
    "--interactive/--no-interactive",
    default=None,
    help="Prompt for date/time/train selection (default: on for TTY)",
)
@click.option("--max-attempts", default=0, show_default=True, help="0 means infinite")
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
@click.option("--auto-pay", is_flag=True, default=False, help="Auto-pay after reservation")
@click.option(
    "--smart-ticket/--no-smart-ticket",
    default=True,
    show_default=True,
    help="Smart-ticket issuance option for auto-pay",
)
@click.option("--telegram", is_flag=True, default=False, help="Send Telegram notification")
def main(
    departure: str,
    arrival: str,
    date: str | None,
    time_str: str | None,
    adults: int,
    headless: bool,
    interactive: bool | None,
    max_attempts: int,
    seat: str,
    set_card_mode: bool,
    auto_pay: bool,
    smart_ticket: bool,
    telegram: bool,
) -> None:
    configure_keyring_backend()

    if set_card_mode:
        if not sys.stdin.isatty():
            raise click.UsageError("--set-card requires a TTY")
        if not _set_card_interactive():
            sys.exit(0)
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

    interactive_mode = sys.stdin.isatty() if interactive is None else interactive
    if interactive_mode and not sys.stdin.isatty():
        raise click.UsageError("--interactive requires a TTY")
    visible_stations = _load_visible_stations()
    if interactive_mode:
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
            if action == "card":
                _set_card_interactive()
                continue
            sys.exit(0)
        departure, arrival, date, time_str, adults = _prompt_conditions(
            departure, arrival, date, time_str, adults, visible_stations
        )

    # Graceful Ctrl+C
    def _sigint(_sig: int, _frame: object) -> None:
        click.echo("\nInterrupted. Exiting.")
        sys.exit(0)

    signal.signal(signal.SIGINT, _sigint)

    with BrowserManager(headless=headless) as manager:
        api = KorailAPI(manager.page)
        api = _ensure_login(api, manager, headless)
        target_trains: list[TrainKey] | None = None
        target_line: str | None = None
        clear_each_attempt = sys.stdout.isatty()

        if interactive_mode:
            while True:
                try:
                    target_trains = _prompt_target_trains(
                        api, departure, arrival, date, time_str, adults
                    )
                    break
                except KorailError as exc:
                    code = exc.code or ""
                    if code in _SESSION_EXPIRED_CODES:
                        click.echo(
                            f"[{_now()}] Session expired before selection. Re-authenticating..."
                        )
                        api = _ensure_login(api, manager, headless)
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
                else:
                    sys.exit(0)

        status_line = (
            f"KTXgo — {departure} → {arrival}  {date} {time_str}:00  "
            f"adults={adults} seat={seat}"
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
                trains = api.search(departure, arrival, date, time_str, adults=adults)
                consecutive_errors = 0
            except KorailError as exc:
                consecutive_errors += 1
                code = exc.code or ""
                if code in _SESSION_EXPIRED_CODES:
                    click.echo(f"[{_now()}] Session expired. Re-authenticating...")
                    api = _ensure_login(api, manager, headless)
                    continue
                click.echo(f"[{_now()}] Search error: {exc}")
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
                candidate_trains, missing_count = _resolve_targets(trains, target_trains)
                if missing_count:
                    click.echo(
                        f"Selected trains not present now: {missing_count}/{len(target_trains)}"
                    )
                if not candidate_trains:
                    time.sleep(POLL_INTERVAL_S)
                    continue

            for train in candidate_trains:
                if not _seat_available(train, seat):
                    continue
                seat_type = _pick_seat(train, seat)
                click.echo(
                    f"\n[{_now()}] Seat found: {train.train_no} "
                    f"({train.dep_time}). Reserving ({seat_type})..."
                )
                try:
                    result = api.reserve(train, seat_type=seat_type, adults=adults)
                except KorailError as exc:
                    code = exc.code or ""
                    if code in _SESSION_EXPIRED_CODES:
                        click.echo(
                            f"[{_now()}] Session expired during reserve. Re-authenticating..."
                        )
                        api = _ensure_login(api, manager, headless)
                        break  # Restart search loop
                    click.echo(f"  → Reserve failed: {exc}")
                    continue

                _print_success_banner("Reservation successful!")
                for key in ("h_pnr_no", "h_rsv_no", "strResult", "h_msg_txt"):
                    if key in result:
                        click.echo(f"  {key}: {result[key]}")

                # Auto-pay
                paid = False
                if auto_pay:
                    paid = _do_pay(api, result, smart_ticket)

                # Telegram notification
                if telegram:
                    _send_telegram(train, result, paid)

                return

            time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    main()
