from __future__ import annotations

import json

from click.testing import CliRunner

from ktxgo import cli
from ktxgo import cookie_import


def test_import_netscape_cookies_filters_korail_and_writes_playwright_state(tmp_path, monkeypatch) -> None:
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text(
        "# Netscape HTTP Cookie File\n"
        "#HttpOnly_.korail.com\tTRUE\t/\tTRUE\t2147483647\tJSESSIONID\tabc123\n"
        ".example.com\tTRUE\t/\tFALSE\t2147483647\tunrelated\tignore\n"
    )
    cookie_path = tmp_path / "cookies.json"
    storage_path = tmp_path / "storage_state.json"
    monkeypatch.setattr(cookie_import, "COOKIE_PATH", cookie_path)
    monkeypatch.setattr(cookie_import, "STORAGE_STATE_PATH", storage_path)
    monkeypatch.setattr(cookie_import, "DATA_DIR", tmp_path)

    count = cookie_import.import_korail_cookies(cookie_file)

    assert count == 1
    cookies = json.loads(cookie_path.read_text())
    assert cookies == [
        {
            "name": "JSESSIONID",
            "value": "abc123",
            "domain": ".korail.com",
            "path": "/",
            "expires": 2147483647,
            "httpOnly": True,
            "secure": True,
        }
    ]
    storage_state = json.loads(storage_path.read_text())
    assert storage_state == {"cookies": cookies, "origins": []}


def test_import_json_cookie_export_normalizes_extension_fields(tmp_path, monkeypatch) -> None:
    cookie_file = tmp_path / "cookies.json"
    cookie_file.write_text(
        json.dumps(
            [
                {
                    "name": "KORAIL_SESSION",
                    "value": "xyz",
                    "domain": "www.korail.com",
                    "path": "/",
                    "expirationDate": 2000000000,
                    "httpOnly": False,
                    "secure": True,
                    "sameSite": "no_restriction",
                },
                {
                    "name": "skip",
                    "value": "1",
                    "domain": "example.com",
                    "path": "/",
                },
            ]
        )
    )
    cookie_path = tmp_path / "cookies.json.out"
    storage_path = tmp_path / "storage_state.json"
    monkeypatch.setattr(cookie_import, "COOKIE_PATH", cookie_path)
    monkeypatch.setattr(cookie_import, "STORAGE_STATE_PATH", storage_path)
    monkeypatch.setattr(cookie_import, "DATA_DIR", tmp_path)

    count = cookie_import.import_korail_cookies(cookie_file)

    assert count == 1
    assert json.loads(cookie_path.read_text()) == [
        {
            "name": "KORAIL_SESSION",
            "value": "xyz",
            "domain": "www.korail.com",
            "path": "/",
            "expires": 2000000000,
            "httpOnly": False,
            "secure": True,
            "sameSite": "None",
        }
    ]


def test_cli_import_cookies_exits_before_starting_browser(tmp_path, monkeypatch) -> None:
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text(".korail.com\tTRUE\t/\tTRUE\t2147483647\tA\tB\n")
    imported: list[object] = []

    monkeypatch.setattr(cli, "import_korail_cookies", lambda path: imported.append(path) or 1)
    monkeypatch.setattr(
        cli,
        "BrowserManager",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("import-only mode must not start a browser")
        ),
    )

    result = CliRunner().invoke(cli.main, ["--import-cookies", str(cookie_file)])

    assert result.exit_code == 0, result.output
    assert imported == [cookie_file]
    assert "Imported 1 Korail cookie" in result.output


def test_import_firefox_cookies_reads_sqlite_profile(tmp_path, monkeypatch) -> None:
    import sqlite3

    profile = tmp_path / "firefox-profile"
    profile.mkdir()
    db_path = profile / "cookies.sqlite"
    con = sqlite3.connect(db_path)
    con.execute(
        "CREATE TABLE moz_cookies ("
        "id INTEGER PRIMARY KEY, "
        "host TEXT, name TEXT, value TEXT, path TEXT, "
        "expiry INTEGER, isSecure INTEGER, isHttpOnly INTEGER, "
        "isSession INTEGER)"
    )
    con.execute(
        "INSERT INTO moz_cookies "
        "(host, name, value, path, expiry, isSecure, isHttpOnly, isSession) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("www.korail.com", "JSESSIONID", "abc", "/", 1893456000, 0, 1, 1),
    )
    con.execute(
        "INSERT INTO moz_cookies "
        "(host, name, value, path, expiry, isSecure, isHttpOnly, isSession) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("www.korail.com", "WMONID", "def", "/", 1900000000000, 0, 0, 0),
    )
    con.execute(
        "INSERT INTO moz_cookies "
        "(host, name, value, path, expiry, isSecure, isHttpOnly, isSession) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("example.com", "skip", "no", "/", 1900000000, 0, 0, 0),
    )
    con.commit()
    con.close()

    cookie_path = tmp_path / "cookies.json"
    storage_path = tmp_path / "storage_state.json"
    monkeypatch.setattr(cookie_import, "COOKIE_PATH", cookie_path)
    monkeypatch.setattr(cookie_import, "STORAGE_STATE_PATH", storage_path)
    monkeypatch.setattr(cookie_import, "DATA_DIR", tmp_path)

    count = cookie_import.import_firefox_korail_cookies(profile)

    assert count == 2
    assert json.loads(cookie_path.read_text()) == [
        {
            "name": "JSESSIONID",
            "value": "abc",
            "domain": "www.korail.com",
            "path": "/",
            "expires": -1,
            "httpOnly": True,
            "secure": False,
        },
        {
            "name": "WMONID",
            "value": "def",
            "domain": "www.korail.com",
            "path": "/",
            "expires": 1900000000,
            "httpOnly": False,
            "secure": False,
        },
    ]


def test_import_firefox_cookies_merges_sessionstore_session_cookies(
    tmp_path, monkeypatch
) -> None:
    import sqlite3

    profile = tmp_path / "firefox-profile"
    profile.mkdir()
    db_path = profile / "cookies.sqlite"
    con = sqlite3.connect(db_path)
    con.execute(
        "CREATE TABLE moz_cookies ("
        "id INTEGER PRIMARY KEY, "
        "host TEXT, name TEXT, value TEXT, path TEXT, "
        "expiry INTEGER, isSecure INTEGER, isHttpOnly INTEGER)"
    )
    con.execute(
        "INSERT INTO moz_cookies "
        "(host, name, value, path, expiry, isSecure, isHttpOnly) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("www.korail.com", "WMONID", "persisted", "/", 1900000000000, 0, 0),
    )
    con.commit()
    con.close()
    cookie_import._write_mozlz4_json(
        profile / "sessionstore.jsonlz4",
        {
            "cookies": [
                {
                    "host": "www.korail.com",
                    "name": "JSESSIONID",
                    "value": "session",
                    "path": "/",
                    "httponly": True,
                    "secure": False,
                }
            ]
        },
    )

    cookie_path = tmp_path / "cookies.json"
    storage_path = tmp_path / "storage_state.json"
    monkeypatch.setattr(cookie_import, "COOKIE_PATH", cookie_path)
    monkeypatch.setattr(cookie_import, "STORAGE_STATE_PATH", storage_path)
    monkeypatch.setattr(cookie_import, "DATA_DIR", tmp_path)

    count = cookie_import.import_firefox_korail_cookies(profile)

    assert count == 2
    assert json.loads(cookie_path.read_text()) == [
        {
            "name": "WMONID",
            "value": "persisted",
            "domain": "www.korail.com",
            "path": "/",
            "expires": 1900000000,
            "httpOnly": False,
            "secure": False,
        },
        {
            "name": "JSESSIONID",
            "value": "session",
            "domain": "www.korail.com",
            "path": "/",
            "expires": -1,
            "httpOnly": True,
            "secure": False,
        },
    ]


def test_default_firefox_profile_from_profiles_ini(tmp_path, monkeypatch) -> None:
    firefox_home = tmp_path / ".mozilla" / "firefox"
    profile = firefox_home / "abcd.default-release"
    profile.mkdir(parents=True)
    (firefox_home / "profiles.ini").write_text(
        "[Profile0]\n"
        "Name=default-release\n"
        "IsRelative=1\n"
        "Path=abcd.default-release\n"
        "Default=1\n"
    )
    monkeypatch.setattr(cookie_import.Path, "home", lambda: tmp_path)

    assert cookie_import.default_firefox_profile_dir() == profile


def test_cli_external_firefox_login_launches_firefox_and_imports_profile(tmp_path, monkeypatch) -> None:
    profile = tmp_path / "ktxgo-firefox-profile"
    launched: list[list[str]] = []
    popen_kwargs: list[dict[str, object]] = []
    imported: list[object] = []
    pause_messages: list[str | None] = []

    class DummyProcess:
        def poll(self):
            return None

    monkeypatch.setattr(
        cli.subprocess,
        "Popen",
        lambda args, **kwargs: launched.append([str(arg) for arg in args])
        or popen_kwargs.append(kwargs)
        or DummyProcess(),
    )
    monkeypatch.setattr(cli, "configure_keyring_backend", lambda: None)
    monkeypatch.setattr(cli.click, "pause", lambda info=None: pause_messages.append(info))
    monkeypatch.setattr(
        cli,
        "colored",
        lambda text, *args, **kwargs: f"COLORED:{text}",
    )
    monkeypatch.setattr(
        cli,
        "import_firefox_korail_cookies",
        lambda path: imported.append(path) or 2,
    )
    monkeypatch.setattr(
        cli,
        "BrowserManager",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("external-login import-only mode must not start Playwright")
        ),
    )

    result = CliRunner().invoke(
        cli.main,
        [
            "--external-firefox-login",
            "--external-firefox",
            "/usr/bin/firefox",
            "--external-firefox-profile",
            str(profile),
        ],
    )

    assert result.exit_code == 0, result.output
    assert launched == [
        [
            "/usr/bin/firefox",
            "--no-remote",
            "--profile",
            str(profile),
            "https://www.korail.com/ticket/login",
        ]
    ]
    assert popen_kwargs == [
        {
            "stdin": cli.subprocess.DEVNULL,
            "stdout": cli.subprocess.DEVNULL,
            "stderr": cli.subprocess.DEVNULL,
            "start_new_session": True,
        }
    ]
    assert imported == [profile]
    assert pause_messages == [
        "COLORED:Korail 로그인을 완료한 뒤 창을 닫고, 이 터미널에서 Enter를 누르세요."
    ]
    assert "Imported 2 Korail cookies from Firefox profile" in result.output


def test_default_external_firefox_profile_prefers_snap_firefox_area(tmp_path, monkeypatch) -> None:
    snap_firefox_home = tmp_path / "snap" / "firefox" / "common" / ".mozilla" / "firefox"
    snap_firefox_home.mkdir(parents=True)
    monkeypatch.setattr(cli.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(cli, "DATA_DIR", tmp_path / ".ktxgo")

    assert cli._default_external_firefox_profile_dir() == (
        snap_firefox_home / "ktxgo-login-profile"
    )
