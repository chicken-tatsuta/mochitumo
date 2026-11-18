import json
from pathlib import Path
import queue
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from b003_lock import B003Lock
from run_b003_lock import run, scan_cards


class B003TransportTest(unittest.TestCase):
    def test_serial_command_and_card_tap(self):
        cards = queue.Queue()
        cards.put("0123456789ABCDEF")
        stopped = threading.Event()
        target = {"device_id": "BIKE03", "relay_id": "RELAY01"}
        command = {"v": 1, "message_type": "command", **target, "bike_id": "B003",
                   "request_id": "req-fixture", "command_id": "cmd-fixture", "action": "unlock"}
        line = json.dumps(command).encode() + b"\r\n"
        incoming = iter([b"x" * 1025, b"\n" + line[:20], line[20:]])
        sent = []
        driven = []

        class Port:
            in_waiting = 1

            def read(self, size):
                chunk = next(incoming, None)
                if chunk is None:
                    stopped.set()
                return chunk or b""

            def write(self, data):
                sent.append(json.loads(data))
                return len(data)

        with tempfile.TemporaryDirectory() as directory:
            controller = B003Lock(Path(directory) / "test.sqlite", **target,
                                  drive=driven.append, confirms_position=lambda action: True)
            try:
                run(Port(), controller, cards, stopped)
            finally:
                controller.close()
        self.assertEqual([frame["message_type"] for frame in sent], ["card_tap", "ack", "result"])
        self.assertEqual(sent[-1]["result"], "unlock_success")
        self.assertEqual(driven, ["unlock"])

    def test_card_reader_callback_queues_idm_and_waits_for_removal(self):
        cards = queue.Queue()
        stopped = threading.Event()
        uid = bytes.fromhex("0123456789abcdef")

        waits_for_removal = []

        class Reader:
            def connect(self, rdwr, terminate):
                tag = type("Tag", (), {"idm": uid})()
                waits_for_removal.append(rdwr["on-connect"](tag))
                stopped.set()

        scan_cards(Reader(), cards, stopped)
        self.assertEqual(waits_for_removal, [True])
        self.assertEqual(cards.get_nowait(), uid.hex())
