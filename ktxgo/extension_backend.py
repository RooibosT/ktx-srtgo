from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Protocol, cast

from .config import DATA_DIR, SEARCH_URL
from .korail import KorailAPI, KorailError


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
                if self.path != "/command":
                    self.send_response(404)
                    self._send_cors()
                    self.end_headers()
                    return

                try:
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
                "run_at": "document_idle",
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
  const report = (payload) => fetch(`${{CONTROL_ORIGIN}}/result`, {{
    method: "POST",
    headers: {{"content-type": "application/json"}},
    body: JSON.stringify(payload),
  }}).catch(() => {{}});

  window.addEventListener("message", (event) => {{
    if (event.source !== window) return;
    if (!event.data || event.data.type !== "KTXGO_RESULT") return;
    report(event.data.payload);
  }});

  const poll = async () => {{
    try {{
      const response = await fetch(`${{CONTROL_ORIGIN}}/command`, {{
        cache: "no-store",
      }});
      if (response.status === 204) return;
      const command = await response.json();
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
      window.postMessage({{type: "KTXGO_COMMAND", command}}, "*");
    }} catch (_) {{
      // The Python control server may not be ready during browser startup.
    }}
  }};

  const script = document.createElement("script");
  script.src = chrome.runtime.getURL("page.js");
  script.onload = () => script.remove();
  (document.documentElement || document.head).appendChild(script);

  report({{
    type: "ready",
    href: location.href,
    userAgent: navigator.userAgent,
    webdriver: navigator.webdriver,
  }});
  setInterval(poll, 300);
}})();
""".lstrip()
    )
    (extension_dir / "background.js").write_text(
        """
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
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
        self.server = ExtensionControlServer()
        self.server.start()
        write_extension_files(self.extension_dir, control_origin=self.server.origin)
        command = [
            self.chromium_executable,
            f"--user-data-dir={self.profile_dir}",
            "--no-first-run",
            "--no-sandbox",
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
        command_id = self.server.enqueue_command(
            {
                "action": "api",
                "endpoint": endpoint,
                "params": params,
                "method": "POST",
            }
        )
        result = self.server.wait_for_result(
            command_id,
            timeout_s=self.command_timeout_s,
        )
        if not bool(result.get("ok", False)):
            raise KorailError(
                str(result.get("error") or "Extension browser API call failed"),
                str(result.get("status") or ""),
            )
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

    def navigate(self, url: str) -> None:
        if self.server is None:
            raise RuntimeError("Extension browser runner is not started")
        self.server.enqueue_command({"action": "navigate", "url": url})

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
