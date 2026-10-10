from datetime import date

import polars as pl
import pytest

from app.market_scope import (
    clean_market_config,
    filter_market_frame,
    purge_removed_market,
    supported_symbol,
)
from app.price_limits import price_limit_pct
from app.services.ext_data import normalize_symbol
from app.tickflow.repository import DataStore, KlineRepository


def test_market_filter_preserves_indices_etfs_and_non_security_dimensions():
    frame = pl.DataFrame({
        "symbol": ["600000.SH", "003001.SZ", "689001.SH", "000001.SH",
                   "399001.SZ", "510300.SH", "159001.SZ", "920001.BJ",
                   "830001.SH", "430001", "920001.SZ", "sector_growth", None],
        "value": list(range(13)),
    })
    result = filter_market_frame(frame)
    assert result["value"].to_list() == [0, 1, 2, 3, 4, 5, 6, 11, 12]
    assert result.schema == frame.schema
    assert filter_market_frame(frame.lazy()).collect().equals(result)
    assert supported_symbol("000001.SH", "index")
    assert not supported_symbol("000001.SH", "stock")
    assert not supported_symbol("830001.SH")


def test_migration_cleans_mixed_files_and_config_idempotently(tmp_path):
    mixed = tmp_path / "financials" / "shares" / "all.parquet"
    mixed.parent.mkdir(parents=True)
    frame = pl.DataFrame({
        "symbol": ["600000.SH", "920001.BJ"],
        "announce_date": [date(2026, 1, 2)] * 2,
        "source": ["rustdx", "legacy"],
        "is_risk_warning": [True, False],
    })
    frame.write_parquet(mixed)
    only = tmp_path / "old.parquet"
    frame.tail(1).write_parquet(only)
    config = tmp_path / "rules.json"
    config.write_text('{"basic_filter":{"boards":["北交所"]},'
                      '"symbols":["600000.SH","920001.BJ"],"threshold":null}')
    audit = purge_removed_market(tmp_path, dry_run=True)
    assert audit == {"parquet_files": 2, "removed_rows": 2, "json_files": 1}
    assert pl.read_parquet(mixed).height == 2
    assert purge_removed_market(tmp_path) == audit
    assert not only.exists()
    assert pl.read_parquet(mixed).equals(frame.head(1))
    assert '"market_scope_empty": true' in config.read_text()
    assert '"threshold": null' in config.read_text()
    mtime = mixed.stat().st_mtime_ns
    purge_removed_market(tmp_path)
    assert mixed.stat().st_mtime_ns == mtime
    assert purge_removed_market(tmp_path, force=True, dry_run=True)["removed_rows"] == 0


def test_migration_does_not_follow_external_symlinks(tmp_path):
    outside = tmp_path.parent / "outside.parquet"
    pl.DataFrame({"symbol": ["920001.BJ"]}).write_parquet(outside)
    (tmp_path / "linked.parquet").symlink_to(outside)
    assert purge_removed_market(tmp_path)["removed_rows"] == 0
    assert pl.read_parquet(outside).height == 1


def test_migration_deletes_old_orders_and_cleans_line_records(tmp_path):
    old_order = tmp_path / "order.json"
    old_order.write_text('{"symbol":"920001.BJ","quantity":100}')
    fills = tmp_path / "fills.jsonl"
    kept = '{"symbol":"600000.SH","quantity":100,"price":8.5}'
    fills.write_text(kept + '\n{"symbol":"920001.BJ","quantity":100}\n')
    secrets = tmp_path / "secrets.json"
    secrets.write_text('{"api_key":"830001","symbols":["920001.BJ","600000.SH"]}')
    secrets.chmod(0o600)
    purge_removed_market(tmp_path)
    assert not old_order.exists()
    assert fills.read_text() == kept + "\n"
    assert '"api_key": "830001"' in secrets.read_text()
    assert secrets.stat().st_mode & 0o777 == 0o600


def test_legacy_board_only_filter_does_not_become_all_market():
    from app.backtest.matrix import build_basic_filter_mask, build_market_data_matrix
    old = {"enabled": True, "boards": ["北交所"], "exclude_st": False}
    cleaned = clean_market_config(old)
    assert cleaned["boards"] == []
    assert cleaned["market_scope_empty"]
    panel = pl.DataFrame({
        "symbol": ["600000.SH"], "date": [date(2026, 1, 2)],
        "open": [10.0], "high": [10.0], "low": [10.0],
        "close": [10.0], "volume": [1000.0],
    })
    market = build_market_data_matrix(panel)
    assert not build_basic_filter_mask(market, cleaned).any()
    assert clean_market_config({**cleaned, "boards": ["沪主板"]}) == {
        "enabled": True, "boards": ["沪主板"], "exclude_st": False,
    }
    assert build_basic_filter_mask(market, {**old, "boards": []}).all()


def test_import_and_repository_cannot_restore_removed_market(tmp_path):
    symbols = normalize_symbol(pl.Series("symbol", ["600000", "830001", "920001.BJ"]))
    assert symbols.to_list() == ["600000.SH", "830001", "920001.BJ"]
    repo = KlineRepository(DataStore(tmp_path))
    panel = pl.DataFrame({
        "symbol": ["600000.SH", "920001.BJ", "830001.SZ"],
        "date": [date(2026, 1, 2)] * 3,
        "open": [10.0] * 3, "high": [10.0] * 3, "low": [10.0] * 3,
        "close": [10.0] * 3, "volume": [1000.0] * 3,
    })
    repo.append_daily(panel)
    path = tmp_path / "kline_daily" / "date=2026-01-02" / "part.parquet"
    assert pl.read_parquet(path)["symbol"].to_list() == ["600000.SH"]
    with pytest.raises(ValueError, match="不支持"):
        price_limit_pct("920001.BJ", date(2026, 1, 2))
    # 迁移后用户再次导入混合历史数据, 矩阵读取仍只计算沪深。
    panel.write_parquet(path)
    from app.backtest.matrix import load_market_data_matrix_from_parquet
    market = load_market_data_matrix_from_parquet(
        tmp_path / "kline_daily", date(2026, 1, 2), date(2026, 1, 2),
        field_columns={"price_limit_pct"},
    )
    assert market.symbols == ("600000.SH",)


def test_monitor_and_paper_reject_removed_market(tmp_path):
    from app.strategy import monitor_rules, paper
    rule = monitor_rules.normalize({
        "id": "legacy_market", "name": "旧市场", "type": "price",
        "scope": "symbols", "symbols": ["920001.BJ"],
        "conditions": [{"field": "close", "op": ">", "value": 10}],
    })
    assert rule["symbols"] == []
    with pytest.raises(ValueError, match="不能为空"):
        monitor_rules.validate(rule)
    paper.create_account(tmp_path, 100_000)
    order, error = paper.create_order(tmp_path, "920001.BJ", "buy", qty=100)
    assert order is None
    assert error == "模拟交易仅支持沪深市场"
