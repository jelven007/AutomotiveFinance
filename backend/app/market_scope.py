"""沪深市场边界与旧市场数据清理; 不改变北京时间或历史交易规则。"""
from __future__ import annotations

import json
import re
from pathlib import Path

import polars as pl

STOCK_BOARDS = ("沪主板", "深主板", "创业板", "科创板")
_REMOVED_CODE = re.compile(r"^(?:[48]\d{5}|92\d{4})(?:\.[A-Z]+)?$")
_SECURITY = re.compile(r"^\d{6}\.(SH|SZ)$")
_MARKER = ".sh-sz-market-v1.json"


def removed_market_symbol(value: object) -> bool:
    """识别旧市场代码, 包括错误标为沪深的旧代码。"""
    text = str(value or "").strip().upper()
    return text.endswith(".BJ") or bool(_REMOVED_CODE.fullmatch(text))


def supported_symbol(value: object, asset_type: str | None = None) -> bool:
    text = str(value or "").strip().upper()
    if asset_type == "stock":
        from app.data_providers.instrument_status import current_a_share_identity
        return current_a_share_identity(text) is not None
    return bool(_SECURITY.fullmatch(text)) and not removed_market_symbol(text)


def filter_market_frame(df):
    """只删除可识别的旧市场记录, 保留扩展表的非证券维度及原 schema。"""
    names = df.collect_schema().names() if isinstance(df, pl.LazyFrame) else df.columns
    predicates = []
    for name in ("symbol", "ts_code", "code", "thscode"):
        if name in names:
            text = pl.col(name).cast(pl.String, strict=False).str.strip_chars().str.to_uppercase()
            removed = text.str.ends_with(".BJ") | text.str.contains(r"^(?:[48]\d{5}|92\d{4})(?:\.[A-Z]+)?$")
            predicates.append(~removed.fill_null(False))
    if not predicates:
        return df
    result = df.filter(pl.all_horizontal(predicates))
    # 未发生市场变化时保留原快照对象, 维持分钟路由的缓存身份契约。
    if isinstance(df, pl.DataFrame) and result.height == df.height:
        return df
    return result


def clean_market_config(value):
    """删除旧证券引用和板块选择; 保留合法选项与其他用户设置。"""
    if isinstance(value, dict):
        if any(removed_market_symbol(value.get(k)) for k in ("symbol", "ts_code", "thscode")):
            return None
        result = {}
        for key, item in value.items():
            if removed_market_symbol(key):
                continue
            if key == "symbols" and isinstance(item, list):
                result[key] = [symbol for symbol in item if not removed_market_symbol(symbol)]
                continue
            if key == "boards" and isinstance(item, list) and all(isinstance(board, str) for board in item):
                result[key] = [board for board in item if board in STOCK_BOARDS]
                if item and not result[key]:
                    result["market_scope_empty"] = True
            else:
                cleaned = clean_market_config(item)
                if cleaned is not None or item is None:
                    result[key] = cleaned
        if result.get("boards"):
            result.pop("market_scope_empty", None)
        return result
    if isinstance(value, list):
        return [
            cleaned for item in value
            if (cleaned := clean_market_config(item)) is not None or item is None
        ]
    if isinstance(value, str) and (
        ("." in value and removed_market_symbol(value)) or value == "北交所"
    ):
        return None
    return value


def purge_removed_market(data_dir: Path, *, force: bool = False, dry_run: bool = False) -> dict:
    """启动前幂等清理。混合 Parquet 按行改写, 成功完成才写迁移标记。

    逐文件只扫描证券列; 发现命中后才读取全表, 避免加载全量历史指标。
    不跟随目录外的符号链接。任何读写失败向上传播, 避免带着半清理数据启动。
    """
    from app.services.fs_utils import atomic_write_parquet, atomic_write_text

    root = Path(data_dir).resolve()
    marker = root / _MARKER
    if marker.exists() and not force:
        return json.loads(marker.read_text(encoding="utf-8"))
    report = {"parquet_files": 0, "removed_rows": 0, "json_files": 0}
    for path in sorted(root.rglob("*.parquet")):
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            continue
        source = pl.scan_parquet(path, hive_partitioning=False)
        names = source.collect_schema().names()
        columns = [name for name in ("symbol", "ts_code", "code", "thscode") if name in names]
        if not columns:
            continue
        identities = source.select(columns)
        before = identities.select(pl.len()).collect().item()
        after = filter_market_frame(identities).select(pl.len()).collect().item()
        if before == after:
            continue
        report["parquet_files"] += 1
        report["removed_rows"] += before - after
        if not dry_run:
            if after == 0:
                path.unlink()
            else:
                atomic_write_parquet(filter_market_frame(pl.read_parquet(path)), path)
    for path in sorted(root.rglob("*.json")):
        if path == marker or path.is_symlink() or not path.resolve().is_relative_to(root):
            continue
        try:
            original = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            continue
        cleaned = clean_market_config(original)
        if original == cleaned:
            continue
        report["json_files"] += 1
        if not dry_run:
            if cleaned is None:
                path.unlink()
            else:
                mode = path.stat().st_mode & 0o777
                atomic_write_text(path, json.dumps(cleaned, ensure_ascii=False, indent=2), mode=mode)
    for path in sorted(root.rglob("*.jsonl")):
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            continue
        original_lines = path.read_text(encoding="utf-8").splitlines()
        lines = []
        for line in original_lines:
            try:
                original = json.loads(line)
            except json.JSONDecodeError:
                lines.append(line)
                continue
            cleaned = clean_market_config(original)
            if cleaned is not None:
                lines.append(line if original == cleaned else json.dumps(cleaned, ensure_ascii=False))
        if lines != original_lines:
            report["json_files"] += 1
            if not dry_run:
                atomic_write_text(path, "\n".join(lines) + ("\n" if lines else ""))
    if not dry_run:
        atomic_write_text(marker, json.dumps(report, ensure_ascii=False, indent=2))
    return report
