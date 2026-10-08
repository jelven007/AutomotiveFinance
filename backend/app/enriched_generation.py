from __future__ import annotations

import json
import os
import threading
import time
import uuid
import weakref
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, BinaryIO

import polars as pl


class EnrichedGenerationUnavailableError(RuntimeError):
    """The enriched dataset has no stable generation available for readers."""


_WRITER_LOCKS_GUARD = threading.Lock()
_WRITER_LOCKS: dict[tuple[str, str], threading.RLock] = {}
_ACTIVE_PUBLICATIONS: weakref.WeakValueDictionary[str, EnrichedPublication] = (
    weakref.WeakValueDictionary()
)


def _marker_path(data_dir: Path, asset_type: str) -> Path:
    return Path(data_dir) / f".matrix_generation_{asset_type}.json"


def _writer_lock(data_dir: Path, asset_type: str) -> threading.RLock:
    key = (str(Path(data_dir).resolve()), asset_type)
    with _WRITER_LOCKS_GUARD:
        return _WRITER_LOCKS.setdefault(key, threading.RLock())


def _read_marker(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise EnrichedGenerationUnavailableError(
            "enriched data generation marker is invalid"
        ) from exc
    if not isinstance(payload, dict):
        raise EnrichedGenerationUnavailableError(
            "enriched data generation marker is invalid"
        )
    return payload


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_marker(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _unlock_file(stream: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _try_lock_file(stream: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise EnrichedGenerationUnavailableError(
                "another enriched publication is active"
            ) from exc
        return
    import fcntl

    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        raise EnrichedGenerationUnavailableError(
            "another enriched publication is active"
        ) from exc


@contextmanager
def _exclusive_generation_lock(data_dir: Path, asset_type: str) -> Iterator[None]:
    lock_path = Path(data_dir) / f".matrix_generation_{asset_type}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with (
        _writer_lock(data_dir, asset_type),
        lock_path.open("a+b") as stream,
    ):
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        _try_lock_file(stream)
        try:
            yield
        finally:
            _unlock_file(stream)


def _process_is_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (OSError, PermissionError) as exc:
        # Windows 对不存在的 pid 返回 WinError 87 (ERROR_INVALID_PARAMETER),
        # 不会映射为 ProcessLookupError; 按存活处理会让孤儿发布锁永远无法恢复。
        if getattr(exc, "winerror", None) == 87:
            return False
        return True
    return True


def _ready_payload(generation: str) -> dict[str, Any]:
    return {
        "state": "ready",
        "generation": generation,
        "updated_at_ns": time.time_ns(),
    }


def _is_ready_payload(payload: dict[str, Any]) -> bool:
    generation = payload.get("generation")
    return (
        payload.get("state", "ready") == "ready"
        and isinstance(generation, str)
        and bool(generation)
    )


def _publication_claim_is_running(payload: dict[str, Any]) -> bool:
    """标记指向的发布是否仍在推进: 进程内活跃对象存在, 或属主进程仍存活。

    owner_pid 等于当前进程但无活跃对象视为可接管 (同进程上一次尝试的遗留),
    与写入方 recover 接管的判定一致。
    """
    if _ACTIVE_PUBLICATIONS.get(str(payload.get("publication_id"))) is not None:
        return True
    owner_pid = payload.get("owner_pid")
    return owner_pid != os.getpid() and _process_is_alive(owner_pid)


def get_enriched_generation(
    data_dir: Path,
    asset_type: str = "stock",
    *,
    initialize: bool = True,
) -> str:
    path = _marker_path(data_dir, asset_type)
    payload = _read_marker(path)
    if payload is None:
        if not initialize:
            raise EnrichedGenerationUnavailableError(
                "enriched data generation marker is unavailable"
            )
    elif _is_ready_payload(payload):
        return payload["generation"]
    else:
        # 属主退出也不能证明多个分区已写完整; 读取方不得解除未完成发布。
        raise EnrichedGenerationUnavailableError(
            "enriched data is being published; retry after the update finishes"
        )
    # 仅允许初始化缺失的标记, 并在锁内复核以免覆盖并发发布。
    with _exclusive_generation_lock(data_dir, asset_type):
        payload = _read_marker(path)
        if payload is None:
            generation = uuid.uuid4().hex
            _write_marker(path, _ready_payload(generation))
            return generation
        if _is_ready_payload(payload):
            return payload["generation"]
        raise EnrichedGenerationUnavailableError(
            "enriched data is being published; retry after the update finishes"
        )


def enriched_publication_incomplete(
    data_dir: Path,
    asset_type: str = "stock",
) -> bool:
    try:
        payload = _read_marker(_marker_path(data_dir, asset_type))
    except EnrichedGenerationUnavailableError:
        return True
    if payload is None:
        return False
    return (
        payload.get("state", "ready") != "ready"
        or not isinstance(payload.get("generation"), str)
        or not payload["generation"]
    )


@contextmanager
def guard_enriched_generations(
    data_dir: Path, expected: dict[str, str | None],
) -> Iterator[None]:
    """短暂锁住发布边界, 防止校验后、release 指针落盘前又有 enriched 写入。"""
    with ExitStack() as stack:
        for asset_type in sorted(expected):
            stack.enter_context(_exclusive_generation_lock(data_dir, asset_type))
        for asset_type, generation in expected.items():
            payload = _read_marker(_marker_path(data_dir, asset_type))
            if payload is None and generation is None:
                continue
            if payload is None or not _is_ready_payload(payload) or payload["generation"] != generation:
                raise EnrichedGenerationUnavailableError(
                    f"{asset_type} enriched generation changed before release publication"
                )
        yield


def bump_enriched_generation(data_dir: Path, asset_type: str = "stock") -> str:
    path = _marker_path(data_dir, asset_type)
    with _exclusive_generation_lock(data_dir, asset_type):
        current = _read_marker(path)
        if current is not None and current.get("state", "ready") != "ready":
            raise EnrichedGenerationUnavailableError(
                "cannot bump an incomplete enriched publication"
            )
        generation = uuid.uuid4().hex
        _write_marker(path, _ready_payload(generation))
        return generation


class EnrichedPublication:
    """Publish one logical enriched write batch under a stable generation token."""

    def __init__(
        self,
        data_dir: Path,
        asset_type: str = "stock",
        *,
        recover: bool = False,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.asset_type = asset_type
        self.recover = recover
        self._publishing = False
        self._changed = False
        self._recovering = False
        self._base_generation: str | None = None
        self._publication_id = uuid.uuid4().hex

    def begin(self) -> None:
        with _exclusive_generation_lock(self.data_dir, self.asset_type):
            self._claim_or_verify()

    def mark_changed(self) -> None:
        if not self._publishing:
            raise RuntimeError("enriched publication has not started")
        self._changed = True

    def abandon(self) -> None:
        if not self._publishing:
            return
        if self._recovering or self._changed:
            # 保留未完成标记, 但释放本次认领, 不依赖 traceback/GC 释放对象。
            self._publishing = False
            self._changed = False
            _ACTIVE_PUBLICATIONS.pop(self._publication_id, None)
            return
        path = _marker_path(self.data_dir, self.asset_type)
        with _exclusive_generation_lock(self.data_dir, self.asset_type):
            current = _read_marker(path)
            if current is not None and current.get("publication_id") == self._publication_id:
                _write_marker(path, _ready_payload(str(self._base_generation)))
        self._publishing = False

    def write_parquet(self, df: pl.DataFrame, out: Path) -> None:
        out.parent.mkdir(parents=True, exist_ok=True)
        temporary = out.with_name(f".{out.name}.{uuid.uuid4().hex}.tmp")
        try:
            df.write_parquet(temporary)
            with temporary.open("r+b") as stream:
                stream.flush()
                os.fsync(stream.fileno())
            with _exclusive_generation_lock(self.data_dir, self.asset_type):
                self._claim_or_verify()
                # 从可能修改目标文件前即视为脏, 包括 replace 后 fsync 失败的路径。
                self._changed = True
                os.replace(temporary, out)
                _fsync_directory(out.parent)
        finally:
            temporary.unlink(missing_ok=True)

    def commit(self) -> str | None:
        if not self._changed:
            return None
        if not self._publishing:
            # 本次是同一发布对象的重复提交: 上一次 commit 已落盘并复位
            # _publishing, 标记也回到了 ready (ready payload 不含
            # publication_id)。写入方会把一个发布对象复用到多个分区
            # (如 ETF enriched 按完整本地历史一次写 245+ 个交易日分区),
            # 其中内容无变化的分区会直接走到这里 —— 此时没有任何待提交
            # 内容, 必须幂等返回, 否则会误判为 "ownership lost" 并打断
            # 整批写入。
            self._changed = False
            return None
        path = _marker_path(self.data_dir, self.asset_type)
        with _exclusive_generation_lock(self.data_dir, self.asset_type):
            current = _read_marker(path)
            if current is None or current.get("publication_id") != self._publication_id:
                raise EnrichedGenerationUnavailableError(
                    "enriched publication ownership was lost"
                )
            generation = uuid.uuid4().hex
            _write_marker(path, _ready_payload(generation))
        self._publishing = False
        # 待发布内容已全部落盘, 复位脏标记: 多日期分区写入复用同一发布对象,
        # 无变化分区会再调 commit() (空提交) — 不复位会以过期脏标记命中
        # ownership 校验而误抛 "ownership was lost" (Issue #417)。
        # 之后的写入会经 _claim_or_verify 重新认领并再次置位。
        self._changed = False
        return generation

    def _claim_or_verify(self) -> None:
        path = _marker_path(self.data_dir, self.asset_type)
        invalid = False
        try:
            current = _read_marker(path)
        except EnrichedGenerationUnavailableError:
            if not self.recover:
                raise
            current = None
            invalid = True
        if self._publishing:
            if current is None or current.get("publication_id") != self._publication_id:
                raise EnrichedGenerationUnavailableError(
                    "enriched publication ownership was lost"
                )
            return
        incomplete = invalid or (current is not None and not _is_ready_payload(current))
        if current is not None and incomplete:
            if _publication_claim_is_running(current):
                raise EnrichedGenerationUnavailableError(
                    "another enriched publication is active"
                )
            if not self.recover:
                raise EnrichedGenerationUnavailableError(
                    "another enriched publication is incomplete"
                )
        self._recovering = incomplete
        generation = None if current is None else current.get("generation")
        if not isinstance(generation, str) or not generation:
            generation = uuid.uuid4().hex
        self._base_generation = generation
        _write_marker(path, {
            "state": "publishing",
            "generation": generation,
            "publication_id": self._publication_id,
            "owner_pid": os.getpid(),
            "updated_at_ns": time.time_ns(),
        })
        self._publishing = True
        _ACTIVE_PUBLICATIONS[self._publication_id] = self
