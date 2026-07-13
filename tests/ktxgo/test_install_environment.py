from __future__ import annotations

import tomllib
from pathlib import Path


def test_project_pins_playwright_for_extension_chromium() -> None:
    data = tomllib.loads(Path("pyproject.toml").read_text())

    dependencies = data["project"]["dependencies"]

    assert "playwright==1.42.0" in dependencies


def test_install_script_installs_chromium_for_extension_backend() -> None:
    script = Path("install.sh").read_text()

    assert 'PLAYWRIGHT_VERSION="1.42.0"' in script
    assert "playwright==${PLAYWRIGHT_VERSION}" in script
    assert "playwright install firefox chromium" in script
    assert 'PLAYWRIGHT_BROWSERS_DIR="${PLAYWRIGHT_BROWSERS_PATH:-${ROOT_DIR}/.cache/ms-playwright}"' in script


def test_run_script_reuses_project_playwright_browser_cache() -> None:
    script = Path("run.sh").read_text()

    assert 'PROJECT_PLAYWRIGHT_BROWSERS="${ROOT_DIR}/.cache/ms-playwright"' in script
    assert "export PLAYWRIGHT_BROWSERS_PATH=" in script


def test_run_script_handles_empty_args_on_macos_bash_32() -> None:
    script = Path("run.sh").read_text()

    safe_expansion = '${TARGET_ARGS[@]+"${TARGET_ARGS[@]}"}'
    assert script.count(safe_expansion) == 2
    assert 'ktxgo "${TARGET_ARGS[@]}"' not in script
    assert 'srtgo "${TARGET_ARGS[@]}"' not in script
