"""内置概念/行业 preset 使用显式上游配置。

ensure_builtin_presets 只负责创建配置, 不直接等待网络请求; 随后
PullScheduler.refresh 会为配置了 URL 的 enabled 配置创建后台任务。未配置 URL
时默认关闭, 避免新安装隐式访问外部服务。已有配置必须保持用户设置, 不因启动
而被覆盖。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.config import settings
from app.services.ext_data import ExtConfigStore
from app.services.ext_presets import (
    _concept_preset,
    _industry_preset,
    ensure_builtin_presets,
)

_PRESET_IDS = ("ext_gn_ths", "ext_hy_ths")


def test_builtin_presets_are_disabled_without_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全新安装未配置上游时保留表结构, 但不自动访问外部服务。"""
    monkeypatch.setattr(settings, "ext_concept_data_url", "")
    monkeypatch.setattr(settings, "ext_industry_data_url", "")

    for preset in (_concept_preset(), _industry_preset()):
        assert preset.pull is not None
        assert preset.pull.url == ""
        assert preset.pull.enabled is False
        assert preset.pull.schedule_minutes == 1
        assert preset.pull.time_window_start == "09:15"
        assert preset.pull.time_window_end == "15:15"


def test_ensure_builtin_presets_writes_enabled_configs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """显式配置上游后写入启用配置, 供调度器创建盘中定时任务。"""
    monkeypatch.setattr(settings, "ext_concept_data_url", "https://data.example/concepts")
    monkeypatch.setattr(settings, "ext_industry_data_url", "https://data.example/industries")

    asyncio.run(ensure_builtin_presets(tmp_path))

    store = ExtConfigStore(tmp_path)
    for cid in _PRESET_IDS:
        config = store.get(cid)
        assert config is not None, f"{cid} 配置未创建"
        assert config.pull is not None
        assert config.pull.url.startswith("https://data.example/")
        assert config.pull.enabled is True
        assert config.pull.schedule_minutes == 1
        assert config.pull.time_window_start == "09:15"
        assert config.pull.time_window_end == "15:15"


def test_ensure_builtin_presets_keeps_existing_user_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """老用户/已存在的配置一律不动, 包括用户主动关闭自动拉取。"""
    monkeypatch.setattr(settings, "ext_concept_data_url", "https://data.example/concepts")
    monkeypatch.setattr(settings, "ext_industry_data_url", "https://data.example/industries")

    asyncio.run(ensure_builtin_presets(tmp_path))
    store = ExtConfigStore(tmp_path)
    config = store.get("ext_gn_ths")
    assert config is not None and config.pull is not None
    config.pull.enabled = False
    store.upsert(config)

    asyncio.run(ensure_builtin_presets(tmp_path))

    refreshed = ExtConfigStore(tmp_path).get("ext_gn_ths")
    assert refreshed is not None and refreshed.pull is not None
    assert refreshed.pull.enabled is False, "已存在配置被静默改写, 违反「绝不覆盖」原则"
