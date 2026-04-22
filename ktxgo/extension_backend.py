from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Protocol, cast
from urllib.parse import parse_qs, urlparse

from .config import API_LOGIN_CHECK, API_SCHEDULE, DATA_DIR, SEARCH_URL
from .korail import KorailAPI, KorailError


EXTENSION_COOKIE_CACHE_PATH = DATA_DIR / "extension_cookies.json"


def extension_login_cookie_cache_available(
    *,
    path: Path = EXTENSION_COOKIE_CACHE_PATH,
) -> bool:
    """Return whether a saved Chromium login cookie cache exists as a fast-path hint.

    This intentionally does not enforce an app-side TTL. Korail controls the
    actual session lifetime, so the CLI uses this file only to avoid a slow
    headless loginCheck probe when a previous extension login saved cookies.
    If the server-side session has expired, the next API call will fail and the
    CLI will fall back to visible login.
    """
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return False
    if not isinstance(payload, dict):
        return False

    cookies = payload.get("cookies")
    document_cookie = str(payload.get("document_cookie") or "").strip()
    return (isinstance(cookies, list) and bool(cookies)) or bool(document_cookie)


def _profile_process_ids(profile_dir: Path, *, proc_dir: Path = Path("/proc")) -> list[int]:
    """Return Linux process IDs currently using the Chromium profile directory."""
    if not proc_dir.is_dir():
        return []

    try:
        target_profile = profile_dir.resolve()
    except OSError:
        target_profile = profile_dir.absolute()

    process_ids: list[int] = []
    current_pid = os.getpid()
    for pid_dir in proc_dir.iterdir():
        if not pid_dir.name.isdigit():
            continue
        pid = int(pid_dir.name)
        if pid == current_pid:
            continue
        try:
            raw = (pid_dir / "cmdline").read_bytes()
        except OSError:
            continue
        if not raw:
            continue
        args = [part.decode("utf-8", "ignore") for part in raw.split(b"\0") if part]
        for index, arg in enumerate(args):
            user_data_dir = ""
            if arg.startswith("--user-data-dir="):
                user_data_dir = arg.split("=", 1)[1]
            elif arg == "--user-data-dir" and index + 1 < len(args):
                user_data_dir = args[index + 1]
            if not user_data_dir:
                continue
            try:
                candidate_profile = Path(user_data_dir).resolve()
            except OSError:
                candidate_profile = Path(user_data_dir).absolute()
            if candidate_profile == target_profile:
                process_ids.append(pid)
                break
    return sorted(process_ids)


def _terminate_processes(process_ids: list[int], *, grace_s: float = 2.0) -> None:
    """Terminate stale Chromium processes for a dedicated ktxgo profile."""
    if not process_ids:
        return

    current_pid = os.getpid()
    targets = [pid for pid in process_ids if pid != current_pid]
    for pid in targets:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        except OSError:
            continue

    deadline = time.monotonic() + max(0.0, grace_s)
    remaining = set(targets)
    while remaining and time.monotonic() < deadline:
        for pid in list(remaining):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                remaining.discard(pid)
            except OSError:
                remaining.discard(pid)
        if remaining:
            time.sleep(0.05)

    for pid in remaining:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


class ExtensionRunner(Protocol):
    def api_call(self, endpoint: str, params: dict[str, str]) -> dict[str, object]:
        ...


class ExtensionControlServer:
    def __init__(self) -> None:
        self._commands: queue.Queue[dict[str, object]] = queue.Queue()
        self._results: dict[str, dict[str, object]] = {}
        self._condition = threading.Condition()
        self._next_id = 0
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.origin = ""

    def __enter__(self) -> ExtensionControlServer:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def start(self) -> None:
        if self._server is not None:
            return

        owner = self

        class Handler(BaseHTTPRequestHandler):
            def _send_cors(self) -> None:
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "content-type")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

            def do_OPTIONS(self) -> None:
                self.send_response(204)
                self._send_cors()
                self.end_headers()

            def do_GET(self) -> None:
                parsed_url = urlparse(self.path)
                if parsed_url.path != "/command":
                    self.send_response(404)
                    self._send_cors()
                    self.end_headers()
                    return

                wait_s = 0.0
                wait_values = parse_qs(parsed_url.query).get("wait", [])
                if wait_values:
                    try:
                        wait_s = max(0.0, min(30.0, float(wait_values[0])))
                    except ValueError:
                        wait_s = 0.0

                try:
                    if wait_s > 0:
                        command = owner._commands.get(timeout=wait_s)
                    else:
                        command = owner._commands.get_nowait()
                except queue.Empty:
                    self.send_response(204)
                    self._send_cors()
                    self.end_headers()
                    return

                body = json.dumps(command, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self._send_cors()
                self.send_header("content-type", "application/json; charset=utf-8")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                if self.path != "/result":
                    self.send_response(404)
                    self._send_cors()
                    self.end_headers()
                    return

                try:
                    length = int(self.headers.get("content-length", "0") or "0")
                    raw = self.rfile.read(length)
                    result_obj = json.loads(raw.decode("utf-8"))
                except Exception:
                    result_obj = {}

                if isinstance(result_obj, dict):
                    result = cast(dict[str, object], result_obj)
                    command_id = str(result.get("id", ""))
                    if command_id:
                        with owner._condition:
                            owner._results[command_id] = result
                            owner._condition.notify_all()

                self.send_response(204)
                self._send_cors()
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = int(self._server.server_address[1])
        self.origin = f"http://127.0.0.1:{port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._server = None
        self._thread = None
        self.origin = ""

    def enqueue_command(self, command: dict[str, object]) -> str:
        with self._condition:
            self._next_id += 1
            command_id = str(self._next_id)
        queued = dict(command)
        queued["id"] = command_id
        self._commands.put(queued)
        return command_id

    def wait_for_result(self, command_id: str, *, timeout_s: float) -> dict[str, object]:
        deadline = time.monotonic() + timeout_s
        with self._condition:
            while command_id not in self._results:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"Timed out waiting for extension result {command_id}")
                self._condition.wait(timeout=remaining)
            return self._results.pop(command_id)


def write_extension_files(extension_dir: Path, *, control_origin: str) -> None:
    """Write the unpacked extension used by the extension backend."""
    extension_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "manifest_version": 3,
        "name": "KTXgo DynaPath Runner",
        "version": "0.1.0",
        "permissions": [
            "tabs",
            "cookies",
        ],
        "host_permissions": [
            "https://www.korail.com/*",
            f"{control_origin}/*",
        ],
        "background": {
            "service_worker": "background.js",
        },
        "content_scripts": [
            {
                "matches": ["https://www.korail.com/*"],
                "js": ["content.js"],
                "run_at": "document_end",
            }
        ],
        "web_accessible_resources": [
            {
                "resources": ["page.js"],
                "matches": ["https://www.korail.com/*"],
            }
        ],
    }
    (extension_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2)
    )
    (extension_dir / "content.js").write_text(
        f"""
(() => {{
  const CONTROL_ORIGIN = {json.dumps(control_origin)};
  let pageReady = false;
  const queuedPageCommands = [];

  const report = (payload) => fetch(`${{CONTROL_ORIGIN}}/result`, {{
    method: "POST",
    headers: {{"content-type": "application/json"}},
    body: JSON.stringify(payload),
  }}).catch(() => {{}});

  window.addEventListener("message", (event) => {{
    if (event.source !== window) return;
    if (!event.data || event.data.type !== "KTXGO_RESULT") return;
    if (event.data.payload && event.data.payload.type === "page-ready") {{
      pageReady = true;
      while (queuedPageCommands.length) {{
        window.postMessage({{type: "KTXGO_COMMAND", command: queuedPageCommands.shift()}}, "*");
      }}
    }}
    report(event.data.payload);
  }});

  const postPageCommand = (command) => {{
    if (!pageReady) {{
      queuedPageCommands.push(command);
      return;
    }}
    window.postMessage({{type: "KTXGO_COMMAND", command}}, "*");
  }};

  const handleCommand = (command) => {{
    if (command.action === "minimize") {{
      chrome.runtime.sendMessage({{type: "KTXGO_MINIMIZE", id: command.id}}, (reply) => {{
        const runtimeError = chrome.runtime.lastError;
        report({{
          type: "minimize-result",
          id: command.id,
          ok: !runtimeError && !!reply && reply.ok === true,
          error: runtimeError ? runtimeError.message : ((reply && reply.error) || ""),
        }});
      }});
      return;
    }}
    if (command.action === "cookies") {{
      chrome.runtime.sendMessage({{type: "KTXGO_GET_COOKIES", id: command.id}}, (reply) => {{
        const runtimeError = chrome.runtime.lastError;
        report({{
          type: "cookies-result",
          id: command.id,
          ok: !runtimeError && !!reply && reply.ok === true,
          cookies: reply && Array.isArray(reply.cookies) ? reply.cookies : [],
          error: runtimeError ? runtimeError.message : ((reply && reply.error) || ""),
        }});
      }});
      return;
    }}
    postPageCommand(command);
  }};

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  const poll = async () => {{
    while (true) {{
    try {{
      const response = await fetch(`${{CONTROL_ORIGIN}}/command?wait=25`, {{
        cache: "no-store",
      }});
      if (response.status === 200) {{
        handleCommand(await response.json());
      }} else if (response.status !== 204) {{
        await sleep(1000);
      }}
    }} catch (_) {{
      // The Python control server may not be ready during browser startup.
      await sleep(1000);
    }}
    }}
  }};

  const injectPageScript = () => {{
    const target = document.documentElement || document.head;
    if (!target) {{
      setTimeout(injectPageScript, 0);
      return;
    }}
    const script = document.createElement("script");
    script.src = chrome.runtime.getURL("page.js");
    script.onload = () => script.remove();
    script.onerror = () => report({{type: "page-script-error", href: location.href}});
    target.appendChild(script);
  }};

  report({{
    type: "ready",
    href: location.href,
    userAgent: navigator.userAgent,
    webdriver: navigator.webdriver,
  }});
  injectPageScript();
  poll();
}})();
""".lstrip()
    )
    (extension_dir / "background.js").write_text(
        """
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message && message.type === "KTXGO_GET_COOKIES") {
    chrome.cookies.getAll({url: "https://www.korail.com/"}, (cookies) => {
      const runtimeError = chrome.runtime.lastError;
      sendResponse({
        ok: !runtimeError,
        error: runtimeError ? runtimeError.message : "",
        cookies: cookies || [],
      });
    });
    return true;
  }

  if (!message || message.type !== "KTXGO_MINIMIZE") {
    return false;
  }

  const windowId = sender && sender.tab && sender.tab.windowId;
  if (typeof windowId !== "number") {
    sendResponse({ok: false, error: "No sender window"});
    return false;
  }

  chrome.windows.update(windowId, {state: "minimized"}, () => {
    const runtimeError = chrome.runtime.lastError;
    sendResponse({
      ok: !runtimeError,
      error: runtimeError ? runtimeError.message : "",
    });
  });
  return true;
});
""".lstrip()
    )
    (extension_dir / "page.js").write_text(
        """
(() => {
  const encodeForm = (params) => Object.entries(params || {})
    .map(([key, value]) => `${encodeURIComponent(key)}=${encodeURIComponent(value == null ? "" : String(value))}`)
    .join("&");

  const send = (payload) => {
    window.postMessage({type: "KTXGO_RESULT", payload}, "*");
  };

  const callApi = (command) => {
    const xhr = new XMLHttpRequest();
    xhr.open(command.method || "POST", command.endpoint, true);
    xhr.setRequestHeader("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8");
    xhr.timeout = Number(command.timeoutMs || 30000);
    xhr.onreadystatechange = () => {
      if (xhr.readyState !== 4) return;
      send({
        type: "api-result",
        id: command.id,
        ok: xhr.status >= 200 && xhr.status < 300,
        status: xhr.status,
        responseURL: xhr.responseURL,
        text: xhr.responseText || "",
      });
    };
    xhr.onerror = () => {
      send({
        type: "api-result",
        id: command.id,
        ok: false,
        status: xhr.status || 0,
        responseURL: xhr.responseURL || "",
        text: "",
        error: "xhr_error",
      });
    };
    xhr.ontimeout = () => {
      send({
        type: "api-result",
        id: command.id,
        ok: false,
        status: xhr.status || 0,
        responseURL: xhr.responseURL || "",
        text: "",
        error: "xhr_timeout",
      });
    };
    xhr.send(encodeForm(command.params || {}));
  };

  window.addEventListener("message", (event) => {
    if (event.source !== window) return;
    const command = event.data && event.data.command;
    if (!event.data || event.data.type !== "KTXGO_COMMAND" || !command) return;

    if (command.action === "navigate") {
      send({type: "navigation-started", id: command.id, url: command.url});
      window.location.href = command.url;
      return;
    }
    if (command.action === "api") {
      callApi(command);
      return;
    }
    if (command.action === "document-cookies") {
      send({
        type: "document-cookies-result",
        id: command.id,
        ok: true,
        cookie: document.cookie || "",
      });
      return;
    }
    send({type: "api-result", id: command.id, ok: false, status: 0, text: "", error: "unknown_action"});
  });

  send({
    type: "page-ready",
    href: location.href,
    webdriver: navigator.webdriver,
    xhr: String(window.XMLHttpRequest),
  });
})();
""".lstrip()
    )


class ExtensionKorailAPI(KorailAPI):
    """Korail API adapter backed by an in-page browser extension runner.

    The existing :class:`KorailAPI` search/reserve/pay parsing code only depends
    on ``_api_call``.  This adapter keeps that code path but sends calls to a
    runner that executes ``XMLHttpRequest`` inside a normal Korail browser page,
    allowing Korail's DynaPath script to wrap protected endpoints.
    """

    def __init__(self, runner: ExtensionRunner):
        self.runner = runner
        self.page = None  # type: ignore[assignment]
        self.last_auto_login_error: str | None = None
        self.last_auto_login_detail: str | None = None

    def _api_call(self, endpoint: str, params: dict[str, str]) -> dict[str, object]:
        data = self.runner.api_call(endpoint, params)

        if str(data.get("strResult", "")) == "FAIL":
            raise KorailError(
                str(
                    data.get("h_msg_txt") or data.get("message") or "Korail API failed"
                ),
                str(data.get("h_msg_cd") or data.get("code") or ""),
            )

        return cast(dict[str, object], data)


class ExtensionBrowserRunner:
    def __init__(
        self,
        *,
        chromium_executable: str | Path,
        profile_dir: str | Path | None = None,
        initial_url: str = SEARCH_URL,
        command_timeout_s: float = 30.0,
        headless: bool = False,
    ) -> None:
        self.chromium_executable = str(chromium_executable)
        self.profile_dir = Path(profile_dir or (DATA_DIR / "chromium-extension-profile"))
        self.initial_url = initial_url
        self.command_timeout_s = command_timeout_s
        self.headless = headless
        self.extension_dir = DATA_DIR / "chromium-extension"
        self.server: ExtensionControlServer | None = None
        self.process: subprocess.Popen[object] | None = None

    @staticmethod
    def _is_retryable_endpoint(endpoint: str) -> bool:
        return endpoint == API_SCHEDULE

    def __enter__(self) -> ExtensionBrowserRunner:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def start(self) -> None:
        if self.process is not None:
            return
        DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        _terminate_processes(_profile_process_ids(self.profile_dir), grace_s=0.35)
        self.server = ExtensionControlServer()
        self.server.start()
        write_extension_files(self.extension_dir, control_origin=self.server.origin)
        command = [
            self.chromium_executable,
            f"--user-data-dir={self.profile_dir}",
            "--no-first-run",
            "--no-sandbox",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-features=CalculateNativeWinOcclusion,IntensiveWakeUpThrottling",
            f"--disable-extensions-except={self.extension_dir}",
            f"--load-extension={self.extension_dir}",
        ]
        if self.headless:
            command.extend(
                [
                    "--headless=new",
                    "--disable-gpu",
                    "--window-size=1280,900",
                ]
            )
        else:
            command.extend(
                [
                    "--new-window",
                    "--start-maximized",
                    "--window-position=0,0",
                    "--window-size=1280,900",
                ]
            )
        command.append(self.initial_url)
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def close(self) -> None:
        if self.process is not None:
            try:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)  # type: ignore[attr-defined]
                except AttributeError:
                    pass
                except subprocess.TimeoutExpired:
                    self.process.kill()  # type: ignore[attr-defined]
                    self.process.wait(timeout=5)  # type: ignore[attr-defined]
            except Exception:
                pass
            self.process = None
        if self.server is not None:
            self.server.close()
            self.server = None

    def api_call(self, endpoint: str, params: dict[str, str]) -> dict[str, object]:
        if self.server is None:
            raise RuntimeError("Extension browser runner is not started")

        attempts = 2 if self._is_retryable_endpoint(endpoint) else 1
        last_timeout: TimeoutError | None = None
        for attempt_idx in range(attempts):
            command_id = self.server.enqueue_command(
                {
                    "action": "api",
                    "endpoint": endpoint,
                    "params": params,
                    "method": "POST",
                    "timeoutMs": int(self.command_timeout_s * 1000),
                }
            )
            wait_timeout_s = self.command_timeout_s + 2
            if endpoint == API_LOGIN_CHECK:
                wait_timeout_s = min(wait_timeout_s, 7.0)
            try:
                result = self.server.wait_for_result(
                    command_id,
                    timeout_s=wait_timeout_s,
                )
            except TimeoutError as exc:
                last_timeout = exc
                if attempt_idx + 1 < attempts:
                    continue
                raise KorailError(
                    f"Extension browser timed out waiting for {endpoint}",
                    "EXTENSION TIMEOUT",
                ) from exc

            if not bool(result.get("ok", False)):
                error = str(result.get("error") or "Extension browser API call failed")
                if error == "xhr_timeout" and attempt_idx + 1 < attempts:
                    continue
                code = "EXTENSION TIMEOUT" if error == "xhr_timeout" else str(
                    result.get("status") or ""
                )
                raise KorailError(error, code)
            break
        else:  # pragma: no cover - defensive; loop either breaks or raises.
            raise KorailError(
                f"Extension browser timed out waiting for {endpoint}",
                "EXTENSION TIMEOUT",
            ) from last_timeout

        text = str(result.get("text") or "").strip()
        if not text:
            raise KorailError(f"Empty response from {endpoint}")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise KorailError(f"Invalid JSON from {endpoint}") from exc
        if not isinstance(data, dict):
            raise KorailError(f"Unexpected JSON payload from {endpoint}")
        return cast(dict[str, object], data)

    def get_cookies(self) -> list[dict[str, object]]:
        if self.server is None:
            raise RuntimeError("Extension browser runner is not started")
        command_id = self.server.enqueue_command({"action": "cookies"})
        try:
            result = self.server.wait_for_result(
                command_id,
                timeout_s=min(3.0, self.command_timeout_s),
            )
        except TimeoutError:
            return []
        if not bool(result.get("ok", False)):
            return []
        cookies = result.get("cookies")
        if not isinstance(cookies, list):
            return []
        return [
            cast(dict[str, object], cookie)
            for cookie in cookies
            if isinstance(cookie, dict)
        ]

    def get_document_cookie(self) -> str:
        if self.server is None:
            raise RuntimeError("Extension browser runner is not started")
        command_id = self.server.enqueue_command({"action": "document-cookies"})
        try:
            result = self.server.wait_for_result(
                command_id,
                timeout_s=min(3.0, self.command_timeout_s),
            )
        except TimeoutError:
            return ""
        if not bool(result.get("ok", False)):
            return ""
        return str(result.get("cookie") or "").strip()

    def save_login_cookie_cache(
        self,
        *,
        now: float | None = None,
        path: Path | None = None,
    ) -> bool:
        cookies = self.get_cookies()
        document_cookie = "" if cookies else self.get_document_cookie()
        if not cookies and not document_cookie:
            return False

        DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        cache_path = path or EXTENSION_COOKIE_CACHE_PATH
        payload = {
            "saved_at": time.time() if now is None else now,
            "cookies": cookies,
            "document_cookie": document_cookie,
        }
        try:
            cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
            cache_path.chmod(0o600)
        except OSError:
            return False
        return True

    def navigate(self, url: str) -> bool:
        if self.server is None:
            raise RuntimeError("Extension browser runner is not started")
        command_id = self.server.enqueue_command({"action": "navigate", "url": url})
        try:
            result = self.server.wait_for_result(
                command_id,
                timeout_s=min(10.0, self.command_timeout_s),
            )
        except Exception:
            return False
        return str(result.get("type", "")) == "navigation-started"

    def minimize(self) -> bool:
        if self.server is None:
            raise RuntimeError("Extension browser runner is not started")
        command_id = self.server.enqueue_command({"action": "minimize"})
        try:
            result = self.server.wait_for_result(
                command_id,
                timeout_s=min(3.0, self.command_timeout_s),
            )
        except Exception:
            return False
        return bool(result.get("ok", False))
