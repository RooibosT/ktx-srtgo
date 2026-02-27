from __future__ import annotations

import asyncio
import signal
import sys
import time
from datetime import datetime, timedelta

import click
import inquirer
import keyring

from .browser import BrowserManager
from .config import DEFAULT_ARRIVAL, DEFAULT_DEPARTURE, POLL_INTERVAL_S, STATIONS
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


def _train_choice_label(idx: int, train: Train) -> str:
    return (
        f"[{idx:>2}] {train.train_no:<5} {train.dep_time}-{train.arr_time} "
        f"{train.departure}->{train.arrival} "
        f"일반:{train.general_seat} 특실:{train.special_seat} 입석:{train.standing_seat}"
    )


def _prompt_conditions(
    departure: str,
    arrival: str,
    date: str,
    time_str: str,
    seat: str,
) -> tuple[str, str, str, str, str]:
    click.echo("\n대화형 모드: 화살표(↑/↓)로 조회 조건을 선택하세요.")
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
    seat_choices = [
        ("일반석", "general"),
        ("특석", "special"),
        ("모두 (일반석/특석)", "any"),
        ("입석/자유석", "standing"),
    ]

    while True:
        info = inquirer.prompt(
            [
                inquirer.List(
                    "departure",
                    message="출발역 선택 (↕:이동, Enter: 선택, Ctrl-C: 취소)",
                    choices=STATIONS,
                    default=departure,
                ),
                inquirer.List(
                    "arrival",
                    message="도착역 선택 (↕:이동, Enter: 선택, Ctrl-C: 취소)",
                    choices=STATIONS,
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
                    "seat",
                    message="좌석 선호 선택 (↕:이동, Enter: 선택, Ctrl-C: 취소)",
                    choices=seat_choices,
                    default=seat,
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
        seat = str(info["seat"])
        return departure, arrival, date, time_str, seat


def _prompt_target_trains(
    api: KorailAPI,
    departure: str,
    arrival: str,
    date: str,
    time_str: str,
) -> list[TrainKey]:
    click.echo("\n예약 시도할 열차를 선택하세요.")
    while True:
        trains = api.search(departure, arrival, date, time_str, adults=1)
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
    header = (
        "idx train    type       dep->arr        time         "
        "gen       spe       stnd      price"
    )
    click.echo(header)
    click.echo("-" * len(header))
    for idx, train in enumerate(trains):
        route = f"{train.departure}->{train.arrival}"
        tm = f"{train.dep_time}-{train.arr_time}"
        price = train.price.lstrip("0") or "0"
        row = (
            f"{idx:>3} {train.train_no:<8} {train.train_type[:10]:<10} "
            f"{route[:14]:<14} {tm[:12]:<12}   "
            f"{train.general_seat[:8]:<9} {train.special_seat[:8]:<9} "
            f"{train.standing_seat[:8]:<9} {price}"
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


def _do_pay(api: KorailAPI, reserve_result: dict[str, object]) -> bool:
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

    click.echo(f"[{_now()}] Paying with card ending ...{card['card_number'][-4:]}")
    try:
        pay_result = api.pay(
            reserve_result,
            card_number=card["card_number"],
            card_password=card["card_password"],
            birthday=card["birthday"],
            card_expire=card["card_expire"],
        )
        click.echo(f"\n{'=' * 50}")
        click.echo("Payment successful!")
        click.echo(f"{'=' * 50}")
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
@click.option("--auto-pay", is_flag=True, default=False, help="Auto-pay after reservation")
@click.option("--telegram", is_flag=True, default=False, help="Send Telegram notification")
def main(
    departure: str,
    arrival: str,
    date: str | None,
    time_str: str | None,
    headless: bool,
    interactive: bool | None,
    max_attempts: int,
    seat: str,
    auto_pay: bool,
    telegram: bool,
) -> None:
    departure = _normalize_station(departure)
    arrival = _normalize_station(arrival)
    if departure == arrival:
        raise click.BadParameter("departure and arrival must be different")

    if date is not None:
        date = _validate_date(date)
    if time_str is not None:
        time_str = _validate_hour(time_str)
    date = date or _fmt_date()
    time_str = time_str or _fmt_hour()

    interactive_mode = sys.stdin.isatty() if interactive is None else interactive
    if interactive_mode and not sys.stdin.isatty():
        raise click.UsageError("--interactive requires a TTY")
    if interactive_mode:
        departure, arrival, date, time_str, seat = _prompt_conditions(
            departure, arrival, date, time_str, seat
        )

    status_line = (
        f"KTXgo — {departure} → {arrival}  {date} {time_str}:00  seat={seat}"
        f"{' auto-pay' if auto_pay else ''}{' telegram' if telegram else ''}"
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
                        api, departure, arrival, date, time_str
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

        if not clear_each_attempt:
            _render_screen(status_line, target_line, clear_screen=False)

        attempt = 0
        consecutive_errors = 0
        while max_attempts == 0 or attempt < max_attempts:
            attempt += 1

            if clear_each_attempt:
                _render_screen(status_line, target_line, clear_screen=True)

            try:
                trains = api.search(departure, arrival, date, time_str, adults=1)
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
                    result = api.reserve(train, seat_type=seat_type, adults=1)
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

                click.echo(f"\n{'=' * 50}")
                click.echo("Reservation successful!")
                click.echo(f"{'=' * 50}")
                for key in ("h_pnr_no", "h_rsv_no", "strResult", "h_msg_txt"):
                    if key in result:
                        click.echo(f"  {key}: {result[key]}")

                # Auto-pay
                paid = False
                if auto_pay:
                    paid = _do_pay(api, result)

                # Telegram notification
                if telegram:
                    _send_telegram(train, result, paid)

                return

            time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    main()
