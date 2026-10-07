"""Protocol regression fixtures and verified archive download behavior."""

import hashlib
import io
import struct
import zipfile

import pytest

from app.plugins.mootdx.client import MootdxClient, MootdxError, _download_report
from app.plugins.mootdx.provider import MootdxProvider


def test_explicit_exchange_routes_stock_and_index_bars(monkeypatch):
    client = MootdxClient()
    calls = []

    def invoke(method, *args, **kwargs):
        calls.append((method, args, kwargs))
        return []

    monkeypatch.setattr(client, "_invoke", invoke)
    client.bars("000001.SH", frequency=9)
    client.bars("000001.SZ", frequency=9)
    client.quotes(["000001.SH", "000001.SZ"])
    assert calls[0][:2] == ("get_index_bars", (4, 1, "000001", 0, 800))
    assert calls[1][:2] == ("get_security_bars", (9, 0, "000001", 0, 800))
    assert calls[2][:2] == ("get_security_quotes", ([(1, "000001"), (0, "000001")],))
    with pytest.raises(MootdxError, match="北交所"):
        client.bars("920001.BJ", frequency=9)


def test_live_sample_daily_quote_and_minute_units():
    # Captured through tdxpy 0.2.7 from TDX on 2026-10-07 (last trading day 09-30).
    daily = {
        "datetime": "2026-09-30 15:00", "open": 1239.53, "high": 1268,
        "low": 1236.05, "close": 1258.62, "vol": 38330, "amount": 4797246464,
    }
    minute = {
        "datetime": "2026-09-30 15:00", "open": 1258.62, "high": 1258.62,
        "low": 1258.62, "close": 1258.62, "vol": 64400, "amount": 80998488,
    }
    day = MootdxProvider._daily_frame([daily], "600519.SH", None, None).to_dicts()[0]
    bar = MootdxProvider._minute_frame([minute], "600519.SH", None, None).to_dicts()[0]
    assert day["volume"] == 38330
    assert day["low"] <= day["amount"] / day["volume"] / 100 <= day["high"]
    assert bar["volume"] == 644
    assert bar["amount"] / bar["volume"] / 100 == pytest.approx(bar["close"], abs=1)


def test_tdx_decoder_zero_and_new_etf_price_coefficients(monkeypatch):
    client = MootdxClient()
    monkeypatch.setattr(
        client, "_invoke",
        lambda *a, **k: [{"vol": 2.0 ** -127, "amount": 2.0 ** -127}],
    )
    assert client.bars("600519.SH", frequency=8) == [{"vol": 0, "amount": 0}]
    monkeypatch.setattr(
        client, "_invoke",
        lambda *a, **k: [
            {"code": "588200", "market": 1, "price": 15.32, "bid1": 15.31, "vol": 300},
            {"code": "510300", "market": 1, "price": 4.5, "vol": 300},
        ],
    )
    etf, old_etf = client.quotes(["588200.SH", "510300.SH"])
    assert etf["price"] == pytest.approx(1.532)
    assert etf["bid1"] == pytest.approx(1.531)
    assert etf["vol"] == 300
    assert old_etf["price"] == 4.5


def test_index_minute_amount_proxy_is_not_used_as_volume(monkeypatch):
    client = MootdxClient()
    monkeypatch.setattr(
        client, "_invoke", lambda *a, **k: [{
            "datetime": "2026-09-30 15:00", "close": 3842.19,
            "vol": 94821848.0, "amount": 9482184704.0,
        }],
    )
    rows = client.bars("000001.SH", frequency=8)
    assert rows[0]["vol"] is None
    assert rows[0]["amount"] == 9482184704.0


def _archive(eps=2.5):
    values = [float(i) for i in range(1, 315)]
    values[0] = eps
    values[313] = 240809
    header = struct.pack("<1hI1H3L", 0, 20240630, 1, 0, len(values) * 4, 0)
    stock = struct.pack("<6s1c1L", b"600519", b"\0", len(header) + 11)
    dat = header + stock + struct.pack(f"<{len(values)}f", *values)
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("gpcw20240630.dat", dat)
    return result.getvalue()


def test_financial_archive_validates_hash_refreshes_and_parses_numeric_columns(
    tmp_path, monkeypatch,
):
    financial = pytest.importorskip("mootdx.financial.financial")
    state = {"payload": _archive(), "corrupt": False}
    requests = []

    class API:
        def __init__(self, **kwargs):
            pass

        def connect(self, *args, **kwargs):
            assert kwargs["time_out"] >= 2

        def close(self):
            pass

        def get_report_file(self, filename, offset):
            requests.append((filename, offset))
            payload = state["payload"]
            if filename.endswith("gpcw.txt"):
                checksum = hashlib.md5(payload).hexdigest()
                payload = f"gpcw20240630.zip,{checksum},{len(payload)}\r\n".encode()
            elif state["corrupt"]:
                payload = b"broken"
            chunk = payload[offset:offset + 100]
            return {"chunksize": len(chunk), "chunkdata": chunk}

    monkeypatch.setattr(financial, "TdxHq_API", API)

    def fetch():
        return MootdxClient.financial_history(
            ["600519.SH"], periods=1, columns={"col1", "col314"}, cache_dir=tmp_path,
        )

    assert fetch() == [
        {"code": "600519", "report_date": "20240630", "col1": 2.5, "col314": 240809},
    ]
    requests.clear()
    assert fetch()[0]["col1"] == 2.5
    assert all(name.endswith("gpcw.txt") for name, _ in requests)
    previous = (tmp_path / "gpcw20240630.zip").read_bytes()
    state.update(payload=_archive(3.5), corrupt=True)
    with pytest.raises(MootdxError, match="校验"):
        fetch()
    assert (tmp_path / "gpcw20240630.zip").read_bytes() == previous
    assert len(list(tmp_path.iterdir())) == 1
    state["corrupt"] = False
    assert fetch()[0]["col1"] == 3.5


def test_report_download_rejects_inconsistent_chunk_length():
    class API:
        def get_report_file(self, filename, offset):
            return {"chunksize": 20, "chunkdata": b"short"}

    with pytest.raises(MootdxError, match="长度"):
        _download_report(API(), "gpcw.txt", io.BytesIO(), max_bytes=100)
