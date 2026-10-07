"""标的维表同步服务。

盘前 9:10 调用 tf.exchanges.get_instruments("SH"/"SZ"/"BJ", type="stock")
获取全量标的元数据，flatten ext 字段，写入 instruments.parquet。

Starter+ 盘后可用 quotes.get(universes) 顺便补充 name。
"""
from __future__ import annotations

import logging
from pathlib import Path

import polars as pl

from app.market_time import cn_today
from app.services.fs_utils import atomic_write_parquet
from app.tickflow.client import get_client

logger = logging.getLogger(__name__)

_EXCHANGES = ["SH", "SZ", "BJ"]


def _flatten_instruments(items: list[dict]) -> list[dict]:
    """把 SDK 返回的 Instrument 列表 flatten 成扁平行。"""
    rows = []
    for item in items:
        row = {
            "symbol": item.get("symbol"),
            "name": item.get("name"),
            "code": item.get("code"),
            "exchange": item.get("exchange"),
            "region": item.get("region"),
            "type": item.get("type"),
        }
        ext = item.get("ext") or {}
        for field in ("listing_date", "total_shares", "float_shares", "tick_size",
                      "limit_up", "limit_down"):
            row[field] = ext.get(field, item.get(field))
        rows.append(row)
    return rows


def _fetch_instruments_via_provider(asset_type: str = "stock") -> list[dict] | None:
    """Follow the selected daily provider for all instrument catalogs.

    Only an explicit TickFlow selection returns None. An unavailable custom
    catalog returns [], so callers keep their last snapshot without mixing sources.
    """
    from app.services import preferences

    provider_name = preferences.get_daily_data_provider()
    if provider_name == "tickflow":
        return None
    from app.data_providers import custom as custom_sources

    try:
        if not custom_sources.is_custom_provider(provider_name):
            logger.warning("instrument provider %s is unavailable", provider_name)
            return []
        provider = custom_sources.get_provider(provider_name)
        if not callable(getattr(provider, "get_instruments", None)):
            logger.warning("provider %s has no instrument catalog", provider_name)
            return []
        items = provider.get_instruments(asset_type)
        if isinstance(items, pl.DataFrame):
            items = items.to_dicts()
        rows = _flatten_instruments(items or [])
    except Exception as e:  # noqa: BLE001
        logger.warning("provider %s get_instruments 失败: %s", provider_name, e)
        return []
    logger.info("instruments via %s: %d %s", provider_name, len(rows), asset_type)
    return rows


def sync_instruments(data_dir: Path) -> int:
    """全量同步标的维表 → data/instruments/instruments.parquet。

    返回写入的行数。
    """
    all_rows = _fetch_instruments_via_provider()
    if all_rows is None:
        # 未命中非 tickflow provider → 走 tickflow 直连
        tf = get_client()
        all_rows = []
        for ex in _EXCHANGES:
            try:
                items = tf.exchanges.get_instruments(ex, instrument_type="stock")
                if not items and ex in {"SH", "SZ"}:
                    logger.warning("empty %s catalog; keeping previous instruments", ex)
                    return 0
                if items:
                    all_rows.extend(_flatten_instruments(items))
                    logger.info("instruments %s: %d stocks", ex, len(items))
            except Exception as e:
                logger.warning("get_instruments(%s) failed: %s", ex, e)
                return 0

    if not all_rows:
        return 0

    df = pl.DataFrame(all_rows)
    df = df.with_columns(pl.lit(cn_today()).alias("as_of"))

    out = data_dir / "instruments" / "instruments.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_parquet(df, out)

    logger.info("instruments synced: %d rows → %s", df.height, out)
    return df.height


def enrich_names_from_quotes(
    data_dir: Path,
    quotes_data: list[dict],
) -> int:
    """从 quotes 响应中提取 name，更新 instruments 维表（兜底补充）。

    盘后 quotes.get(universes) 返回的数据中包含 ext.name，
    用来补充 instruments 中可能缺失的 name。
    """
    if not quotes_data:
        return 0

    # 构建 symbol → name 映射
    name_map: dict[str, str] = {}
    for q in quotes_data:
        symbol = q.get("symbol", "")
        ext = q.get("ext") or {}
        name = ext.get("name") or q.get("name", "")
        if symbol and name:
            name_map[symbol] = name

    if not name_map:
        return 0

    inst_path = data_dir / "instruments" / "instruments.parquet"
    if not inst_path.exists():
        return 0

    df = pl.read_parquet(inst_path)

    # 只更新空 name 的行
    updates = pl.DataFrame({
        "symbol": list(name_map.keys()),
        "_new_name": list(name_map.values()),
    })
    df = df.join(updates, on="symbol", how="left")
    df = df.with_columns(
        pl.when(pl.col("name").is_null() | (pl.col("name") == ""))
        .then(pl.col("_new_name"))
        .otherwise(pl.col("name"))
        .alias("name"),
    ).drop("_new_name")

    atomic_write_parquet(df, inst_path)
    logger.info("instruments name enriched from quotes: %d names", len(name_map))
    return len(name_map)
