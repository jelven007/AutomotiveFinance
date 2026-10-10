"""Desktop runtime and Windows packaging regression tests."""

from __future__ import annotations

import importlib.metadata
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import desktop

ROOT = Path(__file__).resolve().parents[2]


class _FakeLoadedEvent:
    def __init__(self) -> None:
        self.callback = None

    def __iadd__(self, callback):
        self.callback = callback
        return self


class _FakeWindow:
    def __init__(self) -> None:
        self.events = SimpleNamespace(loaded=_FakeLoadedEvent())
        self.destroyed = False

    def evaluate_js(self, _script: str) -> dict:
        return {
            "readyState": "complete",
            "location": "http://127.0.0.1:3018/",
            "title": "TSP",
            "bodyTextLength": 42,
            "htmlLength": 512,
        }

    def destroy(self) -> None:
        self.destroyed = True


def test_smoke_plugin_validation_accepts_default_rustdx_provider() -> None:
    desktop._validate_smoke_plugins(
        [
            {
                "name": name,
                "available": True,
                "datasets": sorted(desktop._RUSTDX_DATASETS),
            } for name in ("rustdx",)
        ]
    )


def test_desktop_smoke_requires_authentication_bypass() -> None:
    source = (ROOT / "backend" / "app" / "desktop.py").read_text(encoding="utf-8")

    assert 'f"{base_url}/api/auth/status"' in source
    assert 'auth_status.get("auth_required") is not False' in source
    assert 'f"{base_url}/api/capabilities"' in source
    assert 'f"{base_url}/api/auth/login"' in source
    assert 'logger.info("DESKTOP_AUTH_BYPASS_SMOKE_TEST_OK")' in source


def test_find_free_port_skips_an_active_listener() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        occupied = listener.getsockname()[1]

        assert desktop._find_free_port(occupied, count=20) != occupied


@pytest.mark.parametrize(
    "plugin",
    [
        None,
        {"name": "rustdx", "available": False, "status": "missing"},
        {"name": "rustdx", "available": True, "datasets": ["daily"]},
    ],
)
def test_smoke_plugin_validation_rejects_incomplete_rustdx(plugin: dict | None) -> None:
    with pytest.raises(RuntimeError, match="rustdx"):
        desktop._validate_smoke_plugins(
            [] if plugin is None else [plugin]
        )


def test_windows_installer_contract() -> None:
    script = (ROOT / "packaging" / "tsp.iss").read_text(encoding="utf-8")
    run_section = script.split("[Run]", 1)[1].split("[UninstallRun]", 1)[0]

    assert "MinVersion=10.0" in script
    assert "ArchitecturesAllowed=x64compatible" in script
    assert "ArchitecturesInstallIn64BitMode=x64compatible" in script
    assert run_section.count('Filename: "{app}\\{#MyAppExeName}"') == 1
    assert 'Source: "redist\\{#WebView2SetupName}"; Flags: dontcopy' in script
    assert "function IsWebView2RuntimeInstalled(): Boolean;" in script
    assert "function PrepareToInstall(var NeedsRestart: Boolean): String;" in script
    assert "'/silent /install'" in script


def test_pyinstaller_collects_default_data_provider() -> None:
    spec = (ROOT / "packaging" / "tsp.spec").read_text(encoding="utf-8")

    assert '"tsp_rustdx_native",' in spec
    assert 'BUILTIN_PLUGINS.glob("*/plugin.yaml")' in spec
    assert '"tsp-rustdx-native",' in spec
    assert '"pywebview", "pythonnet", "clr-loader",' in spec


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (importlib.metadata.PackageNotFoundError(), "metadata-missing"),
        (RuntimeError("broken metadata"), "metadata-error:RuntimeError"),
    ],
)
def test_distribution_metadata_errors_are_nonfatal(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected: str,
) -> None:
    def _missing(_name: str) -> str:
        raise error

    monkeypatch.setattr(importlib.metadata, "version", _missing)

    assert desktop._safe_distribution_version("pywebview") == expected


def test_gui_smoke_opens_window_and_validates_edgechromium(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake_window = _FakeWindow()
    fake_webview = SimpleNamespace(renderer=None)

    def _create_window(*_args, **_kwargs):
        return fake_window

    def _start(**_kwargs) -> None:
        fake_webview.renderer = "edgechromium"
        assert fake_window.events.loaded.callback is not None
        fake_window.events.loaded.callback()

    def _missing_metadata(_name: str) -> str:
        raise importlib.metadata.PackageNotFoundError

    fake_webview.create_window = _create_window
    fake_webview.start = _start
    monkeypatch.setattr(importlib.metadata, "version", _missing_metadata)
    monkeypatch.setitem(sys.modules, "webview", fake_webview)
    monkeypatch.setenv(desktop._GUI_SMOKE_TEST_ENV, "1")

    with caplog.at_level("INFO"):
        desktop._open_window("http://127.0.0.1:3018")

    assert fake_window.destroyed
    assert "DESKTOP_GUI_SMOKE_TEST_OK renderer=edgechromium" in caplog.text


@pytest.mark.parametrize(
    ("renderer", "page", "message"),
    [
        ("mshtml", {}, "renderer"),
        (
            "edgechromium",
            {
                "readyState": "complete",
                "location": "http://127.0.0.1:3018",
                "bodyTextLength": 0,
                "htmlLength": 100,
            },
            "body text",
        ),
    ],
)
def test_gui_smoke_rejects_invalid_renderer_or_blank_page(
    renderer: str,
    page: dict,
    message: str,
) -> None:
    with pytest.raises(RuntimeError, match=message):
        desktop._validate_gui_smoke_page(renderer, page)


def test_gui_smoke_waits_for_react_to_render_after_loaded_event() -> None:
    blank_page = {
        "readyState": "complete",
        "location": "http://127.0.0.1:3018",
        "bodyTextLength": 0,
        "htmlLength": 100,
    }
    rendered_page = {
        **blank_page,
        "bodyTextLength": 42,
        "htmlLength": 512,
    }
    probes = iter([rendered_page])

    page = desktop._wait_for_gui_smoke_page(
        "edgechromium",
        lambda: next(probes),
        initial_page=blank_page,
        timeout=0.1,
        poll_interval=0,
    )

    assert page == rendered_page


def test_gui_smoke_rejects_a_page_that_remains_blank() -> None:
    blank_page = {
        "readyState": "complete",
        "location": "http://127.0.0.1:3018",
        "bodyTextLength": 0,
        "htmlLength": 100,
    }

    with pytest.raises(RuntimeError, match="did not render"):
        desktop._wait_for_gui_smoke_page(
            "edgechromium",
            lambda: blank_page,
            initial_page=blank_page,
            timeout=0,
            poll_interval=0,
        )


def test_windows_smoke_log_checks_use_explicit_powershell_parameters() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert "https://go.microsoft.com/fwlink/p/?LinkId=2124703" in workflow
    assert "Get-AuthenticodeSignature $bootstrapper" in workflow
    assert "Microsoft Corporation" in workflow
    assert workflow.count(
        "Select-String -Path $log -Pattern 'DESKTOP_SMOKE_TEST_OK' -Quiet"
    ) == 1
    assert workflow.count(
        "Select-String -Path $desktopLog -Pattern 'DESKTOP_SMOKE_TEST_OK' -Quiet"
    ) == 1
    assert "$env:TSP_DESKTOP_GUI_SMOKE_TEST = '1'" in workflow
    assert "DESKTOP_AUTH_BYPASS_SMOKE_TEST_OK" in workflow
    assert "matrix.platform == 'macos' || matrix.platform == 'linux'" in workflow
    assert "packaging/smtp_sink.py" not in workflow
    assert "TSP_DESKTOP_SMTP_SMOKE_TEST" not in workflow
    assert (
        "Select-String -Path $desktopLog -Pattern "
        "'DESKTOP_GUI_SMOKE_TEST_OK renderer=edgechromium' -Quiet"
    ) in workflow


def test_release_stays_draft_until_update_manifest_is_uploaded() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert "draft: true" in workflow
    assert '"url": asset["browser_download_url"]' not in workflow
    assert 'f"https://github.com/{repo}/releases/download/"' in workflow
    assert "urllib.parse.quote(tag, safe='')" in workflow
    assert "urllib.parse.quote(name, safe='')" in workflow
    upload = workflow.index('"name": "latest.json"')
    publish = workflow.index('body={"draft": False}')
    assert upload < publish
