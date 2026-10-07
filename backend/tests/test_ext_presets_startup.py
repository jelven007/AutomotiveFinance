"""内置概念/行业 preset 默认启用交易日盘中每小时自动拉取。

ensure_builtin_presets 只负责创建配置, 不直接等待网络请求; 随后
PullScheduler.refresh 会为 enabled 配置创建后台任务, 启动时立即拉取一次,
后续在交易日 09:00-16:00 每 60 分钟刷新。已有配置必须保持用户设置,
不因启动而被覆盖。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from app.services.ext_data import ExtConfigStore
from app.services.ext_presets import (
    _concept_preset,
    _industry_preset,
    ensure_builtin_presets,
)

_PRESET_IDS = ("ext_gn_ths", "ext_hy_ths")


def test_builtin_presets_ship_with_market_hours_pull_enabled() -> None:
    """全新安装的内置 preset 默认在交易日盘中每小时自动拉取。"""
    for preset in (_concept_preset(), _industry_preset()):
        assert preset.pull is not None
        assert preset.pull.url
        assert preset.pull.enabled is True
        assert preset.pull.schedule_minutes == 60
        assert preset.pull.time_window_start == "09:00"
        assert preset.pull.time_window_end == "16:00"


def test_ensure_builtin_presets_writes_enabled_configs(tmp_path: Path) -> None:
    """全新数据目录写入启用的配置, 供调度器创建盘中小时任务。"""
    asyncio.run(ensure_builtin_presets(tmp_path))

    store = ExtConfigStore(tmp_path)
    for cid in _PRESET_IDS:
        config = store.get(cid)
        assert config is not None, f"{cid} 配置未创建"
        assert config.pull is not None
        assert config.pull.enabled is True
        assert config.pull.schedule_minutes == 60
        assert config.pull.time_window_start == "09:00"
        assert config.pull.time_window_end == "16:00"


def test_ensure_builtin_presets_keeps_existing_user_config(tmp_path: Path) -> None:
    """老用户/已存在的配置一律不动, 包括用户主动关闭自动拉取。"""
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
