"""Desktop runtime and Windows packaging regression tests."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from app import desktop

ROOT = Path(__file__).resolve().parents[2]


def test_smoke_plugin_validation_accepts_complete_mootdx() -> None:
    desktop._validate_smoke_plugins(
        [
            {
                "name": "mootdx",
                "available": True,
                "datasets": sorted(desktop._MOOTDX_DATASETS),
            }
        ]
    )


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
        {"name": "mootdx", "available": False, "status": "missing"},
        {"name": "mootdx", "available": True, "datasets": ["daily"]},
    ],
)
def test_smoke_plugin_validation_rejects_incomplete_mootdx(plugin: dict | None) -> None:
    with pytest.raises(RuntimeError, match="mootdx"):
        desktop._validate_smoke_plugins([] if plugin is None else [plugin])


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

    assert '"mootdx", "tdxpy"' in spec
    assert 'BUILTIN_PLUGINS.glob("*/plugin.yaml")' in spec
    assert '"mootdx", "tdxpy",' in spec


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


def test_release_stays_draft_until_update_manifest_is_uploaded() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert "draft: true" in workflow
    upload = workflow.index('"name": "latest.json"')
    publish = workflow.index('body={"draft": False}')
    assert upload < publish
