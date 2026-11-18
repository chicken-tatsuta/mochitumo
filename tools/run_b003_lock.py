"""Connect B003Lock to an RC-S330 and a trusted USB serial link."""

import argparse
from contextlib import contextmanager, ExitStack
import importlib
import json
import os
import pty
import queue
import re
import select
import sys
import threading
import time
import tty

from b003_lock import B003Lock


def diagnostic(reason):
    print(reason, file=sys.stderr, flush=True)


def scan_cards(reader, cards, stopped):
    def on_connect(tag):
        idm = getattr(tag, "idm", None)
        if idm is not None and len(idm) == 8:
            try:
                cards.put_nowait(bytes(idm).hex())
            except queue.Full:
                diagnostic("card_queue_full")
        return True  # nfcpy waits for removal before polling again.

    try:
        while not stopped.is_set():
            reader.connect(rdwr={"targets": ["212F", "424F"], "on-connect": on_connect,
                                 "iterations": 1, "interval": 0.2},
                           terminate=stopped.is_set)
    except Exception:
        diagnostic("card_reader_unavailable")
        stopped.set()


def run(port, lock, cards, stopped):
    buffer = bytearray()
    dropping = False
    pending = None
    attempts = 0
    retry_at = 0

    def send(line):
        data = line.encode("utf-8")
        if port.write(data) != len(data):
            raise OSError("incomplete serial write")

    while not stopped.is_set():
        for byte in port.read(1024):
            if byte != 10:
                if not dropping:
                    buffer.append(byte)
                    if len(buffer) > 1024:
                        buffer.clear()
                        dropping = True
                continue
            if dropping:
                dropping = False
                continue
            try:
                message = json.loads(buffer)
            except (ValueError, UnicodeError):
                continue
            finally:
                buffer.clear()
            if (pending is not None and isinstance(message, dict)
                    and message.get("v") == 1
                    and message.get("message_type") == "card_tap_result"
                    and message.get("client_request_id") == pending[1]
                    and all(message.get(key) == value for key, value in lock.target.items())):
                pending = None
            lock.receive(message, send)

        if pending is None:
            try:
                frame = lock.card_tap(cards.get_nowait())
            except queue.Empty:
                continue
            pending = (frame, json.loads(frame)["client_request_id"])
            attempts = 0
            retry_at = 0
        if time.monotonic() >= retry_at:
            if attempts == 3:
                pending = None
                diagnostic("card_tap_result_timeout")
            else:
                send(pending[0])
                attempts += 1
                retry_at = time.monotonic() + 2


class PtyPort:
    def __init__(self, master):
        self.master = master

    def read(self, size):
        if select.select([self.master], [], [], 0.1)[0]:
            return os.read(self.master, size)
        return b""

    def write(self, data):
        return os.write(self.master, data)


@contextmanager
def virtual_port():
    master, slave = pty.openpty()
    try:
        tty.setraw(slave)
        print(os.ttyname(slave), flush=True)
        yield PtyPort(master)
    finally:
        os.close(slave)
        os.close(master)


def scan_stdin(cards, stopped):
    for line in sys.stdin:
        if stopped.is_set():
            return
        idm = line.strip()
        if not re.fullmatch(r"[0-9a-fA-F]{16}", idm):
            diagnostic("expected_synthetic_16_hex_idm")
            continue
        try:
            cards.put_nowait(idm)
        except queue.Full:
            diagnostic("card_queue_full")


class MockDriver:
    def __init__(self, fail_action):
        self.fail_action = fail_action
        self.position = "lock"

    def drive(self, action):
        diagnostic("mock_drive_" + action)
        if action != self.fail_action:
            self.position = action

    def confirms_position(self, action):
        return action != self.fail_action and self.position == action


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    transport = parser.add_mutually_exclusive_group(required=True)
    transport.add_argument("--port", help="B003-side serial port")
    transport.add_argument("--pty", action="store_true", help="print a virtual port for Backend")
    parser.add_argument("--db", required=True, help="B003-only persistent command ledger")
    parser.add_argument("--device-id", default="BIKE03")
    parser.add_argument("--relay-id", default="RELAY01")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--reader", default="usb:054c:02e1")
    parser.add_argument("--card-input", choices=("reader", "stdin"), default="reader")
    actuator = parser.add_mutually_exclusive_group(required=True)
    actuator.add_argument("--driver", help="local module with drive and confirms_position")
    actuator.add_argument("--mock-drive", choices=("success", "fail-unlock", "fail-lock"))
    args = parser.parse_args()
    if args.card_input == "stdin" and args.driver:
        parser.error("synthetic card input requires --mock-drive")
    driver = (importlib.import_module(args.driver) if args.driver
              else MockDriver(args.mock_drive.removeprefix("fail-")))
    stopped = threading.Event()
    cards = queue.Queue(maxsize=1)
    try:
        with ExitStack() as stack:
            lock = B003Lock(args.db, args.device_id, args.relay_id, driver.drive,
                            driver.confirms_position, diagnostic)
            stack.callback(lock.close)
            if args.card_input == "reader":
                import nfc

                reader = stack.enter_context(nfc.ContactlessFrontend(args.reader))
                worker = threading.Thread(target=scan_cards, args=(reader, cards, stopped))
            else:
                worker = threading.Thread(target=scan_stdin, args=(cards, stopped), daemon=True)
            if args.pty:
                port = stack.enter_context(virtual_port())
            else:
                import serial

                port = stack.enter_context(serial.Serial(
                    args.port, args.baud, timeout=0.1, write_timeout=1,
                ))
            worker.start()
            try:
                run(port, lock, cards, stopped)
            finally:
                stopped.set()
                if args.card_input == "reader":
                    worker.join()
    except KeyboardInterrupt:
        return 0
    return 1  # Reader stopped unexpectedly.


if __name__ == "__main__":
    sys.exit(main())
