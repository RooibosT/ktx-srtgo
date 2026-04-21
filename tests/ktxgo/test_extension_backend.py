from __future__ import annotations

import pytest
import requests

from ktxgo import cli
from ktxgo.config import API_SCHEDULE
from ktxgo.extension_backend import (
    ExtensionBrowserRunner,
    ExtensionControlServer,
    ExtensionKorailAPI,
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


def test_extension_files_inject_page_context_xmlhttprequest(tmp_path) -> None:
    write_extension_files(tmp_path, control_origin="http://127.0.0.1:12345")

    manifest = (tmp_path / "manifest.json").read_text()
    content = (tmp_path / "content.js").read_text()
    page = (tmp_path / "page.js").read_text()

    assert "https://www.korail.com/*" in manifest
    assert "web_accessible_resources" in manifest
    assert "chrome.runtime.getURL(\"page.js\")" in content
    assert "window.postMessage" in content
    assert "new XMLHttpRequest()" in page
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

    monkeypatch.setattr(cli.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(cli.shutil, "which", lambda executable: None)

    assert cli._default_extension_chromium_executable() == chromium
