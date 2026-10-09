"""Small SMTP sink used by the Windows installed-application smoke test."""
from __future__ import annotations

import argparse
import json
import signal
import threading
from email import policy
from email.parser import BytesParser
from pathlib import Path

from aiosmtpd.controller import Controller


class MessageSink:
    def __init__(self, output: Path) -> None:
        self.output = output
        self._lock = threading.Lock()

    async def handle_DATA(self, _server, _session, envelope) -> str:  # noqa: N802
        message = BytesParser(policy=policy.default).parsebytes(envelope.content)
        record = {
            "mail_from": envelope.mail_from,
            "rcpt_tos": list(envelope.rcpt_tos),
            "subject": str(message.get("subject") or ""),
        }
        with self._lock, self.output.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=True) + "\n")
        return "250 Message accepted for delivery"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ready-file", type=Path, required=True)
    args = parser.parse_args()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.unlink(missing_ok=True)
    args.ready_file.parent.mkdir(parents=True, exist_ok=True)
    args.ready_file.unlink(missing_ok=True)

    stop = threading.Event()

    def request_stop(_signum, _frame) -> None:
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)

    controller = Controller(MessageSink(args.output), hostname=args.host, port=args.port)
    controller.start()
    args.ready_file.write_text("ready\n", encoding="ascii")
    try:
        stop.wait()
    finally:
        controller.stop()
        args.ready_file.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
