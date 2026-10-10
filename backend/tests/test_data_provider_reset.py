"""Removing a source restores all affected routes without changing other choices."""

from types import SimpleNamespace

import pytest

from app.api import settings as settings_api
from app.data_providers import custom
from app.services import data_integrity, preferences, trading_day


@pytest.fixture
def selected_source(tmp_path, monkeypatch):
    monkeypatch.setattr(preferences, "_path", lambda: tmp_path / "preferences.json")
    preferences._invalidate_cache()
    stored = dict.fromkeys(preferences._DATA_PROVIDER_FIELDS, "example_source")
    stored["financial_data_provider"] = "fuyao"
    preferences.save(stored)
    monkeypatch.setattr(settings_api, "list_data_sources", lambda: {})
    monkeypatch.setattr(custom, "is_builtin", lambda name: True)
    monkeypatch.setattr(custom, "load_all", lambda: None)
    yield stored
    preferences._invalidate_cache()
    preferences._invalidate_cache()


@pytest.mark.parametrize("success", [True, False])
def test_uninstall_resets_routes_only_after_success(selected_source, monkeypatch, success):
    monkeypatch.setattr(custom, "uninstall_plugin", lambda name: (success, "result"))

    result = settings_api.uninstall_plugin("example_source")

    assert result["uninstall_ok"] is success
    loaded = preferences.load()
    for field in preferences._DATA_PROVIDER_FIELDS:
        assert loaded[field] == (
            "rustdx" if success and selected_source[field] == "example_source"
            else selected_source[field]
        )


def test_delete_restores_routes_before_reload_and_refreshes_capabilities(
    selected_source, monkeypatch,
):
    reloaded = []
    monkeypatch.setattr(custom, "delete_config", lambda name: None)
    monkeypatch.setattr(custom, "load_all", lambda: reloaded.append(preferences.load()))
    monkeypatch.setattr(data_integrity, "reset_calendar_cache", lambda: None)
    monkeypatch.setattr(trading_day, "reset_cache", lambda: None)
    monkeypatch.setattr(settings_api, "detect_capabilities", lambda: "fresh")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    settings_api.delete_data_source("example_source", request)

    assert reloaded[0]["minute_data_provider"] == "rustdx"
    assert reloaded[0]["full_minute_data_provider"] == "rustdx"
    assert reloaded[0]["depth5_data_provider"] == "rustdx"
    assert reloaded[0]["financial_data_provider"] == "fuyao"
    assert request.app.state.capabilities == "fresh"


def test_failed_delete_keeps_routes(selected_source, monkeypatch):
    def failed(name):
        raise ValueError("cannot delete")

    monkeypatch.setattr(custom, "delete_config", failed)
    with pytest.raises(settings_api.HTTPException, match="cannot delete"):
        settings_api.delete_data_source("example_source", SimpleNamespace())
    assert preferences.load() == selected_source
