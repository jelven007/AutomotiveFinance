"""Immutable manifests for globally published local-data releases."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app import __version__
from app.enriched_generation import (
    EnrichedGenerationUnavailableError,
    enriched_publication_incomplete,
    get_enriched_generation,
    guard_enriched_generations,
)
from app.services.provider_audit import audit_provider_routes

SCHEMA_VERSION = 1
_RELEASE_ROOT = "data_releases"

_DATASET_PATHS = {
    "stock_daily": "kline_daily",
    "stock_enriched": "kline_daily_enriched",
    "stock_minute": "kline_minute",
    "stock_adj_factor": "adj_factor",
    "instruments": "instruments",
    "instrument_status": "instrument_status",
    "financials": "financials",
    "auction_snapshot": "auction_snapshot",
    "index_daily": "kline_index_daily",
    "index_enriched": "kline_index_enriched",
    "etf_daily": "kline_etf_daily",
    "etf_enriched": "kline_etf_enriched",
    "etf_minute": "kline_etf_minute",
    "etf_adj_factor": "adj_factor_etf",
    "depth5": "depth5",
}


class DataReleaseError(RuntimeError):
    """A release manifest is missing, malformed, or cannot be published."""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(
                payload,
                stream,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _partition_date(path: Path) -> str | None:
    name = path.name
    if not name.startswith("date="):
        return None
    value = name.removeprefix("date=")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None
    return value


def _dataset_snapshot(root: Path) -> dict[str, Any]:
    if not root.exists():
        return {
            "state": "absent",
            "latest_partition": None,
            "partition_count": 0,
            "modified_at_ns": None,
        }

    partitions = [
        (value, child)
        for child in root.glob("date=*")
        if (value := _partition_date(child)) is not None
    ]
    latest_partition = max((value for value, _ in partitions), default=None)
    candidates = [root]
    candidates.extend(child for _, child in partitions)
    candidates.extend(child for child in root.iterdir() if child.is_file())
    candidates.extend(child for child in root.iterdir() if child.is_dir() and not partitions)
    modified_at_ns: int | None = None
    for candidate in candidates:
        try:
            value = candidate.stat().st_mtime_ns
        except OSError:
            continue
        modified_at_ns = value if modified_at_ns is None else max(modified_at_ns, value)

    has_content = bool(partitions) or any(root.iterdir())
    return {
        "state": "available" if has_content else "empty",
        "latest_partition": latest_partition,
        "partition_count": len(partitions),
        "modified_at_ns": modified_at_ns,
    }


def _generation(data_dir: Path, asset_type: str) -> str | None:
    try:
        return get_enriched_generation(data_dir, asset_type, initialize=False)
    except EnrichedGenerationUnavailableError:
        return None


def collect_dataset_snapshot(data_dir: Path) -> dict[str, dict[str, Any]]:
    """Collect bounded metadata without hashing or loading Parquet contents."""
    data_dir = Path(data_dir)
    datasets = {
        name: _dataset_snapshot(data_dir / relative)
        for name, relative in _DATASET_PATHS.items()
    }
    datasets["stock_enriched"]["generation"] = _generation(data_dir, "stock")
    datasets["etf_enriched"]["generation"] = _generation(data_dir, "etf")
    return datasets


def publish_data_release(data_dir: Path, *, reason: str) -> dict[str, Any]:
    """Publish an immutable manifest, then atomically advance current.json."""
    data_dir = Path(data_dir)
    generations = _publication_generations(data_dir)
    release_id = uuid.uuid4().hex
    provider_audit = audit_provider_routes()
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "release_id": release_id,
        "created_at": _utc_now(),
        "reason": str(reason),
        "app_version": __version__,
        "providers": {
            capability: route["provider"]
            for capability, route in provider_audit["routes"].items()
        },
        "provider_audit": provider_audit,
        "datasets": collect_dataset_snapshot(data_dir),
    }
    after = _publication_generations(data_dir)
    captured = {
        asset: manifest["datasets"][f"{asset}_enriched"]["generation"]
        for asset in generations
    }
    if generations != after or captured != after:
        raise DataReleaseError("enriched generation changed while collecting release metadata")
    _validate_manifest(manifest)
    release_root = data_dir / _RELEASE_ROOT
    history_path = release_root / "manifests" / f"{release_id}.json"
    current_path = release_root / "current.json"
    try:
        with guard_enriched_generations(data_dir, after):
            _atomic_write_json(history_path, manifest)
            _atomic_write_json(current_path, manifest)
    except EnrichedGenerationUnavailableError as exc:
        raise DataReleaseError(str(exc)) from exc
    return manifest


def _publication_generations(data_dir: Path) -> dict[str, str | None]:
    generations = {}
    for asset in ("stock", "etf"):
        if enriched_publication_incomplete(data_dir, asset):
            raise DataReleaseError(f"{asset} enriched publication is incomplete")
        generations[asset] = _generation(data_dir, asset)
        if generations[asset] is None and enriched_publication_incomplete(data_dir, asset):
            raise DataReleaseError(f"{asset} enriched publication is incomplete")
    return generations


def _validate_manifest(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise DataReleaseError("data release manifest must be an object")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise DataReleaseError("unsupported data release manifest schema")
    release_id = payload.get("release_id")
    if not isinstance(release_id, str) or not release_id:
        raise DataReleaseError("data release manifest has no release_id")
    if not isinstance(payload.get("datasets"), dict):
        raise DataReleaseError("data release manifest has no datasets")
    for name, dataset in payload["datasets"].items():
        if not isinstance(dataset, dict):
            raise DataReleaseError(f"invalid dataset metadata: {name}")
        if dataset.get("state") not in ("absent", "empty", "available"):
            raise DataReleaseError(f"invalid dataset state: {name}")
        for field in ("partition_count", "modified_at_ns"):
            value = dataset.get(field)
            if value is not None and (type(value) is not int or value < 0):
                raise DataReleaseError(f"invalid {field}: {name}")
        latest = dataset.get("latest_partition")
        if latest is not None and (
            not isinstance(latest, str) or _partition_date(Path(f"date={latest}")) != latest
        ):
            raise DataReleaseError(f"invalid latest_partition: {name}")
        generation = dataset.get("generation")
        if generation is not None and (not isinstance(generation, str) or not generation):
            raise DataReleaseError(f"invalid generation: {name}")
    providers = payload.get("providers", {})
    if not isinstance(providers, dict) or any(
        not isinstance(value, str) for value in providers.values()
    ):
        raise DataReleaseError("invalid release providers")
    return payload


def read_current_release(data_dir: Path) -> dict[str, Any] | None:
    path = Path(data_dir) / _RELEASE_ROOT / "current.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DataReleaseError("data release manifest is unreadable") from exc
    return _validate_manifest(payload)


def ensure_current_release(data_dir: Path) -> dict[str, Any]:
    current = read_current_release(data_dir)
    return current if current is not None else publish_data_release(
        data_dir,
        reason="startup_baseline",
    )


def stable_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def persist_backtest_manifest(
    data_dir: Path,
    *,
    kind: str,
    result: Mapping[str, Any],
) -> dict[str, Any]:
    """Persist compact trace metadata without duplicating large backtest output."""
    run_id = result.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise DataReleaseError("backtest result has no run_id")
    config = result.get("config")
    provenance = result.get("provenance")
    if not isinstance(config, Mapping) or not isinstance(provenance, Mapping):
        raise DataReleaseError("backtest result has no traceable config/provenance")
    stats = result.get("stats")
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "kind": str(kind),
        "recorded_at": _utc_now(),
        "config": dict(config),
        "provenance": dict(provenance),
        "error": result.get("error"),
        "stats_hash": stable_hash(stats) if isinstance(stats, Mapping) else None,
        "trade_count": len(result.get("trades") or []),
    }
    path = Path(data_dir) / "backtest_results" / "manifests" / f"{run_id}.json"
    _atomic_write_json(path, manifest)
    return manifest


def backtest_provenance(
    data_dir: Path,
    *,
    asset_type: str,
    config: Mapping[str, Any],
    strategy_hash: str | None = None,
    data_generation: str | None = None,
) -> dict[str, Any]:
    """Capture the release and managed generation used by a backtest."""
    data_dir = Path(data_dir)
    try:
        release = read_current_release(data_dir)
        release_error = None
    except DataReleaseError as exc:
        release = None
        release_error = str(exc)

    if data_generation is None:
        data_generation = _generation(data_dir, asset_type)
    dataset_name = "etf_enriched" if asset_type == "etf" else "stock_enriched"
    release_dataset = (
        (release.get("datasets") or {}).get(dataset_name, {})
        if release is not None
        else {}
    )
    release_generation = release_dataset.get("generation")
    return {
        "schema_version": SCHEMA_VERSION,
        "captured_at": _utc_now(),
        "dataset_release_id": release.get("release_id") if release else None,
        "dataset_release_created_at": release.get("created_at") if release else None,
        "dataset_release_reason": release.get("reason") if release else None,
        "asset_type": asset_type,
        "data_generation": data_generation,
        "release_generation": release_generation,
        "release_consistent": bool(
            release is not None
            and data_generation is not None
            and release_generation == data_generation
        ),
        "providers": dict(release.get("providers") or {}) if release else {},
        "config_hash": stable_hash(config),
        "strategy_hash": strategy_hash,
        "release_error": release_error,
    }


def release_health(data_dir: Path) -> dict[str, Any]:
    """Return current release readability and enriched-generation consistency."""
    try:
        release = read_current_release(data_dir)
    except DataReleaseError as exc:
        return {"status": "error", "release_id": None, "message": str(exc)}
    if release is None:
        try:
            _publication_generations(Path(data_dir))
        except DataReleaseError as exc:
            return {"status": "error", "release_id": None, "message": str(exc)}
        return {
            "status": "warning",
            "release_id": None,
            "message": "尚未发布数据 release manifest",
        }

    datasets = release.get("datasets") or {}
    mismatches: list[str] = []
    unavailable: list[str] = []
    for asset_type, dataset_name in (
        ("stock", "stock_enriched"),
        ("etf", "etf_enriched"),
    ):
        current = _generation(Path(data_dir), asset_type)
        if enriched_publication_incomplete(Path(data_dir), asset_type):
            unavailable.append(dataset_name)
        published = (datasets.get(dataset_name) or {}).get("generation")
        if current != published:
            mismatches.append(dataset_name)
    return {
        "status": "error" if unavailable else ("warning" if mismatches else "ok"),
        "release_id": release["release_id"],
        "created_at": release.get("created_at"),
        "reason": release.get("reason"),
        "generation_mismatches": mismatches,
        "unavailable_generations": unavailable,
        "message": (
            "enriched 发布未完成, 需要完整重建恢复"
            if unavailable
            else "存在尚未纳入全局 release 的 enriched 更新"
            if mismatches
            else None
        ),
    }
