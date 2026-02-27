from __future__ import annotations

import asyncio
import signal
import sys
import time
from datetime import datetime, timedelta

import click
import keyring

from .browser import BrowserManager
from .config import DEFAULT_ARRIVAL, DEFAULT_DEPARTURE, POLL_INTERVAL_S, STATIONS
from .korail import KorailAPI, KorailError, Train
# Session-expired error codes returned by Korail.
_SESSION_EXPIRED_CODES = {"P058", "WRT300004", "WRD000003"}

# Number of header lines to preserve when clearing (banner + login status).
_HEADER_LINES = 3


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


def _clear_below_header() -> None:
    """Move cursor up and clear everything below the header."""
    sys.stdout.write(f"\033[{_HEADER_LINES + 1};1H\033[J")
    sys.stdout.flush()

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
    max_attempts: int,
    seat: str,
    auto_pay: bool,
    telegram: bool,
) -> None:
    departure = _normalize_station(departure)
    arrival = _normalize_station(arrival)
    if departure == arrival:
        raise click.BadParameter("departure and arrival must be different")

    date = date or _fmt_date()
    time_str = time_str or _fmt_hour()

    click.echo(f"KTXgo — {departure} → {arrival}  {date} {time_str}:00  seat={seat}"
              f"{' auto-pay' if auto_pay else ''}{' telegram' if telegram else ''}")

    # Graceful Ctrl+C
    def _sigint(_sig: int, _frame: object) -> None:
        click.echo("\nInterrupted. Exiting.")
        sys.exit(0)

    signal.signal(signal.SIGINT, _sigint)

    with BrowserManager(headless=headless) as manager:
        api = KorailAPI(manager.page)
        api = _ensure_login(api, manager, headless)

        attempt = 0
        consecutive_errors = 0
        while max_attempts == 0 or attempt < max_attempts:
            attempt += 1

            # Clear previous output (keep header)
            _clear_below_header()

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

            for train in trains:
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
