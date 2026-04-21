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
