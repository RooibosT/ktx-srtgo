from __future__ import annotations

import json
import sqlite3
import threading
import time

import pytest
import requests

from ktxgo import cli
from ktxgo.config import API_LOGIN_CHECK, API_RESERVE, API_SCHEDULE
from ktxgo.extension_backend import (
    ExtensionBrowserRunner,
    ExtensionControlServer,
    ExtensionKorailAPI,
    _chrome_cookie_expires_utc,
    _persist_profile_session_cookies,
    extension_login_cookie_cache_available,
    _profile_process_ids,
    write_extension_files,
)
from ktxgo.korail import KorailError


def test_extension_api_reuses_korail_search_parser() -> None:
    captured: dict[str, object] = {}

    class FakeRunner:
        def api_call(self, endpoint: str, params: dict[str, str]) -> dict[str, object]:
            captured["endpoint"] = endpoint
            captured["params"] = params
            return {
                "strResult": "SUCC",
                "trn_infos": {
                    "trn_info": [
                        {
                            "h_trn_no": "001",
                            "h_trn_clsf_nm": "KTX",
                            "h_trn_gp_cd": "100",
                            "h_dpt_rs_stn_nm": "서울",
                            "h_arv_rs_stn_nm": "부산",
                            "h_dpt_dt": "20260422",
                            "h_dpt_tm_qb": "06:00",
                            "h_arv_tm_qb": "08:30",
                            "h_gen_rsv_cd": "11",
                            "h_spe_rsv_cd": "13",
                        }
                    ]
                },
            }

    api = ExtensionKorailAPI(FakeRunner())

    trains = api.search("서울", "부산", "20260422", "06", train_types=("ktx",))

    assert captured["endpoint"] == API_SCHEDULE
    assert captured["params"]["txtGoStart"] == "서울"
    assert trains[0].train_no == "001"
    assert trains[0].has_general


def test_extension_api_raises_korail_error_on_failed_payload() -> None:
    class FakeRunner:
        def api_call(self, endpoint: str, params: dict[str, str]) -> dict[str, object]:
            return {
                "strResult": "FAIL",
                "h_msg_cd": "MACRO ERROR",
                "h_msg_txt": "blocked",
            }

    api = ExtensionKorailAPI(FakeRunner())

    with pytest.raises(KorailError) as exc_info:
        api.search("서울", "부산", "20260422", "06", train_types=("ktx",))

    assert exc_info.value.code == "MACRO ERROR"


def test_extension_api_raises_korail_error_on_dynapath_payload() -> None:
    class FakeRunner:
        def api_call(self, endpoint: str, params: dict[str, str]) -> dict[str, object]:
            return {
                "errCode": "macro_err1",
                "errMsg": "blocked by DynaPath",
                "dynaPathResultCode": "-4001",
            }

    api = ExtensionKorailAPI(FakeRunner())

    with pytest.raises(KorailError) as exc_info:
        api.search("서울", "부산", "20260422", "06", train_types=("ktx",))

    assert exc_info.value.code == "macro_err1"
    assert "DynaPath" in str(exc_info.value)


def test_extension_search_skips_train_class_without_direct_service() -> None:
    requested_codes: list[str] = []

    class FakeRunner:
        def api_call(self, endpoint: str, params: dict[str, str]) -> dict[str, object]:
            train_code = params["selGoTrain"]
            requested_codes.append(train_code)
            if train_code == "100":
                return {
                    "strResult": "FAIL",
                    "h_msg_cd": "NO_DIRECT",
                    "h_msg_txt": "직통열차는 없지만, 환승으로 조회 가능합니다.",
                }
            return {
                "strResult": "SUCC",
                "trn_infos": {
                    "trn_info": [
                        {
                            "h_trn_no": "1002",
                            "h_trn_clsf_nm": "ITX-새마을",
                            "h_trn_gp_cd": "101",
                            "h_dpt_rs_stn_nm": "대전",
                            "h_arv_rs_stn_nm": "수원",
                            "h_dpt_dt": "20260714",
                            "h_dpt_tm_qb": "09:22",
                            "h_arv_tm_qb": "10:34",
                            "h_gen_rsv_cd": "11",
                            "h_spe_rsv_cd": "13",
                        }
                    ]
                },
            }

    api = ExtensionKorailAPI(FakeRunner())

    trains = api.search(
        "대전",
        "수원",
        "20260714",
        "09",
        train_types=("ktx", "itx-saemaeul"),
    )

    assert requested_codes == ["100", "101"]
    assert [train.train_no for train in trains] == ["1002"]


def test_extension_files_inject_page_context_xmlhttprequest(tmp_path) -> None:
    write_extension_files(tmp_path, control_origin="http://127.0.0.1:12345")

    manifest = (tmp_path / "manifest.json").read_text()
    content = (tmp_path / "content.js").read_text()
    page = (tmp_path / "page.js").read_text()

    assert "https://www.korail.com/*" in manifest
    assert '"run_at": "document_end"' in manifest
    assert '"cookies"' in manifest
    assert "web_accessible_resources" in manifest
    assert "chrome.runtime.getURL(\"page.js\")" in content
    assert "queuedPageCommands" in content
    assert "pageReady = true" in content
    assert "window.postMessage" in content
    assert "KTXGO_GET_COOKIES" in content
    assert "KTXGO_GET_COOKIES" in (tmp_path / "background.js").read_text()
    assert "document.cookie" in page
    assert "/command?wait=25" in content
    assert "setInterval(poll, 300)" not in content
    assert "new XMLHttpRequest()" in page
    assert "xhr.timeout = Number(command.timeoutMs || 30000)" in page
    assert "xhr.ontimeout" in page
    assert "/classes/com.korail.mobile.seatMovie.ScheduleView" not in page


def test_extension_page_dispatch_has_api_branch_outside_navigation_branch(
    tmp_path,
) -> None:
    write_extension_files(tmp_path, control_origin="http://127.0.0.1:12345")

    page = (tmp_path / "page.js").read_text()

    assert page.count('if (command.action === "navigate")') == 1
    assert page.count('if (command.action === "api")') == 1


def test_extension_control_server_round_trips_commands() -> None:
    with ExtensionControlServer() as server:
        command_id = server.enqueue_command({"action": "api", "endpoint": "/x"})

        response = requests.get(f"{server.origin}/command", timeout=2)
        assert response.status_code == 200
        assert response.json()["id"] == command_id

        empty = requests.get(f"{server.origin}/command", timeout=2)
        assert empty.status_code == 204

        result = {
            "type": "api-result",
            "id": command_id,
            "ok": True,
            "status": 200,
            "text": '{"strResult":"SUCC"}',
        }
        posted = requests.post(f"{server.origin}/result", json=result, timeout=2)
        assert posted.status_code == 204

        assert server.wait_for_result(command_id, timeout_s=1) == result


def test_extension_control_server_long_poll_waits_for_command() -> None:
    with ExtensionControlServer() as server:
        responses: list[requests.Response] = []

        thread = threading.Thread(
            target=lambda: responses.append(
                requests.get(f"{server.origin}/command?wait=2", timeout=3)
            )
        )
        thread.start()
        time.sleep(0.1)

        command_id = server.enqueue_command({"action": "api", "endpoint": "/x"})
        thread.join(timeout=3)

        assert not thread.is_alive()
        assert responses[0].status_code == 200
        assert responses[0].json()["id"] == command_id


def test_extension_browser_runner_api_call_parses_json(monkeypatch, tmp_path) -> None:
    class FakeServer:
        origin = "http://127.0.0.1:12345"

        def __init__(self) -> None:
            self.command: dict[str, object] | None = None

        def start(self) -> None:
            pass

        def close(self) -> None:
            pass

        def enqueue_command(self, command: dict[str, object]) -> str:
            self.command = command
            return "7"

        def wait_for_result(self, command_id: str, *, timeout_s: float) -> dict[str, object]:
            assert command_id == "7"
            return {
                "type": "api-result",
                "id": "7",
                "ok": True,
                "status": 200,
                "text": '{"strResult":"SUCC","value":3}',
            }

    fake_server = FakeServer()
    monkeypatch.setattr(
        "ktxgo.extension_backend.ExtensionControlServer",
        lambda: fake_server,
    )
    monkeypatch.setattr("ktxgo.extension_backend.write_extension_files", lambda *a, **k: None)
    monkeypatch.setattr(
        "ktxgo.extension_backend.subprocess.Popen",
        lambda *a, **k: type("DummyProcess", (), {"terminate": lambda self: None})(),
    )

    runner = ExtensionBrowserRunner(
        chromium_executable="/bin/chromium",
        profile_dir=tmp_path / "profile",
        initial_url="https://www.korail.com/ticket/search/general",
    )
    runner.start()

    data = runner.api_call("/endpoint", {"a": "b"})

    assert data == {"strResult": "SUCC", "value": 3}
    assert fake_server.command == {
        "action": "api",
        "endpoint": "/endpoint",
        "params": {"a": "b"},
        "method": "POST",
        "timeoutMs": 30000,
    }


def test_extension_browser_runner_retries_schedule_timeout_once(
    monkeypatch,
    tmp_path,
) -> None:
    class FakeServer:
        origin = "http://127.0.0.1:12345"

        def __init__(self) -> None:
            self.commands: list[dict[str, object]] = []
            self.wait_timeouts: list[float] = []
            self.wait_count = 0

        def start(self) -> None:
            pass

        def close(self) -> None:
            pass

        def enqueue_command(self, command: dict[str, object]) -> str:
            self.commands.append(command)
            return str(len(self.commands))

        def wait_for_result(self, command_id: str, *, timeout_s: float) -> dict[str, object]:
            self.wait_count += 1
            if self.wait_count == 1:
                raise TimeoutError("lost extension command")
            return {
                "type": "api-result",
                "id": command_id,
                "ok": True,
                "status": 200,
                "text": '{"strResult":"SUCC","retried":true}',
            }

    fake_server = FakeServer()
    monkeypatch.setattr(
        "ktxgo.extension_backend.ExtensionControlServer",
        lambda: fake_server,
    )
    monkeypatch.setattr("ktxgo.extension_backend.write_extension_files", lambda *a, **k: None)
    monkeypatch.setattr(
        "ktxgo.extension_backend.subprocess.Popen",
        lambda *a, **k: type("DummyProcess", (), {"terminate": lambda self: None})(),
    )

    runner = ExtensionBrowserRunner(
        chromium_executable="/bin/chromium",
        profile_dir=tmp_path / "profile",
        initial_url="https://www.korail.com/ticket/search/general",
    )
    runner.start()

    assert runner.api_call(API_SCHEDULE, {"txtGoStart": "서울"}) == {
        "strResult": "SUCC",
        "retried": True,
    }
    assert len(fake_server.commands) == 2


def test_extension_browser_runner_wraps_non_retryable_timeout(
    monkeypatch,
    tmp_path,
) -> None:
    class FakeServer:
        origin = "http://127.0.0.1:12345"

        def __init__(self) -> None:
            self.commands: list[dict[str, object]] = []
            self.wait_timeouts: list[float] = []

        def start(self) -> None:
            pass

        def close(self) -> None:
            pass

        def enqueue_command(self, command: dict[str, object]) -> str:
            self.commands.append(command)
            return str(len(self.commands))

        def wait_for_result(self, command_id: str, *, timeout_s: float) -> dict[str, object]:
            raise TimeoutError("lost extension command")

    fake_server = FakeServer()
    monkeypatch.setattr(
        "ktxgo.extension_backend.ExtensionControlServer",
        lambda: fake_server,
    )
    monkeypatch.setattr("ktxgo.extension_backend.write_extension_files", lambda *a, **k: None)
    monkeypatch.setattr(
        "ktxgo.extension_backend.subprocess.Popen",
        lambda *a, **k: type("DummyProcess", (), {"terminate": lambda self: None})(),
    )

    runner = ExtensionBrowserRunner(
        chromium_executable="/bin/chromium",
        profile_dir=tmp_path / "profile",
        initial_url="https://www.korail.com/ticket/search/general",
    )
    runner.start()

    with pytest.raises(KorailError) as exc_info:
        runner.api_call(API_RESERVE, {"txtJobId": "1101"})

    assert exc_info.value.code == "EXTENSION TIMEOUT"
    assert len(fake_server.commands) == 1


def test_extension_browser_runner_does_not_retry_login_check_timeout(
    monkeypatch,
    tmp_path,
) -> None:
    class FakeServer:
        origin = "http://127.0.0.1:12345"

        def __init__(self) -> None:
            self.commands: list[dict[str, object]] = []
            self.wait_timeouts: list[float] = []

        def start(self) -> None:
            pass

        def close(self) -> None:
            pass

        def enqueue_command(self, command: dict[str, object]) -> str:
            self.commands.append(command)
            return str(len(self.commands))

        def wait_for_result(self, command_id: str, *, timeout_s: float) -> dict[str, object]:
            self.wait_timeouts.append(timeout_s)
            raise TimeoutError("login check command was not processed")

    fake_server = FakeServer()
    monkeypatch.setattr(
        "ktxgo.extension_backend.ExtensionControlServer",
        lambda: fake_server,
    )
    monkeypatch.setattr("ktxgo.extension_backend.write_extension_files", lambda *a, **k: None)
    monkeypatch.setattr(
        "ktxgo.extension_backend.subprocess.Popen",
        lambda *a, **k: type("DummyProcess", (), {"terminate": lambda self: None})(),
    )

    runner = ExtensionBrowserRunner(
        chromium_executable="/bin/chromium",
        profile_dir=tmp_path / "profile",
        initial_url="https://www.korail.com/ticket/login",
    )
    runner.start()

    with pytest.raises(KorailError):
        runner.api_call(API_LOGIN_CHECK, {})

    assert len(fake_server.commands) == 1
    assert fake_server.wait_timeouts[0] <= 7


def test_extension_browser_runner_navigate_waits_for_navigation_start(
    monkeypatch,
    tmp_path,
) -> None:
    class FakeServer:
        origin = "http://127.0.0.1:12345"

        def __init__(self) -> None:
            self.command: dict[str, object] | None = None

        def start(self) -> None:
            pass

        def close(self) -> None:
            pass

        def enqueue_command(self, command: dict[str, object]) -> str:
            self.command = command
            return "11"

        def wait_for_result(self, command_id: str, *, timeout_s: float) -> dict[str, object]:
            assert command_id == "11"
            return {
                "type": "navigation-started",
                "id": "11",
                "url": "https://www.korail.com/ticket/login",
            }

    fake_server = FakeServer()
    monkeypatch.setattr(
        "ktxgo.extension_backend.ExtensionControlServer",
        lambda: fake_server,
    )
    monkeypatch.setattr("ktxgo.extension_backend.write_extension_files", lambda *a, **k: None)
    monkeypatch.setattr(
        "ktxgo.extension_backend.subprocess.Popen",
        lambda *a, **k: type("DummyProcess", (), {"terminate": lambda self: None})(),
    )

    runner = ExtensionBrowserRunner(
        chromium_executable="/bin/chromium",
        profile_dir=tmp_path / "profile",
        initial_url="https://www.korail.com/ticket/search/general",
    )
    runner.start()

    assert runner.navigate("https://www.korail.com/ticket/login") is True
    assert fake_server.command == {
        "action": "navigate",
        "url": "https://www.korail.com/ticket/login",
    }


def test_extension_browser_runner_can_launch_headless(monkeypatch, tmp_path) -> None:
    launched: dict[str, object] = {}

    class FakeServer:
        origin = "http://127.0.0.1:12345"

        def start(self) -> None:
            pass

        def close(self) -> None:
            pass

    class DummyProcess:
        def terminate(self) -> None:
            pass

    monkeypatch.setattr(
        "ktxgo.extension_backend.ExtensionControlServer",
        lambda: FakeServer(),
    )
    monkeypatch.setattr("ktxgo.extension_backend.write_extension_files", lambda *a, **k: None)

    def fake_popen(command, **kwargs):
        launched["command"] = command
        launched["kwargs"] = kwargs
        return DummyProcess()

    monkeypatch.setattr("ktxgo.extension_backend.subprocess.Popen", fake_popen)

    runner = ExtensionBrowserRunner(
        chromium_executable="/bin/chromium",
        profile_dir=tmp_path / "profile",
        initial_url="https://www.korail.com/ticket/search/general",
        headless=True,
    )
    runner.start()

    command = launched["command"]
    assert "--headless=new" in command
    assert "--disable-gpu" in command
    assert "--disable-background-timer-throttling" in command
    assert "--disable-renderer-backgrounding" in command
    assert "--disable-backgrounding-occluded-windows" in command
    assert "--disable-features=CalculateNativeWinOcclusion,IntensiveWakeUpThrottling" in command


def test_extension_browser_runner_visible_launch_is_forced_onscreen(
    monkeypatch,
    tmp_path,
) -> None:
    launched: dict[str, object] = {}

    class FakeServer:
        origin = "http://127.0.0.1:12345"

        def start(self) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        "ktxgo.extension_backend.ExtensionControlServer",
        lambda: FakeServer(),
    )
    monkeypatch.setattr("ktxgo.extension_backend.write_extension_files", lambda *a, **k: None)

    def fake_popen(command, **kwargs):
        launched["command"] = command
        return type("DummyProcess", (), {"terminate": lambda self: None})()

    monkeypatch.setattr("ktxgo.extension_backend.subprocess.Popen", fake_popen)

    runner = ExtensionBrowserRunner(
        chromium_executable="/bin/chromium",
        profile_dir=tmp_path / "profile",
        initial_url="https://www.korail.com/ticket/login",
        headless=False,
    )
    runner.start()

    command = launched["command"]
    assert "--new-window" in command
    assert "--start-maximized" in command
    assert "--window-position=0,0" in command


def test_profile_process_ids_detects_matching_user_data_dir(tmp_path) -> None:
    proc_dir = tmp_path / "proc"
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    matching_pid_dir = proc_dir / "1234"
    matching_pid_dir.mkdir(parents=True)
    (matching_pid_dir / "cmdline").write_bytes(
        b"/usr/bin/chromium\0--user-data-dir="
        + str(profile_dir).encode()
        + b"\0"
    )
    other_pid_dir = proc_dir / "5678"
    other_pid_dir.mkdir()
    (other_pid_dir / "cmdline").write_bytes(
        b"/usr/bin/chromium\0--user-data-dir=/tmp/other\0"
    )

    assert _profile_process_ids(profile_dir, proc_dir=proc_dir) == [1234]


def test_extension_browser_runner_terminates_existing_profile_processes(
    monkeypatch,
    tmp_path,
) -> None:
    terminated: list[int] = []
    cleanup_grace: list[float] = []

    class FakeServer:
        origin = "http://127.0.0.1:12345"

        def start(self) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        "ktxgo.extension_backend.ExtensionControlServer",
        lambda: FakeServer(),
    )
    monkeypatch.setattr("ktxgo.extension_backend.write_extension_files", lambda *a, **k: None)
    monkeypatch.setattr(
        "ktxgo.extension_backend._profile_process_ids",
        lambda profile_dir: [1234],
    )
    monkeypatch.setattr(
        "ktxgo.extension_backend._terminate_processes",
        lambda process_ids, *, grace_s=2.0: (
            terminated.extend(process_ids),
            cleanup_grace.append(grace_s),
        ),
    )
    monkeypatch.setattr(
        "ktxgo.extension_backend.subprocess.Popen",
        lambda *a, **k: type("DummyProcess", (), {"terminate": lambda self: None})(),
    )

    runner = ExtensionBrowserRunner(
        chromium_executable="/bin/chromium",
        profile_dir=tmp_path / "profile",
        initial_url="https://www.korail.com/ticket/login",
        headless=False,
    )
    runner.start()

    assert terminated == [1234]
    assert cleanup_grace and cleanup_grace[0] <= 0.5


def test_extension_browser_runner_can_request_window_minimize(monkeypatch, tmp_path) -> None:
    class FakeServer:
        origin = "http://127.0.0.1:12345"

        def __init__(self) -> None:
            self.command: dict[str, object] | None = None

        def start(self) -> None:
            pass

        def close(self) -> None:
            pass

        def enqueue_command(self, command: dict[str, object]) -> str:
            self.command = command
            return "9"

        def wait_for_result(self, command_id: str, *, timeout_s: float) -> dict[str, object]:
            assert command_id == "9"
            return {
                "type": "minimize-result",
                "id": "9",
                "ok": True,
            }

    fake_server = FakeServer()
    monkeypatch.setattr(
        "ktxgo.extension_backend.ExtensionControlServer",
        lambda: fake_server,
    )
    monkeypatch.setattr("ktxgo.extension_backend.write_extension_files", lambda *a, **k: None)
    monkeypatch.setattr(
        "ktxgo.extension_backend.subprocess.Popen",
        lambda *a, **k: type("DummyProcess", (), {"terminate": lambda self: None})(),
    )

    runner = ExtensionBrowserRunner(
        chromium_executable="/bin/chromium",
        profile_dir=tmp_path / "profile",
        initial_url="https://www.korail.com/ticket/search/general",
    )
    runner.start()

    assert runner.minimize() is True
    assert fake_server.command == {"action": "minimize"}


def test_extension_browser_runner_saves_login_cookie_cache(
    monkeypatch,
    tmp_path,
) -> None:
    cache_path = tmp_path / "extension-cookies.json"

    class FakeServer:
        origin = "http://127.0.0.1:12345"

        def __init__(self) -> None:
            self.command: dict[str, object] | None = None

        def start(self) -> None:
            pass

        def close(self) -> None:
            pass

        def enqueue_command(self, command: dict[str, object]) -> str:
            self.command = command
            return "21"

        def wait_for_result(self, command_id: str, *, timeout_s: float) -> dict[str, object]:
            assert command_id == "21"
            return {
                "type": "cookies-result",
                "id": "21",
                "ok": True,
                "cookies": [
                    {
                        "name": "JSESSIONID",
                        "value": "abc",
                        "domain": "www.korail.com",
                        "path": "/",
                    }
                ],
            }

    fake_server = FakeServer()
    monkeypatch.setattr(
        "ktxgo.extension_backend.ExtensionControlServer",
        lambda: fake_server,
    )
    monkeypatch.setattr("ktxgo.extension_backend.write_extension_files", lambda *a, **k: None)
    monkeypatch.setattr("ktxgo.extension_backend.EXTENSION_COOKIE_CACHE_PATH", cache_path)
    monkeypatch.setattr(
        "ktxgo.extension_backend.subprocess.Popen",
        lambda *a, **k: type("DummyProcess", (), {"terminate": lambda self: None})(),
    )

    runner = ExtensionBrowserRunner(
        chromium_executable="/bin/chromium",
        profile_dir=tmp_path / "profile",
        initial_url="https://www.korail.com/ticket/login",
    )
    runner.start()

    assert runner.save_login_cookie_cache(now=1234.5) is True
    assert fake_server.command == {"action": "cookies"}

    payload = json.loads(cache_path.read_text())
    assert payload["saved_at"] == 1234.5
    assert payload["cookies"][0]["name"] == "JSESSIONID"
    assert "ttl_s" not in payload


def test_extension_browser_runner_saves_document_cookie_fallback(
    monkeypatch,
    tmp_path,
) -> None:
    cache_path = tmp_path / "extension-cookies.json"

    class FakeServer:
        origin = "http://127.0.0.1:12345"

        def __init__(self) -> None:
            self.commands: list[dict[str, object]] = []

        def start(self) -> None:
            pass

        def close(self) -> None:
            pass

        def enqueue_command(self, command: dict[str, object]) -> str:
            self.commands.append(command)
            return str(len(self.commands))

        def wait_for_result(self, command_id: str, *, timeout_s: float) -> dict[str, object]:
            if command_id == "1":
                return {
                    "type": "cookies-result",
                    "id": "1",
                    "ok": True,
                    "cookies": [],
                }
            assert command_id == "2"
            return {
                "type": "document-cookies-result",
                "id": "2",
                "ok": True,
                "cookie": "JSESSIONID=abc; WMONID=xyz",
            }

    fake_server = FakeServer()
    monkeypatch.setattr(
        "ktxgo.extension_backend.ExtensionControlServer",
        lambda: fake_server,
    )
    monkeypatch.setattr("ktxgo.extension_backend.write_extension_files", lambda *a, **k: None)
    monkeypatch.setattr(
        "ktxgo.extension_backend.subprocess.Popen",
        lambda *a, **k: type("DummyProcess", (), {"terminate": lambda self: None})(),
    )

    runner = ExtensionBrowserRunner(
        chromium_executable="/bin/chromium",
        profile_dir=tmp_path / "profile",
        initial_url="https://www.korail.com/ticket/login",
    )
    runner.start()

    assert runner.save_login_cookie_cache(now=1234.5, path=cache_path) is True
    assert fake_server.commands == [
        {"action": "cookies"},
        {"action": "document-cookies"},
    ]

    payload = json.loads(cache_path.read_text())
    assert payload["document_cookie"] == "JSESSIONID=abc; WMONID=xyz"
    assert payload["cookies"] == []
    assert "ttl_s" not in payload


def test_extension_browser_runner_restores_login_cookie_cache(
    monkeypatch,
    tmp_path,
) -> None:
    cache_path = tmp_path / "extension-cookies.json"
    cache_path.write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "JSESSIONID",
                        "value": "abc",
                        "domain": "www.korail.com",
                        "path": "/",
                    }
                ],
                "document_cookie": "",
            }
        )
    )

    class FakeServer:
        origin = "http://127.0.0.1:12345"

        def __init__(self) -> None:
            self.command: dict[str, object] | None = None

        def start(self) -> None:
            pass

        def close(self) -> None:
            pass

        def enqueue_command(self, command: dict[str, object]) -> str:
            self.command = command
            return "21"

        def wait_for_result(self, command_id: str, *, timeout_s: float) -> dict[str, object]:
            assert command_id == "21"
            return {
                "type": "set-cookies-result",
                "id": "21",
                "ok": True,
            }

    fake_server = FakeServer()
    monkeypatch.setattr(
        "ktxgo.extension_backend.ExtensionControlServer",
        lambda: fake_server,
    )
    monkeypatch.setattr("ktxgo.extension_backend.write_extension_files", lambda *a, **k: None)
    monkeypatch.setattr(
        "ktxgo.extension_backend.subprocess.Popen",
        lambda *a, **k: type("DummyProcess", (), {"terminate": lambda self: None})(),
    )

    runner = ExtensionBrowserRunner(
        chromium_executable="/bin/chromium",
        profile_dir=tmp_path / "profile",
        initial_url="https://www.korail.com/ticket/login",
    )
    runner.start()

    assert runner.restore_login_cookie_cache(path=cache_path) is True
    assert fake_server.command == {
        "action": "set-cookies",
        "cookies": [
            {
                "name": "JSESSIONID",
                "value": "abc",
                "domain": "www.korail.com",
                "path": "/",
            }
        ],
    }


def test_persist_profile_session_cookies_keeps_korail_login_after_restart(
    tmp_path,
) -> None:
    cookies_db = tmp_path / "profile" / "Default" / "Cookies"
    cookies_db.parent.mkdir(parents=True)
    con = sqlite3.connect(cookies_db)
    con.execute(
        """
        create table cookies (
            host_key text,
            name text,
            expires_utc integer,
            has_expires integer,
            is_persistent integer
        )
        """
    )
    con.execute(
        "insert into cookies values (?, ?, ?, ?, ?)",
        ("www.korail.com", "JSESSIONID", 0, 0, 0),
    )
    con.execute(
        "insert into cookies values (?, ?, ?, ?, ?)",
        ("example.com", "JSESSIONID", 0, 0, 0),
    )
    con.commit()
    con.close()

    assert _persist_profile_session_cookies(tmp_path / "profile", now=1_000.0) == 1

    con = sqlite3.connect(cookies_db)
    rows = con.execute(
        """
        select host_key, name, expires_utc, has_expires, is_persistent
        from cookies
        order by host_key
        """
    ).fetchall()
    con.close()

    assert rows == [
        ("example.com", "JSESSIONID", 0, 0, 0),
        (
            "www.korail.com",
            "JSESSIONID",
            _chrome_cookie_expires_utc(1_000.0 + 86_400),
            1,
            1,
        ),
    ]


def test_extension_login_cookie_cache_available_does_not_apply_ttl(tmp_path) -> None:
    cache_path = tmp_path / "extension-cookies.json"
    cache_path.write_text(
        json.dumps(
            {
                "saved_at": 1_000.0,
                "cookies": [
                    {
                        "name": "JSESSIONID",
                        "value": "abc",
                        "domain": "www.korail.com",
                        "path": "/",
                    }
                ],
            }
        )
    )

    assert extension_login_cookie_cache_available(path=cache_path)

    cache_path.write_text(
        json.dumps(
            {
                "cookies": [],
                "document_cookie": "JSESSIONID=abc",
            }
        )
    )
    assert extension_login_cookie_cache_available(path=cache_path)

    cache_path.write_text(json.dumps({"cookies": []}))
    assert not extension_login_cookie_cache_available(path=cache_path)



def test_default_extension_chromium_prefers_playwright_chromium_1105(
    monkeypatch,
    tmp_path,
) -> None:
    chromium = tmp_path / ".cache/ms-playwright/chromium-1105/chrome-linux/chrome"
    chromium.parent.mkdir(parents=True)
    chromium.write_text("")

    newer = tmp_path / ".cache/ms-playwright/chromium-1208/chrome-linux64/chrome"
    newer.parent.mkdir(parents=True)
    newer.write_text("")

    monkeypatch.setattr(cli, "__file__", str(tmp_path / "project/ktxgo/cli.py"))
    monkeypatch.setattr(cli.Path, "home", lambda: tmp_path)
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    monkeypatch.setattr(cli.shutil, "which", lambda executable: None)

    assert cli._default_extension_chromium_executable() == chromium


def test_default_extension_chromium_finds_macos_playwright_cache(
    monkeypatch,
    tmp_path,
) -> None:
    chromium = (
        tmp_path
        / "Library/Caches/ms-playwright/chromium-1105/chrome-mac-arm64"
        / "Chromium.app/Contents/MacOS/Chromium"
    )
    chromium.parent.mkdir(parents=True)
    chromium.write_text("")

    monkeypatch.setattr(cli, "__file__", str(tmp_path / "project/ktxgo/cli.py"))
    monkeypatch.setattr(cli.Path, "home", lambda: tmp_path)
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    monkeypatch.setattr(cli.shutil, "which", lambda executable: None)

    assert cli._default_extension_chromium_executable() == chromium


def test_default_extension_chromium_uses_configured_playwright_cache(
    monkeypatch,
    tmp_path,
) -> None:
    cache = tmp_path / "custom-playwright"
    chromium = (
        cache
        / "chromium-1105/chrome-mac"
        / "Chromium.app/Contents/MacOS/Chromium"
    )
    chromium.parent.mkdir(parents=True)
    chromium.write_text("")

    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(cache))
    monkeypatch.setattr(cli.shutil, "which", lambda executable: None)

    assert cli._default_extension_chromium_executable() == chromium
