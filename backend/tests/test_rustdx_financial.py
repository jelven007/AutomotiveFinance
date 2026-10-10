"""Independent archive transport/parsing and point-in-time financial contracts."""

import hashlib
import io
import struct
import zipfile

import pytest

from app.plugins.rustdx.financial import financial_history, parse_archive
from app.plugins.rustdx.provider import RustdxProvider


def archive_bytes(*, announcement=20260820):
    rows = []
    header = struct.pack("<hIH3L", 1, 20260630, 2, 0, 314 * 4, 0)
    offset = len(header) + 2 * 11
    directory = b""
    for code in ["600519", "000001"]:
        values = [float(index) for index in range(1, 315)]
        values[0] = 3.5
        values[313] = float(announcement)
        raw = struct.pack("<314f", *values)
        directory += struct.pack("<6scL", code.encode(), b"\0", offset)
        rows.append(raw)
        offset += len(raw)
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("gpcw20260630.dat", header + directory + b"".join(rows))
    return payload.getvalue()


def test_financial_uses_native_download_and_verified_cache(tmp_path):
    payload = archive_bytes()
    checksum = hashlib.md5(payload).hexdigest()
    calls = []

    class Native:
        def report_file(self, filename, max_bytes):
            calls.append((filename, max_bytes))
            if filename.endswith(".txt"):
                return (
                    f"gpcw20260630.zip,{checksum},{len(payload)}\n"
                    f"gpcw20991231.zip,{checksum},{len(payload)}\n"
                    "gpcw20260930.zip,invalid,250\n"
                ).encode()
            return payload

    kwargs = dict(periods=8, columns={"col1", "col314"}, cache_dir=tmp_path)
    rows = financial_history(Native(), ["600519.SH"], **kwargs)
    assert rows == [
        {"code": "600519", "report_date": "20260630", "col1": 3.5, "col314": 20260820.0}
    ]
    financial_history(Native(), ["600519.SH"], **kwargs)
    assert sum(name.endswith(".zip") for name, _ in calls) == 1
    mapped = RustdxProvider._map_history("metrics", rows)
    assert mapped["eps_basic"].to_list() == [3.5]
    assert mapped["announce_date"].to_list() == ["2026-08-20"]


def test_financial_checksum_failure_is_not_cached(tmp_path):
    class Native:
        def report_file(self, filename, max_bytes):
            if filename.endswith(".txt"):
                return f"gpcw20260630.zip,{'0' * 32},300\n".encode()
            return b"x" * 300

    with pytest.raises(ValueError, match="校验失败"):
        financial_history(Native(), ["600519.SH"], periods=1, columns={"col1"}, cache_dir=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_financial_preserves_unknown_announcement(tmp_path):
    path = tmp_path / "report.zip"
    path.write_bytes(archive_bytes(announcement=0))
    rows = parse_archive(path, {"600519"}, {"col1", "col314"}, "20260630")
    frame = RustdxProvider._map_history("metrics", rows)
    assert frame.is_empty()


def test_financial_rejects_report_date_mismatch(tmp_path):
    path = tmp_path / "report.zip"
    path.write_bytes(archive_bytes())
    with pytest.raises(ValueError, match="日期"):
        parse_archive(path, {"600519"}, {"col1"}, "20260331")


def test_financial_rejects_archive_with_multiple_members(tmp_path):
    path = tmp_path / "report.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("report.dat", b"x")
        archive.writestr("../extra.dat", b"x")
    with pytest.raises(ValueError, match="结构"):
        parse_archive(path, {"600519"}, {"col1"}, "20260630")
