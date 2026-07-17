from pathlib import Path


def test_fingerprint_probe_html_contains_copyable_snapshot_script() -> None:
    path = Path("tools/korail_fingerprint_probe.html")

    assert path.is_file()
    content = path.read_text(encoding="utf-8")
    assert "navigator.webdriver" in content
    assert "navigator.plugins.length" in content
    assert "WEBGL_debug_renderer_info" in content
    assert "copy" in content.lower()
