from __future__ import annotations

import configparser
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import COOKIE_PATH, DATA_DIR, STORAGE_STATE_PATH

_KORAIL_DOMAIN_SUFFIX = "korail.com"


def import_korail_cookies(path: str | Path) -> int:
    """Import Korail cookies from a browser export into KTXgo session files.

    Supported formats:
    - Netscape cookies.txt
    - JSON list of cookies exported by browser extensions
    - Playwright storage_state JSON with a top-level ``cookies`` list
    """
    source = Path(path)
    cookies = load_korail_cookies(source)
    if not cookies:
        return 0
    save_korail_cookies(cookies)
    return len(cookies)


def import_firefox_korail_cookies(profile_dir: str | Path) -> int:
    """Import Korail cookies from a Firefox profile directory."""
    cookies = load_firefox_korail_cookies(profile_dir)
    if not cookies:
        return 0
    save_korail_cookies(cookies)
    return len(cookies)


def default_firefox_profile_dir() -> Path | None:
    """Return the default Firefox profile directory from profiles.ini, if found."""
    firefox_home = Path.home() / ".mozilla" / "firefox"
    profiles_ini = firefox_home / "profiles.ini"
    if not profiles_ini.is_file():
        return None

    parser = configparser.ConfigParser()
    parser.read(profiles_ini)
    profile_sections = [
        section for section in parser.sections() if section.lower().startswith("profile")
    ]
    selected = None
    for section in profile_sections:
        if parser.get(section, "Default", fallback="0") == "1":
            selected = section
            break
    if selected is None and profile_sections:
        selected = profile_sections[0]
    if selected is None:
        return None

    raw_path = parser.get(selected, "Path", fallback="").strip()
    if not raw_path:
        return None
    is_relative = parser.get(selected, "IsRelative", fallback="1") == "1"
    path = firefox_home / raw_path if is_relative else Path(raw_path).expanduser()
    return path


def load_firefox_korail_cookies(profile_dir: str | Path) -> list[dict[str, Any]]:
    profile_path = Path(profile_dir).expanduser()
    source_db = profile_path / "cookies.sqlite"
    cookies: list[dict[str, Any]] = []
    if not source_db.is_file():
        return _dedupe_cookies(load_firefox_sessionstore_korail_cookies(profile_path))

    try:
        sqlite_cookies = _read_firefox_cookies(source_db)
        cookies.extend(cookie for cookie in sqlite_cookies if _is_korail_cookie(cookie))
    except sqlite3.Error:
        pass

    if not cookies:
        with tempfile.TemporaryDirectory(prefix="ktxgo-firefox-cookies-") as temp_dir:
            temp_path = Path(temp_dir)
            copied_db = temp_path / "cookies.sqlite"
            _copy_firefox_cookie_db(source_db, copied_db)
            copied_cookies = _read_firefox_cookies(copied_db)
        cookies.extend(cookie for cookie in copied_cookies if _is_korail_cookie(cookie))

    cookies.extend(load_firefox_sessionstore_korail_cookies(profile_path))
    return _dedupe_cookies(cookies)


def load_firefox_sessionstore_korail_cookies(profile_dir: str | Path) -> list[dict[str, Any]]:
    profile_path = Path(profile_dir).expanduser()
    cookies: list[dict[str, Any]] = []
    for path in (
        profile_path / "sessionstore.jsonlz4",
        profile_path / "sessionstore-backups" / "recovery.jsonlz4",
        profile_path / "sessionstore-backups" / "previous.jsonlz4",
    ):
        if not path.is_file():
            continue
        try:
            data = _read_mozlz4_json(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        raw_cookies = data.get("cookies", []) if isinstance(data, dict) else []
        if not isinstance(raw_cookies, list):
            continue
        for item in raw_cookies:
            if not isinstance(item, dict):
                continue
            cookie = _normalize_firefox_sessionstore_cookie(item)
            if cookie is not None and _is_korail_cookie(cookie):
                cookies.append(cookie)
    return _dedupe_cookies(cookies)


def load_korail_cookies(path: str | Path) -> list[dict[str, Any]]:
    text = Path(path).read_text()
    stripped = text.lstrip()
    if stripped.startswith("[") or stripped.startswith("{"):
        cookies = _load_json_cookies(stripped)
    else:
        cookies = _load_netscape_cookies(text)
    return [cookie for cookie in cookies if _is_korail_cookie(cookie)]


def _read_firefox_cookies(db_path: Path) -> list[dict[str, Any]]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=0.5)
    con.row_factory = sqlite3.Row
    try:
        columns = {
            row[1]
            for row in con.execute("PRAGMA table_info(moz_cookies)").fetchall()
        }
        optional_columns = [
            column
            for column in ("expiry", "isSecure", "isHttpOnly", "isSession")
            if column in columns
        ]
        select_columns = ["host", "name", "value", "path", *optional_columns]
        rows = con.execute(
            f"SELECT {', '.join(select_columns)} FROM moz_cookies"
        ).fetchall()
    finally:
        con.close()

    cookies: list[dict[str, Any]] = []
    for row in rows:
        name = str(row["name"] or "")
        if not name:
            continue
        cookie: dict[str, Any] = {
            "name": name,
            "value": str(row["value"] or ""),
            "domain": str(row["host"] or ""),
            "path": str(row["path"] or "/"),
            "expires": -1
            if "isSession" in row.keys() and int(row["isSession"] or 0)
            else _normalize_expires(row["expiry"] if "expiry" in row.keys() else -1),
            "httpOnly": bool(row["isHttpOnly"]) if "isHttpOnly" in row.keys() else False,
            "secure": bool(row["isSecure"]) if "isSecure" in row.keys() else False,
        }
        cookies.append(cookie)
    return cookies


def _normalize_firefox_sessionstore_cookie(item: dict[str, Any]) -> dict[str, Any] | None:
    name = str(item.get("name") or "")
    host = str(item.get("host") or "")
    if not name or not host:
        return None
    cookie: dict[str, Any] = {
        "name": name,
        "value": str(item.get("value") or ""),
        "domain": host,
        "path": str(item.get("path") or "/"),
        "expires": _normalize_expires(item.get("expiry", item.get("expires", -1))),
        "httpOnly": bool(item.get("httponly", item.get("httpOnly", False))),
        "secure": bool(item.get("secure", False)),
    }
    same_site = _normalize_firefox_same_site(item.get("sameSite"))
    if same_site is not None:
        cookie["sameSite"] = same_site
    return cookie


def _normalize_firefox_same_site(value: Any) -> str | None:
    if value in (None, 0, 256):
        return None
    if value == 1:
        return "Lax"
    if value == 2:
        return "Strict"
    if value == 3:
        return "None"
    return None


def _dedupe_cookies(cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for cookie in cookies:
        key = (
            str(cookie.get("domain") or cookie.get("url") or ""),
            str(cookie.get("path") or "/"),
            str(cookie.get("name") or ""),
        )
        deduped[key] = cookie
    return list(deduped.values())


def _read_mozlz4_json(path: Path) -> Any:
    data = path.read_bytes()
    if not data.startswith(b"mozLz40\0") or len(data) < 12:
        raise ValueError("not a Firefox mozlz4 file")
    expected_size = int.from_bytes(data[8:12], "little")
    raw = _decompress_lz4_block(data[12:], expected_size=expected_size)
    return json.loads(raw.decode("utf-8"))


def _decompress_lz4_block(data: bytes, *, expected_size: int | None = None) -> bytes:
    output = bytearray()
    index = 0
    data_len = len(data)
    while index < data_len:
        token = data[index]
        index += 1

        literal_len = token >> 4
        if literal_len == 15:
            while index < data_len:
                value = data[index]
                index += 1
                literal_len += value
                if value != 255:
                    break
        output.extend(data[index : index + literal_len])
        index += literal_len
        if index >= data_len:
            break

        if index + 2 > data_len:
            raise ValueError("truncated lz4 offset")
        offset = data[index] | (data[index + 1] << 8)
        index += 2
        if offset <= 0 or offset > len(output):
            raise ValueError("invalid lz4 offset")

        match_len = token & 0x0F
        if match_len == 15:
            while index < data_len:
                value = data[index]
                index += 1
                match_len += value
                if value != 255:
                    break
        match_len += 4
        start = len(output) - offset
        for pos in range(match_len):
            output.append(output[start + pos])

    if expected_size is not None and len(output) != expected_size:
        raise ValueError("unexpected lz4 output size")
    return bytes(output)


def _write_mozlz4_json(path: Path, data: Any) -> None:
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    path.write_bytes(b"mozLz40\0" + len(raw).to_bytes(4, "little") + _literal_lz4_block(raw))


def _literal_lz4_block(raw: bytes) -> bytes:
    length = len(raw)
    if length < 15:
        return bytes([length << 4]) + raw
    extra = length - 15
    encoded = bytearray([0xF0])
    while extra >= 255:
        encoded.append(255)
        extra -= 255
    encoded.append(extra)
    encoded.extend(raw)
    return bytes(encoded)


def _copy_firefox_cookie_db(source_db: Path, destination_db: Path) -> None:
    try:
        source = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True, timeout=2.0)
        destination = sqlite3.connect(destination_db)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        return
    except sqlite3.Error:
        pass

    shutil.copy2(source_db, destination_db)
    for suffix in ("-wal", "-shm"):
        sidecar = source_db.with_name(source_db.name + suffix)
        if sidecar.exists():
            shutil.copy2(sidecar, destination_db.with_name(destination_db.name + suffix))


def save_korail_cookies(cookies: list[dict[str, Any]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    COOKIE_PATH.write_text(json.dumps(cookies, ensure_ascii=False, indent=2))
    COOKIE_PATH.chmod(0o600)
    STORAGE_STATE_PATH.write_text(
        json.dumps({"cookies": cookies, "origins": []}, ensure_ascii=False, indent=2)
    )
    STORAGE_STATE_PATH.chmod(0o600)
    try:
        DATA_DIR.chmod(0o700)
    except OSError:
        pass


def _load_json_cookies(text: str) -> list[dict[str, Any]]:
    raw = json.loads(text)
    if isinstance(raw, dict):
        raw_cookies = raw.get("cookies", [])
    else:
        raw_cookies = raw
    if not isinstance(raw_cookies, list):
        return []

    cookies: list[dict[str, Any]] = []
    for item in raw_cookies:
        if not isinstance(item, dict):
            continue
        cookie = _normalize_json_cookie(item)
        if cookie is not None:
            cookies.append(cookie)
    return cookies


def _normalize_json_cookie(item: dict[str, Any]) -> dict[str, Any] | None:
    name = str(item.get("name", ""))
    if not name:
        return None
    value = str(item.get("value", ""))
    domain = item.get("domain")
    url = item.get("url")
    path = str(item.get("path") or "/")

    cookie: dict[str, Any] = {"name": name, "value": value, "path": path}
    if domain:
        cookie["domain"] = str(domain)
    elif url:
        cookie["url"] = str(url)
    else:
        return None

    expires = item.get("expires", item.get("expirationDate"))
    if expires is not None:
        cookie["expires"] = _normalize_expires(expires)
    for key in ("httpOnly", "secure"):
        if key in item:
            cookie[key] = bool(item[key])
    same_site = _normalize_same_site(item.get("sameSite"))
    if same_site is not None:
        cookie["sameSite"] = same_site
    return cookie


def _load_netscape_cookies(text: str) -> list[dict[str, Any]]:
    cookies: list[dict[str, Any]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        http_only = False
        if line.startswith("#HttpOnly_"):
            http_only = True
            line = line.removeprefix("#HttpOnly_")
        elif line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 7:
            continue
        domain, _include_subdomains, path, secure, expires, name, value = parts
        cookie: dict[str, Any] = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": path or "/",
            "expires": _normalize_expires(expires),
            "httpOnly": http_only,
            "secure": secure.upper() == "TRUE",
        }
        cookies.append(cookie)
    return cookies


def _normalize_expires(value: Any) -> int:
    try:
        expires = int(float(value))
    except (TypeError, ValueError):
        return -1
    # Some browser stores/exporters expose milliseconds. Playwright requires
    # Unix timestamps in seconds.
    if expires > 100_000_000_000:
        expires //= 1000
    # Playwright uses -1 for session cookies; many Netscape exporters use 0.
    return expires if expires > 0 else -1


def _normalize_same_site(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower().replace("-", "_")
    mapping = {
        "strict": "Strict",
        "lax": "Lax",
        "none": "None",
        "no_restriction": "None",
        "unspecified": None,
    }
    return mapping.get(normalized)


def _is_korail_cookie(cookie: dict[str, Any]) -> bool:
    domain = str(cookie.get("domain") or "").lstrip(".").lower()
    if domain and domain.endswith(_KORAIL_DOMAIN_SUFFIX):
        return True
    url = str(cookie.get("url") or "")
    if not url:
        return False
    hostname = (urlparse(url).hostname or "").lstrip(".").lower()
    return hostname.endswith(_KORAIL_DOMAIN_SUFFIX)
