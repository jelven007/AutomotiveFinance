"""Exercise real native sockets against a deterministic local TDX server."""

import json
import socket
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest


def _exact(connection, count):
    result = b""
    while len(result) < count:
        block = connection.recv(count - len(result))
        if not block:
            raise EOFError
        result += block
    return result


def _quote(market, code):
    # All variable integers are zero. amount uses the upstream TDX zero sentinel.
    return bytes([market]) + code + b"\0" * (2 + 9 + 4 + 4 + 20 + 2 + 4 + 4)


class _Server:
    def __init__(self, *, incomplete=False):
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(100)
        self.listener.settimeout(0.2)
        self.address = f"127.0.0.1:{self.listener.getsockname()[1]}"
        self.closed = threading.Event()
        self.lock = threading.Lock()
        self.total = 0
        self.live = 0
        self.peak = 0
        self.batches = []
        self.incomplete = incomplete
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()

    def _accept(self):
        while not self.closed.is_set():
            try:
                connection, _ = self.listener.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            with self.lock:
                self.total += 1
                self.live += 1
                self.peak = max(self.peak, self.live)
            threading.Thread(target=self._handle, args=(connection,), daemon=True).start()

    def _handle(self, connection):
        try:
            with connection:
                connection.settimeout(3)
                while not self.closed.is_set():
                    header = _exact(connection, 10)
                    request = _exact(connection, struct.unpack_from("<H", header, 6)[0])
                    response = b""
                    if request[:2] == b"\x3e\x05":
                        count = struct.unpack_from("<H", request, 10)[0]
                        items = [
                            (request[i], request[i + 1 : i + 7])
                            for i in range(12, 12 + count * 7, 7)
                        ]
                        with self.lock:
                            self.batches.append(items)
                        time.sleep(0.02)
                        returned = [] if self.incomplete else list(reversed(items))
                        response = (
                            b"\0\0"
                            + struct.pack("<H", len(returned))
                            + b"".join(_quote(market, code) for market, code in returned)
                        )
                    elif request[:2] == b"\xb9\x06":
                        offset = struct.unpack_from("<I", request, 2)[0]
                        data = b"financial listing" if offset == 0 else b""
                        response = struct.pack("<I", len(data)) + data
                    reply = b"\0" * 12 + struct.pack("<HH", len(response), len(response))
                    # Split the header to prove native read_exact tolerates TCP fragmentation.
                    connection.sendall(reply[:5])
                    connection.sendall(reply[5:] + response)
        except (EOFError, OSError):
            pass
        finally:
            with self.lock:
                self.live -= 1

    def close(self):
        self.closed.set()
        self.listener.close()
        self.thread.join(timeout=1)


def test_native_pool_reuses_35_sockets_and_preserves_response_identity():
    module = pytest.importorskip("tsp_rustdx_native")
    server = _Server()
    client = module.RustdxClient(100, server.address, 2)
    securities = [(0, f"{index:06d}") for index in range(2_100)]
    securities += [(1, "000001"), (0, "000001")]  # mixed markets + duplicate
    try:
        first = json.loads(client.quotes_json(securities))
        opened = server.total
        assert opened == 35
        assert [(row["market"], row["code"]) for row in first] == securities
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(client.quotes_json, securities) for _ in range(2)]
            futures.append(pool.submit(client.report_file, "tdxfin/gpcw.txt", 512_000))
            assert len(json.loads(futures[0].result())) == len(securities)
            assert len(json.loads(futures[1].result())) == len(securities)
            assert bytes(futures[2].result()) == b"financial listing"
        assert server.total == opened
        assert server.peak <= 35
        assert all(
            len(batch) <= 60 and len({market for market, _ in batch}) == 1
            for batch in server.batches
        )
        assert json.loads(client.stats_json())["total_connections"] == 35
        client.close()
        assert json.loads(client.stats_json())["closed"] is True
        with pytest.raises(RuntimeError, match="closed"):
            client.quotes_json([(0, "000001")])
        assert server.total == opened
    finally:
        client.close()
        server.close()


def test_native_empty_batch_fails_after_bounded_retries():
    module = pytest.importorskip("tsp_rustdx_native")
    server = _Server(incomplete=True)
    client = module.RustdxClient(1, server.address, 2)
    try:
        with pytest.raises(RuntimeError, match="incomplete"):
            client.quotes_json([(0, "000001")])
        assert len(server.batches) == 3
        assert json.loads(client.stats_json())["total_connections"] == 0
    finally:
        client.close()
        server.close()


def test_financial_server_switch_uses_existing_slots():
    module = pytest.importorskip("tsp_rustdx_native")
    quotes = _Server()
    reports = _Server()
    client = module.RustdxClient(1, quotes.address, 2)
    try:
        client.quotes_json([(0, "000001")])
        assert (
            bytes(client.report_file("tdxfin/gpcw.txt", 512_000, reports.address))
            == b"financial listing"
        )
        assert json.loads(client.stats_json())["total_connections"] == 1
        client.quotes_json([(0, "000001")])
        assert json.loads(client.stats_json())["total_connections"] == 1
    finally:
        client.close()
        quotes.close()
        reports.close()
